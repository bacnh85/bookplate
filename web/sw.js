/* Bookplate service worker — offline shell + offline reading.
   Static shell: cache-first with background revalidate.
   Library JSON: network-first, cache fallback (offline shows cached shelf).
   Book files: cached whole on first full-body fetch; Range requests are
   answered by slicing the cached body (206 + Content-Range synthesized).
   Never cached: anything but GET, auth/admin/store endpoints, /api/me. */
const VERSION = "bookplate-v3";
const SHELL = [
  "/", "/index.html", "/reader.html", "/app.css", "/app.js", "/reader.js",
  "/manifest.webmanifest", "/fonts/Literata-VF.woff2",
  "/fonts/Literata-Italic-VF.woff2", "/icons/icon-192.png", "/icons/icon-512.png",
];
const API_CACHE = "bookplate-api-v1";

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(VERSION).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== VERSION && k !== API_CACHE)
        .map((k) => caches.delete(k))))
      .then(() => self.clients.claim()));
});

const isBookFile = (url) => /^\/api\/books\/\d+\/file/.test(url.pathname);

async function respondWithRange(request, url) {
  const cache = await caches.open(API_CACHE);
  const cached = await cache.match(url.pathname + url.search);
  if (!cached) return null;
  const range = request.headers.get("range");
  if (!range) return cached;
  const buf = await cached.arrayBuffer();
  const m = /bytes=(\d*)-(\d*)/.exec(range);
  let start = m && m[1] ? parseInt(m[1], 10) : 0;
  let end = m && m[2] ? parseInt(m[2], 10) : buf.byteLength - 1;
  if (isNaN(start) || start >= buf.byteLength) {
    return new Response(null, { status: 416,
      headers: { "Content-Range": `bytes */${buf.byteLength}` } });
  }
  end = Math.min(end, buf.byteLength - 1);
  return new Response(buf.slice(start, end + 1), {
    status: 206,
    headers: {
      "Content-Type": cached.headers.get("Content-Type") || "application/octet-stream",
      "Content-Range": `bytes ${start}-${end}/${buf.byteLength}`,
      "Content-Length": String(end - start + 1),
      "Accept-Ranges": "bytes",
    },
  });
}

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (url.origin !== location.origin) return;
  const path = url.pathname;

  if (e.request.method !== "GET") return;
  // never touch auth/admin/store or user endpoints
  if (/^\/api\/(auth|admin|zlib|annas|me|tokens|mcp)/.test(path) || path === "/mcp") return;

  // book files: serve Range requests from the cache; cache whole bodies
  if (isBookFile(url)) {
    e.respondWith((async () => {
      const fromCache = await respondWithRange(e.request, url);
      if (fromCache) return fromCache;
      const network = await fetch(e.request);
      // only cache complete 200 bodies — partials would corrupt future slices
      if (network.status === 200 && !e.request.headers.get("range")) {
        const cache = await caches.open(API_CACHE);
        cache.put(url.pathname + url.search, network.clone());
      }
      return network;
    })());
    return;
  }

  // library JSON + covers: network-first with cache fallback
  if (path.startsWith("/api/")) {
    e.respondWith((async () => {
      try {
        const r = await fetch(e.request);
        if (r.ok) {
          const cache = await caches.open(API_CACHE);
          cache.put(e.request, r.clone());
        }
        return r;
      } catch {
        const cached = await caches.match(e.request);
        if (cached) return cached;
        return new Response(JSON.stringify({ offline: true }), {
          status: 503, headers: { "Content-Type": "application/json" } });
      }
    })());
    return;
  }

  // static shell + foliate modules + fonts: cache-first, background revalidate
  e.respondWith((async () => {
    const cached = await caches.match(e.request);
    if (cached) {
      // background revalidate (stale-while-revalidate)
      e.waitUntil(fetch(e.request).then((r) => {
        if (r.ok) caches.open(VERSION).then((c) => c.put(e.request, r.clone()));
      }).catch(() => {}));
      return cached;
    }
    // exact miss: fall back to any stamped version of the same path
    const anyVersion = await caches.match(e.request, { ignoreSearch: true });
    if (anyVersion) return anyVersion;
    try {
      const r = await fetch(e.request);
      if (r.ok) caches.open(VERSION).then((c) => c.put(e.request, r.clone()));
      return r;
    } catch {
      return new Response("offline", { status: 503 });
    }
  })());
});

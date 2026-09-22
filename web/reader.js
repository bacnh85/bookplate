/* Reader: epub/mobi/azw3/fb2/cbz/pdf via foliate-js. PDFs stream page-by-page
   through HTTP Range requests — the whole file is never downloaded up front. */
const $ = (s) => document.querySelector(s);
const id = new URLSearchParams(location.search).get("id");
const token = localStorage.getItem("token");
const headers = { Authorization: `Bearer ${token}` };
const progressKey = `progress-${id}`;

const bookRes = await fetch(`/api/books/${id}`, { headers });
if (bookRes.status === 401) location.assign("/");
const book = await bookRes.json();
$("#title").textContent = book.title || "";

/* ---- reader settings (global, like the old reader-font) ----
   Invariant (test_frontend.py enforces it): settings keys ARE localStorage
   suffixes — put(k) writes "reader-<k>", reads come back via get(). */
const get = (k, d) => localStorage.getItem(k) ?? d;
const settings = {
  theme: get("reader-theme", "day"),
  "font-family": get("reader-font-family", "serif"),
  lineheight: get("reader-lineheight", "1.6"),
  margin: get("reader-margin", "48"),
  flow: get("reader-flow", "paginated"),
};
const put = (k, v) => { settings[k] = v; localStorage.setItem(`reader-${k}`, v); };
document.body.dataset.theme = settings.theme;
if (book.ext === "pdf") document.body.classList.add("reading-pdf");  // enables canvas filters

/* theme colours live in app.css tokens — read them so injected book CSS stays in sync */
const themeColors = () => {
  const css = getComputedStyle(document.body);
  return ["paper", "ink", "ink-soft", "accent"].map(v => css.getPropertyValue(`--${v}`).trim());
};
const FONTS = {
  serif: "'Iowan Old Style', 'Palatino Linotype', Palatino, Georgia, serif",
  georgia: "Georgia, 'Times New Roman', serif",
  sans: "system-ui, -apple-system, 'Segoe UI', sans-serif",
};

let view = null;
let fontPx = parseFloat(localStorage.getItem("reader-font")) || 17;  // size — separate key from font-family

function applyStyles() {
  if (!view || view.isFixedLayout) return;  // PDF/CBZ render canvases — no book CSS to restyle
  const [bg, fg, soft, accent] = themeColors();
  view.renderer.setStyles(`
    html { background: ${bg} !important; }
    body { font-family: ${FONTS[settings["font-family"]] || FONTS.serif} !important;
           background: ${bg} !important; color: ${fg} !important;
           line-height: ${settings.lineheight} !important; }
    * { font-size: ${fontPx}px !important; }
    a { color: ${accent} !important; }
    ::selection { background: ${accent}; color: ${bg}; }
  `);
}

function applyLayout() {
  if (!view) return;
  view.renderer.setAttribute("flow", settings.flow);
  view.renderer.setAttribute("margin", settings.margin);
  view.renderer.setAttribute("animated", "");  // 300ms eased page turns
  if (view.isFixedLayout) applyZoom();
}

function applyZoom() {  // fit-page is tiny on phones — fit width in narrow viewports
  if (!view?.renderer) return;  // resize before the book opened
  view.renderer.setAttribute("zoom",
    innerWidth < 700 || innerHeight < 500 ? "fit-width" : "fit-page");
}
addEventListener("resize", applyZoom);

function applySettings() {
  document.body.dataset.theme = settings.theme;
  applyStyles();
  applyLayout();
}

/* ---- progress sync (server + localStorage, newer-of resume) ---- */
let putTimer = null, pendingPos = null;
function syncProgress() {  // also flushes on close — server never staler than localStorage
  clearTimeout(putTimer);
  putTimer = null;
  if (!pendingPos) return;
  const pos = pendingPos;
  const body = JSON.stringify(pos);
  headers["Content-Type"] = "application/json";
  fetch(`/api/books/${id}/progress`, { method: "PUT", headers, body, keepalive: true })
    .then(async (r) => {
      if (r.status >= 400 && r.status < 500) { pendingPos = null; return; }  // rejected token/body — permanent
      if (!r.ok) throw new Error(`HTTP ${r.status}`);  // 5xx — transient
      if (pendingPos === pos) pendingPos = null;
      // anchor local clock to the server's — keeps newer-of resume skew-proof
      const { updated_at } = await r.json().catch(() => ({}));
      if (updated_at) localStorage.setItem(`${progressKey}-off`,
        Date.parse(updated_at.replace(" ", "T") + "Z") - Date.now());
    })
    .catch(() => {  // network/5xx — retry, a dropped sync must not lose the position
      putTimer = setTimeout(syncProgress, 2000);
    });
}

/* ---- seek slider ---- */
const seek = $("#seek");
let seeking = false, seekTimer = null;
seek.addEventListener("pointerdown", () => { seeking = true; });
seek.addEventListener("pointerup", () => { seeking = false; });
seek.addEventListener("input", () => {
  if (!view) return;
  clearTimeout(seekTimer);
  seekTimer = setTimeout(() => view.goToFraction(seek.value / 1000), 150);
});

async function openFoliate(file) {
  // ?v=2 defeats any stale-cached foliate (Cloudflare pins static JS for 1y if
  // Browser Cache TTL misconfigures to override the origin's no-cache)
  await import("/foliate-js/view.js?v=2");  // side-effect: defines <foliate-view>
  view = document.createElement("foliate-view");
  $("#viewer").prepend(view);
  await view.open(file);
  applyStyles();
  applyLayout();
  $("#zone-left").hidden = $("#zone-right").hidden = false;
  $("#type-wrap").hidden = view.isFixedLayout;  // typography controls are reflow-only

  // TOC (EPUB nav + PDF outline both land here)
  const toc = view.book.toc || [];
  if (toc.length) {
    const sel = $("#toc");
    sel.hidden = false;
    const add = (items, depth = 0) => {
      for (const item of items) {
        const opt = document.createElement("option");
        opt.textContent = " ".repeat(depth * 2) + (item.label?.trim() || "—");
        opt.value = item.href;
        sel.append(opt);
        if (item.subitems) add(item.subitems, depth + 1);
      }
    };
    add(toc);
    sel.onchange = () => view.goTo(sel.value);
  }

  view.addEventListener("relocate", (e) => {
    const { cfi, fraction, section } = e.detail;
    const pct = Math.round((fraction ?? 0) * 100);
    localStorage.setItem(progressKey, cfi);  // instant/offline resume cache
    localStorage.setItem(`${progressKey}-pct`, String(pct));
    localStorage.setItem(`${progressKey}-t`, String(Date.now()));
    pendingPos = { cfi, pct };
    clearTimeout(putTimer);
    putTimer = setTimeout(syncProgress, 2000);
    $("#progress").textContent = view.isFixedLayout
      ? `${section.current + 1} / ${section.total}` : `${pct}%`;
    if (!seeking) seek.value = Math.round((fraction ?? 0) * 1000);
    $("#loading")?.remove();
    if (view.isFixedLayout)  // warm the next page's byte ranges + render cache
      view.book.sections[section.current + 1]?.load?.().catch(() => {});
  });

  // resume at the NEWER position (server syncs across devices, local wins when
  // the last sync failed); offset re-anchors the local clock to the server's
  const off = +(localStorage.getItem(`${progressKey}-off`) || 0);
  const localT = (+(localStorage.getItem(`${progressKey}-t`) || 0)) + off;
  const serverT = book.progress_at ? Date.parse(book.progress_at.replace(" ", "T") + "Z") : 0;
  const cfi = serverT >= localT ? (book.progress_cfi || localStorage.getItem(progressKey))
                                : localStorage.getItem(progressKey) || book.progress_cfi;
  await view.init({ lastLocation: cfi });
  if (!cfi) view.goToTextStart?.();  // brand-new book: no position anywhere
}

addEventListener("pagehide", syncProgress);
document.addEventListener("visibilitychange", () => { if (document.hidden) syncProgress(); });

if (book.ext === "pdf") {
  // pseudo-File whose slices are fetched with HTTP Range: pdf.js requests only
  // the xref + the objects of visible pages (206 verified). foliate's call
  // sites all do `file.slice(a, b).arrayBuffer()`, so slice() returns that
  // shape sync. If a proxy strips Range (200 instead of 206), the full body
  // is fetched ONCE and every further slice is cut from memory.
  let fullBody = null;
  const file = {
    size: +book.size,
    name: `${book.title || "book"}.pdf`,
    // never called on the PDF path; marks this as a File for makeBook()
    arrayBuffer: () => fetch(`/api/books/${id}/file`, { headers }).then(r => r.arrayBuffer()),
    slice: (begin = 0, end = +book.size) => ({
      arrayBuffer: () => fullBody
        ? Promise.resolve(fullBody.slice(begin, end))
        : fetch(`/api/books/${id}/file`,
            { headers: { ...headers, Range: `bytes=${begin}-${end - 1}` } })
          .then(r => r.arrayBuffer().then(buf => {
            if (r.status !== 206) fullBody = buf;  // Range ignored — remember it
            return r.status === 206 ? buf : buf.slice(begin, end);
          })),
    }),
  };
  try {
    await openFoliate(file);
  } catch (e) {
    const el = document.getElementById("loading");
    if (el) el.textContent = `Failed to open book — ${e.message}`;
  }
} else {
  try {
    const res = await fetch(`/api/books/${id}/file`, { headers });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    await openFoliate(new File([await res.blob()], `${book.title || "book"}.${book.ext}`));
  } catch (e) {
    const el = document.getElementById("loading");
    if (el) el.textContent = `Failed to open book — ${e.message}`;
  }
}

/* ---- controls ---- */
$("#theme").value = settings.theme;
$("#theme").onchange = (e) => { put("theme", e.target.value); applySettings(); };

$("#type-btn").onclick = () => { $("#type-panel").hidden = !$("#type-panel").hidden; };
$("#font-family").value = settings["font-family"];
$("#font-family").onchange = (e) => { put("font-family", e.target.value); applyStyles(); };
$("#font-inc").onclick = () => { fontPx = Math.min(fontPx + 1, 28); localStorage.setItem("reader-font", fontPx); applyStyles(); };
$("#font-dec").onclick = () => { fontPx = Math.max(fontPx - 1, 11); localStorage.setItem("reader-font", fontPx); applyStyles(); };
$("#line-height").value = settings.lineheight;
$("#line-height").onchange = (e) => { put("lineheight", e.target.value); applyStyles(); };
$("#page-margin").value = settings.margin;
$("#page-margin").onchange = (e) => { put("margin", e.target.value); applyLayout(); };
$("#flow").value = settings.flow;
$("#flow").onchange = (e) => { put("flow", e.target.value); applyLayout(); };

$("#prev").onclick = () => view?.prev();
$("#next").onclick = () => view?.next();
$("#zone-left").onclick = () => view?.prev();
$("#zone-right").onclick = () => view?.next();
document.onkeydown = (e) => {
  if (e.key === "ArrowLeft") view?.prev();
  if (e.key === "ArrowRight") view?.next();
};
/* swipe */
let touchX = null;
$("#viewer").addEventListener("touchstart", (e) => { touchX = e.touches[0].clientX; }, { passive: true });
$("#viewer").addEventListener("touchend", (e) => {
  if (touchX == null) return;
  const dx = e.changedTouches[0].clientX - touchX;
  if (dx < -48) view?.next();
  else if (dx > 48) view?.prev();
  touchX = null;
}, { passive: true });

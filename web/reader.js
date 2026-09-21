/* Reader: epub/mobi/azw3/fb2/cbz via foliate-js; pdf via the browser's native viewer. */
const $ = (s) => document.querySelector(s);
const id = new URLSearchParams(location.search).get("id");
const token = localStorage.getItem("token");
const headers = { Authorization: `Bearer ${token}` };
const progressKey = `progress-${id}`;

const bookRes = await fetch(`/api/books/${id}`, { headers });
if (bookRes.status === 401) location.assign("/");
const book = await bookRes.json();
$("#title").textContent = book.title || "";

let view = null;
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
let fontPx = parseFloat(localStorage.getItem("reader-font") || "17");

function applyStyles() {
  if (!view) return;
  view.renderer.setStyles(`
    body { font-family: 'Iowan Old Style', 'Palatino Linotype', Palatino, Georgia, serif !important; }
    * { font-size: ${fontPx}px !important; }
  `);
}

async function openFoliate(blob) {
  await import("/foliate-js/view.js");  // side-effect: defines <foliate-view>
  view = document.createElement("foliate-view");
  $("#viewer").prepend(view);
  const file = new File([blob], `${book.title || "book"}.${book.ext}`);
  await view.open(file);
  applyStyles();
  $("#loading").remove();
  $("#zone-left").hidden = $("#zone-right").hidden = false;

  // TOC
  const toc = view.book.toc || [];
  if (toc.length) {
    const sel = $("#toc");
    sel.hidden = false;
    const add = (items, depth = 0) => {
      for (const item of items) {
        const opt = document.createElement("option");
        opt.textContent = " ".repeat(depth * 2) + (item.label?.trim() || "—");
        opt.value = item.href;
        sel.append(opt);
        if (item.subitems) add(item.subitems, depth + 1);
      }
    };
    add(toc);
    sel.onchange = () => view.goTo(sel.value);
  }

  view.addEventListener("relocate", (e) => {
    const { cfi, fraction } = e.detail;
    const pct = Math.round((fraction ?? 0) * 100);
    localStorage.setItem(progressKey, cfi);  // instant/offline resume cache
    localStorage.setItem(`${progressKey}-pct`, String(pct));
    localStorage.setItem(`${progressKey}-t`, String(Date.now()));
    pendingPos = { cfi, pct };
    clearTimeout(putTimer);
    putTimer = setTimeout(syncProgress, 2000);
    $("#progress").textContent = `${pct}%`;
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
  const iframe = document.createElement("iframe");
  iframe.src = `/api/books/${id}/file`;
  iframe.title = book.title;
  $("#viewer").append(iframe);
  $("#loading").remove();
} else {
  try {
    const res = await fetch(`/api/books/${id}/file`, { headers });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    await openFoliate(await res.blob());
  } catch (e) {
    const el = document.getElementById("loading");
    if (el) el.textContent = `Failed to open book — ${e.message}`;
  }
}

/* controls */
$("#prev").onclick = () => view?.prev();
$("#next").onclick = () => view?.next();
$("#zone-left").onclick = () => view?.prev();
$("#zone-right").onclick = () => view?.next();
$("#font-inc").onclick = () => { fontPx = Math.min(fontPx + 1, 28); localStorage.setItem("reader-font", fontPx); applyStyles(); };
$("#font-dec").onclick = () => { fontPx = Math.max(fontPx - 1, 11); localStorage.setItem("reader-font", fontPx); applyStyles(); };
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

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
    localStorage.setItem(progressKey, cfi);
    // ponytail: progress is per-browser; server-side sync only if multi-device resume matters
    localStorage.setItem(`${progressKey}-pct`, String(pct));
    $("#progress").textContent = `${pct}%`;
  });

  await view.init({ lastLocation: localStorage.getItem(progressKey) });
  if (!localStorage.getItem(progressKey)) view.goToTextStart?.();
}

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

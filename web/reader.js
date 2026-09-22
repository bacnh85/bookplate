/* Reader: epub/mobi/azw3/fb2/cbz/pdf via foliate-js. PDFs stream page-by-page
   through HTTP Range requests — the whole file is never downloaded up front.
   ?reader=1 prefers the server's linearized PDF derivative when present. */
const $ = (s) => document.querySelector(s);
const id = new URLSearchParams(location.search).get("id");
const token = localStorage.getItem("token");
const headers = { Authorization: `Bearer ${token}` };
const progressKey = `progress-${id}`;

const bookRes = await fetch(`/api/books/${id}`, { headers });
if (bookRes.status === 401) location.assign("/");
const book = await bookRes.json();
$("#title").textContent = book.title || "";
$("#load-title").textContent = book.title || "Opening…";

/* ---- reader settings (global, like the old reader-font) ----
   Invariant (test_frontend.py enforces it): settings keys ARE localStorage
   suffixes — put(k) writes "reader-<k>", reads come back via get(). */
const get = (k, d) => localStorage.getItem(k) ?? d;
const settings = {
  theme: get("reader-theme", "day"),
  "font-family": get("reader-font-family", "literata"),
  lineheight: get("reader-lineheight", "1.6"),
  align: get("reader-align", "justify"),
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
  literata: "'Literata', Georgia, serif",
  serif: "'Iowan Old Style', 'Palatino Linotype', Palatino, Georgia, serif",
  georgia: "Georgia, 'Times New Roman', serif",
  sans: "system-ui, -apple-system, 'Segoe UI', sans-serif",
};
// book sections load as blob: documents — font URLs inside them must be absolute
const FONT_URLS = {
  literata: `@font-face { font-family: 'Literata';
    src: url('${new URL('/fonts/Literata-VF.woff2', location.href)}') format('woff2');
    font-weight: 200 900; font-style: normal; font-display: swap; }
  @font-face { font-family: 'Literata';
    src: url('${new URL('/fonts/Literata-Italic-VF.woff2', location.href)}') format('woff2');
    font-weight: 200 900; font-style: italic; font-display: swap; }`,
};

let view = null;
let fontPx = parseFloat(localStorage.getItem("reader-font")) || 17;  // size — separate key from font-family

function applyStyles() {
  if (!view || view.isFixedLayout) return;  // PDF/CBZ render canvases — no book CSS to restyle
  const [bg, fg, , accent] = themeColors();
  const justify = settings.align === "justify";
  view.renderer.setStyles(`
    ${FONT_URLS[settings["font-family"]] || ""}
    html { background: ${bg} !important; }
    body { font-family: ${FONTS[settings["font-family"]] || FONTS.literata} !important;
           background: ${bg} !important; color: ${fg} !important;
           font-size: ${fontPx}px !important;
           line-height: ${settings.lineheight} !important;
           ${justify ? `text-align: justify !important; -webkit-hyphens: auto !important; hyphens: auto !important;` : "text-align: left !important;"} }
    p + p { ${justify ? "text-indent: 1.5em !important;" : ""} }
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

/* ---- chrome visibility (Yomu-style auto-hiding bars) ---- */
function toggleChrome() {
  document.body.classList.toggle("chrome-hidden");
}
// keep controls usable from the keyboard: bars stay visible while focus is inside them
document.addEventListener("focusin", (e) => {
  if (e.target.closest("#top-bar, #bottom-bar")) document.body.classList.remove("chrome-hidden");
});

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
  // ?v=3 defeats any stale-cached foliate (Cloudflare pins static JS for 1y if
  // Browser Cache TTL misconfigures to override the origin's no-cache)
  await import("/foliate-js/view.js?v=3");  // side-effect: defines <foliate-view>
  view = document.createElement("foliate-view");
  $("#viewer").prepend(view);
  await view.open(file);
  applyStyles();
  applyLayout();
  $("#zone-left").hidden = $("#zone-center").hidden = $("#zone-right").hidden = false;
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

  // segmented seek bar: chapter ticks (same data foliate's own reader uses)
  const ticks = $("#ticks");
  for (const f of view.getSectionFractions()) {
    if (f <= 0 || f >= 1) continue;
    const t = document.createElement("div");
    t.className = "tick";
    t.style.left = `${f * 100}%`;
    ticks.append(t);
  }

  view.addEventListener("relocate", (e) => {
    const { cfi, fraction, section, tocItem } = e.detail;
    const pct = Math.round((fraction ?? 0) * 100);
    localStorage.setItem(progressKey, cfi);  // instant/offline resume cache
    localStorage.setItem(`${progressKey}-pct`, String(pct));
    localStorage.setItem(`${progressKey}-t`, String(Date.now()));
    pendingPos = { cfi, pct };
    clearTimeout(putTimer);
    putTimer = setTimeout(syncProgress, 2000);
    $("#progress").textContent = view.isFixedLayout
      ? `${section.current + 1} / ${section.total}` : `${pct}%`;
    $("#section-label").textContent = tocItem?.label?.trim() || "";
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
  // shape sync. reader=1 → server prefers its linearized derivative (built
  // lazily, once). If a proxy strips Range (200 instead of 206), the full body
  // is fetched ONCE and every further slice is cut from memory.
  const fileUrl = `/api/books/${id}/file?reader=1`;
  // the derivative's byte length can differ from books.size (the original's) —
  // pdf.js validates slices against the declared size, so probe the real one.
  // If a proxy strips Range we get 200 here; the true size then only surfaces
  // with the body — handled below by re-deriving pdfSize from fullBody.
  const probe = await fetch(fileUrl, { headers: { ...headers, Range: "bytes=0-0" } });
  let pdfSize = Number((probe.headers.get("content-range") || "").split("/")[1]) || 0;
  let fullBody = null;
  const file = {
    size: pdfSize,
    name: `${book.title || "book"}.pdf`,
    // never called on the PDF path; marks this as a File for makeBook()
    arrayBuffer: () => fetch(fileUrl, { headers }).then(r => r.arrayBuffer()),
    slice: (begin = 0, end = pdfSize) => ({
      arrayBuffer: () => fullBody
        ? Promise.resolve(fullBody.slice(begin, end))
        : fetch(fileUrl, { headers: { ...headers, Range: `bytes=${begin}-${end - 1}` } })
          .then(r => r.arrayBuffer().then(buf => {
            if (r.status !== 206) {  // Range stripped — body IS the whole derivative
              fullBody = buf;
              pdfSize = buf.byteLength;  // fix the declared size before pdf.js validates slices
              file.size = pdfSize;
            }
            return r.status === 206 ? buf : buf.slice(begin, end);
          })),
    }),
  };
  // first open of a non-warmed PDF pays a one-time server linearize — say so
  const prepTimer = setTimeout(() => {
    const sub = document.getElementById("load-sub");
    if (sub) sub.textContent = "Preparing optimized layout…";
  }, 2000);
  try {
    await openFoliate(file);
  } catch (e) {
    const el = document.getElementById("loading");
    if (el) el.textContent = `Failed to open book — ${e.message}`;
  } finally {
    clearTimeout(prepTimer);
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
const THEMES = ["day", "sepia", "night"];
const themeLabel = (t) => t[0].toUpperCase() + t.slice(1);
$("#theme-cycle").textContent = themeLabel(settings.theme);
$("#theme-cycle").onclick = (e) => {
  const next = THEMES[(THEMES.indexOf(settings.theme) + 1) % THEMES.length];
  put("theme", next);
  e.target.textContent = themeLabel(next);
  applySettings();
};

$("#type-btn").onclick = () => { $("#type-panel").hidden = !$("#type-panel").hidden; };
$("#font-family").value = settings["font-family"];
$("#font-family").onchange = (e) => { put("font-family", e.target.value); applyStyles(); };
$("#font-inc").onclick = () => { fontPx = Math.min(fontPx + 1, 28); localStorage.setItem("reader-font", fontPx); applyStyles(); };
$("#font-dec").onclick = () => { fontPx = Math.max(fontPx - 1, 11); localStorage.setItem("reader-font", fontPx); applyStyles(); };
$("#line-height").value = settings.lineheight;
$("#line-height").onchange = (e) => { put("lineheight", e.target.value); applyStyles(); };
$("#align").value = settings.align;
$("#align").onchange = (e) => { put("align", e.target.value); applyStyles(); };
$("#page-margin").value = settings.margin;
$("#page-margin").onchange = (e) => { put("margin", e.target.value); applyLayout(); };
$("#flow").value = settings.flow;
$("#flow").onchange = (e) => { put("flow", e.target.value); applyLayout(); };

$("#prev").onclick = () => view?.prev();
$("#next").onclick = () => view?.next();
$("#zone-left").onclick = () => view?.prev();
$("#zone-right").onclick = () => view?.next();
$("#zone-center").onclick = toggleChrome;
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

/* Reader: epub/mobi/azw3/fb2/cbz/pdf via foliate-js. PDFs stream page-by-page
   through HTTP Range requests — the whole file is never downloaded up front.
   ?reader=1 prefers the server's linearized PDF derivative when present. */
const $ = (s) => document.querySelector(s);
let Overlayer = null;  // drawn types for annotations — set when foliate loads (openFoliate)
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
  margin: get("reader-margin", "48px"),
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
    font-weight: 200 900; font-style: italic; font-display: swap; }
  @font-face { font-family: 'Literata';
    src: url('${new URL('/fonts/Literata-VF-viet.woff2', location.href)}') format('woff2');
    font-weight: 200 900; font-style: normal; font-display: swap;
    unicode-range: U+0102-0103, U+0110-0111, U+0128-0129, U+0168-0169, U+01A0-01A1, U+01AF-01B0, U+0300-0301, U+0303-0304, U+0308-0309, U+0323, U+0329, U+1EA0-1EF9, U+20AB; }
  @font-face { font-family: 'Literata';
    src: url('${new URL('/fonts/Literata-Italic-VF-viet.woff2', location.href)}') format('woff2');
    font-weight: 200 900; font-style: italic; font-display: swap;
    unicode-range: U+0102-0103, U+0110-0111, U+0128-0129, U+0168-0169, U+01A0-01A1, U+01AF-01B0, U+0300-0301, U+0303-0304, U+0308-0309, U+0323, U+0329, U+1EA0-1EF9, U+20AB; }`,
};
/* stale/removed font keys (e.g. pre-2026-09 'serif') heal to the default —
   otherwise the select renders blank and applyStyles() injects no @font-face */
if (!FONTS[settings["font-family"]]) put("font-family", "literata");

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
  // paginator consumes margin as a CSS length (minmax(var(--_margin), 1fr));
  // a unitless value invalidates the grid rows and the page renders at ~45%
  // height — normalize any legacy unitless reader-margin (no migration needed)
  const marginPx = /^\d+(\.\d+)?$/.test(settings.margin) ? settings.margin + "px" : settings.margin;
  view.renderer.setAttribute("margin", marginPx);
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

/* ---- annotations: highlights, notes, bookmarks ---- */
const annos = new Map();  // id -> {id, cfi, text, note, color, kind}
let selTimer = null, selRange = null, selCfi = "", selText = "", selIndex = -1;
const ANNO_HIDDEN = () => view?.isFixedLayout;  // no text CFIs on fixed layout

function drawAnnotation(e) {  // vendored view asks us HOW to draw; pass its own color through
  const { draw, annotation } = e.detail;
  const a = annotation.note != null ? annotation : annosByValue.get(annotation.value);
  if (a?.kind === "underline" || a?.color === "under") draw(Overlayer.underline, { color: "currentColor" });
  else draw(Overlayer.highlight, { color: a?.color || "yellow" });
}
const annosByValue = new Map();  // cfi -> anno (show-annotation only hands us the value)

async function annoApi(path, opts) {
  const r = await fetch(path, { headers: { ...headers, "Content-Type": "application/json" }, ...opts });
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  return r.status === 204 ? null : r.json();
}

async function loadAnnotations() {
  if (ANNO_HIDDEN()) return;
  for (const a of await annoApi(`/api/books/${id}/annotations`)) {
    annos.set(a.id, a); annosByValue.set(a.cfi, a);
    try { await view.addAnnotation({ value: a.cfi, color: a.color, note: a.note }); } catch {}
  }
}

function redrawAnnotation(a) {  // color/note edits: remove + re-add so the overlay picks it up
  view.addAnnotation({ value: a.cfi }, true).catch(() => {});
  view.addAnnotation({ value: a.cfi, color: a.color, note: a.note }).catch(() => {});
}

/* selection popover — coordinate mapping across blob iframes is flaky, so this
   is the plan's fallback: a fixed action pill above the bottom bar (ponytail: position-independent beats iframe rect math) */
function hideSelPop() { $("#sel-pop").hidden = true; }

function onSelectionChange() {
  if (ANNO_HIDDEN()) return;
  clearTimeout(selTimer);
  selTimer = setTimeout(() => {
    const sel = getActiveSelection();
    if (!sel || sel.isCollapsed) { hideSelPop(); return; }
    const contents = view.renderer.getContents();
    const entry = contents.find(c => c.doc === sel.anchorNode?.getRootNode?.());
    if (!entry) { hideSelPop(); return; }
    try {
      selCfi = view.getCFI(entry.index, sel.getRangeAt(0));
      selText = String(sel).slice(0, 2000);
      selRange = sel.getRangeAt(0);
      selIndex = entry.index;
    } catch { hideSelPop(); return; }
    $("#sel-pop").hidden = false;
  }, 300);
}

function getActiveSelection() {  // selection lives inside blob-iframe docs, not the top document
  for (const { doc } of view.renderer.getContents()) {
    const sel = doc.getSelection?.();
    if (sel && !sel.isCollapsed && sel.rangeCount) return sel;
  }
  return null;
}

function onDocSelection() {  // fires on the SECTION doc — iframe selections don't reach the top document
  if (ANNO_HIDDEN()) return;
  clearTimeout(selTimer);
  selTimer = setTimeout(onSelectionChange, 300);
}
document.addEventListener("selectionchange", onDocSelection);

$("#sel-highlight").onclick = async () => {
  if (!selCfi) return;
  const a = await annoApi(`/api/books/${id}/annotations`, {
    method: "POST", body: JSON.stringify({ cfi: selCfi, text: selText, kind: "highlight", color: "yellow" }),
  }).catch(() => null);
  if (!a) return;
  annos.set(a.id, a); annosByValue.set(a.cfi, a);
  view.addAnnotation({ value: a.cfi, color: a.color, note: a.note }).catch(() => {});
  hideSelPop(); getActiveSelection()?.removeAllRanges?.();
};
$("#sel-note").onclick = async () => {
  if (!selCfi) return;
  const note = prompt("Note:");
  if (note == null) return;
  const a = await annoApi(`/api/books/${id}/annotations`, {
    method: "POST", body: JSON.stringify({ cfi: selCfi, text: selText, note, kind: "highlight", color: "yellow" }),
  }).catch(() => null);
  if (!a) return;
  annos.set(a.id, a); annosByValue.set(a.cfi, a);
  view.addAnnotation({ value: a.cfi, color: a.color, note: a.note }).catch(() => {});
  hideSelPop(); getActiveSelection()?.removeAllRanges?.();
};

/* tap an existing highlight -> manage it */
function onShowAnnotation(e) {
  const a = annosByValue.get(e.detail.value);
  if (!a) return;
  const action = prompt(`Note: ${a.note || "(none)"}\n\nType a new note, "del" to delete, empty to close:`, a.note || "");
  if (action == null) return;
  if (action === "del") {
    annoApi(`/api/annotations/${a.id}`, { method: "DELETE" }).catch(() => {});
    view.addAnnotation({ value: a.cfi }, true).catch(() => {});
    annos.delete(a.id); annosByValue.delete(a.cfi);
  } else if (action !== a.note) {
    annoApi(`/api/annotations/${a.id}`, { method: "PATCH", body: JSON.stringify({ note: action }) }).catch(() => {});
    a.note = action; redrawAnnotation(a);
  }
}

/* bookmarks: current position only — works for PDFs too if its fake CFI round-trips */
async function toggleBookmark() {
  const cfi = view?.lastLocation?.cfi;
  if (!cfi) return;
  const existing = [...annos.values()].find(a => a.kind === "bookmark" && a.cfi === cfi);
  if (existing) {
    annoApi(`/api/annotations/${existing.id}`, { method: "DELETE" }).catch(() => {});
    annos.delete(existing.id); annosByValue.delete(existing.cfi);
    $("#btn-bookmark").classList.remove("active");
  } else {
    const a = await annoApi(`/api/books/${id}/annotations`, {
      method: "POST", body: JSON.stringify({ cfi, kind: "bookmark" }),
    }).catch(() => null);
    if (!a) return;
    annos.set(a.id, a); annosByValue.set(a.cfi, a);
    $("#btn-bookmark").classList.add("active");
  }
}
$("#btn-bookmark").onclick = toggleBookmark;

function syncBookmarkBtn() {
  const cfi = view?.lastLocation?.cfi;
  const on = cfi && [...annos.values()].some(a => a.kind === "bookmark" && a.cfi === cfi);
  $("#btn-bookmark").classList.toggle("active", !!on);
}

/* annotation list popover */
function renderAnnoList() {
  const list = $("#anno-list");
  list.innerHTML = "";
  const items = [...annos.values()].sort((a, b) => a.id - b.id);
  $("#anno-export").hidden = !items.length;
  if (!items.length) { list.append(Object.assign(document.createElement("div"), { className: "anno-empty", textContent: "No highlights yet." })); return; }
  for (const a of items) {
    const row = document.createElement("div");
    row.className = "anno-row";
    row.textContent = a.kind === "bookmark" ? "🔖 Bookmark" : (a.text || a.note || "Highlight");
    if (a.note && a.text) row.textContent = `${a.text.slice(0, 60)} — ${a.note.slice(0, 60)}`;
    const del = document.createElement("button");
    del.type = "button"; del.className = "anno-del"; del.textContent = "×"; del.setAttribute("aria-label", "Delete");
    del.onclick = async (e) => {
      e.stopPropagation();
      await annoApi(`/api/annotations/${a.id}`, { method: "DELETE" }).catch(() => {});
      annos.delete(a.id); annosByValue.delete(a.cfi);
      view.addAnnotation({ value: a.cfi }, true).catch(() => {});
      renderAnnoList(); syncBookmarkBtn();
    };
    row.append(del);
    row.onclick = () => { view.goTo(a.cfi).catch(() => {}); $("#anno-panel").hidden = true; };
    list.append(row);
  }
}
$("#btn-anno").onclick = () => { renderAnnoList(); $("#anno-panel").hidden = !$("#anno-panel").hidden; };
$("#anno-close").onclick = () => { $("#anno-panel").hidden = true; };
$("#anno-export").onclick = () => open(`/api/annotations/export.md?book_id=${id}`, "_blank");

/* ---- in-book search (foliate view.search; reflow formats) ---- */
let searchIter = null;
const searchEsc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

function closeSearch() {
  searchIter?.return?.().catch?.(() => {});
  searchIter = null;
  view?.clearSearch();
  $("#search-panel").hidden = true;
  $("#search-results").innerHTML = "";
  $("#search-input").value = "";
  $("#search-status").textContent = "";
}

async function runSearch() {
  const q = $("#search-input").value.trim();
  if (!q || !view) return;
  searchIter?.return?.().catch?.(() => {});  // a previous run may still be iterating
  view.clearSearch();
  $("#search-results").innerHTML = "";
  $("#search-status").textContent = "Searching…";
  const results = $("#search-results");
  const addResult = (label, sub) => {
    const row = document.createElement("button");
    row.type = "button"; row.className = "search-row";
    row.innerHTML = (label ? `<span class="search-label">${searchEsc(label)}</span>` : "") +
      `<span class="search-excerpt">${sub}</span>`;
    return row;
  };
  let count = 0;
  const iter = searchIter = view.search({ query: q, draw: Overlayer.highlight, drawOptions: { color: "orange" } });
  try {
    for await (const result of iter) {
      if (searchIter !== iter) return;  // closed / superseded mid-search
      if (result === "done") { $("#search-status").textContent = `${count} result${count === 1 ? "" : "s"}`; break; }
      if (result.progress != null) { $("#search-status").textContent = `Searching… ${Math.round(result.progress * 100)}%`; continue; }
      const sectionLabel = result.label || "";
      for (const sub of result.subitems) {
        count++;
        const row = addResult(sectionLabel && count === 1 ? sectionLabel : "",
          `${searchEsc(sub.excerpt.pre)}<mark>${searchEsc(sub.excerpt.match)}</mark>${searchEsc(sub.excerpt.post)}`);
        row.onclick = () => { view.goTo(sub.cfi).catch(() => {}); };
        results.append(row);
      }
    }
  } catch { $("#search-status").textContent = "Search failed"; }
  if (searchIter === iter) searchIter = null;
}

$("#btn-search").onclick = () => {
  const panel = $("#search-panel");
  if (panel.hidden) { panel.hidden = false; $("#search-input").focus(); }
  else closeSearch();
};
$("#search-close").onclick = closeSearch;
$("#search-input").onkeydown = (e) => {
  if (e.key === "Enter") runSearch();
  if (e.key === "Escape") closeSearch();
};
$("#search-go").onclick = runSearch;

/* ---- read aloud (TTS, reflow formats; speak per sentence fragment) ---- */
let tts = null, ttsOn = false, ttsVoice = null;
// Chrome loads voices asynchronously — prime them on first user gesture
speechSynthesis.onvoiceschanged = () => { ttsVoice = null; };

function ttsStop() {
  speechSynthesis.cancel();
  tts = null; ttsOn = false;
  clearTimeout(speakSSML.watchdog);
  try {
    const { overlayer } = view.renderer.getContents()[0];
    overlayer?.remove("tts-current");
  } catch {}
  $("#btn-tts").textContent = "▶";
  $("#btn-tts").setAttribute("aria-label", "Read aloud");
  $("#btn-tts-stop").hidden = true;
}

function ttsHighlight(range) {  // sentence-level outline via the section overlayer
  try {
    const { overlayer } = view.renderer.getContents()[0];
    if (!overlayer) return;
    overlayer.remove("tts-current");
    overlayer.add("tts-current", range, Overlayer.outline, { color: getComputedStyle(document.body).getPropertyValue("--accent").trim() || "#7C2D2D" });
  } catch {}
}

function ttsSpeak() {  // speak from the current fragment; onend advances
  if (!ttsOn) return;
  speakSSML(tts.start());
}

function speakSSML(ssml) {
  if (!ttsOn || !ssml) return;  // section done — onend chain advances the book
  const text = new DOMParser().parseFromString(ssml, "application/xml")
    .documentElement.textContent.replace(/\s+/g, " ").trim();
  if (!text) { ttsUtterNext(); return; }
  const u = new SpeechSynthesisUtterance(text);
  u.rate = +(localStorage.getItem("reader-tts-rate") || 1);
  // Chrome populates getVoices() async — speak voiceless (platform default)
  // rather than blocking on an empty list; only hard-fail if the engine
  // itself errors (see onerror).
  if (!ttsVoice) {
    const voices = speechSynthesis.getVoices();
    ttsVoice = voices.find(v => v.default) || voices[0] || null;
  }
  if (ttsVoice) u.voice = ttsVoice;
  u.onend = () => ttsUtterNext();
  u.onerror = (e) => {
    if (e.error === "interrupted" || e.error === "canceled") return;  // our own stop
    ttsShowError(`Speech failed (${e.error}). Try another browser if it repeats.`);
  };
  // some engines silently no-op (no voices, blocked audio) with NO event fired:
  // if neither speaking nor pending 5s after speak(), nothing will ever sound
  clearTimeout(speakSSML.watchdog);
  speakSSML.watchdog = setTimeout(() => {
    if (ttsOn && !speechSynthesis.speaking && !speechSynthesis.pending)
      ttsShowError("Speech isn't starting — this browser has no working TTS engine. Try Safari or Chrome with system voices installed.");
  }, 5000);
  speechSynthesis.speak(u);
}

function ttsShowError(msg) {
  ttsStop();
  clearTimeout(speakSSML.watchdog);
  const el = document.createElement("div");
  el.className = "tts-error";
  el.textContent = msg;
  el.onclick = () => el.remove();
  $("#viewer").append(el);
  setTimeout(() => el.remove(), 8000);
}

function ttsUtterNext() {
  if (!ttsOn || !tts) return;
  clearTimeout(speakSSML.watchdog);  // section advance may take >5s — not a failure
  const paused = speechSynthesis.paused;
  const ssml = tts.next(paused);
  if (!ssml) {  // fragment list done — try the next section
    if (view.renderer.scrolled || true) {  // paginated & scrolled both auto-advance
      view.next().then(() => ttsReinit()).catch(() => ttsStop());
    }
    return;
  }
  if (paused) speechSynthesis.resume();
  speakSSML(ssml);
}

async function ttsReinit() {  // rebuild for the section that just opened
  if (!ttsOn || !view) return;
  await new Promise(r => setTimeout(r, 300));  // let the renderer settle
  await view.initTTS("sentence", ttsHighlight).catch(() => {});
  if (tts) { const ssml = tts.start(); speakSSML(ssml); }
}

$("#btn-tts").onclick = async () => {
  if (ttsOn) { speechSynthesis.pause(); ttsOn = false; return; }  // pause keeps the fragment position
  if (speechSynthesis.paused && tts) { speechSynthesis.resume(); ttsOn = true; return; }
  if (view.isFixedLayout) return;
  speechSynthesis.cancel();  // flush any stale queue before speaking
  // speak a zero-length utterance inside the click's user-activation window:
  // iOS Safari requires activation for speechSynthesis, and our await chain
  // (initTTS import + settle) loses it before the first real speak()
  ttsOn = true;
  $("#btn-tts").textContent = "⏸";
  $("#btn-tts").setAttribute("aria-label", "Pause reading");
  $("#btn-tts-stop").hidden = false;
  try { speechSynthesis.speak(new SpeechSynthesisUtterance(" ")); } catch {}
  await ttsReinit();
};

$("#btn-tts-stop").onclick = ttsStop;

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
  ({ Overlayer } = await import("/foliate-js/overlayer.js?v=3"));
  view = document.createElement("foliate-view");
  $("#viewer").prepend(view);
  await view.open(file);
  applyStyles();
  applyLayout();
  $("#zone-left").hidden = $("#zone-center").hidden = $("#zone-right").hidden = false;
  $("#type-wrap").hidden = view.isFixedLayout;  // typography controls are reflow-only
  $("#btn-search").hidden = view.isFixedLayout;  // search walks section docs — reflow only
  $("#btn-tts").hidden = $("#btn-tts-stop").hidden = view.isFixedLayout;  // TTS reads text — reflow only

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
    syncBookmarkBtn();
  });

  // annotation wiring: draw HOW (colors), re-draw when a section's overlay is
  // (re)created (annotations only render in the open section), tap -> manage
  view.addEventListener("draw-annotation", drawAnnotation);
  view.addEventListener("create-overlay", (e) => {
    for (const a of annos.values())
      if (a.kind !== "bookmark") view.addAnnotation({ value: a.cfi, color: a.color, note: a.note }).catch(() => {});
  });
  view.addEventListener("show-annotation", onShowAnnotation);
  view.addEventListener("load", ({ detail }) => {  // per-section selection watching
    detail.doc.addEventListener("selectionchange", onDocSelection);
  });

  await loadAnnotations();

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

// One Range-backed pseudo-File for EVERY format. foliate/pdf.js/zip.js all
// consume only {size, slice(a,b).arrayBuffer()} (zip.js's BlobReader does
// `blob.slice(e,n).arrayBuffer()`), so EPUBs are no longer downloaded whole —
// zip.js reads the zip tail + entry headers via Range, and pages/images load
// per-entry. pdf.js behaves the same (that is how the PDF path always worked).
// A proxy that strips Range (200 instead of 206) is handled by memoizing the
// full body once and slicing from memory — the pre-existing fallback.
function makeStreamingFile(fileUrl, headers, name, size) {
  let fullBody = null
  const file = {
    size,
    name,
    // full fetch; also marks this object as a File for makeBook()
    arrayBuffer: () => fetch(fileUrl, { headers }).then(r => r.arrayBuffer()),
    slice: (begin = 0, end = size) => ({
      arrayBuffer: () => {
        end = Math.min(end ?? size, size)  // never request past EOF (416)
        if (fullBody) return Promise.resolve(fullBody.slice(begin, end))
        const get = () => fetch(fileUrl, { headers: { ...headers, Range: `bytes=${begin}-${end - 1}` } })
          .then(r => r.arrayBuffer().then(buf => {
            if (r.status !== 206) {  // Range stripped — body IS the whole file
              fullBody = buf
              file.size = buf.byteLength  // re-derive before consumers validate offsets
              return buf.slice(begin, end)
            }
            return buf
          }))
        // one retry: transient mobile drops fail a single small range, not the book
        return get().catch(e => { if (e.name !== "TypeError") throw e; return get() })
      },
    }),
  }
  return file
}

function showOpenError(e) {
  const el = document.getElementById("loading")
  if (!el) return
  el.textContent = `Failed to open book — ${e.message}`
  const hint = document.createElement("div")
  hint.textContent = "Check your connection, then tap to retry."
  hint.style.cssText = "cursor:pointer;text-decoration:underline"
  hint.onclick = () => location.reload()
  el.append(hint)
}

const isPdf = book.ext === "pdf"
// reader=1 → server prefers its linearized derivative (built lazily, once);
// ignored for non-PDF. The derivative's byte length can differ from books.size
// (the original's) — pdf.js validates slices against the declared size, so the
// PDF path probes the real one. Non-PDF has no derivative: book.size is exact.
const fileUrl = `/api/books/${id}/file${isPdf ? "?reader=1" : ""}`
// first open of a non-warmed PDF pays a one-time server linearize — say so
const prepTimer = isPdf ? setTimeout(() => {
  const sub = document.getElementById("load-sub")
  if (sub) sub.textContent = "Preparing optimized layout…"
}, 2000) : null
try {
  let size = book.size || 0
  if (isPdf) {
    // If a proxy strips Range we get 200 here; the true size then only surfaces
    // with the body — handled inside makeStreamingFile via the fullBody fallback.
    const probe = await fetch(fileUrl, { headers: { ...headers, Range: "bytes=0-0" } })
    size = Number((probe.headers.get("content-range") || "").split("/")[1]) || size
  }
  if (!size) throw new Error("book file is empty")
  await openFoliate(makeStreamingFile(fileUrl, headers, `${book.title || "book"}.${book.ext}`, size))
} catch (e) {
  showOpenError(e)
} finally {
  if (prepTimer) clearTimeout(prepTimer)
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

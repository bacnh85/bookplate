/* Shelf frontend — vanilla JS, no build step. */
const $ = (s) => document.querySelector(s);
const token = () => localStorage.getItem("token");

async function api(path, opts = {}) {
  const headers = opts.headers || {};
  if (token()) headers.Authorization = `Bearer ${token()}`;
  if (opts.json) { headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(opts.json); }
  const r = await fetch(path, { ...opts, headers });
  if (r.status === 401 && !path.startsWith("/api/auth")) { logout(); throw new Error("signed out"); }
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.detail || r.statusText);
  return data;
}

function logout() { localStorage.removeItem("token"); location.reload(); }

/* ---------- xhr helper (fetch can't report upload/download progress) ---------- */
function xhr(method, path, { body, responseType, onProgress, uploadProgress } = {}) {
  return new Promise((resolve, reject) => {
    const x = new XMLHttpRequest();
    x.open(method, path);
    if (token()) x.setRequestHeader("Authorization", `Bearer ${token()}`);
    if (responseType) x.responseType = responseType;
    if (onProgress) x.onprogress = (e) => onProgress(e.loaded, e.total);
    if (uploadProgress) x.upload.onprogress = (e) => uploadProgress(e.loaded, e.total);
    x.onload = () => {
      if (x.status === 401 && !path.startsWith("/api/auth")) { logout(); return reject(new Error("signed out")); }
      if (x.status >= 200 && x.status < 300) {
        if (x.responseType === "blob") return resolve(x.response);
        let data = {};
        try { data = JSON.parse(x.responseText); } catch { /* empty body */ }
        return resolve(data);
      }
      let detail = x.statusText;
      try { detail = JSON.parse(x.responseText).detail || detail; } catch { /* not JSON */ }
      reject(new Error(detail));
    };
    x.onerror = () => reject(new Error("network error"));
    x.send(body);
  });
}

const fmtBytes = (n) => n == null ? "" : n >= 1e9 ? `${(n / 1e9).toFixed(1)} GB`
  : n >= 1e6 ? `${(n / 1e6).toFixed(1)} MB` : `${Math.round(n / 1e3)} kB`;

let me_kindle = false;
let me_sources = { zlib: false, annas: false, zlib_domain: "", annas_base: "" };
let me_devices = [], me_role = "";

/* ---------- transfer stack (upload progress) ---------- */
function transferCard(name) {
  const el = document.createElement("div");
  el.className = "transfer";
  el.innerHTML = `<div class="transfer-name">${esc(name)}</div>
    <div class="bar"><div class="bar-fill" style="width:0%"></div></div>
    <div class="transfer-state"></div>`;
  $("#transfers").append(el);
  const fill = el.querySelector(".bar-fill"), state = el.querySelector(".transfer-state"), bar = el.querySelector(".bar");
  return {
    percent(p) { bar.classList.remove("indeterminate"); fill.style.width = `${p}%`; state.textContent = `${p}%`; },
    indeterminate(text) { bar.classList.add("indeterminate"); state.textContent = text; },
    done(text) { this.percent(100); state.textContent = text; setTimeout(() => el.remove(), 4000); },
    fail(text) { el.classList.add("failed"); bar.classList.remove("indeterminate"); fill.style.width = "100%"; state.textContent = text; },
  };
}

/* ---------- auth ---------- */
let registerMode = false;
$("#auth-toggle").onclick = () => {
  registerMode = !registerMode;
  $("#auth-submit").textContent = registerMode ? "Create account" : "Sign in";
  $("#auth-toggle").textContent = registerMode ? "Have an account? Sign in" : "New here? Create an account";
};
$("#auth-form").onsubmit = async (e) => {
  e.preventDefault();
  $("#auth-error").textContent = "";
  $("#auth-ok").hidden = true;
  $("#auth-submit").disabled = true;
  try {
    const fn = registerMode ? "/api/auth/register" : "/api/auth/login";
    const res = await api(fn, {
      method: "POST",
      json: { username: $("#auth-user").value, password: $("#auth-pass").value },
    });
    if (res.status === "pending") {  // registered, awaiting approval — no token yet
      $("#auth-ok").hidden = false;
      $("#auth-submit").disabled = false;  // else the form locks until a reload
      return;
    }
    localStorage.setItem("token", res.token);
    boot();
  } catch (err) { $("#auth-error").textContent = err.message; }
  $("#auth-submit").disabled = false;
};

/* ---------- shelf ---------- */
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

/* ---------- library views (All / format filter / collection) ---------- */
let shelfFilter = null;  // null = All; "books" | "pdf" | "cbz"
const EXT_GROUPS = { all: null, books: ["epub", "mobi", "azw3", "fb2"], pdf: ["pdf"], cbz: ["cbz"] };
const FILTER_NAMES = { all: "All", books: "Books", pdf: "PDFs", cbz: "Comics" };

function bookTile(b) {
  const el = document.createElement("article");
  el.className = "tile";
  const pct = parseInt(localStorage.getItem(`progress-${b.id}-pct`), 10);
  el.innerHTML = `
    <div class="cover" title="${esc(b.title)}">
      <div class="spine-title">${esc(b.title)}</div>
      ${b.cover_ext ? `<img loading="lazy" src="/api/books/${b.id}/cover?v=${b.cover_v ?? 0}" alt="" onerror="this.remove()">` : ""}
    </div>
    ${pct > 0 && pct < 100 ? `<span class="tile-progress">${pct}%</span>` : ""}
    <button class="tile-more" type="button" aria-haspopup="menu" aria-label="Actions — ${esc(b.title)}">⋯</button>`;
  el.querySelector(".cover").onclick = () => openReader(b.id);
  el.querySelector(".tile-more").onclick = (e) => { e.stopPropagation(); openMenu(bookMenuItems(b), e.currentTarget); };
  return el;
}

function fillTiles(box, books) {
  box.innerHTML = "";
  for (const b of books) box.append(bookTile(b));
}

async function loadShelf(q = "") {
  const books = await api(`/api/books?q=${encodeURIComponent(q)}`);
  const filtered = shelfFilter ? books.filter((b) => EXT_GROUPS[shelfFilter].includes(b.ext)) : books;
  $("#library-title").textContent = FILTER_NAMES[shelfFilter] || "All";
  $("#library-count").textContent = filtered.length ? `${filtered.length} item${filtered.length > 1 ? "s" : ""}` : "";
  $("#shelf-empty").innerHTML = q ? `No results for “${esc(q)}”.`
    : `Nothing here yet.<small>Add books, or use the Book Store to pull them from Z-Library.</small>`;
  $("#shelf-empty").hidden = !!filtered.length;
  fillTiles($("#grid"), filtered);
}

/* ---------- context menu (⋯ on tiles) ---------- */
const ctxMenu = $("#ctx-menu");
let ctxAnchor = null;

function openMenu(items, anchor) {
  ctxMenu.innerHTML = "";
  for (const it of items) {
    if (it === "sep") { const s = document.createElement("div"); s.className = "menu-sep"; ctxMenu.append(s); continue; }
    const btn = document.createElement("button");
    btn.type = "button";
    btn.setAttribute("role", "menuitem");
    btn.textContent = it.label;
    if (it.danger) btn.classList.add("danger");
    btn.onclick = () => { closeMenu(); it.fn(); };
    ctxMenu.append(btn);
  }
  ctxMenu.hidden = false;
  ctxAnchor = anchor;
  const r = anchor.getBoundingClientRect();
  const w = ctxMenu.offsetWidth, h = ctxMenu.offsetHeight;
  ctxMenu.style.left = `${Math.max(8, Math.min(r.right - w, innerWidth - w - 8))}px`;
  let y = r.bottom + 4;
  if (y + h > innerHeight - 8) y = Math.max(8, r.top - h - 4);
  ctxMenu.style.top = `${y}px`;
  ctxMenu.querySelector("button")?.focus();
}

function closeMenu() {
  if (ctxMenu.hidden) return;
  ctxMenu.hidden = true;
  ctxAnchor?.focus();
  ctxAnchor = null;
}
document.addEventListener("click", (e) => { if (!ctxMenu.contains(e.target)) closeMenu(); });
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeMenu(); });

function bookMenuItems(b) {
  const items = [
    { label: "Read", fn: () => openReader(b.id) },
    { label: "Download", fn: () => downloadBook(null, b) },
  ];
  if (me_kindle && ["epub", "pdf"].includes(b.ext))
    items.push({ label: "Send to Kindle…", fn: () => sendToKindle(null, b) });
  if (b.own) items.push({ label: "Share…", fn: () => shareBook(b) });
  items.push("sep", { label: "Add to Collection…", fn: () => openCollectionDialog(b) });
  if (b.own) items.push("sep", { label: "Remove from shelf", danger: true, fn: async () => {
    if (confirm(`Remove "${b.title}" from your shelf?`)) {
      await api(`/api/books/${b.id}`, { method: "DELETE" });
      rerenderView();
    }
  } });
  return items;
}

let searchTimer;
$("#search").oninput = (e) => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => {
    shelfFilter = null;  // search spans the whole library
    show("library");
  }, 250);
};

/* ---------- shelf download (streamed with progress) ---------- */
async function downloadBook(btn, b) {
  if (btn?.disabled) return;
  if (btn) { btn.disabled = true; }
  try {
    const blob = await xhr("GET", `/api/books/${b.id}/file?dl=1`, { responseType: "blob",
      onProgress: (done, total) => {
        if (btn) btn.textContent = total ? `${Math.round((done / total) * 100)}%` : "…";
      } });
    const a = document.createElement("a");
    const url = URL.createObjectURL(blob);
    a.href = url;
    a.download = `${b.title || "book"}.${b.ext}`;
    a.click();
    setTimeout(() => URL.revokeObjectURL(url), 10000);
  } catch (err) {
    alert(`Download failed: ${err.message}`);
  }
  if (btn) { btn.disabled = false; btn.textContent = "Download"; }
}

/* ---------- upload ---------- */
$("#upload-btn").onclick = () => $("#upload-dialog").showModal();
$("#upload-close").onclick = () => $("#upload-dialog").close();
$("#upload-dialog").addEventListener("click", (e) => {
  if (e.target === e.currentTarget) e.currentTarget.close();  // backdrop click
});

const dialog = $("#upload-dialog");
const isFileDrag = (e) => [...(e.dataTransfer?.types || [])].includes("Files");
// a file drop anywhere on the page must never navigate — that destroys SPA
// state and aborts in-flight uploads
document.addEventListener("dragover", (e) => { if (isFileDrag(e)) e.preventDefault(); });
document.addEventListener("drop", (e) => { if (isFileDrag(e)) e.preventDefault(); });
// dropping anywhere on the open dialog is upload intent, not just the dropzone
dialog.addEventListener("dragover", (e) => { if (isFileDrag(e)) e.preventDefault(); });
dialog.addEventListener("drop", (e) => {
  if (!isFileDrag(e)) return;
  e.preventDefault();
  dialog.close();
  uploadFiles([...e.dataTransfer.files]);
});
const dropzone = $("#dropzone");
dropzone.onclick = () => $("#file-input").click();
dropzone.addEventListener("dragover", (e) => {
  if (!isFileDrag(e)) return;
  e.preventDefault();
  dropzone.classList.add("drag");
});
dropzone.addEventListener("dragleave", () => dropzone.classList.remove("drag"));
dropzone.addEventListener("drop", (e) => {
  if (!isFileDrag(e)) return;
  e.preventDefault();
  e.stopPropagation();  // dialog-level drop handler must not double-upload
  dropzone.classList.remove("drag");
  dialog.close();
  uploadFiles([...e.dataTransfer.files]);
});
$("#file-input").onchange = (e) => {
  $("#upload-dialog").close();
  uploadFiles([...e.target.files]);
  e.target.value = "";
};

const ACCEPT_EXTS = $("#file-input").accept.split(",").map((s) => s.trim().toLowerCase().replace(/^\./, ""));

async function uploadFiles(files) {
  const good = [], bad = [];
  for (const f of files)
    (ACCEPT_EXTS.includes(f.name.split(".").pop().toLowerCase()) ? good : bad).push(f);
  if (bad.length) transferCard(bad.map((f) => f.name).join(", "))
    .fail(`unsupported format — allowed: ${ACCEPT_EXTS.join(", ")}`);
  for (const f of good) {  // sequential: gentler on the server, readable progress
    const card = transferCard(f.name);
    try {
      const fd = new FormData(); fd.append("file", f);
      const res = await xhr("POST", "/api/books", { body: fd, uploadProgress: (done, total) => {
        const p = total ? Math.round((done / total) * 100) : 0;
        if (p >= 100) card.indeterminate("Processing…");  // server-side metadata enrichment
        else card.percent(p);
      } });
      if (res.duplicate) card.done("Already on shelf ✓");
      else if (res.similar.length) card.done(`Added — note: "${res.similar[0].title}" may be the same book`);
      else card.done("Added ✓");
    } catch (err) { card.fail(err.message); }
  }
  rerenderView();  // stay in the current view (collection/home), don't jump to All
}

/* ---------- share ---------- */
let shareBookId = null;
function shareBook(b) {
  shareBookId = b.id;
  $("#share-error").textContent = "";
  $("#share-user").value = "";
  $("#share-dialog").showModal();
}
$("#share-ok").onclick = async () => {
  try {
    await api(`/api/books/${shareBookId}/share`, { method: "POST", json: { username: $("#share-user").value } });
    $("#share-dialog").close();
  } catch (err) { $("#share-error").textContent = err.message; }
};

/* ---------- send to kindle ---------- */
let kindleBusy = false;
async function doKindleSend(b, device, fail) {
  if (kindleBusy) return false;
  kindleBusy = true;
  let err = null;
  try {
    await api(`/api/books/${b.id}/send-to-kindle`, { method: "POST", json: { device_id: device.id } });
  } catch (e) { err = e.message; fail?.(err); }
  finally { kindleBusy = false; }
  return !err;
}

async function sendToKindle(btn, b) {
  if (btn?.disabled) return;
  if (me_devices.length > 1) { renderKindleDialog(b); return; }
  if (!me_devices.length) { alert("Add a Kindle device first (Settings → Kindle devices)."); return; }
  if (btn) { btn.disabled = true; btn.textContent = "Sending…"; }
  if (await doKindleSend(b, me_devices[0], (m) => alert(`Send failed: ${m}`))) {
    if (btn) {
      btn.textContent = "Sent ✓";
      setTimeout(() => { btn.disabled = false; btn.textContent = "Kindle"; }, 4000);
    }
  } else if (btn) {
    btn.disabled = false;
    btn.textContent = "Kindle";
  }
}

function renderKindleDialog(b) {
  const list = $("#kindle-device-list");
  list.innerHTML = "";
  $("#kindle-error").textContent = "";
  for (const d of me_devices) {
    const row = document.createElement("button");
    row.type = "button";
    row.className = "kindle-device";
    row.innerHTML = `<strong>${esc(d.label)}</strong><span class="result-sub">${esc(d.email)}</span>`;
    row.onclick = async () => {
      row.disabled = true;
      row.textContent = "Sending…";
      let msg = null;
      if (await doKindleSend(b, d, (m) => { msg = m; })) { $("#kindle-dialog").close(); return; }
      renderKindleDialog(b);
      $("#kindle-error").textContent = msg || "";
    };
    list.appendChild(row);
  }
  $("#kindle-dialog").showModal();
}
$("#kindle-close").onclick = () => $("#kindle-dialog").close();

/* ---------- find (z-library / anna's archive) ---------- */
/* ---------- navigation ---------- */
let currentView = "home";
let currentCollectionId = null;
const VIEW_ELS = { home: "#home-view", store: "#store-view", library: "#library-view", admin: "#admin-view" };

function show(view, collectionId = null) {
  currentView = view;
  if (collectionId != null) currentCollectionId = collectionId;
  const elKey = view === "collection" ? "library" : view;  // collection shares the library pane
  for (const [k, sel] of Object.entries(VIEW_ELS)) $(sel).hidden = k !== elKey;
  document.querySelectorAll(".side-link").forEach((b) => {
    const on = b.dataset.view === view
      && (view !== "library" || b.dataset.filter === (shelfFilter || "all"))
      && (view !== "collection" || +b.dataset.cid === currentCollectionId);
    b.classList.toggle("active", on);
  });
  $("#collection-actions").hidden = view !== "collection";
  if (view === "home") loadHome();
  else if (view === "store") showStore();
  else if (view === "library") loadShelf($("#search").value);
  else if (view === "collection") loadCollection();
  else if (view === "admin") setAdminTab(adminTab);
}

function rerenderView() {
  if (currentView === "home") loadHome();
  else if (currentView === "library") loadShelf($("#search").value);
  else if (currentView === "collection") loadCollection();
}

document.querySelectorAll(".side-link[data-view]").forEach((b) => {
  b.onclick = () => {
    if (b.dataset.view === "library") shelfFilter = b.dataset.filter === "all" ? null : b.dataset.filter;
    show(b.dataset.view, b.dataset.cid ? +b.dataset.cid : null);
  };
});
$("#tab-settings").onclick = () => show("admin");

/* ---------- collections ---------- */
let collections = [];

async function loadCollections() {
  try { collections = await api("/api/collections"); }
  catch { return; }  // signed out
  const box = $("#side-collections");
  box.innerHTML = "";
  for (const c of collections) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "side-link";
    btn.dataset.view = "collection";
    btn.dataset.cid = c.id;
    btn.innerHTML = `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M8 6h13M8 12h13M8 18h13M3.5 6h.01M3.5 12h.01M3.5 18h.01"/></svg>${esc(c.name)}`;
    btn.onclick = () => show("collection", c.id);
    box.append(btn);
  }
}

async function loadCollection() {
  try {
    const c = await api(`/api/collections/${currentCollectionId}`);
    $("#library-title").textContent = c.name;
    $("#library-count").textContent = c.books.length ? `${c.books.length} item${c.books.length > 1 ? "s" : ""}` : "";
    $("#shelf-empty").innerHTML = `No books in this collection.<small>Use the ⋯ menu on a book cover to add it.</small>`;
    $("#shelf-empty").hidden = !!c.books.length;
    fillTiles($("#grid"), c.books);
  } catch {  // deleted elsewhere or not ours — fall back to All
    show("library");
  }
}

function promptDialog(title, initial = "") {
  return new Promise((resolve) => {
    const dlg = $("#prompt-dialog");
    $("#prompt-title").textContent = title;
    $("#prompt-input").value = initial;
    const onClose = () => { dlg.removeEventListener("close", onClose); resolve(dlg.returnValue === "ok" ? $("#prompt-input").value.trim() : null); };
    dlg.addEventListener("close", onClose);
    dlg.showModal();
    $("#prompt-input").focus();
  });
}

$("#nav-new-collection").onclick = async () => {
  const name = await promptDialog("New collection");
  if (!name) return;
  try {
    const c = await api("/api/collections", { method: "POST", json: { name } });
    await loadCollections();
    show("collection", c.id);
  } catch (e) { alert(e.message); }
};

$("#collection-rename").onclick = async () => {
  const c = collections.find((x) => x.id === currentCollectionId);
  if (!c) return;
  const name = await promptDialog("Rename collection", c.name);
  if (!name || name === c.name) return;
  try {
    await api(`/api/collections/${c.id}`, { method: "PATCH", json: { name } });
    loadCollections();
    loadCollection();
  } catch (e) { alert(e.message); }
};

$("#collection-delete").onclick = async () => {
  const c = collections.find((x) => x.id === currentCollectionId);
  if (!c || !confirm(`Delete collection "${c.name}"? Books stay on your shelf.`)) return;
  try {
    await api(`/api/collections/${c.id}`, { method: "DELETE" });
    loadCollections();
    show("library");
  } catch (e) { alert(e.message); }
};

let collectBookId = null;

async function openCollectionDialog(b) {
  collectBookId = b.id;
  $("#collection-error").textContent = "";
  try {
    const cols = await api(`/api/collections?book_id=${b.id}`);
    const box = $("#collection-list");
    box.innerHTML = "";
    if (!cols.length) box.innerHTML = `<p class="result-sub">No collections yet — create one below.</p>`;
    for (const c of cols) {
      const row = document.createElement("label");
      row.className = "check-row collection-row";
      const cb = document.createElement("input");
      cb.type = "checkbox";
      cb.checked = !!c.member;
      cb.onchange = async () => {
        try {
          if (cb.checked) await api(`/api/collections/${c.id}/books`, { method: "POST", json: { book_id: b.id } });
          else await api(`/api/collections/${c.id}/books/${b.id}`, { method: "DELETE" });
          loadCollections();
          rerenderView();
        } catch (e) { cb.checked = !cb.checked; $("#collection-error").textContent = e.message; }
      };
      row.append(cb, document.createTextNode(` ${c.name} `));
      const n = document.createElement("span");
      n.className = "result-sub";
      n.textContent = c.book_count ? `(${c.book_count})` : "(empty)";
      row.append(n);
      box.append(row);
    }
    $("#collection-dialog").showModal();
  } catch (e) { alert(e.message); }
}

$("#collection-close").onclick = () => $("#collection-dialog").close();
$("#collection-create-form").onsubmit = async (e) => {
  e.preventDefault();
  const name = $("#collection-new-name").value.trim();
  if (!name) return;
  try {
    await api("/api/collections", { method: "POST", json: { name } });
    $("#collection-new-name").value = "";
    await loadCollections();
    if (collectBookId) openCollectionDialog({ id: collectBookId });  // re-render with the new collection
  } catch (e) { $("#collection-error").textContent = e.message; }
};

/* rows carry `source: "annas"` from the annas search; absent = z-library.
   Quota lines are z-lib only — anna's archive has no quota endpoint. */
const rowSource = (r) => (r && r.source === "annas") ? "annas" : "zlib";
const SOURCES = {
  zlib: { search: "/api/zlib/search", queue: "/api/zlib/queue" },
  annas: { search: "/api/annas/search", queue: "/api/annas/queue" },
};
let findSource = "zlib";
function applySourceUI(s) {
  $("#src-zlib").classList.toggle("active", s === "zlib");
  $("#src-annas").classList.toggle("active", s === "annas");
  $("#zlib-search").placeholder = s === "annas" ? "Search Anna's Archive…" : "Search Z-Library…";
}
function setSource(s) {
  if (findSource === s) return;
  findSource = s;
  localStorage.setItem("findSource", s);  // survive reloads: the toggle used to reset
  applySourceUI(s);
  $("#zlib-error").textContent = "";
  $("#zlib-results").innerHTML = "";
  if (s === "zlib") showQuota();
  if ($("#zlib-search").value.trim()) zlibSearch($("#zlib-search").value);
}
$("#src-zlib").onclick = () => setSource("zlib");
$("#src-annas").onclick = () => setSource("annas");
applySourceUI(findSource = localStorage.getItem("findSource") === "annas" ? "annas" : "zlib");

/* ---------- book store ---------- */
function showStore() {
  $("#zlib-error").textContent = "";
  renderSourceCards();
  if (findSource === "zlib") showQuota();
}

function sourceCard(name, status, extra = "") {
  return `<div class="source-card">
    <h3>${esc(name)}</h3>
    <p class="source-status">${esc(status)}</p>
    ${extra}
  </div>`;
}

function renderSourceCards() {
  const s = me_sources, admin = me_role === "admin";
  $("#source-cards").innerHTML =
    (s.zlib
      ? sourceCard("Z-Library", `Connected${s.zlib_domain ? ` · ${s.zlib_domain}` : ""}`,
          `<p class="result-sub" id="store-zlib-quota">Checking daily quota…</p>`)
      : sourceCard("Z-Library", "Not configured",
          `<p class="result-sub">${admin
            ? "Add the shared account under Settings → App settings → Z-Library account."
            : "Ask the admin to add the shared Z-Library account."}</p>`))
    + (s.annas
      ? sourceCard("Anna's Archive", `Member key configured${s.annas_base ? ` · ${s.annas_base}` : ""}`,
          `<p class="result-sub">Free accounts download via slow partner servers automatically.</p>`)
      : sourceCard("Anna's Archive", "Not configured",
          `<p class="result-sub">${admin
            ? "Add a secret key under Settings → App settings → Anna's Archive — no membership needed; downloads fall back to free partner servers."
            : "Ask the admin to configure Anna's Archive."}</p>`));
  $("#src-zlib").hidden = !s.zlib;
  $("#src-annas").hidden = !s.annas;
  $("#store-toolbar").hidden = !s.zlib && !s.annas;
  if (!s[findSource]) setSource(s.zlib ? "zlib" : "annas");  // current source vanished
}

async function showQuota() {
  if (findSource !== "zlib") return;
  const src = findSource;  // the reply may land after the user switched sources
  const el = $("#store-zlib-quota");  // hoisted: the catch below clears it too
  try {
    const l = await api("/api/zlib/limits");
    if (findSource !== src) return;
    if (el) el.textContent = `Daily quota: ${l.daily_remaining ?? "?"} of ${l.daily_allowed ?? "?"} downloads left`;
  } catch { if (el) el.textContent = ""; /* unconfigured — surfaced on search */ }
}

let zlibTimer;
let zlibSearchSeq = 0;  // discard stale responses from overlapping searches
$("#zlib-search").oninput = (e) => {
  clearTimeout(zlibTimer);
  zlibTimer = setTimeout(() => zlibSearch(e.target.value), 400);
};
async function zlibSearch(q) {
  const seq = ++zlibSearchSeq;
  $("#zlib-error").textContent = "";
  if (!q.trim()) { $("#zlib-results").innerHTML = ""; return; }
  const box = $("#zlib-results");
  box.innerHTML = `<div class="skeleton" style="height:84px"></div>`;
  try {
    const { results } = await api(`${SOURCES[findSource].search}?q=${encodeURIComponent(q)}`);
    if (seq !== zlibSearchSeq) return;  // a newer search superseded this one
    box.innerHTML = "";
    if (!results.length) { box.innerHTML = `<div class="empty">No results.</div>`; return; }
    results.forEach((r, i) => box.append(resultRow(r, i + 1)));
  } catch (err) {
    box.innerHTML = "";
    $("#zlib-error").textContent = err.message;
  }
}

function ratingLine(r) {
  return `★ ${r.rating || "?"}${r.quality ? ` / ${r.quality}` : ""}`;
}

function resultRow(r, rank) {
  const row = document.createElement("div");
  row.className = "result-row";
  row.tabIndex = 0;
  row.setAttribute("role", "button");
  row.innerHTML = `
    <span class="rank" aria-hidden="true">${rank}</span>
    <img loading="lazy" src="${esc(r.cover)}" alt="" onerror="this.removeAttribute('src')">
    <div class="result-main">
      <div class="result-title">${esc(r.name)}</div>
      <div class="result-authors">${esc(r.authors || "—")}</div>
      ${r.publisher ? `<div class="result-sub">${esc(r.publisher)}</div>` : ""}
    </div>
    <div class="result-meta">
      <span class="badge">${r.source === "annas" ? "AA" : "z-lib"}</span>
      ${r.year ? `<span>Year: ${esc(r.year)}</span>` : ""}
      ${r.language ? `<span>Language: ${esc(r.language)}</span>` : ""}
      <span>File: ${esc((r.extension || "?").toUpperCase())}${r.size ? `, ${esc(r.size)}` : ""}</span>
      ${(r.rating || r.quality) ? `<span class="result-rating">${esc(ratingLine(r))}</span>` : ""}
    </div>`;
  const open = () => openDetail(r);
  row.onclick = open;
  row.onkeydown = (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(); } };
  const later = document.createElement("button");
  later.className = "btn-ghost queue-add";
  later.type = "button";
  later.textContent = "Get later";
  later.title = "Add to download queue";
  later.onclick = async (e) => {
    e.stopPropagation();
    try {
      await enqueue(r);
      later.textContent = "Queued ✓";
      later.disabled = true;
    } catch (err) { $("#zlib-error").textContent = err.message; }
  };
  row.querySelector(".result-meta").append(later);
  return row;
}

/* ---------- book detail panel ---------- */
let detailBook = null;
function openDetail(r) {
  detailBook = r;
  const dlg = $("#detail-dialog");
  $("#detail-error").textContent = "";
  $("#detail-quota").textContent = "";
  const img = $("#detail-cover");
  img.removeAttribute("src");
  img.hidden = !r.cover;
  if (r.cover) img.src = r.cover;
  img.onerror = () => { img.hidden = true; };
  $("#detail-title").textContent = r.name;
  $("#detail-authors").textContent = r.authors || "—";
  const dl = $("#detail-download");
  dl.disabled = false; dl.textContent = "Get later";
  const link = $("#detail-zlib");
  link.hidden = true;  // third-party data: only http(s) becomes clickable
  if (r.url && /^https?:\/\//i.test(r.url)) { link.href = r.url; link.hidden = false; }
  const facts = $("#detail-facts");
  facts.innerHTML = "";
  const add = (k, v) => {
    if (!v) return;
    const dt = document.createElement("dt"); dt.textContent = k;
    const dd = document.createElement("dd"); dd.textContent = v;
    facts.append(dt, dd);
  };
  add("Publisher", r.publisher);
  add("Year", r.year);
  add("Language", r.language);
  add("File", (r.extension || "").toUpperCase() + (r.size ? `, ${r.size}` : ""));
  add("Rating", r.rating ? ratingLine(r) : "");
  add("ISBN", r.isbn);
  $("#detail-desc").textContent = r.description || "";
  $("#detail-desc").hidden = !r.description;
  if (!dlg.open) dlg.showModal();
  dlg.scrollTop = 0;
  loadRelated(r);
  refreshQueue();  // mirror any existing queue state for this book onto the button
  if (rowSource(r) === "zlib") {
    api("/api/zlib/limits").then((l) => {
      $("#detail-quota").textContent = `Downloads today: ${l.daily_remaining ?? "?"} of ${l.daily_allowed ?? "?"} remaining`;
    }).catch(() => { /* unconfigured — surfaced on download */ });
  } else {
    $("#detail-quota").textContent = "";  // anna's archive: no quota endpoint
  }
}

$("#detail-download").onclick = async () => {
  if (!detailBook) return;
  const btn = $("#detail-download");
  btn.disabled = true;
  $("#detail-error").textContent = "";
  try { await enqueue(detailBook); }
  catch (err) { $("#detail-error").textContent = err.message; }
  btn.disabled = false;
};
$("#detail-close").onclick = () => $("#detail-dialog").close();
$("#detail-dialog").addEventListener("click", (e) => {
  if (e.target === e.currentTarget) e.currentTarget.close();  // backdrop click
});

async function loadRelated(r) {
  const section = $("#detail-related");
  const author = (r.authors || "").split(",")[0].trim();
  if (!author) { section.hidden = true; return; }
  const seq = ++zlibSearchSeq;  // share the search sequence: last openDetail wins
  $("#related-title").textContent = `More by ${author}`;
  $("#related-strip").innerHTML = `<div class="skeleton" style="height:96px"></div>`;
  section.hidden = false;
  try {
    const { results } = await api(`${SOURCES[rowSource(r)].search}?q=${encodeURIComponent(author)}`);
    if (seq !== zlibSearchSeq || detailBook?.id !== r.id) return;
    const rel = results.filter((b) => b.id !== r.id).slice(0, 6);
    if (!rel.length) { section.hidden = true; return; }
    const strip = $("#related-strip");
    strip.innerHTML = "";
    for (const b of rel) {
      const item = document.createElement("button");
      item.type = "button";
      item.className = "related-item";
      item.title = b.name;
      item.innerHTML = `
        <img loading="lazy" src="${esc(b.cover)}" alt="" onerror="this.remove()">
        <span class="related-name">${esc(b.name)}</span>`;
      item.onclick = () => openDetail(b);
      strip.append(item);
    }
  } catch { section.hidden = true; }
}

/* ---------- download queue (z-lib) ---------- */
const QUEUE_TERMINAL = ["done", "failed", "canceled"];
let queueTimer = null;

async function enqueue(r) {
  const j = await api(`${SOURCES[rowSource(r)].queue}`, { method: "POST", json: {
    id: r.id, name: r.name, authors: r.authors, cover: r.cover,
    extension: r.extension, size: r.size } });
  if (j.status === "failed") await api(`/api/zlib/queue/${j.id}/retry`, { method: "POST" });
  refreshQueue();
  return j;
}

async function refreshQueue() {
  let jobs = [];
  try { ({ jobs } = await api("/api/zlib/queue")); } catch { /* signed out etc. */ }
  renderQueue(jobs);
  mirrorDetail(jobs);
  const need = jobs.some((j) => !QUEUE_TERMINAL.includes(j.status)) || $("#downloads-dialog").open;
  if (need && !queueTimer) queueTimer = setInterval(refreshQueue, 3000);
  if (!need && queueTimer) { clearInterval(queueTimer); queueTimer = null; }
}

function barWidth(j) {
  if (j.status === "done" || j.status === "processing") return 100;
  if (j.status === "downloading") return j.bytes_total ? Math.round((j.bytes_done / j.bytes_total) * 100) : 100;
  return 0;
}

function jobStatusLine(j) {
  const bytes = j.bytes_done != null ? ` · ${fmtBytes(j.bytes_done)}${j.bytes_total ? ` / ${fmtBytes(j.bytes_total)}` : ""}` : "";
  switch (j.status) {
    case "queued": return `Queued${j.error ? ` · last error: ${j.error}` : ""}`;
    case "waiting_quota": return "Waiting for daily quota — retries automatically";
    case "downloading": return `Downloading${j.bytes_total ? ` ${Math.round((j.bytes_done / j.bytes_total) * 100)}%` : ""}${bytes}`;
    case "processing": return "Processing…";
    case "done": return "On shelf ✓";
    case "failed": return j.error || "Failed";
    default: return j.status;
  }
}

function renderQueue(jobs) {
  const list = $("#downloads-list");
  list.innerHTML = "";
  $("#downloads-empty").hidden = jobs.length > 0;
  for (const j of jobs) {
    const el = document.createElement("div");
    el.className = "queue-row";
    const initial = esc(((j.title || "?").trim()[0] || "?").toUpperCase());
    el.innerHTML = `
      ${j.cover_url
        ? `<img class="queue-cover" loading="lazy" src="${esc(j.cover_url)}" alt="" onerror="this.remove()">`
        : `<div class="queue-cover generated" aria-hidden="true">${initial}</div>`}
      <div class="queue-main">
        <div class="queue-title">${j.source === "annas" ? '<span class="badge">AA</span> ' : ""}${esc(j.title || j.zlib_id)}</div>
        ${j.authors ? `<div class="result-sub">${esc(j.authors)}</div>` : ""}
        <div class="bar ${j.status === "downloading" && !j.bytes_total ? "indeterminate" : ""}${j.status === "processing" ? " indeterminate" : ""}"><div class="bar-fill" style="width:${barWidth(j)}%"></div></div>
        <div class="queue-state ${j.status === "failed" ? "failed" : ""}">${esc(jobStatusLine(j))}</div>
      </div>
      <div class="queue-actions"></div>`;
    const actions = el.querySelector(".queue-actions");
    if (j.status === "failed") {
      const retry = document.createElement("button");
      retry.className = "btn-ghost"; retry.type = "button"; retry.textContent = "Retry";
      retry.onclick = async () => { await api(`/api/zlib/queue/${j.id}/retry`, { method: "POST" }); refreshQueue(); };
      actions.append(retry);
    }
    if (!["downloading", "processing"].includes(j.status)) {
      const rm = document.createElement("button");
      rm.className = "btn-danger"; rm.type = "button"; rm.textContent = "✕";
      rm.setAttribute("aria-label", "Remove from queue");
      rm.onclick = async () => { await api(`/api/zlib/queue/${j.id}`, { method: "DELETE" }); refreshQueue(); };
      actions.append(rm);
    }
    list.append(el);
  }
  const n = jobs.filter((j) => !QUEUE_TERMINAL.includes(j.status)).length;
  $("#downloads-badge").hidden = !n;
  $("#downloads-badge").textContent = n;
}

function mirrorDetail(jobs) {
  if (!detailBook || !$("#detail-dialog").open) return;
  const btn = $("#detail-download");
  const j = jobs.find((x) => x.zlib_id === String(detailBook.id));
  if (!j) { btn.textContent = "Get later"; btn.disabled = false; return; }
  btn.textContent = {
    queued: "Queued", waiting_quota: "Waiting for quota", downloading: "Downloading…",
    processing: "Processing…", done: "On shelf ✓", failed: "Failed — click to retry",
  }[j.status] || "Get later";
  btn.disabled = !["failed", "canceled"].includes(j.status);  // failed/canceled: click re-enqueues (retry)
}

$("#downloads-btn").onclick = () => {
  $("#downloads-dialog").showModal();
  refreshQueue();
  api("/api/zlib/limits").then((l) => {
    $("#downloads-quota").textContent = `Daily quota: ${l.daily_remaining ?? "?"} of ${l.daily_allowed ?? "?"} downloads remaining`;
  }).catch(() => { $("#downloads-quota").textContent = ""; });
};
$("#downloads-close").onclick = () => $("#downloads-dialog").close();
$("#downloads-dialog").addEventListener("click", (e) => {
  if (e.target === e.currentTarget) e.currentTarget.close();  // backdrop click
});
$("#downloads-dialog").addEventListener("close", () => refreshQueue());  // re-evaluate polling

/* ---------- reader ---------- */
function openReader(id) { location.assign(`/reader.html?id=${id}`); }

/* ---------- home ---------- */
async function loadHome() {
  const books = await api("/api/books");
  const total = books.length;
  const bytes = books.reduce((s, b) => s + (b.size || 0), 0);
  const fromStore = books.filter((b) => b.source === "zlibrary" || b.source === "annas-archive").length;
  const shared = books.filter((b) => b.shared_by).length;
  const stats = [
    ["Books on shelf", String(total)],
    ["Library size", fmtBytes(bytes) || "0 kB"],
    ["From the store", String(fromStore)],
    ["Shared with me", String(shared)],
  ];
  $("#home-stats").innerHTML = stats.map(([label, num]) =>
    `<div class="stat"><div class="stat-num">${esc(num)}</div><div class="stat-label">${esc(label)}</div></div>`).join("");
  $("#home-empty").hidden = !!total;
  const pct = (b) => parseInt(localStorage.getItem(`progress-${b.id}-pct`), 10);
  const reading = books.filter((b) => pct(b) > 0 && pct(b) < 100);
  $("#home-reading-sec").hidden = !reading.length;
  fillTiles($("#reading-strip"), reading);
  const recent = books.slice(0, 12);
  $("#home-recent-sec").hidden = !recent.length;
  fillTiles($("#recent-strip"), recent);
  const line = $("#home-queue-line");
  try {
    const { jobs } = await api("/api/zlib/queue");
    const active = jobs.filter((j) => !QUEUE_TERMINAL.includes(j.status)).length;
    const failed = jobs.filter((j) => j.status === "failed").length;
    const parts = [];
    if (active) parts.push(`${active} download${active > 1 ? "s" : ""} in progress`);
    if (failed) parts.push(`${failed} failed — open Downloads to retry`);
    line.textContent = parts.join(" · ");
    line.hidden = !parts.length;
  } catch { line.hidden = true; }
}

/* ---------- boot ---------- */
async function boot() {
  if (!token()) {
    $("#auth-view").hidden = false;
    $("#app-view").hidden = true;
    return;
  }
  $("#auth-view").hidden = true;
  $("#app-view").hidden = false;
  try {
    const me = await api("/api/me");
    $("#user-name").textContent = me.username;
    $("#tab-settings").hidden = false;
    me_role = me.role;
    document.querySelectorAll('.admin-tab[data-tab="users"],.admin-tab[data-tab="settings"],.admin-tab[data-tab="zlib"]')
      .forEach((b) => { b.hidden = me_role !== "admin"; });
    me_kindle = !!me.kindle;
    me_devices = me.devices || [];
    // devices but no send button => admin SMTP missing; say why instead of a dead-end
    $("#smtp-hint").hidden = !(me_devices.length && !me_kindle);
    me_id = me.id;
    me_sources = me.sources || me_sources;
    loadCollections();
    show("home");
    refreshQueue();  // badge + resume polling if jobs are active
  } catch { /* 401 handled in api() */ }
}
$("#logout-btn").onclick = logout;
boot();

/* ---------- settings (all users; admin-only sub-tabs gated in boot) ---------- */
let adminTab = "devices";
let zhPage = 1, zhTotal = 1;

document.querySelectorAll(".admin-tab").forEach((b) => {
  b.onclick = () => setAdminTab(b.dataset.tab);
});
function setAdminTab(tab) {
  if (me_role !== "admin" && tab !== "devices") tab = "devices";
  adminTab = tab;
  document.querySelectorAll(".admin-tab").forEach((b) =>
    b.classList.toggle("active", b.dataset.tab === tab));
  ["devices", "users", "settings", "zlib"].forEach((t) => { $(`#admin-${t}`).hidden = t !== tab; });
  $("#admin-error").textContent = "";
  if (tab === "devices") loadDevices();
  if (tab === "users") loadAdminUsers();
  if (tab === "settings") loadAdminSettings();
  if (tab === "zlib") loadAdminZlib();
}
const adminFail = (e) => { $("#admin-error").textContent = e.message; };

/* ----- kindle devices tab (every user) ----- */
async function loadDevices() {
  const box = $("#device-list");
  let devs;
  try { devs = await api("/api/kindle/devices"); }
  catch (e) { box.textContent = e.message; return; }
  me_devices = devs;
  box.innerHTML = devs.length ? "" : "No devices yet — add your Kindle's @kindle.com address above.";
  for (const d of devs) {
    const row = document.createElement("div");
    row.className = "admin-row";
    row.innerHTML = `<strong>${esc(d.label)}</strong><span class="au-email">${esc(d.email)}</span>`;
    const del = document.createElement("button");
    del.type = "button";
    del.className = "btn-ghost";
    del.textContent = "Delete";
    del.onclick = async () => {
      try { await api(`/api/kindle/devices/${d.id}`, { method: "DELETE" }); loadDevices(); boot(); }
      catch (e) { adminFail(e); }
    };
    row.appendChild(del);
    box.appendChild(row);
  }
}
$("#device-form").onsubmit = async (e) => {
  e.preventDefault();
  try {
    await api("/api/kindle/devices", { method: "POST", json: {
      label: $("#nd-label").value.trim(), email: $("#nd-email").value.trim() } });
    $("#nd-label").value = ""; $("#nd-email").value = "";
    loadDevices(); boot();  // boot re-gates the shelf's Kindle buttons
  } catch (err) { adminFail(err); }
};

/* ----- users tab ----- */
let adminUsersSeq = 0;
async function loadAdminUsers() {
  const seq = ++adminUsersSeq;  // stale-response guard: rapid tab switches / reloads
  const box = $("#admin-user-list");
  box.innerHTML = "";
  let users;
  try { users = await api("/api/admin/users"); } catch (e) { return adminFail(e); }
  if (seq !== adminUsersSeq) return;  // a newer load superseded this one
  for (const u of users) {
    const row = document.createElement("div");
    row.className = "admin-row";
    row.innerHTML = `
      <div class="au-email">${esc(u.username)}</div>
      <span class="badge">${esc(u.role)}</span>
      <span class="badge ${u.status === "active" ? "" : u.status === "disabled" ? "badge-danger" : "badge-warn"}">${esc(u.status)}</span>
      <span class="result-sub">${u.books} books</span>
      <span class="au-actions"></span>`;
    const acts = row.querySelector(".au-actions");
    const act = (label, fn, cls = "btn-ghost") => {
      const b = document.createElement("button");
      b.type = "button"; b.className = cls; b.textContent = label;
      b.onclick = async () => {
        try { await fn(); loadAdminUsers(); } catch (e) { adminFail(e); }
      };
      acts.appendChild(b);
    };
    if (u.status === "pending") act("Approve", () => api(`/api/admin/users/${u.id}/approve`, { method: "POST" }));
    if (u.status === "disabled") act("Enable", () => api(`/api/admin/users/${u.id}/enable`, { method: "POST" }));
    if (u.status === "active" && u.id !== me_id) {
      act(u.role === "admin" ? "Demote" : "Make admin", () =>
        api(`/api/admin/users/${u.id}/set-role`, { method: "POST", json: { role: u.role === "admin" ? "user" : "admin" } }));
      act("Disable", () => api(`/api/admin/users/${u.id}/disable`, { method: "POST" }));
      act("Reset PW", async () => {
        const pw = prompt(`New password for ${u.username} (min 6 chars)`);
        if (pw) await api(`/api/admin/users/${u.id}/reset-password`, { method: "POST", json: { password: pw } });
      });
    }
    box.appendChild(row);
  }
}
let me_id = null;  // set in boot(); self-demotion is hidden, backend still guards

$("#admin-user-form").onsubmit = async (e) => {
  e.preventDefault();
  try {
    await api("/api/admin/users", { method: "POST", json: {
      username: $("#nu-user").value, password: $("#nu-pass").value, role: $("#nu-role").value } });
    $("#nu-user").value = ""; $("#nu-pass").value = "";
    loadAdminUsers();
  } catch (err) { adminFail(err); }
};

/* ----- settings tab ----- */
const SECRET_FIELDS = { "zlib.password": "#set-zlib-password", "annas.secret_key": "#set-annas-key", "ai.api_key": "#set-ai-key", "kindle.smtp_password": "#set-kindle-password" };
const clearFlags = new Set();
document.querySelectorAll("[data-clear]").forEach((b) => {
  b.onclick = () => { clearFlags.add(b.dataset.clear); $(SECRET_FIELDS[b.dataset.clear]).value = ""; b.textContent = "cleared on save"; };
});

let settingsLoaded = false;
async function loadAdminSettings() {
  let s;
  try { s = await api("/api/admin/settings"); }
  catch (e) {
    settingsLoaded = false;
    $("#settings-save").disabled = true;  // a blank form must not wipe stored values
    return adminFail(e);
  }
  settingsLoaded = true;
  $("#settings-save").disabled = false;
  $("#set-zlib-email").value = s["zlib.email"] || "";
  $("#set-zlib-domain").value = s["zlib.domain"] || "";
  $("#set-annas-base").value = s["annas.base_url"] || "";
  $("#set-ai-base").value = s["ai.base_url"] || "";
  $("#set-ai-model").value = s["ai.model"] || "";
  $("#set-kindle-from").value = s["kindle.from"] || "";
  $("#set-kindle-host").value = s["kindle.smtp_host"] || "";
  $("#set-kindle-port").value = s["kindle.smtp_port"] || "";
  $("#set-kindle-user").value = s["kindle.smtp_user"] || "";
  $("#set-kindle-security").value = s["kindle.smtp_security"] || "starttls";
  $("#set-registration").value = s.registration || "approval";
  $("#set-ai-enabled").checked = s["ai.enabled"] !== "0";
  for (const [key, sel] of Object.entries(SECRET_FIELDS)) {
    const v = s[key];
    $(sel).placeholder = v?.set ? `saved (…${v.hint.slice(-4)}) — type to replace` : sel.includes("zlib") ? "password"
      : sel.includes("annas") ? "secret key" : sel.includes("kindle") ? "SMTP password" : "API key";
    $(sel).value = "";
  }
  clearFlags.clear();
  document.querySelectorAll("[data-clear]").forEach((b) => { b.textContent = "clear"; });
}

$("#admin-settings-form").onsubmit = async (e) => {
  e.preventDefault();
  if (!settingsLoaded) return adminFail(new Error("settings not loaded — nothing to save"));
  const values = {
    "zlib.email": $("#set-zlib-email").value.trim(),
    "zlib.domain": $("#set-zlib-domain").value.trim(),
    "annas.base_url": $("#set-annas-base").value.trim(),
    "ai.base_url": $("#set-ai-base").value.trim(),
    "ai.model": $("#set-ai-model").value.trim(),
    "kindle.from": $("#set-kindle-from").value.trim(),
    "kindle.smtp_host": $("#set-kindle-host").value.trim(),
    "kindle.smtp_port": $("#set-kindle-port").value.trim(),
    "kindle.smtp_security": $("#set-kindle-security").value,
    "kindle.smtp_user": $("#set-kindle-user").value.trim(),
    "ai.enabled": $("#set-ai-enabled").checked ? "1" : "0",
    registration: $("#set-registration").value,
  };
  for (const [key, sel] of Object.entries(SECRET_FIELDS)) {
    const v = $(sel).value;
    if (v) values[key] = v;                 // typed replacement
    else if (clearFlags.has(key)) values[key] = "";  // explicit clear -> empty value
    // else: leave untouched
  }
  try { await api("/api/admin/settings", { method: "PUT", json: { values } }); loadAdminSettings(); boot(); }  // boot re-gates Kindle buttons (smtp_ready changed)
  catch (err) { adminFail(err); }
};

/* ----- z-library tab ----- */
async function loadAdminZlib() {
  api("/api/admin/zlib/limits").then((l) => {
    $("#zlib-admin-quota").textContent = `Daily quota: ${l.daily_remaining ?? "?"} of ${l.daily_allowed ?? "?"} downloads remaining`;
  }).catch((e) => { $("#zlib-admin-quota").textContent = e.message; });
  await loadZlibHistory();
  loadZlibLibrary();
  loadZlibBooklists();
}
$("#zh-prev").onclick = () => { if (zhPage > 1) { zhPage--; loadZlibHistory(); } };
$("#zh-next").onclick = () => { if (zhPage < zhTotal) { zhPage++; loadZlibHistory(); } };

const bookRow = (b, queueLabel) => {
  const row = document.createElement("div");
  row.className = "admin-row";
  row.innerHTML = `
    <div class="au-email">${esc(b.name || b.id)}</div>
    <span class="badge">${esc((b.extension || "").toUpperCase())}</span>
    <span class="result-sub">${esc(b.size || "")}${b.year ? " · " + esc(b.year) : ""}</span>
    <span class="au-actions"></span>`;
  if (queueLabel) {
    const btn = document.createElement("button");
    btn.type = "button"; btn.className = "btn-primary"; btn.textContent = queueLabel;
    btn.onclick = async () => {
      btn.disabled = true; btn.textContent = "Queued ✓";
      try {
        await api("/api/zlib/queue", { method: "POST", json: {
          id: String(b.id), name: b.name || "", authors: b.authors || "",
          cover: b.cover || "", extension: b.extension || "", size: String(b.size || "") } });
        refreshQueue();
      } catch (e) { btn.disabled = false; btn.textContent = queueLabel; adminFail(e); }
    };
    row.querySelector(".au-actions").appendChild(btn);
  }
  return row;
};

async function loadZlibHistory() {
  const box = $("#zlib-history");
  box.innerHTML = "";
  let h;
  try { h = await api(`/api/admin/zlib/history?page=${zhPage}`); } catch (e) {
    box.textContent = e.message; return;
  }
  zhPage = h.page; zhTotal = h.total_pages || 1;
  $("#zh-page").textContent = `page ${zhPage} / ${zhTotal}`;
  for (const b of h.items) box.appendChild(bookRow(b, "Download"));
  if (!h.items.length) box.textContent = "No download history.";
}

async function loadZlibLibrary() {
  const box = $("#zlib-library");
  box.innerHTML = "";
  let lib;
  try { lib = await api("/api/admin/zlib/library"); } catch (e) { box.textContent = e.message; return; }
  if (!lib.available) { box.textContent = "Not available via API — tracked upstream (heartleo/zlib)."; return; }
  for (const b of lib.items) box.appendChild(bookRow(b, "Download"));
  if (!lib.items.length) box.textContent = "No saved books in the z-lib account.";
}

async function loadZlibBooklists() {
  const box = $("#zlib-booklists");
  box.innerHTML = "";
  let bl;
  try { bl = await api("/api/admin/zlib/booklists"); } catch (e) { box.textContent = e.message; return; }
  if (!bl.available) { box.textContent = "Not available via API — tracked upstream (heartleo/zlib)."; return; }
  for (const b of bl.items) box.appendChild(bookRow(b, ""));
}

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
  $("#auth-submit").disabled = true;
  try {
    const fn = registerMode ? "/api/auth/register" : "/api/auth/login";
    const { token: t } = await api(fn, {
      method: "POST",
      json: { email: $("#auth-email").value, password: $("#auth-pass").value },
    });
    localStorage.setItem("token", t);
    boot();
  } catch (err) { $("#auth-error").textContent = err.message; }
  $("#auth-submit").disabled = false;
};

/* ---------- shelf ---------- */
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

async function loadShelf(q = "") {
  const grid = $("#grid");
  grid.innerHTML = "";
  $("#shelf-empty").hidden = true;
  const books = await api(`/api/books?q=${encodeURIComponent(q)}`);
  if (!books.length) {
    $("#shelf-empty").hidden = false;
    return;
  }
  for (const b of books) {
    const card = document.createElement("article");
    card.className = "book-card";
    card.innerHTML = `
      <div class="cover" title="Read ${esc(b.title)}">
        <div class="spine-title">${esc(b.title)}</div>
        ${b.cover_ext ? `<img loading="lazy" src="/api/books/${b.id}/cover" alt="" onerror="this.remove()">` : ""}
      </div>
      <div class="book-meta">
        <div class="book-title">${esc(b.title)}</div>
        <div class="book-author">${esc(b.authors || "—")}</div>
        ${b.shared_by ? `<div class="shared-by">shared by ${esc(b.shared_by)}</div>` : ""}
        <span class="badge">${esc(b.ext)} · ${b.year || ""}</span>
      </div>
      <div class="book-actions">
        <button class="btn-ghost" data-act="read">Read</button>
        <button class="btn-ghost" data-act="download">Download</button>
        ${b.own ? `<button class="btn-ghost" data-act="share">Share</button>
        <button class="btn-danger" data-act="delete">✕</button>` : ""}
      </div>`;
    card.querySelector(".cover").onclick = () => openReader(b.id);
    card.querySelector('[data-act="read"]').onclick = () => openReader(b.id);
    card.querySelector('[data-act="download"]').onclick = () =>
      location.assign(`/api/books/${b.id}/file?dl=1`);
    const share = card.querySelector('[data-act="share"]');
    if (share) share.onclick = () => shareBook(b);
    const del = card.querySelector('[data-act="delete"]');
    if (del) del.onclick = async () => {
      if (confirm(`Remove "${b.title}" from your shelf?`)) {
        await api(`/api/books/${b.id}`, { method: "DELETE" });
        loadShelf($("#search").value);
      }
    };
    grid.append(card);
  }
}

let searchTimer;
$("#search").oninput = (e) => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => loadShelf(e.target.value), 250);
};

/* ---------- upload ---------- */
$("#upload-btn").onclick = () => $("#file-input").click();
$("#file-input").onchange = async (e) => {
  const files = [...e.target.files];
  for (const f of files) {
    try {
      const res = await api("/api/books", { method: "POST", body: (() => {
        const fd = new FormData(); fd.append("file", f); return fd;
      })() });
      if (res.duplicate) alert(`"${res.book.title}" was already on your shelf — added anyway, no copy stored.`);
      else if (res.similar.length) alert(`Note: "${res.similar[0].title}" (${res.similar[0].ext}) may be the same book in another format.`);
    } catch (err) { alert(`Upload failed: ${err.message}`); }
  }
  e.target.value = "";
  loadShelf();
};

/* ---------- share ---------- */
let shareBookId = null;
function shareBook(b) {
  shareBookId = b.id;
  $("#share-error").textContent = "";
  $("#share-email").value = "";
  $("#share-dialog").showModal();
}
$("#share-ok").onclick = async () => {
  try {
    await api(`/api/books/${shareBookId}/share`, { method: "POST", json: { email: $("#share-email").value } });
    $("#share-dialog").close();
  } catch (err) { $("#share-error").textContent = err.message; }
};

/* ---------- find (z-library) ---------- */
$("#tab-shelf").onclick = () => switchTab("shelf");
$("#tab-find").onclick = () => switchTab("find");
function switchTab(tab) {
  $("#shelf-view").hidden = tab !== "shelf";
  $("#find-view").hidden = tab !== "find";
  $("#tab-shelf").classList.toggle("active", tab === "shelf");
  $("#tab-find").classList.toggle("active", tab === "find");
  if (tab === "shelf") loadShelf($("#search").value);
  else if (tab === "find") showQuota();
}

async function showQuota() {
  $("#zlib-error").textContent = "";
  try {
    const l = await api("/api/zlib/limits");
    $("#zlib-error").textContent = `Downloads today: ${l.daily_remaining ?? "?"} of ${l.daily_allowed ?? "?"} remaining`;
  } catch { /* unconfigured — surfaced on search */ }
}

let zlibTimer;
$("#zlib-search").oninput = (e) => {
  clearTimeout(zlibTimer);
  zlibTimer = setTimeout(() => zlibSearch(e.target.value), 400);
};
async function zlibSearch(q) {
  $("#zlib-error").textContent = "";
  if (!q.trim()) { $("#zlib-results").innerHTML = ""; return; }
  const box = $("#zlib-results");
  box.innerHTML = `<div class="skeleton" style="height:84px"></div>`;
  try {
    const { results } = await api(`/api/zlib/search?q=${encodeURIComponent(q)}`);
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
      ${r.year ? `<span>Year: ${esc(r.year)}</span>` : ""}
      ${r.language ? `<span>Language: ${esc(r.language)}</span>` : ""}
      <span>File: ${esc((r.extension || "?").toUpperCase())}${r.size ? `, ${esc(r.size)}` : ""}</span>
      ${(r.rating || r.quality) ? `<span class="result-rating">${esc(ratingLine(r))}</span>` : ""}
    </div>`;
  const open = () => openDetail(r);
  row.onclick = open;
  row.onkeydown = (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(); } };
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
  dl.disabled = false; dl.textContent = "Download";
  const link = $("#detail-zlib");
  link.hidden = !r.url;
  if (r.url) link.href = r.url;
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
  api("/api/zlib/limits").then((l) => {
    $("#detail-quota").textContent = `Downloads today: ${l.daily_remaining ?? "?"} of ${l.daily_allowed ?? "?"} remaining`;
  }).catch(() => { /* unconfigured — surfaced on download */ });
}

$("#detail-download").onclick = async () => {
  if (!detailBook) return;
  const btn = $("#detail-download");
  btn.disabled = true; btn.textContent = "Fetching…";
  $("#detail-error").textContent = "";
  try {
    await api("/api/zlib/download", { method: "POST", json: { id: detailBook.id } });
    btn.textContent = "On shelf ✓";
  } catch (err) {
    btn.disabled = false; btn.textContent = "Download";
    $("#detail-error").textContent = err.message;
  }
};
$("#detail-close").onclick = () => $("#detail-dialog").close();
$("#detail-dialog").addEventListener("click", (e) => {
  if (e.target === e.currentTarget) e.currentTarget.close();  // backdrop click
});

async function loadRelated(r) {
  const section = $("#detail-related");
  const author = (r.authors || "").split(",")[0].trim();
  if (!author) { section.hidden = true; return; }
  $("#related-title").textContent = `More by ${author}`;
  $("#related-strip").innerHTML = `<div class="skeleton" style="height:96px"></div>`;
  section.hidden = false;
  try {
    const { results } = await api(`/api/zlib/search?q=${encodeURIComponent(author)}`);
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

/* ---------- reader ---------- */
function openReader(id) { location.assign(`/reader.html?id=${id}`); }

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
    $("#user-email").textContent = me.email;
    loadShelf();
  } catch { /* 401 handled in api() */ }
}
$("#logout-btn").onclick = logout;
boot();

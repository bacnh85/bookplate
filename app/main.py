"""Bookplate API — FastAPI + SQLite/FTS5 + content-addressed store."""
import asyncio
import os
import re
import shutil
import sqlite3
import tempfile
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import db, metadata, opds, settings
from .annas_client import AnnasConfigError, AnnasUnavailable, annas
from .auth import AdminDep, UserDep, admin_required, hash_password, make_token, verify_password
from .kindle import KindleError, smtp_ready, send as kindle_send
from .storage import book_path, cover_path, sha256_file, store_file
from .webfetch import _fetch_bytes, _resolve_public_ip
from .zlib_client import ZlibConfigError, ZlibUnavailable, parse_size, zlib
from . import zlib_eapi

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
TMP_DIR = db.DATA_DIR / "tmp"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    with db.conn() as con:  # resume jobs a previous process left mid-flight
        con.execute("UPDATE download_jobs SET status='queued', next_attempt_at=NULL "
                    "WHERE status IN ('downloading','processing')")
    worker = asyncio.create_task(download_worker())
    yield
    worker.cancel()
    with suppress(asyncio.CancelledError):
        await worker


app = FastAPI(title="Ebook Manager", lifespan=lifespan)
db.init()
TMP_DIR.mkdir(exist_ok=True)


# ---------- auth ----------

class Credentials(BaseModel):
    username: str
    password: str


@app.post("/api/auth/register")
def register(c: Credentials):
    u = c.username.strip().lower()
    if not u or len(c.password) < 6:
        raise HTTPException(400, "username and password (>=6 chars) required")
    if settings.get("registration", "approval") == "closed":
        raise HTTPException(403, "registration is closed")
    with db.conn() as con:
        try:
            con.execute(
                "INSERT INTO users(username, password_hash, role, status) VALUES(?,?,?,?)",
                (u, hash_password(c.password), "user", "pending"))
        except Exception:
            raise HTTPException(409, "username already registered")
    return {"status": "pending"}  # an admin approves before first login


@app.post("/api/auth/login")
def login(c: Credentials, response: Response):
    with db.conn() as con:
        row = con.execute(
            "SELECT * FROM users WHERE username=?",
            (c.username.strip().lower(),)).fetchone()
    if not row or not verify_password(c.password, row["password_hash"]):
        raise HTTPException(401, "invalid credentials")
    if row["status"] == "pending":
        raise HTTPException(403, "account awaiting admin approval")
    if row["status"] == "disabled":
        raise HTTPException(403, "account disabled")
    token = make_token(row["id"])
    _set_session(response, token)
    return {"token": token}


def _set_session(response: Response, token: str) -> None:
    """Cookie fallback so <img>/OPDS clients authenticate without headers."""
    response.set_cookie("session", token, httponly=True, samesite="lax",
                        max_age=30 * 86400)


@app.get("/api/me")
def me(user=UserDep):
    devices = _devices(user["id"])
    return {"id": user["id"], "username": user["username"],
            "role": user["role"], "status": user["status"],
            "kindle": bool(smtp_ready() and devices), "devices": devices,
            "sources": {"zlib": bool(settings.get("zlib.email") and settings.get("zlib.password")),
                        "annas": bool(settings.get("annas.secret_key")),
                        "zlib_domain": settings.get("zlib.domain"),
                        "annas_base": settings.get("annas.base_url")}}


# ---------- kindle devices (per-user) ----------

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _devices(user_id: int) -> list[dict]:
    with db.conn() as con:
        rows = con.execute("SELECT id, label, email FROM kindle_devices "
                           "WHERE user_id=? ORDER BY id", (user_id,)).fetchall()
    return [dict(r) for r in rows]


class DeviceReq(BaseModel):
    label: str = ""
    email: str


@app.get("/api/kindle/devices")
def kindle_devices(user=UserDep):
    return _devices(user["id"])


@app.post("/api/kindle/devices")
def kindle_device_add(req: DeviceReq, user=UserDep):
    email = req.email.strip()
    if not EMAIL_RE.match(email) or not email.lower().endswith("@kindle.com"):
        raise HTTPException(400, "enter your Kindle address (ends in @kindle.com)")
    label = req.label.strip() or email
    with db.conn() as con:
        try:
            con.execute("INSERT INTO kindle_devices(user_id, label, email) VALUES(?,?,?)",
                        (user["id"], label, email))
        except Exception:
            raise HTTPException(409, f"{email} is already in your device list")
    return _devices(user["id"])


@app.delete("/api/kindle/devices/{device_id}")
def kindle_device_delete(device_id: int, user=UserDep):
    with db.conn() as con:
        cur = con.execute("DELETE FROM kindle_devices WHERE id=? AND user_id=?",
                          (device_id, user["id"]))
    if not cur.rowcount:
        raise HTTPException(404, "no such device")
    return {"ok": True}


# ---------- books ----------

def _fts_term(t: str) -> str:
    """Quote an FTS5 prefix term, escaping embedded double quotes."""
    return '"' + t.replace('"', '""') + '"*'


def _norm_title(s: str) -> str:
    """Fold titles for logical-dup detection: strip parentheticals, punctuation, case."""
    return _norm(re.sub(r"\([^)]*\)", "", s))


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def _book_visible(con, user_id: int, book_id: int):
    return con.execute(
        "SELECT 1 FROM user_books WHERE user_id=? AND book_id=? "
        "UNION SELECT 1 FROM shares WHERE to_user=? AND book_id=?",
        (user_id, book_id, user_id, book_id)).fetchone()


@app.get("/api/books")
def list_books(q: str = "", user=UserDep):
    sql = """SELECT b.*, EXISTS(SELECT 1 FROM user_books ub
                 WHERE ub.user_id=:uid AND ub.book_id=b.id) AS own,
             (SELECT u.username FROM shares s JOIN users u ON u.id=s.from_user
                 WHERE s.book_id=b.id AND s.to_user=:uid LIMIT 1) AS shared_by
             FROM books b WHERE b.id IN (
               SELECT book_id FROM user_books WHERE user_id=:uid
               UNION SELECT book_id FROM shares WHERE to_user=:uid)"""
    params: dict = {"uid": user["id"]}
    if q.strip():
        terms = " AND ".join(_fts_term(t.strip()) for t in q.split() if t.strip())
        sql += " AND b.id IN (SELECT rowid FROM books_fts WHERE books_fts MATCH :q)"
        params["q"] = terms
    sql += " ORDER BY b.created_at DESC, b.id DESC"
    with db.conn() as con:
        rows = [dict(r) for r in con.execute(sql, params)]
    return _with_cover_v(rows)


def _with_cover_v(rows: list[dict]) -> list[dict]:
    # cover URL version = cover file mtime: covers are served immutable+1y, so a
    # re-render (backfill) must change the URL or browsers keep the old pixels
    for b in rows:
        if b["cover_ext"]:
            try:
                b["cover_v"] = int(cover_path(b["sha256"], b["cover_ext"]).stat().st_mtime)
            except OSError:
                b["cover_v"] = 0
    return rows


@app.get("/api/books/{book_id}")
def get_book(book_id: int, user=UserDep):
    with db.conn() as con:
        if not _book_visible(con, user["id"], book_id):
            raise HTTPException(404, "not found")
        row = con.execute("SELECT * FROM books WHERE id=?", (book_id,)).fetchone()
        return dict(row) if row else _raise404()


def _raise404():
    raise HTTPException(404, "not found")


async def _ingest(tmp: Path, orig_name: str, source: str, user_id: int,
                  fallback: dict | None = None) -> tuple[dict, bool]:
    """Shared ingest: sha dedup -> metadata (with optional z-lib fallback fields)
    -> store -> insert -> shelf link. Returns (book, duplicate). tmp carries the
    file extension; its bytes are moved into the store on first ingest."""
    sha = sha256_file(tmp)
    with db.conn() as con:
        row = con.execute("SELECT * FROM books WHERE sha256=?", (sha,)).fetchone()
        if row:
            book, dup = dict(row), True
        else:
            dup = False
            meta = await metadata.build_metadata(tmp, orig_name)
            if fallback:
                if not meta["title"] or meta["title"] == "Unknown title":
                    meta["title"] = fallback.get("name") or meta["title"]
                if not meta["authors"]:
                    meta["authors"] = fallback.get("authors") or ""
                if not meta["categories"] and fallback.get("categories"):
                    meta["categories"] = str(fallback["categories"]).split("-")[-1].strip()
                if not meta["description"] and fallback.get("description"):
                    meta["description"] = str(fallback["description"])
                if not meta["cover"] and fallback.get("cover"):
                    img = await _fetch_bytes(fallback["cover"])
                    if img:
                        meta["cover"], meta["cover_ext"] = img, "jpg"
            if not meta["cover"]:  # guaranteed thumbnail: deterministic generated cover
                meta["cover"] = metadata.generated_cover(meta["title"], meta["authors"], sha)
                meta["cover_ext"] = "svg"
            size = tmp.stat().st_size
            store_file(tmp, sha, tmp.suffix.lstrip(".").lower())
            cur = con.execute(
                """INSERT INTO books(sha256, ext, size, title, norm_title, authors, isbn, language,
                   categories, description, year, cover_ext, source, added_by)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (sha, tmp.suffix.lstrip(".").lower(), size, meta["title"], _norm_title(meta["title"]),
                 meta["authors"], meta["isbn"], meta["language"], meta["categories"],
                 meta["description"], meta["year"], meta["cover_ext"], source, user_id))
            if meta["cover"]:
                cover_path(sha, meta["cover_ext"] or "jpg").write_bytes(meta["cover"])
            book = dict(con.execute("SELECT * FROM books WHERE id=?", (cur.lastrowid,)).fetchone())
        con.execute("INSERT OR IGNORE INTO user_books(user_id, book_id) VALUES(?,?)",
                    (user_id, book["id"]))
        return book, dup


@app.post("/api/books")
async def upload(file: UploadFile = File(...), user=UserDep):
    ext = Path(file.filename or "").suffix.lower().lstrip(".")
    if ext not in metadata.EXTS:
        raise HTTPException(400, f"unsupported format, allowed: {', '.join(sorted(metadata.EXTS))}")
    fd, name = tempfile.mkstemp(dir=TMP_DIR, suffix=f".{ext}")
    os.close(fd)  # mkstemp leaks an fd if the int is discarded
    tmp = Path(name)
    try:
        with open(tmp, "wb") as f:
            shutil.copyfileobj(file.file, f)
        book, dup = await _ingest(tmp, file.filename or "", "upload", user["id"])
        similar = []
        if not dup:  # logical dup: same normalized title+author under a different file
            with db.conn() as con:
                similar = [dict(r) for r in con.execute(
                    "SELECT id, title, ext FROM books WHERE id != ? AND norm_title=? AND norm_title != ''",
                    (book["id"], _norm_title(book["title"])))]
        return {"book": book, "duplicate": dup, "similar": similar}
    finally:
        tmp.unlink(missing_ok=True)


@app.get("/api/books/{book_id}/file")
def book_file(book_id: int, dl: int = 0, user=UserDep):
    with db.conn() as con:
        if not _book_visible(con, user["id"], book_id):
            _raise404()
        b = con.execute("SELECT * FROM books WHERE id=?", (book_id,)).fetchone()
    path = book_path(b["sha256"], b["ext"])
    if not path.exists():
        _raise404()
    dispo = "attachment" if dl else "inline"
    return FileResponse(path, filename=path.name,
                        headers={"Content-Disposition": f'{dispo}; filename="{path.name}"'})


@app.get("/api/books/{book_id}/cover")
def book_cover(book_id: int, user=UserDep):
    with db.conn() as con:
        if not _book_visible(con, user["id"], book_id):
            _raise404()
        b = con.execute("SELECT sha256, cover_ext FROM books WHERE id=?", (book_id,)).fetchone()
    if not b["cover_ext"]:
        raise HTTPException(404, "no cover")
    path = cover_path(b["sha256"], b["cover_ext"])
    if not path.exists():
        raise HTTPException(404, "no cover")
    return FileResponse(path, headers={"Cache-Control": "private, max-age=31536000, immutable"})


@app.delete("/api/books/{book_id}")
def remove_book(book_id: int, user=UserDep):
    with db.conn() as con:
        b = con.execute("SELECT * FROM books WHERE id=?", (book_id,)).fetchone()
        if not b or not _book_visible(con, user["id"], book_id):
            _raise404()
        con.execute("DELETE FROM user_books WHERE user_id=? AND book_id=?", (user["id"], book_id))
        # drop the remover's collection memberships too: the book row can survive
        # via other owners/shares, so its ON DELETE CASCADE never fires for them
        con.execute("DELETE FROM collection_books WHERE book_id=? AND collection_id IN "
                    "(SELECT id FROM collections WHERE user_id=?)", (book_id, user["id"]))
        left = con.execute("SELECT COUNT(*) c FROM user_books WHERE book_id=?", (book_id,)).fetchone()["c"]
        shared = con.execute("SELECT COUNT(*) c FROM shares WHERE book_id=?", (book_id,)).fetchone()["c"]
        if left == 0 and shared == 0:
            con.execute("DELETE FROM books WHERE id=?", (book_id,))  # FTS trigger cleans up
            book_path(b["sha256"], b["ext"]).unlink(missing_ok=True)
            if b["cover_ext"]:
                cover_path(b["sha256"], b["cover_ext"]).unlink(missing_ok=True)
    return {"ok": True}


class ShareReq(BaseModel):
    username: str


@app.post("/api/books/{book_id}/share")
def share_book(book_id: int, req: ShareReq, user=UserDep):
    with db.conn() as con:
        if not con.execute("SELECT 1 FROM user_books WHERE user_id=? AND book_id=?",
                           (user["id"], book_id)).fetchone():
            _raise404()
        to = con.execute("SELECT id FROM users WHERE username=?",
                         (req.username.strip().lower(),)).fetchone()
        if not to:
            raise HTTPException(404, f"no user {req.username}")
        con.execute("INSERT OR IGNORE INTO shares(book_id, from_user, to_user) VALUES(?,?,?)",
                    (book_id, user["id"], to["id"]))
    return {"ok": True}


# ---------- collections (per-user shelves of visible books) ----------

class CollectionReq(BaseModel):
    name: str


class BookIdReq(BaseModel):
    book_id: int


@app.get("/api/collections")
def list_collections(user=UserDep, book_id: int | None = None):
    """book_id optional: adds a `member` flag so the UI can render
    add-to-collection checkboxes without per-collection probes."""
    with db.conn() as con:
        rows = con.execute(
            """SELECT c.id, c.name, COUNT(cb2.book_id) AS book_count,
                      EXISTS(SELECT 1 FROM collection_books cb
                             WHERE cb.collection_id=c.id AND cb.book_id=:bid) AS member
               FROM collections c LEFT JOIN collection_books cb2 ON cb2.collection_id=c.id
               WHERE c.user_id=:uid GROUP BY c.id ORDER BY c.name COLLATE NOCASE""",
            {"uid": user["id"], "bid": book_id if book_id is not None else -1}).fetchall()
    return [dict(r) for r in rows]


@app.post("/api/collections")
def create_collection(req: CollectionReq, user=UserDep):
    name = req.name.strip()
    if not name:
        raise HTTPException(400, "name required")
    with db.conn() as con:
        try:
            cur = con.execute("INSERT INTO collections(user_id, name) VALUES(?,?)",
                              (user["id"], name))
        except sqlite3.IntegrityError:
            raise HTTPException(409, f"collection '{name}' already exists")
    return {"id": cur.lastrowid, "name": name, "book_count": 0}


def _own_collection(con, collection_id: int, user_id: int):
    if not con.execute("SELECT 1 FROM collections WHERE id=? AND user_id=?",
                       (collection_id, user_id)).fetchone():
        _raise404()


@app.patch("/api/collections/{collection_id}")
def rename_collection(collection_id: int, req: CollectionReq, user=UserDep):
    name = req.name.strip()
    if not name:
        raise HTTPException(400, "name required")
    with db.conn() as con:
        _own_collection(con, collection_id, user["id"])
        try:
            con.execute("UPDATE collections SET name=? WHERE id=?", (name, collection_id))
        except sqlite3.IntegrityError:
            raise HTTPException(409, f"collection '{name}' already exists")
    return {"ok": True}


@app.delete("/api/collections/{collection_id}")
def delete_collection(collection_id: int, user=UserDep):
    with db.conn() as con:
        cur = con.execute("DELETE FROM collections WHERE id=? AND user_id=?",
                          (collection_id, user["id"]))
    if not cur.rowcount:
        _raise404()
    return {"ok": True}


@app.get("/api/collections/{collection_id}")
def get_collection(collection_id: int, user=UserDep):
    with db.conn() as con:
        c = con.execute("SELECT id, name FROM collections WHERE id=? AND user_id=?",
                        (collection_id, user["id"])).fetchone()
        if not c:
            _raise404()
        rows = [dict(r) for r in con.execute(
            """SELECT b.*, EXISTS(SELECT 1 FROM user_books ub
                    WHERE ub.user_id=:uid AND ub.book_id=b.id) AS own,
                (SELECT u.username FROM shares s JOIN users u ON u.id=s.from_user
                    WHERE s.book_id=b.id AND s.to_user=:uid LIMIT 1) AS shared_by
                FROM books b JOIN collection_books cb ON cb.book_id=b.id
                WHERE cb.collection_id=:cid AND b.id IN (
                  SELECT book_id FROM user_books WHERE user_id=:uid
                  UNION SELECT book_id FROM shares WHERE to_user=:uid)
                ORDER BY cb.added_at DESC, b.id DESC""",
            {"uid": user["id"], "cid": collection_id})]
    return {"id": c["id"], "name": c["name"], "books": _with_cover_v(rows)}


@app.post("/api/collections/{collection_id}/books")
def collection_add_book(collection_id: int, req: BookIdReq, user=UserDep):
    with db.conn() as con:
        _own_collection(con, collection_id, user["id"])
        if not _book_visible(con, user["id"], req.book_id):
            _raise404()
        con.execute("INSERT OR IGNORE INTO collection_books(collection_id, book_id) VALUES(?,?)",
                    (collection_id, req.book_id))
    return {"ok": True}


@app.delete("/api/collections/{collection_id}/books/{book_id}")
def collection_remove_book(collection_id: int, book_id: int, user=UserDep):
    with db.conn() as con:
        cur = con.execute(
            """DELETE FROM collection_books WHERE collection_id=? AND book_id=?
               AND collection_id IN (SELECT id FROM collections WHERE id=? AND user_id=?)""",
            (collection_id, book_id, collection_id, user["id"]))
    if not cur.rowcount:
        _raise404()
    return {"ok": True}


class KindleSendReq(BaseModel):
    device_id: int


@app.post("/api/books/{book_id}/send-to-kindle")
def send_to_kindle(book_id: int, req: KindleSendReq, user=UserDep):
    with db.conn() as con:
        if not _book_visible(con, user["id"], book_id):
            _raise404()
        b = con.execute("SELECT * FROM books WHERE id=?", (book_id,)).fetchone()
        d = con.execute("SELECT email FROM kindle_devices WHERE id=? AND user_id=?",
                        (req.device_id, user["id"])).fetchone()
    if not b or not d:
        _raise404()
    path = book_path(b["sha256"], b["ext"])
    if not path.exists():
        _raise404()
    try:
        kindle_send(path, b["title"], b["authors"], d["email"])
    except KindleError as e:
        raise HTTPException(502, str(e))
    return {"ok": True}


# ---------- z-library (env-gated) ----------

@app.get("/api/zlib/search")
async def zlib_search(q: str, user=UserDep):
    try:
        return {"results": await zlib.search(q)}
    except ZlibUnavailable as e:
        raise HTTPException(503, str(e))


@app.get("/api/zlib/limits")
async def zlib_limits(user=UserDep):
    try:
        return await zlib.limits()
    except ZlibUnavailable as e:
        raise HTTPException(503, str(e))


class ZlibQueueReq(BaseModel):
    id: str
    name: str = ""
    authors: str = ""
    cover: str = ""
    extension: str = ""
    size: str = ""


@app.post("/api/zlib/queue")
def zlib_enqueue(req: ZlibQueueReq, user=UserDep):
    if not req.id:
        raise HTTPException(400, "z-lib book id required")
    with db.conn() as con:
        con.execute(
            """INSERT OR IGNORE INTO download_jobs
               (user_id, zlib_id, title, authors, cover_url, ext, size_text)
               VALUES(?,?,?,?,?,?,?)""",
            (user["id"], req.id, req.name, req.authors, req.cover,
             (req.extension or "").lower(), req.size))
        row = con.execute("SELECT * FROM download_jobs WHERE user_id=? AND zlib_id=?",
                          (user["id"], req.id)).fetchone()
    return dict(row)


@app.get("/api/zlib/queue")
def zlib_queue(user=UserDep):
    # limits are NOT included: the frontend fetches /api/zlib/limits once per
    # dialog open — calling the zlib CLI on every 3s poll would spawn a process
    # per poll.
    with db.conn() as con:
        jobs = [dict(r) for r in con.execute(
            "SELECT * FROM download_jobs WHERE user_id=? ORDER BY id DESC LIMIT 200",
            (user["id"],))]
    return {"jobs": jobs}


@app.delete("/api/zlib/queue/{job_id}")
def zlib_queue_remove(job_id: int, user=UserDep):
    with db.conn() as con:
        # atomic: a job claimed by the worker between check and delete must not
        # be removed mid-download (else quota is spent for a book nobody sees)
        cur = con.execute("DELETE FROM download_jobs WHERE id=? AND user_id=? "
                          "AND status NOT IN ('downloading','processing')", (job_id, user["id"]))
        if cur.rowcount == 0:
            row = con.execute("SELECT 1 FROM download_jobs WHERE id=? AND user_id=?",
                              (job_id, user["id"])).fetchone()
            if not row:
                _raise404()
            raise HTTPException(409, "download in progress")
    return {"ok": True}


@app.post("/api/zlib/queue/{job_id}/retry")
@app.post("/api/annas/queue/{job_id}/retry")  # retry is source-agnostic; alias for symmetry
def zlib_queue_retry(job_id: int, user=UserDep):
    with db.conn() as con:
        row = con.execute("SELECT status FROM download_jobs WHERE id=? AND user_id=?",
                          (job_id, user["id"])).fetchone()
        if not row:
            _raise404()
        if row["status"] != "failed":
            raise HTTPException(400, "only failed jobs can be retried")
        con.execute("UPDATE download_jobs SET status='queued', error='', attempts=0, "
                    "bytes_done=NULL, bytes_total=NULL, next_attempt_at=NULL, updated_at=? "
                    "WHERE id=?", (_now_str(), job_id))
    return {"ok": True}


# ---------- anna's archive (env-gated, member secret key) ----------

@app.get("/api/annas/search")
async def annas_search(q: str, user=UserDep):
    try:
        return {"results": await annas.search(q)}
    except AnnasConfigError as e:
        raise HTTPException(400, str(e))  # permanent: missing/rejected key
    except AnnasUnavailable as e:
        raise HTTPException(503, str(e))


@app.get("/api/annas/queue")
def annas_queue_list(user=UserDep):
    # alias: one shared queue table; clients of either source can list it
    return zlib_queue(user=user)


@app.post("/api/annas/queue")
def annas_enqueue(req: ZlibQueueReq, user=UserDep):
    # id is the book's md5 from the search page — validate before it reaches URLs
    if not re.fullmatch(r"[a-f0-9]{32}", req.id or ""):
        raise HTTPException(400, "anna's archive md5 required")
    with db.conn() as con:
        con.execute(
            """INSERT OR IGNORE INTO download_jobs
               (user_id, zlib_id, title, authors, cover_url, ext, size_text, source)
               VALUES(?,?,?,?,?,?,?, 'annas')""",
            (user["id"], req.id, req.name, req.authors, req.cover,
             (req.extension or "").lower(), req.size))
        row = con.execute("SELECT * FROM download_jobs WHERE user_id=? AND zlib_id=?",
                          (user["id"], req.id)).fetchone()
    return dict(row)


@app.delete("/api/annas/queue/{job_id}")
def annas_queue_remove(job_id: int, user=UserDep):
    return zlib_queue_remove(job_id, user=user)


# ---------- admin: user management ----------

class AdminUserReq(BaseModel):
    username: str
    password: str
    role: str = "user"


class SetRoleReq(BaseModel):
    role: str


class PasswordReq(BaseModel):
    password: str


def _active_admin_others(con, uid: int) -> int:
    return con.execute(
        "SELECT COUNT(*) c FROM users WHERE role='admin' AND status='active' AND id != ?",
        (uid,)).fetchone()["c"]


@app.get("/api/admin/users")
def admin_users(admin=AdminDep):
    with db.conn() as con:
        return [dict(r) for r in con.execute(
            """SELECT u.id, u.username, u.role, u.status, u.created_at,
               (SELECT COUNT(*) FROM user_books ub WHERE ub.user_id=u.id) AS books
               FROM users u ORDER BY u.id""")]


@app.post("/api/admin/users")
def admin_create_user(req: AdminUserReq, admin=AdminDep):
    u = req.username.strip().lower()
    if not u or len(req.password) < 6:
        raise HTTPException(400, "username and password (>=6 chars) required")
    if req.role not in ("user", "admin"):
        raise HTTPException(400, "role must be user or admin")
    with db.conn() as con:
        try:
            cur = con.execute(
                "INSERT INTO users(username, password_hash, role, status) VALUES(?,?,?,'active')",
                (u, hash_password(req.password), req.role))
        except Exception:
            raise HTTPException(409, "username already registered")
        row = con.execute("SELECT id, username, role, status FROM users WHERE id=?",
                          (cur.lastrowid,)).fetchone()
    return dict(row)


def _set_status(uid: int, status: str):
    with db.conn() as con:
        row = con.execute("SELECT id, role FROM users WHERE id=?", (uid,)).fetchone()
        if not row:
            _raise404()
        # never strand the system without an active admin (approval queue would deadlock)
        if status == "disabled" and row["role"] == "admin" and _active_admin_others(con, uid) == 0:
            raise HTTPException(409, "cannot disable the last admin")
        con.execute("UPDATE users SET status=? WHERE id=?", (status, uid))
    return {"ok": True}


@app.post("/api/admin/users/{uid}/approve")
def admin_approve(uid: int, admin=AdminDep):
    return _set_status(uid, "active")


@app.post("/api/admin/users/{uid}/enable")
def admin_enable(uid: int, admin=AdminDep):
    return _set_status(uid, "active")


@app.post("/api/admin/users/{uid}/disable")
def admin_disable(uid: int, admin=AdminDep):
    return _set_status(uid, "disabled")


@app.post("/api/admin/users/{uid}/set-role")
def admin_set_role(uid: int, req: SetRoleReq, admin=AdminDep):
    if req.role not in ("user", "admin"):
        raise HTTPException(400, "role must be user or admin")
    with db.conn() as con:
        row = con.execute("SELECT id, role FROM users WHERE id=?", (uid,)).fetchone()
        if not row:
            _raise404()
        if row["role"] == "admin" and req.role == "user" and _active_admin_others(con, uid) == 0:
            raise HTTPException(409, "cannot demote the last admin")
        con.execute("UPDATE users SET role=? WHERE id=?", (req.role, uid))
    return {"ok": True}


@app.post("/api/admin/users/{uid}/reset-password")
def admin_reset_password(uid: int, req: PasswordReq, admin=AdminDep):
    if len(req.password) < 6:
        raise HTTPException(400, "password (>=6 chars) required")
    with db.conn() as con:
        cur = con.execute("UPDATE users SET password_hash=? WHERE id=?",
                          (hash_password(req.password), uid))
        if cur.rowcount == 0:
            _raise404()
    return {"ok": True}


# ---------- admin: app-managed settings ----------

SECRET_SETTINGS = {"zlib.password", "annas.secret_key", "ai.api_key", "kindle.smtp_password"}


def _mask(v: str) -> dict:
    return {"set": bool(v), "hint": ("…" + v[-4:]) if v else ""}


@app.get("/api/admin/settings")
def admin_get_settings(admin=AdminDep):
    out = {}
    for key in settings.KEYS:
        v = settings.get(key, "approval" if key == "registration" else "")
        out[key] = _mask(v) if key in SECRET_SETTINGS else v
    return out


class SettingsReq(BaseModel):
    values: dict[str, str]


@app.put("/api/admin/settings")
def admin_put_settings(req: SettingsReq, admin=AdminDep):
    for k, v in req.values.items():
        if k not in settings.KEYS:
            raise HTTPException(400, f"unknown setting {k}")
        if k == "registration" and v not in ("approval", "closed"):
            raise HTTPException(400, "registration must be 'approval' or 'closed'")
        if k == "kindle.smtp_security" and v not in ("", "starttls", "ssl", "none"):
            raise HTTPException(400, "kindle.smtp_security must be starttls, ssl or none")
        if k == "kindle.smtp_port" and v and not v.isdigit():
            raise HTTPException(400, "kindle.smtp_port must be a number")
    for k, v in req.values.items():  # validate all before writing any
        settings.set(k, v)
    return {"ok": True}


# ---------- admin: z-library account management ----------

@app.get("/api/admin/zlib/limits")
async def admin_zlib_limits(admin=AdminDep):
    try:
        return await zlib.limits()
    except ZlibUnavailable as e:
        raise HTTPException(503, str(e))


@app.get("/api/admin/zlib/history")
async def admin_zlib_history(page: int = 1, fmt: str = "", admin=AdminDep):
    try:
        return await zlib.history(page=page, fmt=fmt)
    except ZlibUnavailable as e:
        raise HTTPException(503, str(e))


@app.get("/api/admin/zlib/library")
async def admin_zlib_library(page: int = 1, admin=AdminDep):
    try:
        return await zlib_eapi.library(page=page)
    except ZlibUnavailable as e:
        raise HTTPException(503, str(e))


@app.get("/api/admin/zlib/booklists")
async def admin_zlib_booklists(admin=AdminDep):
    try:
        return await zlib_eapi.booklists()
    except ZlibUnavailable as e:
        raise HTTPException(503, str(e))


# ---------- download queue worker ----------

# ponytail: sequential worker is deliberate — one shared z-lib account; parallel
# downloads would burn quota and stress mirrors. Revisit only with per-account keys.
QUOTA_RECHECK_MIN = 30  # re-poll daily_remaining this often while quota is exhausted
MAX_ATTEMPTS = 3
BACKOFF_MIN = (5, 30)   # retry delay after attempt 1, 2


def _now_str(offset_min: int = 0) -> str:
    """UTC timestamp matching sqlite datetime('now') format, for string compares."""
    dt = datetime.now(timezone.utc) + timedelta(minutes=offset_min)
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _job_update(job_id: int, **fields) -> None:
    cols = ", ".join(f"{k}=?" for k in fields)
    with db.conn() as con:
        con.execute(f"UPDATE download_jobs SET {cols}, updated_at=? WHERE id=?",
                    (*fields.values(), _now_str(), job_id))


def _claim_next_job() -> dict | None:
    """Atomically mark the oldest due job as downloading; None when idle.
    Also reclaims 'downloading' rows gone stale (15 min without an updated_at
    touch — active downloads bump it every second), so a job whose requeue
    update was lost to a DB hiccup self-heals instead of stranding."""
    with db.conn() as con:
        row = con.execute(
            """SELECT * FROM download_jobs
               WHERE (status IN ('queued','waiting_quota')
                      AND (next_attempt_at IS NULL OR next_attempt_at <= ?))
                  OR (status = 'downloading' AND updated_at < ?)
               ORDER BY id LIMIT 1""", (_now_str(), _now_str(-15))).fetchone()
        if not row:
            return None
        con.execute("UPDATE download_jobs SET status='downloading', updated_at=? WHERE id=?",
                    (_now_str(), row["id"]))
        return dict(row)


def _job_backoff(jid: int, job: dict, e: Exception) -> None:
    """Shared retry ladder (both sources): 3 attempts, backoff 5/30 min."""
    attempts = job["attempts"] + 1
    if attempts >= MAX_ATTEMPTS:
        _job_update(jid, status="failed", error=str(e)[:300], attempts=attempts,
                    bytes_done=None, bytes_total=None, next_attempt_at=None)
    else:
        _job_update(jid, status="queued", error=str(e)[:300], attempts=attempts,
                    bytes_done=None, bytes_total=None,
                    next_attempt_at=_now_str(BACKOFF_MIN[attempts - 1]))


async def _run_job(job: dict) -> None:
    jid = job["id"]
    try:
        if job.get("source") == "annas":
            data, meta = await annas.download(
                job["zlib_id"],
                on_progress=lambda d, t: _job_update(jid, bytes_done=d, bytes_total=t),
                expected_size=parse_size(job["size_text"]))
        else:
            try:
                limits = await zlib.limits()
                if isinstance(limits.get("daily_remaining"), int) and limits["daily_remaining"] <= 0:
                    # quota exhausted: re-check when the daily window may have reset
                    _job_update(jid, status="waiting_quota", next_attempt_at=_now_str(QUOTA_RECHECK_MIN))
                    return
            except ZlibUnavailable:
                pass  # quota probe failed — attempt the download anyway
            data, meta = await zlib.download(
                job["zlib_id"],
                on_progress=lambda d, t: _job_update(jid, bytes_done=d, bytes_total=t),
                expected_size=parse_size(job["size_text"]))
    except (ZlibConfigError, AnnasConfigError) as e:
        _job_update(jid, status="failed", error=str(e)[:300], next_attempt_at=None)
        return
    except (ZlibUnavailable, AnnasUnavailable) as e:
        _job_backoff(jid, job, e)
        return
    _job_update(jid, status="processing", bytes_done=None, bytes_total=None)
    ext = Path(meta["_filename"]).suffix.lower().lstrip(".") or job["ext"]
    if ext not in metadata.EXTS:
        ext = "pdf"
    fd, name = tempfile.mkstemp(dir=TMP_DIR, suffix=f".{ext}")
    os.close(fd)  # mkstemp leaks an fd if the int is discarded
    tmp = Path(name)
    try:
        tmp.write_bytes(data)
        await _ingest(tmp, meta["_filename"],
                      "annas-archive" if job.get("source") == "annas" else "zlibrary",
                      job["user_id"],
                      fallback={"name": job["title"], "authors": job["authors"],
                                "cover": job["cover_url"]})
    finally:
        tmp.unlink(missing_ok=True)
    _job_update(jid, status="done", error="")


async def download_worker():
    while True:
        job = None
        try:
            job = _claim_next_job()
            if job:
                try:  # a poisoned job must fail alone, never kill the queue task
                    await _run_job(job)
                except Exception as e:  # noqa: BLE001
                    _job_update(job["id"], status="failed", error=str(e)[:300], next_attempt_at=None)
            else:
                await asyncio.sleep(5)
        except Exception as e:  # noqa: BLE001 — DB hiccup etc: never let the queue task die
            print(f"download_worker: {e!r}", flush=True)
            if job:  # claim happened before the failure — put it back for a later pass
                try:
                    _job_update(job["id"], status="queued", next_attempt_at=_now_str(1))
                except Exception:
                    pass
            await asyncio.sleep(1)


# ---------- OPDS ----------

@app.get("/opds")
def opds_catalog(request: Request, user=UserDep):
    with db.conn() as con:
        rows = [dict(r) for r in con.execute(
            """SELECT b.* FROM books b WHERE b.id IN (
                 SELECT book_id FROM user_books WHERE user_id=?
                 UNION SELECT book_id FROM shares WHERE to_user=?)
               ORDER BY b.created_at DESC""", (user["id"], user["id"]))]
    return Response(opds.catalog(rows, str(request.url.path)),
                    media_type="application/atom+xml;profile=opds-catalog")


# ---------- frontend ----------

class NoCacheStaticFiles(StaticFiles):
    """Revalidate every asset each load (ETag -> 304 when unchanged): browsers
    otherwise heuristic-cache JS/HTML and keep running a stale frontend after a
    deploy. Stamp every file response rather than intercepting routes — a
    catch-all route 404s sibling files (reader.html was the casualty)."""

    def file_response(self, *args, **kwargs):
        resp = super().file_response(*args, **kwargs)
        resp.headers["Cache-Control"] = "no-cache"
        return resp


app.mount("/", NoCacheStaticFiles(directory=WEB_DIR, html=True), name="web")

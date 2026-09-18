"""Bookplate API — FastAPI + SQLite/FTS5 + content-addressed store."""
import asyncio
import os
import re
import shutil
import tempfile
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import db, metadata, opds
from .annas_client import AnnasConfigError, AnnasUnavailable, annas
from .auth import UserDep, hash_password, make_token, verify_password
from .storage import book_path, cover_path, sha256_file, store_file
from .webfetch import _fetch_bytes, _resolve_public_ip
from .zlib_client import ZlibConfigError, ZlibUnavailable, parse_size, zlib

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
    email: str
    password: str


@app.post("/api/auth/register")
def register(c: Credentials, response: Response):
    if not c.email or len(c.password) < 6:
        raise HTTPException(400, "email and password (>=6 chars) required")
    with db.conn() as con:
        try:
            cur = con.execute(
                "INSERT INTO users(email, password_hash) VALUES(?,?)",
                (c.email.strip().lower(), hash_password(c.password)))
        except Exception:
            raise HTTPException(409, "email already registered")
        token = make_token(cur.lastrowid)
        _set_session(response, token)
        return {"token": token}


@app.post("/api/auth/login")
def login(c: Credentials, response: Response):
    with db.conn() as con:
        row = con.execute(
            "SELECT * FROM users WHERE email=?", (c.email.strip().lower(),)).fetchone()
    if not row or not verify_password(c.password, row["password_hash"]):
        raise HTTPException(401, "invalid credentials")
    token = make_token(row["id"])
    _set_session(response, token)
    return {"token": token}


def _set_session(response: Response, token: str) -> None:
    """Cookie fallback so <img>/OPDS clients authenticate without headers."""
    response.set_cookie("session", token, httponly=True, samesite="lax",
                        max_age=30 * 86400)


@app.get("/api/me")
def me(user=UserDep):
    return {"id": user["id"], "email": user["email"]}


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
             (SELECT u.email FROM shares s JOIN users u ON u.id=s.from_user
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
        return [dict(r) for r in con.execute(sql, params)]


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
        left = con.execute("SELECT COUNT(*) c FROM user_books WHERE book_id=?", (book_id,)).fetchone()["c"]
        shared = con.execute("SELECT COUNT(*) c FROM shares WHERE book_id=?", (book_id,)).fetchone()["c"]
        if left == 0 and shared == 0:
            con.execute("DELETE FROM books WHERE id=?", (book_id,))  # FTS trigger cleans up
            book_path(b["sha256"], b["ext"]).unlink(missing_ok=True)
            if b["cover_ext"]:
                cover_path(b["sha256"], b["cover_ext"]).unlink(missing_ok=True)
    return {"ok": True}


class ShareReq(BaseModel):
    email: str


@app.post("/api/books/{book_id}/share")
def share_book(book_id: int, req: ShareReq, user=UserDep):
    with db.conn() as con:
        if not con.execute("SELECT 1 FROM user_books WHERE user_id=? AND book_id=?",
                           (user["id"], book_id)).fetchone():
            _raise404()
        to = con.execute("SELECT id FROM users WHERE email=?",
                         (req.email.strip().lower(),)).fetchone()
        if not to:
            raise HTTPException(404, f"no user {req.email}")
        con.execute("INSERT OR IGNORE INTO shares(book_id, from_user, to_user) VALUES(?,?,?)",
                    (book_id, user["id"], to["id"]))
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
    except AnnasUnavailable as e:
        raise HTTPException(503, str(e))


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

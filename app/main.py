"""Bookplate API — FastAPI + SQLite/FTS5 + content-addressed store."""
import os
import re
import shutil
import tempfile
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import db, metadata, opds
from .auth import UserDep, hash_password, make_token, verify_password
from .storage import book_path, cover_path, sha256_file, store_file
from .zlib_client import ZlibUnavailable, zlib

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
TMP_DIR = db.DATA_DIR / "tmp"

app = FastAPI(title="Ebook Manager")
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
    row = db.conn().execute(
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
    return [dict(r) for r in db.conn().execute(sql, params)]


@app.get("/api/books/{book_id}")
def get_book(book_id: int, user=UserDep):
    with db.conn() as con:
        if not _book_visible(con, user["id"], book_id):
            raise HTTPException(404, "not found")
        row = con.execute("SELECT * FROM books WHERE id=?", (book_id,)).fetchone()
        return dict(row) if row else _raise404()


def _raise404():
    raise HTTPException(404, "not found")


@app.post("/api/books")
async def upload(file: UploadFile = File(...), user=UserDep):
    ext = Path(file.filename or "").suffix.lower().lstrip(".")
    if ext not in metadata.EXTS:
        raise HTTPException(400, f"unsupported format, allowed: {', '.join(sorted(metadata.EXTS))}")
    tmp = Path(tempfile.mkstemp(dir=TMP_DIR, suffix=f".{ext}")[1])
    try:
        with open(tmp, "wb") as f:
            shutil.copyfileobj(file.file, f)
        sha = sha256_file(tmp)
        with db.conn() as con:
            row = con.execute("SELECT * FROM books WHERE sha256=?", (sha,)).fetchone()
            if row:
                book, dup = dict(row), True
            else:
                dup = False
                meta = await metadata.build_metadata(tmp, file.filename)
                size = tmp.stat().st_size
                store_file(tmp, sha, ext)
                cur = con.execute(
                    """INSERT INTO books(sha256, ext, size, title, norm_title, authors, isbn, language,
                       categories, description, year, cover_ext, source, added_by)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (sha, ext, size, meta["title"], _norm_title(meta["title"]), meta["authors"],
                     meta["isbn"], meta["language"], meta["categories"],
                     meta["description"], meta["year"], meta["cover_ext"], "upload", user["id"]))
                book_id = cur.lastrowid
                if meta["cover"]:
                    cover_path(sha, meta["cover_ext"] or "jpg").write_bytes(meta["cover"])
                row = con.execute("SELECT * FROM books WHERE id=?", (book_id,)).fetchone()
                book = dict(row)
            con.execute("INSERT OR IGNORE INTO user_books(user_id, book_id) VALUES(?,?)",
                        (user["id"], book["id"]))
            # logical dup: same normalized title+author under a different file
            similar = []
            if not dup:
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


class ZlibDownload(BaseModel):
    id: str


@app.get("/api/zlib/limits")
async def zlib_limits(user=UserDep):
    try:
        return await zlib.limits()
    except ZlibUnavailable as e:
        raise HTTPException(503, str(e))


@app.post("/api/zlib/download")
async def zlib_download(req: ZlibDownload, user=UserDep):
    try:
        data, meta = await zlib.download(req.id)
    except ZlibUnavailable as e:
        raise HTTPException(503, str(e))
    # write to tmp then reuse the upload ingest path
    ext = Path(meta["_filename"]).suffix.lower().lstrip(".")
    if ext not in metadata.EXTS:
        ext = "pdf"
    tmp = Path(tempfile.mkstemp(dir=TMP_DIR, suffix=f".{ext}")[1])
    tmp.write_bytes(data)
    try:
        with db.conn() as con:
            sha = sha256_file(tmp)
            row = con.execute("SELECT * FROM books WHERE sha256=?", (sha,)).fetchone()
        if row:
            book, dup = dict(row), True
        else:
            dup = False
            m = await metadata.build_metadata(tmp, meta["_filename"])
            if not m["title"] or m["title"] == "Unknown title":
                m["title"] = meta.get("name") or m["title"]
            if not m["authors"]:
                m["authors"] = meta.get("authors") or ""
            if not m["categories"] and meta.get("categories"):
                m["categories"] = str(meta["categories"]).split("-")[-1].strip()
            if not m["description"] and meta.get("description"):
                m["description"] = str(meta["description"])
            if not m["cover"] and meta.get("cover"):
                img = await _fetch_bytes(meta["cover"])
                if img:
                    m["cover"], m["cover_ext"] = img, "jpg"
            store_file(tmp, sha, ext)
            with db.conn() as con:
                cur = con.execute(
                    """INSERT INTO books(sha256, ext, size, title, norm_title, authors, isbn, language,
                       categories, description, year, cover_ext, source, added_by)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (sha, ext, len(data), m["title"], _norm_title(m["title"]), m["authors"], m["isbn"], m["language"],
                     m["categories"], m["description"], m["year"], m["cover_ext"], "zlibrary",
                     user["id"]))
                if m["cover"]:
                    cover_path(sha, m["cover_ext"] or "jpg").write_bytes(m["cover"])
                row = con.execute("SELECT * FROM books WHERE id=?", (cur.lastrowid,)).fetchone()
                book = dict(row)
        with db.conn() as con:
            con.execute("INSERT OR IGNORE INTO user_books(user_id, book_id) VALUES(?,?)",
                        (user["id"], book["id"]))
        return {"book": book, "duplicate": dup}
    finally:
        tmp.unlink(missing_ok=True)


async def _fetch_bytes(url: str) -> bytes | None:
    import httpx
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as cx:
            r = await cx.get(url)
            return r.content if r.status_code == 200 else None
    except Exception:
        return None


# ---------- OPDS ----------

@app.get("/opds")
def opds_catalog(request: Request, user=UserDep):
    rows = [dict(r) for r in db.conn().execute(
        """SELECT b.* FROM books b WHERE b.id IN (
             SELECT book_id FROM user_books WHERE user_id=?
             UNION SELECT book_id FROM shares WHERE to_user=?)
           ORDER BY b.created_at DESC""", (user["id"], user["id"]))]
    return Response(opds.catalog(rows, str(request.url.path)),
                    media_type="application/atom+xml;profile=opds-catalog")


# ---------- frontend ----------

@app.get("/{asset}", include_in_schema=False)
def web_asset(asset: str):
    """JS/CSS must revalidate each load (ETag -> 304 when unchanged): browsers
    otherwise heuristic-cache them and keep running a stale frontend after a
    deploy. Must be defined before the StaticFiles mount below."""
    if asset not in {"app.js", "app.css", "reader.js"}:
        raise HTTPException(404)
    return FileResponse(WEB_DIR / asset, headers={"Cache-Control": "no-cache"})


app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")

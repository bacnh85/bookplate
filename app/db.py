"""SQLite (WAL) + FTS5 external-content index."""
import os
import secrets
import sqlite3
from pathlib import Path

DATA_DIR = Path(os.getenv("BOOKPLATE_DATA_DIR") or Path(__file__).resolve().parent.parent / "data")
DB_PATH = DATA_DIR / "ebook.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY,
  username TEXT UNIQUE NOT NULL COLLATE NOCASE,
  password_hash TEXT NOT NULL,
  role TEXT NOT NULL DEFAULT 'user',
  status TEXT NOT NULL DEFAULT 'active',
  created_at TEXT DEFAULT (datetime('now'))
);
-- app-managed settings (DB value wins over env fallback; see app/settings.py)
CREATE TABLE IF NOT EXISTS settings(
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL DEFAULT ''
);
-- books = unique content-addressed files
CREATE TABLE IF NOT EXISTS books(
  id INTEGER PRIMARY KEY,
  sha256 TEXT UNIQUE NOT NULL,
  ext TEXT NOT NULL,
  size INTEGER NOT NULL,
  title TEXT NOT NULL DEFAULT 'Unknown title',
  norm_title TEXT NOT NULL DEFAULT '',
  authors TEXT NOT NULL DEFAULT '',
  isbn TEXT NOT NULL DEFAULT '',
  language TEXT NOT NULL DEFAULT '',
  categories TEXT NOT NULL DEFAULT '',
  description TEXT NOT NULL DEFAULT '',
  year INTEGER,
  cover_ext TEXT,
  source TEXT NOT NULL DEFAULT 'upload',
  added_by INTEGER REFERENCES users(id),
  created_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS user_books(
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  book_id INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
  added_at TEXT DEFAULT (datetime('now')),
  PRIMARY KEY (user_id, book_id)
);
CREATE TABLE IF NOT EXISTS download_jobs(
  id INTEGER PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  zlib_id TEXT NOT NULL,
  title TEXT NOT NULL DEFAULT '',
  authors TEXT NOT NULL DEFAULT '',
  cover_url TEXT NOT NULL DEFAULT '',
  ext TEXT NOT NULL DEFAULT '',
  size_text TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'queued',
  error TEXT NOT NULL DEFAULT '',
  attempts INTEGER NOT NULL DEFAULT 0,
  bytes_done INTEGER,
  bytes_total INTEGER,
  next_attempt_at TEXT,
  source TEXT NOT NULL DEFAULT 'zlibrary',
  created_at TEXT DEFAULT (datetime('now')),
  updated_at TEXT DEFAULT (datetime('now')),
  UNIQUE(user_id, zlib_id)
);
CREATE TABLE IF NOT EXISTS kindle_devices(
  id INTEGER PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  label TEXT NOT NULL DEFAULT '',
  email TEXT NOT NULL,
  created_at TEXT DEFAULT (datetime('now')),
  UNIQUE(user_id, email)
);
CREATE TABLE IF NOT EXISTS shares(
  id INTEGER PRIMARY KEY,
  book_id INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
  from_user INTEGER NOT NULL REFERENCES users(id),
  to_user INTEGER NOT NULL REFERENCES users(id),
  created_at TEXT DEFAULT (datetime('now')),
  UNIQUE(book_id, to_user)
);
CREATE TABLE IF NOT EXISTS collections(
  id INTEGER PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  name TEXT NOT NULL COLLATE NOCASE,
  created_at TEXT DEFAULT (datetime('now')),
  UNIQUE(user_id, name)
);
CREATE TABLE IF NOT EXISTS collection_books(
  collection_id INTEGER NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
  book_id INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
  added_at TEXT DEFAULT (datetime('now')),
  PRIMARY KEY (collection_id, book_id)
);
CREATE VIRTUAL TABLE IF NOT EXISTS books_fts USING fts5(
  title, authors, categories, isbn, content='books', content_rowid='id'
);
CREATE TRIGGER IF NOT EXISTS books_fts_ai AFTER INSERT ON books BEGIN
  INSERT INTO books_fts(rowid, title, authors, categories, isbn)
  VALUES (new.id, new.title, new.authors, new.categories, new.isbn);
END;
CREATE TRIGGER IF NOT EXISTS books_fts_ad AFTER DELETE ON books BEGIN
  INSERT INTO books_fts(books_fts, rowid, title, authors, categories, isbn)
  VALUES('delete', old.id, old.title, old.authors, old.categories, old.isbn);
END;
CREATE TRIGGER IF NOT EXISTS books_fts_au AFTER UPDATE ON books BEGIN
  INSERT INTO books_fts(books_fts, rowid, title, authors, categories, isbn)
  VALUES('delete', old.id, old.title, old.authors, old.categories, old.isbn);
  INSERT INTO books_fts(rowid, title, authors, categories, isbn)
  VALUES (new.id, new.title, new.authors, new.categories, new.isbn);
END;
"""


class _Conn(sqlite3.Connection):
    """with-block commits/rolls back AND closes. Without close(), connections
    linger as open db+wal fd pairs — sqlite3 defers cross-thread dealloc, so
    the threadpool's refcount-frees never released them deterministically."""

    def __exit__(self, et, ev, tb):
        try:
            if et is None:
                self.commit()
            else:
                self.rollback()
        finally:
            self.close()
        return False


def conn() -> sqlite3.Connection:
    DATA_DIR.mkdir(exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=10, factory=_Conn)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    c.execute("PRAGMA journal_mode=WAL")
    return c


def init() -> None:
    with conn() as c:
        c.executescript(SCHEMA)
        # pre-existing DBs: CREATE TABLE IF NOT EXISTS won't add new columns
        cols = {r["name"] for r in c.execute("PRAGMA table_info(download_jobs)")}
        if "source" not in cols:
            c.execute("ALTER TABLE download_jobs ADD COLUMN source TEXT NOT NULL DEFAULT 'zlibrary'")
        ucols = {r["name"] for r in c.execute("PRAGMA table_info(users)")}
        if "email" in ucols and "username" not in ucols:
            c.execute("ALTER TABLE users RENAME COLUMN email TO username")
            ucols = {r["name"] for r in c.execute("PRAGMA table_info(users)")}
        if "role" not in ucols:
            c.execute("ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'user'")
        if "status" not in ucols:
            c.execute("ALTER TABLE users ADD COLUMN status TEXT NOT NULL DEFAULT 'active'")
        # at-least-one-admin invariant: an upgraded DB (everyone role='user') with
        # registration defaulting to 'approval' would deadlock — nobody could approve
        # or reach /api/admin/*. Promote the earliest ACTIVE account. (Debris
        # accounts left disabled can never be promoted.)
        if not c.execute("SELECT 1 FROM users WHERE role='admin'").fetchone():
            if c.execute("SELECT 1 FROM users WHERE status='active'").fetchone():
                c.execute("UPDATE users SET role='admin' "
                          "WHERE id=(SELECT MIN(id) FROM users WHERE status='active')")
        _bootstrap_admin(c)
        from . import settings as _settings  # lazy: settings imports db.conn
        _settings.seed_legacy_env(c)


def _bootstrap_admin(c: sqlite3.Connection) -> None:
    """First boot on an empty DB: create the admin account. Password comes from
    BOOKPLATE_ADMIN_PASS, else generated (logged once + saved to a 0600 file).
    Runs only when the users table is empty — never touches existing accounts."""
    from .auth import hash_password  # lazy: auth imports db
    if c.execute("SELECT 1 FROM users").fetchone():
        return
    user = os.getenv("BOOKPLATE_ADMIN_USER", "admin").strip().lower() or "admin"
    pw = os.getenv("BOOKPLATE_ADMIN_PASS", "")
    generated = not pw
    if generated:
        pw = secrets.token_urlsafe(12)
    try:
        c.execute("INSERT INTO users(username, password_hash, role, status) VALUES(?,?,?,?)",
                  (user, hash_password(pw), "admin", "active"))
    except sqlite3.IntegrityError:
        return  # racing boot — the other process created it
    if generated:
        f = DATA_DIR / "initial_admin_password"
        fd = os.open(f, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(pw + "\n")
        print(f"\n=== Bookplate first boot: created admin user '{user}' ===\n"
              f"    password: {pw}\n"
              f"    (also saved to {f} — delete it after first login)", flush=True)
    else:
        print(f"bookplate: bootstrap admin '{user}' created (BOOKPLATE_ADMIN_PASS)", flush=True)

"""SQLite (WAL) + FTS5 external-content index."""
import sqlite3
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DB_PATH = DATA_DIR / "ebook.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY,
  email TEXT UNIQUE NOT NULL,
  password_hash TEXT NOT NULL,
  created_at TEXT DEFAULT (datetime('now'))
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
CREATE TABLE IF NOT EXISTS shares(
  id INTEGER PRIMARY KEY,
  book_id INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
  from_user INTEGER NOT NULL REFERENCES users(id),
  to_user INTEGER NOT NULL REFERENCES users(id),
  created_at TEXT DEFAULT (datetime('now')),
  UNIQUE(book_id, to_user)
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

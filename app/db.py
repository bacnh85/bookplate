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


def conn() -> sqlite3.Connection:
    DATA_DIR.mkdir(exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=10)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    c.execute("PRAGMA journal_mode=WAL")
    return c


def init() -> None:
    with conn() as c:
        c.executescript(SCHEMA)

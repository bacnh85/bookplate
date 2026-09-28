#!/usr/bin/env python3
"""Offline tests for per-user book meta: status/rating upsert + COALESCE clear
semantics, visibility guard, auto-transitions from progress (never overriding
an explicit choice), list_books join.

Run: .venv/bin/python scripts/test_meta.py
"""
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

_TMP = pathlib.Path(tempfile.mkdtemp(prefix="bookplate-meta-test-"))
import app.db as db  # noqa: E402

db.DATA_DIR = _TMP
db.DB_PATH = _TMP / "ebook.db"
import os  # noqa: E402

os.environ["BOOKPLATE_ADMIN_USER"] = "rootadmin"
os.environ["BOOKPLATE_ADMIN_PASS"] = "bootpass1"

from fastapi import HTTPException  # noqa: E402
from pydantic import ValidationError  # noqa: E402

from app import main  # noqa: E402
from app.auth import hash_password  # noqa: E402


def mk_user(username):
    with db.conn() as con:
        cur = con.execute(
            "INSERT INTO users(username, password_hash, role, status) VALUES(?,?,?,?)",
            (username, hash_password("secret6"), "user", "active"))
        return cur.lastrowid


def mk_book(title, owner_id, sha):
    with db.conn() as con:
        cur = con.execute(
            "INSERT INTO books(sha256, ext, size, title, added_by) VALUES(?,?,?,?,?)",
            (sha, "epub", 1000, title, owner_id))
        bid = cur.lastrowid
        con.execute("INSERT INTO user_books(user_id, book_id) VALUES(?,?)", (owner_id, bid))
        return bid


class MetaTests(unittest.TestCase):
    def setUp(self):
        self.u1 = mk_user("alice")
        self.u2 = mk_user("bob")
        self.b1 = mk_book("Book One", self.u1, f"sha-{self.u1}-1")

    def tearDown(self):
        with db.conn() as con:
            for t in ("user_book_meta", "reading_sessions", "reading_progress",
                      "user_books", "books", "users"):
                con.execute(f"DELETE FROM {t}")

    def test_validation(self):
        with self.assertRaises(ValidationError):
            main.BookMetaReq(status="skimming")
        with self.assertRaises(ValidationError):
            main.BookMetaReq(rating=6)

    def test_upsert_and_coalesce(self):
        out = main.put_book_meta(self.b1, main.BookMetaReq(status="reading"), {"id": self.u1})
        self.assertEqual(out, {"status": "reading", "rating": None})
        out = main.put_book_meta(self.b1, main.BookMetaReq(rating=4), {"id": self.u1})
        self.assertEqual(out, {"status": "reading", "rating": 4})  # status kept
        out = main.put_book_meta(self.b1, main.BookMetaReq(status="read"), {"id": self.u1})
        self.assertEqual(out, {"status": "read", "rating": 4})  # rating kept
        with self.assertRaises(HTTPException) as cm:  # both null = nothing to do
            main.put_book_meta(self.b1, main.BookMetaReq(), {"id": self.u1})
        self.assertEqual(cm.exception.status_code, 400)

    def test_visibility_404(self):
        with self.assertRaises(HTTPException) as cm:
            main.put_book_meta(self.b1, main.BookMetaReq(status="want"), {"id": self.u2})
        self.assertEqual(cm.exception.status_code, 404)

    def test_per_user_isolation(self):
        with db.conn() as con:
            con.execute("INSERT INTO shares(book_id, from_user, to_user) VALUES(?,?,?)",
                        (self.b1, self.u1, self.u2))
        main.put_book_meta(self.b1, main.BookMetaReq(status="want", rating=5), {"id": self.u1})
        main.put_book_meta(self.b1, main.BookMetaReq(status="reading"), {"id": self.u2})
        books1 = main.list_books(user={"id": self.u1})
        books2 = main.list_books(user={"id": self.u2})
        self.assertEqual(books1[0]["status"], "want")
        self.assertEqual(books2[0]["status"], "reading")
        self.assertEqual(books2[0]["rating"], None)

    def test_auto_transitions(self):
        main.put_progress(self.b1, main.ProgressReq(cfi="c", pct=10), {"id": self.u1})
        row = main.get_book(self.b1, {"id": self.u1})
        self.assertEqual(row["status"], "reading")
        main.put_progress(self.b1, main.ProgressReq(cfi="c", pct=100), {"id": self.u1})
        self.assertEqual(main.get_book(self.b1, {"id": self.u1})["status"], "read")

    def test_auto_never_overrides_explicit(self):
        main.put_book_meta(self.b1, main.BookMetaReq(status="dnf"), {"id": self.u1})
        main.put_progress(self.b1, main.ProgressReq(cfi="c", pct=50), {"id": self.u1})
        self.assertEqual(main.get_book(self.b1, {"id": self.u1})["status"], "dnf")


if __name__ == "__main__":
    unittest.main(verbosity=2)

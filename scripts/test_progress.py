#!/usr/bin/env python3
"""Offline tests for reading-progress sync: upsert, clamping, per-user
visibility, get_book payload, cascade cleanup.

No TestClient and no live server (same pattern as test_collections.py): patch
db paths BEFORE importing app.main, then call route functions directly with
fake user dicts.

Run: .venv/bin/python scripts/test_progress.py
"""
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

_TMP = pathlib.Path(tempfile.mkdtemp(prefix="bookplate-progress-test-"))
import app.db as db  # noqa: E402

db.DATA_DIR = _TMP
db.DB_PATH = _TMP / "ebook.db"
import os  # noqa: E402

os.environ["BOOKPLATE_ADMIN_USER"] = "rootadmin"
os.environ["BOOKPLATE_ADMIN_PASS"] = "bootpass1"

from fastapi import HTTPException  # noqa: E402
from pydantic import ValidationError  # noqa: E402

from app import main  # noqa: E402  (runs db.init on the temp dir)
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


class ProgressTests(unittest.TestCase):
    def setUp(self):
        self.u1 = mk_user("alice")
        self.u2 = mk_user("bob")

    def tearDown(self):
        with db.conn() as con:
            con.execute("DELETE FROM shares")
            con.execute("DELETE FROM books")
            con.execute("DELETE FROM users WHERE username != 'rootadmin'")

    def test_upsert_list_and_clamp(self):
        bid = mk_book("Dune", self.u1, "sha-up" + "0" * 32)
        resp = main.put_progress(bid, main.ProgressReq(cfi="epubcfi(/6/2)", pct=57), {"id": self.u1})
        self.assertTrue(resp["updated_at"])  # clients anchor their clock to this
        main.put_progress(bid, main.ProgressReq(cfi="epubcfi(/6/4)", pct=150), {"id": self.u1})  # upsert + clamp
        rows = main.list_books(user={"id": self.u1})
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["progress_pct"], 100)
        book = main.get_book(bid, user={"id": self.u1})
        self.assertEqual(book["progress_pct"], 100)
        self.assertEqual(book["progress_cfi"], "epubcfi(/6/4)")
        self.assertTrue(book["progress_at"])  # newer-of resume needs a server timestamp

    def test_oversize_cfi_rejected(self):
        bid = mk_book("Dune 3", self.u1, "sha-big" + "0" * 32)
        with self.assertRaises(ValidationError):
            main.put_progress(bid, main.ProgressReq(cfi="x" * 600, pct=10), {"id": self.u1})

    def test_negative_clamps_to_zero(self):
        bid = mk_book("Dune 2", self.u1, "sha-neg" + "0" * 32)
        main.put_progress(bid, main.ProgressReq(cfi="", pct=-5), {"id": self.u1})
        self.assertEqual(main.list_books(user={"id": self.u1})[0]["progress_pct"], 0)

    def test_per_user_visibility(self):
        bid = mk_book("Shared", self.u2, "sha-sh" + "0" * 32)
        with db.conn() as con:  # u2 shares with u1
            con.execute("INSERT INTO shares(book_id, from_user, to_user) VALUES(?,?,?)",
                        (bid, self.u2, self.u1))
        main.put_progress(bid, main.ProgressReq(cfi="c1", pct=42), {"id": self.u1})
        by_u1 = {r["id"]: r["progress_pct"] for r in main.list_books(user={"id": self.u1})}
        by_u2 = {r["id"]: r["progress_pct"] for r in main.list_books(user={"id": self.u2})}
        self.assertEqual(by_u1[bid], 42)
        self.assertIsNone(by_u2[bid])  # reader progress never leaks across users

    def test_invisible_book_404(self):
        bid = mk_book("Secret", self.u2, "sea-404" + "0" * 32)
        with self.assertRaises(HTTPException) as ctx:
            main.put_progress(bid, main.ProgressReq(cfi="", pct=10), {"id": self.u1})
        self.assertEqual(ctx.exception.status_code, 404)

    def test_delete_book_cascades(self):
        bid = mk_book("Doomed", self.u1, "sha-del" + "0" * 32)
        main.put_progress(bid, main.ProgressReq(cfi="c", pct=10), {"id": self.u1})
        with db.conn() as con:  # main.delete_book also touches files — cascade directly
            con.execute("DELETE FROM books WHERE id=?", (bid,))
        left = db.conn().execute(
            "SELECT COUNT(*) c FROM reading_progress WHERE book_id=?", (bid,)).fetchone()["c"]
        self.assertEqual(left, 0)


if __name__ == "__main__":
    unittest.main()

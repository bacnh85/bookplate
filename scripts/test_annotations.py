#!/usr/bin/env python3
"""Offline tests for annotations: CRUD, visibility (404 for non-shared),
per-user isolation, ownership (PATCH/DELETE only by owner), field caps/patterns,
cascade on book delete, export Markdown shape.

Run: .venv/bin/python scripts/test_annotations.py
"""
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

_TMP = pathlib.Path(tempfile.mkdtemp(prefix="bookplate-annotations-test-"))
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


class AnnotationTests(unittest.TestCase):
    def setUp(self):
        self.u1 = mk_user("alice")
        self.u2 = mk_user("bob")
        self.b1 = mk_book("Book One", self.u1, f"sha-{self.u1}-1")

    def tearDown(self):
        with db.conn() as con:
            for t in ("annotations", "shares", "user_books", "books", "users"):
                con.execute(f"DELETE FROM {t}")

    def test_create_and_list(self):
        a = main.create_annotation(self.b1, main.AnnotationReq(cfi="epubcfi(/6/4)", text="a line", note="hmm"),
                                   {"id": self.u1})
        self.assertEqual(a["color"], "yellow")
        self.assertEqual(a["kind"], "highlight")
        rows = main.list_annotations(self.b1, {"id": self.u1})
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["text"], "a line")

    def test_visibility_404(self):  # bob can't see alice's book
        with self.assertRaises(HTTPException) as cm:
            main.list_annotations(self.b1, {"id": self.u2})
        self.assertEqual(cm.exception.status_code, 404)

    def test_shared_book_visible_but_own_list(self):
        with db.conn() as con:
            con.execute("INSERT INTO shares(book_id, from_user, to_user) VALUES(?,?,?)",
                        (self.b1, self.u1, self.u2))
        a = main.create_annotation(self.b1, main.AnnotationReq(cfi="epubcfi(/6/4)"), {"id": self.u2})
        self.assertEqual([r["id"] for r in main.list_annotations(self.b1, {"id": self.u2})], [a["id"]])
        self.assertEqual([r["id"] for r in main.list_annotations(self.b1, {"id": self.u1})], [])  # isolated

    def test_patch_and_delete_owner_only(self):
        a = main.create_annotation(self.b1, main.AnnotationReq(cfi="c1"), {"id": self.u1})
        out = main.update_annotation(a["id"], main.AnnotationPatch(note="edited", color="blue"), {"id": self.u1})
        self.assertEqual(out["note"], "edited")
        self.assertEqual(out["color"], "blue")
        with self.assertRaises(HTTPException) as cm:  # bob doesn't own it
            main.update_annotation(a["id"], main.AnnotationPatch(note="x"), {"id": self.u2})
        self.assertEqual(cm.exception.status_code, 404)
        self.assertEqual(main.delete_annotation(a["id"], {"id": self.u1}), {"ok": True})
        self.assertEqual(main.list_annotations(self.b1, {"id": self.u1}), [])

    def test_caps_and_patterns(self):
        with self.assertRaises(ValidationError):
            main.AnnotationReq(cfi="x" * 513)
        with self.assertRaises(ValidationError):
            main.AnnotationReq(cfi="", text="y" * 2001)
        with self.assertRaises(ValidationError):
            main.AnnotationReq(cfi="c", color="purple")
        with self.assertRaises(ValidationError):
            main.AnnotationReq(cfi="c", kind="margin-note")

    def test_cascade_on_book_delete(self):
        a = main.create_annotation(self.b1, main.AnnotationReq(cfi="c"), {"id": self.u1})
        with db.conn() as con:
            con.execute("DELETE FROM user_books WHERE book_id=?", (self.b1,))
            con.execute("DELETE FROM books WHERE id=?", (self.b1,))
        with db.conn() as con:
            self.assertIsNone(con.execute("SELECT 1 FROM annotations WHERE id=?", (a["id"],)).fetchone())

    def test_export_markdown(self):
        main.create_annotation(self.b1, main.AnnotationReq(cfi="c1", text="quoted line", note="my note"), {"id": self.u1})
        main.create_annotation(self.b1, main.AnnotationReq(cfi="c2", kind="bookmark"), {"id": self.u1})
        main.create_annotation(self.b1, main.AnnotationReq(cfi="c3", text="under", color="under", kind="underline"), {"id": self.u1})
        resp = main.export_annotations(book_id=self.b1, user={"id": self.u1})
        body = resp.body.decode()
        self.assertIn("# My notes", body)
        self.assertIn("## Book One", body)
        self.assertIn("> quoted line", body)
        self.assertIn("my note", body)
        self.assertIn("- Bookmark at c2", body)
        self.assertIn("> under", body)
        self.assertTrue(resp.headers["content-disposition"].startswith("attachment"))

    def test_export_whole_shelf_groups_by_book(self):
        b2 = mk_book("Book Two", self.u1, f"sha-{self.u1}-2")
        main.create_annotation(self.b1, main.AnnotationReq(cfi="c", text="one"), {"id": self.u1})
        main.create_annotation(b2, main.AnnotationReq(cfi="c", text="two"), {"id": self.u1})
        body = main.export_annotations(book_id=None, user={"id": self.u1}).body.decode()
        self.assertLess(body.index("## Book One"), body.index("## Book Two"))


if __name__ == "__main__":
    unittest.main(verbosity=2)

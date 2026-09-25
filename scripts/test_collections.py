#!/usr/bin/env python3
"""Offline tests for collections: CRUD, visibility rules, membership.

No TestClient and no live server (same pattern as test_admin.py): patch db
paths BEFORE importing app.main, then call route functions directly with fake
user dicts.

Run: .venv/bin/python scripts/test_collections.py
"""
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

_TMP = pathlib.Path(tempfile.mkdtemp(prefix="bookplate-collections-test-"))
import app.db as db  # noqa: E402

db.DATA_DIR = _TMP
db.DB_PATH = _TMP / "ebook.db"
import os  # noqa: E402

os.environ["BOOKPLATE_ADMIN_USER"] = "rootadmin"
os.environ["BOOKPLATE_ADMIN_PASS"] = "bootpass1"

from fastapi import HTTPException  # noqa: E402

from app import main, settings  # noqa: E402  (runs db.init on the temp dir)
from app.auth import hash_password  # noqa: E402


def mk_user(username, role="user"):
    with db.conn() as con:
        cur = con.execute(
            "INSERT INTO users(username, password_hash, role, status) VALUES(?,?,?,?)",
            (username, hash_password("secret6"), role, "active"))
        return cur.lastrowid


def mk_book(title, owner_id, sha):
    with db.conn() as con:
        cur = con.execute(
            "INSERT INTO books(sha256, ext, size, title, added_by) VALUES(?,?,?,?,?)",
            (sha, "epub", 1000, title, owner_id))
        bid = cur.lastrowid
        con.execute("INSERT INTO user_books(user_id, book_id) VALUES(?,?)", (owner_id, bid))
        return bid


def admin():
    with db.conn() as con:
        return dict(con.execute("SELECT * FROM users WHERE username='rootadmin'").fetchone())


class CollectionTests(unittest.TestCase):
    def setUp(self):
        self.u1 = mk_user("alice")
        self.u2 = mk_user("bob")

    def tearDown(self):
        with db.conn() as con:
            con.execute("DELETE FROM collections")
            con.execute("DELETE FROM books")
            con.execute("DELETE FROM users WHERE username != 'rootadmin'")
            con.execute("DELETE FROM settings")

    def test_crud_and_duplicate_name(self):
        u = {"id": self.u1}
        c = main.create_collection(main.CollectionReq(name="Sci-fi"), u)
        self.assertEqual(c["book_count"], 0)
        rows = main.list_collections(u)
        self.assertEqual([r["name"] for r in rows], ["Sci-fi"])
        with self.assertRaises(HTTPException) as ctx:
            main.create_collection(main.CollectionReq(name=" sci-fi "), u)  # unique per user
        self.assertEqual(ctx.exception.status_code, 409)
        main.rename_collection(c["id"], main.CollectionReq(name="Space"), u)
        self.assertEqual(main.list_collections(u)[0]["name"], "Space")
        main.delete_collection(c["id"], u)
        self.assertEqual(main.list_collections(u), [])
        with self.assertRaises(HTTPException):
            main.delete_collection(c["id"], u)

    def test_names_are_per_user(self):
        a, b = {"id": self.u1}, {"id": self.u2}
        main.create_collection(main.CollectionReq(name="Same"), a)
        main.create_collection(main.CollectionReq(name="Same"), b)  # no clash across users
        self.assertEqual(len(main.list_collections(a)), 1)
        self.assertEqual(len(main.list_collections(b)), 1)

    def test_empty_name_rejected(self):
        with self.assertRaises(HTTPException) as ctx:
            main.create_collection(main.CollectionReq(name="   "), {"id": self.u1})
        self.assertEqual(ctx.exception.status_code, 400)

    def test_membership_and_visibility(self):
        u = {"id": self.u1}
        other = {"id": self.u2}
        own = mk_book("Own book", self.u1, "a" * 64)
        foreign = mk_book("Foreign book", self.u2, "b" * 64)  # not visible to u1
        c = main.create_collection(main.CollectionReq(name="Reading"), u)
        main.collection_add_book(c["id"], main.BookIdReq(book_id=own), u)
        with self.assertRaises(HTTPException):  # invisible book can't be added
            main.collection_add_book(c["id"], main.BookIdReq(book_id=foreign), u)
        got = main.get_collection(c["id"], u)
        self.assertEqual([b["id"] for b in got["books"]], [own])
        self.assertEqual(got["books"][0]["own"], 1)
        self.assertEqual(main.list_collections(u)[0]["book_count"], 1)
        # another user can't see or mutate the collection
        with self.assertRaises(HTTPException):
            main.get_collection(c["id"], other)
        with self.assertRaises(HTTPException):
            main.collection_add_book(c["id"], main.BookIdReq(book_id=foreign), other)
        with self.assertRaises(HTTPException):
            main.delete_collection(c["id"], other)
        # remove membership
        main.collection_remove_book(c["id"], own, u)
        with self.assertRaises(HTTPException):
            main.collection_remove_book(c["id"], own, u)  # already gone
        self.assertEqual(main.get_collection(c["id"], u)["books"], [])

    def test_shared_book_is_addable(self):
        u = {"id": self.u1}
        giver = {"id": self.u2}
        bid = mk_book("Gift", self.u2, "c" * 64)
        main.share_book(bid, main.ShareReq(username="alice"), giver)
        c = main.create_collection(main.CollectionReq(name="From Bob"), u)
        main.collection_add_book(c["id"], main.BookIdReq(book_id=bid), u)  # visible via share
        got = main.get_collection(c["id"], u)
        self.assertEqual([b["id"] for b in got["books"]], [bid])
        self.assertEqual(got["books"][0]["own"], 0)  # visible via share, not owned

    def test_delete_collection_cascades_membership(self):
        u = {"id": self.u1}
        bid = mk_book("B", self.u1, "d" * 64)
        c = main.create_collection(main.CollectionReq(name="X"), u)
        main.collection_add_book(c["id"], main.BookIdReq(book_id=bid), u)
        main.delete_collection(c["id"], u)
        with db.conn() as con:
            left = con.execute("SELECT COUNT(*) n FROM collection_books").fetchone()["n"]
        self.assertEqual(left, 0)

    def test_me_sources_flags(self):
        u = admin()
        src = main.me(u)["sources"]
        self.assertEqual(src["zlib"], False)
        self.assertEqual(src["annas"], False)
        # pool: a configured enabled account (seeded from the legacy settings keys)
        settings.set("zlib.email", "a@b.c")
        settings.set("zlib.password", "pw")
        with db.conn() as con:
            main.db._seed_zlib_accounts(con)
        self.assertEqual(main.me(u)["sources"]["zlib"], True)
        with db.conn() as con:  # disabled account = not configured
            con.execute("UPDATE zlib_accounts SET enabled=0")
        self.assertEqual(main.me(u)["sources"]["zlib"], False)
        settings.set("annas.secret_key", "k")
        self.assertEqual(main.me(u)["sources"]["annas"], True)

    def test_remove_book_drops_memberships(self):
        # two owners: removing the book from one shelf must not leave stale
        # collection rows behind (the book row survives via the other owner,
        # so the FK cascade never fires for the remover's memberships)
        a, b = {"id": self.u1}, {"id": self.u2}
        bid = mk_book("Two owners", self.u1, "e" * 64)
        with db.conn() as con:
            con.execute("INSERT INTO user_books(user_id, book_id) VALUES(?,?)",
                        (self.u2, bid))
        c = main.create_collection(main.CollectionReq(name="Mine"), b)
        main.collection_add_book(c["id"], main.BookIdReq(book_id=bid), b)
        main.remove_book(bid, b)  # B removes; book survives (A still owns it)
        with db.conn() as con:
            self.assertIsNotNone(con.execute("SELECT 1 FROM books WHERE id=?", (bid,)).fetchone())
            orphans = con.execute("SELECT COUNT(*) n FROM collection_books").fetchone()["n"]
        self.assertEqual(orphans, 0)
        self.assertEqual(main.list_collections(b)[0]["book_count"], 0)
        self.assertEqual(main.list_collections(b, book_id=bid)[0]["member"], 0)
        self.assertIsNotNone(main.get_book(bid, a))  # A keeps the book


if __name__ == "__main__":
    unittest.main()

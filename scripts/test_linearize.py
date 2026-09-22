#!/usr/bin/env python3
"""Offline tests for the linearized-PDF derivative cache.

Covers the two review findings:
1. identity: original bytes are never mutated; re-ingesting the same original
   still dedupes to ONE row (books.sha256 stays the identity key).
2. responsiveness/derivative: ?reader=1 builds the derivative off the event
   loop, serves it on this and later requests, and the plain URL keeps serving
   the original. Derivative is removed when the book is deleted.

Run: .venv/bin/python scripts/test_linearize.py
"""
import asyncio
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

_TMP = pathlib.Path(tempfile.mkdtemp(prefix="bookplate-linearize-test-"))
import app.db as db  # noqa: E402

db.DATA_DIR = _TMP
db.DB_PATH = _TMP / "ebook.db"
import app.storage as storage  # noqa: E402

storage.BOOKS_DIR = _TMP / "books"
storage.COVERS_DIR = _TMP / "covers"
storage.LINEARIZED_DIR = _TMP / "linearized"
import os  # noqa: E402

os.environ["BOOKPLATE_ADMIN_USER"] = "rootadmin"
os.environ["BOOKPLATE_ADMIN_PASS"] = "bootpass1"

import starlette.responses  # noqa: E402
from fastapi import HTTPException  # noqa: E402

from app import main  # noqa: E402  (runs db.init on the temp dir)
from app.auth import hash_password  # noqa: E402
from app.storage import ensure_linearized, linearized_path  # noqa: E402


async def _offline_metadata(path, name, **_):
    """Stub the enrichment chain — these tests must never touch the network."""
    return {"title": "Test", "authors": "", "isbn": "", "language": "",
            "categories": "", "description": "", "year": None,
            "cover": None, "cover_ext": None}


main.metadata.build_metadata = _offline_metadata


def make_pdf(path):
    """Deterministic tiny multi-page PDF via pymupdf (already a dependency)."""
    import fitz

    doc = fitz.open()
    for i in range(6):
        page = doc.new_page()
        page.insert_text((72, 72 + 20 * i), f"page {i} of the test document")
    doc.save(str(path))
    doc.close()
    return path


def mk_user():
    with db.conn() as con:
        cur = con.execute(
            "INSERT INTO users(username, password_hash, role, status) VALUES(?,?,?,?)",
            ("reader", hash_password("secret6"), "user", "active"))
        return cur.lastrowid


class LinearizeTests(unittest.TestCase):
    def setUp(self):
        self.user = mk_user()
        self.pdf = make_pdf(pathlib.Path(_TMP / "src.pdf"))
        self.original_bytes = self.pdf.read_bytes()
        self.sha = None

    def _ingest(self):
        import shutil

        tmp = pathlib.Path(_TMP / "ingest.pdf")
        shutil.copy(self.pdf, tmp)
        book, dup = asyncio.run(main._ingest(tmp, "test.pdf", "upload", self.user))
        self.sha = book["sha256"]
        return book, dup

    def test_ingest_identity_untouched_and_dedupes(self):
        book1, dup1 = self._ingest()
        self.assertFalse(dup1)
        # original in the store is byte-identical to what was uploaded
        stored = pathlib.Path(storage.book_path(self.sha, "pdf"))
        self.assertEqual(stored.read_bytes(), self.original_bytes)
        # re-ingesting the SAME bytes dedupes — linearize must never break this
        book2, dup2 = self._ingest()
        self.assertTrue(dup2)
        self.assertEqual(book1["id"], book2["id"])
        with db.conn() as con:
            n = con.execute("SELECT COUNT(*) c FROM books WHERE sha256=?",
                            (self.sha,)).fetchone()["c"]
        self.assertEqual(n, 1)

    def test_reader_flag_builds_and_serves_derivative(self):
        self._ingest()
        self.assertFalse(linearized_path(self.sha).exists())
        resp = asyncio.run(main.book_file(book_id=self.book_id, reader=1, user={"id": self.user}))
        self.assertIsNotNone(resp)
        self.assertTrue(linearized_path(self.sha).exists())
        import pikepdf

        self.assertTrue(pikepdf.Pdf.open(str(linearized_path(self.sha))).is_linearized)
        # second call: served from cache (no rebuild — mtime unchanged)
        mtime = linearized_path(self.sha).stat().st_mtime
        asyncio.run(main.book_file(book_id=self.book_id, reader=1, user={"id": self.user}))
        self.assertEqual(linearized_path(self.sha).stat().st_mtime, mtime)

    def test_plain_url_serves_original_bytes(self):
        self._ingest()
        resp = asyncio.run(main.book_file(book_id=self.book_id, user={"id": self.user}))
        import starlette.responses

        assert isinstance(resp, starlette.responses.FileResponse)
        self.assertEqual(pathlib.Path(resp.path).read_bytes(), self.original_bytes)

    def test_reader_flag_falls_back_when_linearize_fails(self):
        book, _ = self._ingest()
        # corrupt the stored original AFTER ingest: derivative build must fail
        # cleanly and the route must still return a FileResponse (the original)
        stored = pathlib.Path(storage.book_path(self.sha, "pdf"))
        stored.write_bytes(b"not a pdf at all")
        resp = asyncio.run(main.book_file(book_id=self.book_id, reader=1, user={"id": self.user}))
        import starlette.responses

        assert isinstance(resp, starlette.responses.FileResponse)
        self.assertFalse(linearized_path(self.sha).exists())

    def test_reader_flag_negative_cache(self):
        """A failed (corrupt) build is negative-cached: repeated reader=1
        requests must not re-run the CPU-bound pikepdf open+save every time."""
        self._ingest()
        stored = pathlib.Path(storage.book_path(self.sha, "pdf"))
        stored.write_bytes(b"not a pdf at all")
        calls = {"n": 0}
        real = storage.ensure_linearized

        def counting(original, sha):
            calls["n"] += 1
            return real(original, sha)

        main.ensure_linearized = counting
        try:
            for _ in range(3):
                resp = asyncio.run(main.book_file(
                    book_id=self.book_id, reader=1, user={"id": self.user}))
                assert isinstance(resp, starlette.responses.FileResponse)
            # first request builds (fails); the next two hit the negative cache
            self.assertEqual(calls["n"], 1, "negative cache not consulted")
            # TTL expiry retries the build (worker would fix the file underneath)
            main._LIN_FAILED[self.sha] -= main._LIN_FAIL_TTL + 1
            asyncio.run(main.book_file(book_id=self.book_id, reader=1, user={"id": self.user}))
            self.assertEqual(calls["n"], 2, "TTL did not re-arm the build")
        finally:
            main.ensure_linearized = real
            main._LIN_FAILED.pop(self.sha, None)

    def test_delete_removes_derivative(self):
        self._ingest()
        ensure_linearized(storage.book_path(self.sha, "pdf"), self.sha)
        self.assertTrue(linearized_path(self.sha).exists())
        # exercise the REAL route: last owner deletes -> row + original + derivative gone
        main.remove_book(book_id=self.book_id, user={"id": self.user})
        self.assertFalse(linearized_path(self.sha).exists())
        self.assertFalse(storage.book_path(self.sha, "pdf").exists())
        with db.conn() as con:
            self.assertIsNone(con.execute("SELECT id FROM books WHERE sha256=?",
                                          (self.sha,)).fetchone())

    @property
    def book_id(self):
        with db.conn() as con:
            return con.execute("SELECT id FROM books WHERE sha256=?", (self.sha,)).fetchone()["id"]

    def tearDown(self):
        with db.conn() as con:
            con.execute("DELETE FROM user_books")
            con.execute("DELETE FROM books")
            con.execute("DELETE FROM users")


if __name__ == "__main__":
    unittest.main()

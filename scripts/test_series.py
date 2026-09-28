#!/usr/bin/env python3
"""Offline tests for series support: EPUB extraction (calibre:series OPF meta +
EPUB3 belongs-to-collection), CBZ ComicInfo Series/Number, /api/series grouping
order, BookPatch series fields.

Run: .venv/bin/python scripts/test_series.py
"""
import pathlib
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

_TMP = pathlib.Path(tempfile.mkdtemp(prefix="bookplate-series-test-"))
import app.db as db  # noqa: E402

db.DATA_DIR = _TMP
db.DB_PATH = _TMP / "ebook.db"
import os  # noqa: E402

os.environ["BOOKPLATE_ADMIN_USER"] = "rootadmin"
os.environ["BOOKPLATE_ADMIN_PASS"] = "bootpass1"

from app import main, metadata  # noqa: E402
from app.auth import hash_password  # noqa: E402


def mk_user(username):
    with db.conn() as con:
        cur = con.execute(
            "INSERT INTO users(username, password_hash, role, status) VALUES(?,?,?,?)",
            (username, hash_password("secret6"), "user", "active"))
        return cur.lastrowid


def mk_book(title, owner_id, sha, series=None, series_index=None):
    with db.conn() as con:
        cur = con.execute(
            "INSERT INTO books(sha256, ext, size, title, added_by, series, series_index) "
            "VALUES(?,?,?,?,?,?,?)",
            (sha, "epub", 1000, title, owner_id, series, series_index))
        bid = cur.lastrowid
        con.execute("INSERT INTO user_books(user_id, book_id) VALUES(?,?)", (owner_id, bid))
        return bid


def make_epub(tmp: pathlib.Path, opf_meta: bool, extra_opf: str = "") -> pathlib.Path:
    """Minimal EPUB the ebooklib parser accepts, with calibre series meta or
    an EPUB3 belongs-to-collection block injected into the OPF."""
    import ebooklib
    from ebooklib import epub
    book = epub.EpubBook()
    book.set_identifier("t1")
    book.set_title("T")
    book.set_language("en")
    book.add_item(epub.EpubHtml(title="c", file_name="c.xhtml", content="<p>hi</p>"))
    book.add_item(epub.EpubNav())
    book.add_item(epub.EpubNcx())
    book.spine = ["nav"]
    path = tmp / f"t{opf_meta}{len(extra_opf)}.epub"
    epub.write_epub(str(path), book)
    if extra_opf:
        # splice extra OPF entries into the package (ebooklib has no API for these)
        import shutil
        raw = path.read_bytes()
        tmp2 = tmp / (path.name + ".zip")
        tmp2.write_bytes(raw)
        with zipfile.ZipFile(tmp2) as z:
            names = z.namelist()
            data = {n: z.read(n) for n in names}
        opf_name = next(n for n in names if n.endswith(".opf"))
        opf = data[opf_name].decode()
        opf = opf.replace("</metadata>", extra_opf + "</metadata>")
        data[opf_name] = opf.encode()
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
            for n, b in data.items():
                z.writestr(n, b)
        tmp2.unlink()
    return path


class SeriesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = pathlib.Path(tempfile.mkdtemp(prefix="bookplate-series-fx-"))

    def test_epub_calibre_meta(self):
        p = make_epub(self.tmp, True,
                      '<meta name="calibre:series" content="Rust Wars"/>'
                      '<meta name="calibre:series_index" content="3"/>')
        meta = metadata.blank()
        metadata.from_epub(p, meta)
        self.assertEqual(meta["series"], "Rust Wars")
        self.assertEqual(meta["series_index"], 3.0)

    def test_epub_belongs_to_collection(self):
        p = make_epub(self.tmp, False,
                      '<meta property="belongs-to-collection" id="s1">Collection X</meta>'
                      '<meta property="group-position" refines="#s1">2</meta>')
        meta = metadata.blank()
        metadata.from_epub(p, meta)
        self.assertEqual(meta["series"], "Collection X")
        self.assertEqual(meta["series_index"], 2.0)

    def test_cbz_comicinfo(self):
        p = self.tmp / "c.cbz"
        with zipfile.ZipFile(p, "w") as z:
            z.writestr("ComicInfo.xml",
                       "<ComicInfo><Series>Space Adventures</Series><Number>7</Number></ComicInfo>")
            z.writestr("p1.jpg", b"x")
        meta = metadata.blank()
        metadata.from_cbz(p, meta)
        self.assertEqual(meta["series"], "Space Adventures")
        self.assertEqual(meta["series_index"], 7.0)

    def test_series_grouping_order(self):
        u = mk_user("carol")
        b2 = mk_book("B2", u, "s-2", "Alpha Saga", 2)
        b1 = mk_book("B1", u, "s-1", "Alpha Saga", 1)
        b10 = mk_book("B10", u, "s-10", "Alpha Saga", 10)
        mk_book("Other", u, "s-x", "Beta", 1)
        mk_book("NoSeries", u, "s-n", None, None)
        groups = main.list_series({"id": u})
        self.assertEqual([g["series"] for g in groups], ["Alpha Saga", "Beta"])
        alpha = groups[0]
        self.assertEqual(alpha["count"], 3)
        self.assertEqual([b["series_index"] for b in alpha["books"]], [1, 2, 10])

    def test_series_visibility(self):
        u1, u2 = mk_user("d1"), mk_user("d2")
        mk_book("Mine", u1, "v-1", "Secret Saga", 1)
        self.assertEqual(main.list_series({"id": u2}), [])

    def test_patch_series(self):
        u = mk_user("eve")
        b = mk_book("Patched", u, "p-1")
        out = main.update_book(b, main.BookPatch(series="New Saga", series_index=1.5),
                               {"id": u, "role": "user"})
        self.assertEqual(out["series"], "New Saga")
        self.assertEqual(out["series_index"], 1.5)


if __name__ == "__main__":
    unittest.main(verbosity=2)

#!/usr/bin/env python3
"""Unit tests for non-EPUB/PDF embedded metadata: MOBI/AZW3 EXTH, FB2 XML, CBZ.

These are pure parsers — no server, no network. The end-to-end path (upload →
stored cover → OPDS MIME → reader file) is covered by scripts/selftest.py.

Usage: .venv/bin/python scripts/test_formats.py
"""
import base64
import io
import pathlib
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app import metadata  # noqa: E402

# smallest valid 1x1 JPEG: passes the cover magic-byte check
JPEG_1PX = base64.b64decode(
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAP//////////////////////////////"
    "////////////////////////////////////////////////////wAALCAABAAEB"
    "AREA/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIE"
    "BAQDBQcDBQAAARECAwQFEQYhEjFBE1FhBxQicYGRMqGx8AjB0eEjQlLxFTOColJi"
    "/9oADAMBAAIQAxAAAAH/xAAfEAACAgIDAQAAAAAAAAAAAAABAgADBBEFEiEx/9oA"
    "CAEBAAEFAq7FrOKjTWVsM1WvJ0xRSyZc49j9OFakX//EABQRAQAAAAAAAAAAAAAA"
    "AAAAAP/aAAgBAwEBPxEf/8QAFBEBAAAAAAAAAAAAAAAAAAAAAP/aAAgBAgEBPxEf"
    "/8QAHhABAAICAwADAAAAAAAAAAAAAQARITFBUWFx/9oACAEBAAY/AnCoAAAA"
    "AA==")


def _exth_rec(rtype: int, data: bytes) -> bytes:
    return rtype.to_bytes(4, "big") + (8 + len(data)).to_bytes(4, "big") + data


def make_mobi(title: str, author: str, *, isbn: str = "", subject: str = "",
              boundary: int | None = None, cover: bytes | None = JPEG_1PX,
              version: int = 6) -> bytes:
    """Record 0 carries the EXTH; record 1 the raw image. `boundary` marks the
    KF8 half of a combo file (EXTH 121) — its headers hold the real title."""
    def header(title_: str) -> bytes:
        hdr = bytearray(248)
        hdr[0:2] = (1).to_bytes(2, "big")        # compression 1 = stored
        hdr[16:20] = b"MOBI"
        hdr[20:24] = (232).to_bytes(4, "big")    # MOBI header length
        hdr[28:32] = (65001).to_bytes(4, "big")  # utf-8
        hdr[36:40] = version.to_bytes(4, "big")
        hdr[108:112] = (1).to_bytes(4, "big")    # resourceStart
        hdr[128:132] = (0x40).to_bytes(4, "big")  # EXTH present
        items = [_exth_rec(503, title_.encode()), _exth_rec(100, author.encode())]
        if isbn:
            items.append(_exth_rec(104, isbn.encode()))
        if subject:
            items.append(_exth_rec(105, subject.encode()))
        items += [_exth_rec(106, b"2011-01-01"), _exth_rec(201, b"\x00\x00\x00\x00")]
        if boundary is not None:
            items.append(_exth_rec(121, boundary.to_bytes(4, "big")))
        body = len(items).to_bytes(4, "big") + b"".join(items)
        return bytes(hdr) + b"EXTH" + (12 + len(body)).to_bytes(4, "big") + body

    n = 12
    records = [b"" for _ in range(n)]
    records[0] = header(title if boundary is None else f"{title} (MOBI6)")
    records[1] = cover or b""
    if boundary is not None:
        records[boundary] = header(title)
    head = bytearray(78)
    head[0:8] = b"TestBook"
    head[60:64] = b"BOOK"
    head[64:68] = b"MOBI"
    head[76:78] = n.to_bytes(2, "big")
    pos, offsets = 78 + n * 8, []
    for r in records:
        offsets.append(pos)
        pos += len(r)
    index = b"".join(o.to_bytes(4, "big") + b"\x00\x00\x00\x00" for o in offsets)
    return bytes(head) + index + b"".join(records)


def make_fb2(title: str, first: str, last: str = "Tester", *,
             namespaced: bool = True, isbn: str = "978-3-16-148410-0",
             cover: bytes | None = JPEG_1PX) -> bytes:
    ns = ' xmlns="http://www.gribuser.ru/xml/fictionbook/2.0"' if namespaced else ""
    binary = (f'<binary id="cover.jpg" content-type="image/jpeg">'
              f'{base64.b64encode(cover).decode()}</binary>') if cover else ""
    coverpage = '<coverpage><image l:href="#cover.jpg"/></coverpage>' if cover else ""
    return f"""<?xml version="1.0" encoding="utf-8"?>
<FictionBook{ns} xmlns:l="http://www.w3.org/1999/xlink">
  <description>
    <title-info>
      <genre>sf</genre><genre>detective</genre>
      <author><first-name>{first}</first-name><last-name>{last}</last-name></author>
      <book-title>{title}</book-title>
      <date value="2009-05-05">2009</date><lang>en</lang>{coverpage}
    </title-info>
    <publish-info><isbn>{isbn}</isbn></publish-info>
  </description>
  <body><section><p>Body.</p></section></body>
  {binary}
</FictionBook>""".encode()


def make_cbz(*, comicinfo: str | None, pages: list[str] | None = None) -> bytes:
    pages = pages or ["page002.jpg", "page001.jpg"]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        if comicinfo is not None:
            z.writestr("ComicInfo.xml", comicinfo)
        for p in pages:
            z.writestr(p, JPEG_1PX)
    return buf.getvalue()


class TempFileMixin:
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self._dir.name)

    def tearDown(self):
        self._dir.cleanup()

    def parse(self, name: str, data: bytes, fn) -> dict:
        p = self.dir / name
        p.write_bytes(data)
        meta = metadata.blank()
        fn(p, meta)
        return meta


class MobiTests(TempFileMixin, unittest.TestCase):
    def test_exth_fields(self):
        meta = self.parse("a.mobi", make_mobi("Mobi Title", "Mobi Author",
                                             isbn="978-3-16-148410-0", subject="Fiction"),
                          metadata.from_mobi)
        self.assertEqual(meta["title"], "Mobi Title")
        self.assertEqual(meta["authors"], "Mobi Author")
        self.assertEqual(meta["isbn"], "9783161484100")  # hyphens stripped
        self.assertEqual(meta["categories"], "Fiction")
        self.assertEqual(meta["year"], 2011)
        self.assertEqual(meta["cover_ext"], "jpg")
        self.assertTrue(meta["cover"].startswith(b"\xff\xd8\xff"))

    def test_combo_reads_kf8_half(self):
        """AZW3 is typically a MOBI6/KF8 combo: EXTH 121 names the KF8 record and
        its headers carry the authoritative title."""
        meta = self.parse("b.azw3", make_mobi("Real Title", "A", boundary=2),
                          metadata.from_mobi)
        self.assertEqual(meta["title"], "Real Title")

    def test_plain_kf8_version_8(self):
        meta = self.parse("c.azw3", make_mobi("KF8 Title", "A", version=8),
                          metadata.from_mobi)
        self.assertEqual(meta["title"], "KF8 Title")

    def test_cover_offset_zero_is_real(self):
        """EXTH 201 == 0 means the first resource record — not 'unset'."""
        meta = self.parse("d.mobi", make_mobi("T", "A"), metadata.from_mobi)
        self.assertEqual(meta["cover_ext"], "jpg")

    def test_non_image_cover_record_is_rejected(self):
        meta = self.parse("e.mobi", make_mobi("T", "A", cover=b"NOT AN IMAGE"),
                          metadata.from_mobi)
        self.assertIsNone(meta["cover_ext"])
        self.assertEqual(meta["title"], "T")  # metadata still parsed

    def test_garbage_file_raises_for_fallback(self):
        """build_metadata swallows this and falls back to filename/enrichment."""
        with self.assertRaises(Exception):
            self.parse("f.mobi", b"not a mobi at all" * 10, metadata.from_mobi)

    def test_shorter_than_pdb_header_is_skipped(self):
        """Too small to hold a PDB header: no metadata, and no raise (the
        filename/enrichment fallback takes over)."""
        meta = self.parse("g.mobi", b"BOOKMOBI", metadata.from_mobi)
        self.assertEqual(meta["title"], "")

    def test_garbage_offsets_raise_for_fallback(self):
        """Long enough to look like a PDB, but the index points nowhere."""
        junk = bytearray(120)
        junk[76:78] = (5).to_bytes(2, "big")  # claims 5 records, none present
        with self.assertRaises(Exception):
            self.parse("h.mobi", bytes(junk), metadata.from_mobi)


class Fb2Tests(TempFileMixin, unittest.TestCase):
    def test_namespaced_document(self):
        meta = self.parse("a.fb2", make_fb2("FB2 Title", "Fb"), metadata.from_fb2)
        self.assertEqual(meta["title"], "FB2 Title")
        self.assertEqual(meta["authors"], "Fb Tester")
        self.assertEqual(meta["language"], "en")
        self.assertEqual(meta["year"], 2009)
        self.assertEqual(meta["isbn"], "9783161484100")
        self.assertEqual(meta["categories"], "sf, detective")
        self.assertEqual(meta["cover_ext"], "jpg")

    def test_un_namespaced_document(self):
        meta = self.parse("b.fb2", make_fb2("Plain", "A", namespaced=False),
                          metadata.from_fb2)
        self.assertEqual(meta["title"], "Plain")
        self.assertEqual(meta["authors"], "A Tester")

    def test_no_cover_binary(self):
        meta = self.parse("c.fb2", make_fb2("No Art", "A", cover=None), metadata.from_fb2)
        self.assertEqual(meta["title"], "No Art")
        self.assertIsNone(meta["cover"])

    def test_broken_xml_raises_for_fallback(self):
        with self.assertRaises(Exception):
            self.parse("d.fb2", b"<FictionBook><unclosed>", metadata.from_fb2)


class CbzTests(TempFileMixin, unittest.TestCase):
    def test_comicinfo_metadata(self):
        meta = self.parse("a.cbz", make_cbz(comicinfo=(
            "<ComicInfo><Title>Comic Title</Title><Writer>Comic Writer</Writer>"
            "<Summary>Tom &amp; Jerry.</Summary><Year>2015</Year>"
            "<Genre>Action</Genre></ComicInfo>")), metadata.from_cbz)
        self.assertEqual(meta["title"], "Comic Title")
        self.assertEqual(meta["authors"], "Comic Writer")
        self.assertEqual(meta["description"], "Tom & Jerry.")  # entity decoded
        self.assertEqual(meta["year"], 2015)
        self.assertEqual(meta["categories"], "Action")

    def test_series_number_fallback_title(self):
        meta = self.parse("b.cbz", make_cbz(comicinfo=(
            "<ComicInfo><Series>Saga</Series><Number>3</Number></ComicInfo>")),
            metadata.from_cbz)
        self.assertEqual(meta["title"], "Saga #3")

    def test_natural_sort_picks_first_page(self):
        pages = ["p10.jpg", "p2.jpg", "p1.jpg"]
        meta = self.parse("c.cbz", make_cbz(comicinfo=None, pages=pages), metadata.from_cbz)
        self.assertEqual(meta["cover_ext"], "jpg")

    def test_no_comicinfo_no_cover_failure(self):
        meta = self.parse("d.cbz", make_cbz(comicinfo=None), metadata.from_cbz)
        self.assertEqual(meta["cover_ext"], "jpg")  # first image still works

    def test_image_only_archive(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("nested/001.png", b"not really a png but named so")
        meta = self.parse("e.cbz", buf.getvalue(), metadata.from_cbz)
        self.assertEqual(meta["cover_ext"], "png")

    def test_not_a_zip_raises_for_fallback(self):
        with self.assertRaises(Exception):
            self.parse("f.cbz", b"NOPE", metadata.from_cbz)


class ExtsTests(unittest.TestCase):
    def test_all_reader_formats_accepted(self):
        for ext in ("pdf", "epub", "mobi", "azw", "azw3", "prc", "fb2", "cbz"):
            self.assertIn(ext, metadata.EXTS)

    def test_unreadable_formats_rejected(self):
        for ext in ("djvu", "cbr", "docx", "txt", "rtf", "lit", "zip"):
            self.assertNotIn(ext, metadata.EXTS)

    def test_annas_whitelist_matches(self):
        from app import annas_client
        self.assertEqual(annas_client._GOOD_FILE_EXTS, metadata.EXTS)


if __name__ == "__main__":
    unittest.main(verbosity=2)

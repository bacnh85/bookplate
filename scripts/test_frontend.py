#!/usr/bin/env python3
"""Offline structural checks for the frontend: web/index.html must declare
unique element ids (a duplicate silently breaks the JS that queries it), and
reader.js reading-preference keys must pair up across writes and reads.

Run: .venv/bin/python scripts/test_frontend.py
"""
import html.parser
import pathlib
import re
import unittest

WEB = pathlib.Path(__file__).resolve().parent.parent / "web"
INDEX = WEB / "index.html"
READER = WEB / "reader.js"


class IdCollector(html.parser.HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = []

    def handle_starttag(self, tag, attrs):
        for name, value in attrs:
            if name == "id" and value:
                self.ids.append(value)


class TestIndexHtml(unittest.TestCase):
    def test_ids_unique(self):
        parser = IdCollector()
        parser.feed(INDEX.read_text())
        dupes = sorted({i for i in parser.ids if parser.ids.count(i) > 1})
        self.assertEqual(dupes, [], f"duplicate ids in index.html: {dupes}")

    def test_script_cache_bust_present(self):
        self.assertRegex(INDEX.read_text(), r'src="/app\.js\?v=\d+"')


class TestReaderSettings(unittest.TestCase):
    """reader.js persists each reading preference via put(k) -> localStorage
    key `reader-<k>` and reads it back with get("reader-<k>"). A key that is
    written but never read silently drops the choice on reload; worse, a put
    that collides with another key corrupts it (put("font") once overwrote the
    numeric font-size slot with "georgia" -> parseFloat -> NaN)."""

    def test_put_keys_pair_with_get_keys(self):
        src = READER.read_text()
        puts = set(re.findall(r'\bput\("([\w-]+)"', src))
        gets = set(re.findall(r'\bget\("reader-([\w-]+)"', src))
        self.assertEqual(puts, gets,
                         f"unpaired settings keys — written but never read: "
                         f"{sorted(puts - gets)}; read but never written: {sorted(gets - puts)}")

    def test_font_size_key_not_used_for_family(self):
        # reader-font holds the numeric size; the family must never touch it
        src = READER.read_text()
        self.assertNotIn('put("font",', src)
        self.assertRegex(src, r'parseFloat\(localStorage\.getItem\("reader-font"\)\) \|\| 17')


if __name__ == "__main__":
    unittest.main()

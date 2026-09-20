#!/usr/bin/env python3
"""Offline structural checks for the frontend: web/index.html must declare
unique element ids (a duplicate silently breaks the JS that queries it).

Run: .venv/bin/python scripts/test_frontend.py
"""
import html.parser
import pathlib
import re
import unittest

INDEX = pathlib.Path(__file__).resolve().parent.parent / "web" / "index.html"


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


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""Offline tests for metadata.cover_color (3D book case colour): svg palette
fast path (+ case-insensitivity), raster edge-band average, dark lift, junk
input. Same offline pattern as test_progress.py — no server needed.

Run: .venv/bin/python scripts/test_cover_color.py
"""
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app import metadata  # noqa: E402
from app.metadata import cover_color  # noqa: E402


def _lum(hex6: str) -> float:
    r, g, b = (int(hex6[i:i + 2], 16) for i in (1, 3, 5))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def solid_png(rgb) -> bytes:
    import pymupdf
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 60, 90))
    pix.set_rect(pix.irect, rgb)
    return pix.tobytes("png")


class CoverColorTests(unittest.TestCase):
    def test_generated_svg_palette_both_cases(self):
        svg = metadata.generated_cover("Some Title", "Author", "seed-1")
        expected = cover_color(svg, "svg")
        self.assertRegex(expected, r"^#[0-9a-f]{6}$")
        self.assertEqual(cover_color(svg, "SVG"), expected)  # EPUB item names keep case

    def test_solid_bitmap_edge_average(self):
        self.assertEqual(cover_color(solid_png((200, 60, 40)), "png"), "#c83c28")
        self.assertEqual(cover_color(solid_png((255, 255, 255)), "png"), "#ffffff")
        self.assertEqual(cover_color(solid_png((255, 255, 255)), "PNG"), "#ffffff")

    def test_near_black_lifted(self):
        got = cover_color(solid_png((5, 5, 5)), "png")
        self.assertIsNotNone(got)
        self.assertGreaterEqual(_lum(got), 60)  # lift keeps the spine shade visible

    def test_junk_returns_none(self):
        self.assertIsNone(cover_color(b"not an image at all", "jpg"))
        self.assertIsNone(cover_color(b"", "png"))


if __name__ == "__main__":
    unittest.main()

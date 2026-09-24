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


class TestAssetVersioning(unittest.TestCase):
    """Cloudflare may override the origin's Cache-Control: no-cache with a 1-year
    browser TTL on static assets, pinning stale JS after every deploy (it did).
    HTML is served no-cache, so stamped asset URLs (?v=N) are the propagation
    mechanism: new HTML must always reference versioned JS/CSS. Bump ?v= on
    every app.js/app.css/reader.js change."""

    READER_HTML = WEB / "reader.html"

    def test_reader_html_stamps_assets(self):
        src = self.READER_HTML.read_text()
        self.assertRegex(src, r'href="/app\.css\?v=\d+"')
        self.assertRegex(src, r'src="/reader\.js\?v=\d+"')

    def test_index_html_stamps_css(self):
        self.assertRegex(INDEX.read_text(), r'href="/app\.css\?v=\d+"')

    def test_foliate_entry_import_stamped(self):
        # the view.js entry import must carry ?v= too (relative imports inside
        # the foliate tree resolve fresh only when the tree dir is swapped)
        self.assertRegex(READER.read_text(),
                         r'import\("/foliate-js/view\.js\?v=\d+"\)')


class TestTheme(unittest.TestCase):
    """Library-shell theme feature (Auto/Day/Sepia/Night): the same tokens as
    the reader. Both surfaces MUST switch via body[data-theme] on the SAME
    shared token set — a palette hardcoded in either file would leave the app
    half-dark after dark mode (it did: scrim/skeleton carried light-only rgba)."""

    def test_night_override_swaps_full_palette(self):
        css = (WEB / "app.css").read_text()
        for tok in ["--paper", "--card", "--ink", "--ink-soft", "--line",
                    "--accent", "--accent-ink", "--danger", "color-scheme",
                    "--e-xs", "--e-sm", "--e-md", "--scrim"]:
            self.assertRegex(css, re.compile(
                r'body\[data-theme="night"\]\s*\{[^}]*' + re.escape(tok), re.S),
                f"night override must set {tok}")
        # scrim/backdrop overlays consume the token, never hardcoded rgba
        self.assertEqual([v.strip() for v in re.findall(r'(?:#scrim \{[^}]*background:|dialog::backdrop \{ background: )([^;]+);', css)],
                         ["var(--scrim)", "var(--scrim)"], "overlay scrims must use --scrim")

    def test_theme_button_and_fouc_guard_exist(self):
        idx = INDEX.read_text()
        self.assertIn('id="theme-btn"', idx)
        # boot script sets the resolved theme from localStorage before CSS paint
        self.assertRegex(idx, r"localStorage\.getItem\(\"theme\"\)")
        self.assertIn("prefers-color-scheme: dark", idx)

    def test_theme_cycle_and_auto_resolution(self):
        src = (WEB / "app.js").read_text()
        self.assertIn('matchMedia("(prefers-color-scheme: dark)")', src)
        self.assertIn('addEventListener("change", applyTheme)', src)  # live auto switching
        # cycle + names must cover exactly the four theme values (structure, not substrings)
        cycle = re.search(r'THEME_CYCLE = \[([^\]]+)\]', src)
        self.assertIsNotNone(cycle, "THEME_CYCLE array missing from app.js")
        names = re.search(r'THEME_NAMES = \{([^}]+)\}', src)
        self.assertIsNotNone(names, "THEME_NAMES map missing from app.js")
        values = set(re.findall(r'"(\w+)"', cycle.group(1)))
        self.assertEqual(values, {"auto", "day", "sepia", "night"})
        self.assertEqual(set(re.findall(r'(\w+): "', names.group(1))), values,
                         "THEME_NAMES keys must match THEME_CYCLE values")
        # storage-safe: reads/writes guarded, invalid values fall back to auto
        self.assertIn("try {", src[src.index("const readTheme"):src.index("function applyTheme")])
        self.assertRegex(src, r'THEME_CYCLE\.includes\(t\) \? t : "auto"')
        self.assertIn('catch { /* blocked: session-only theme */ }', src)

    def test_boot_script_mirrors_appjs_resolution(self):
        # index.html's pre-paint script must validate against the same value set
        idx = INDEX.read_text()
        boot = re.search(r'<script>/\* theme before first paint.*?</script>', idx, re.S)
        self.assertIsNotNone(boot, "theme boot script missing from index.html")
        self.assertIn('includes(s)', boot.group(0))
        self.assertIn('prefers-color-scheme: dark', boot.group(0))
        for t in ["day", "sepia", "night"]:
            self.assertIn(f'"{t}"', boot.group(0))
        self.assertNotIn("localStorage.getItem", boot.group(0).split("try")[0])  # guarded access


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


class TestReaderExperience(unittest.TestCase):
    """Yomu-style reader contracts: bundled webfont + immersive chrome +
    sectioned seek bar + book typography (justify/hyphens) + the ?reader=1
    derivative flag on PDF fetches."""

    READER_HTML_SRC = (WEB / "reader.html").read_text()

    def test_reader_html_preloads_and_defines_webfont(self):
        src = self.READER_HTML_SRC
        self.assertIn('href="/fonts/Literata-VF.woff2"', src)
        self.assertIn("@font-face", src)
        self.assertIn("Literata-Italic-VF.woff2", src)

    def test_reader_html_has_sectioned_seek_and_chrome(self):
        src = self.READER_HTML_SRC
        for el_id in ("ticks", "section-label", "zone-center", "top-bar", "bottom-bar"):
            self.assertIn(f'id="{el_id}"', src)

    def test_reader_js_uses_section_progress_apis(self):
        src = READER.read_text()
        self.assertIn("getSectionFractions", src)  # chapter ticks
        self.assertIn("tocItem", src)              # section label on relocate

    def test_reader_js_justifies_with_hyphenation(self):
        src = READER.read_text()
        self.assertIn("text-align: justify", src)
        self.assertIn("hyphens: auto", src)
        # the old sledgehammer that flattened heading hierarchy must stay gone
        self.assertNotIn("* { font-size", src)

    def test_reader_pdf_fetches_use_reader_flag(self):
        src = READER.read_text()
        # flag built at the single fileUrl site: PDFs get ?reader=1 (server
        # linearized derivative), other formats don't
        self.assertIn('isPdf ? "?reader=1" : ""', src)

    def test_reader_streams_every_format(self):
        """The non-PDF path must stream via the Range pseudo-File like PDFs do.
        A whole-file fetch+blob is what killed iOS Safari on a 36.5MB EPUB
        ('Failed to open book — Load failed'): one long body the phone drops."""
        src = READER.read_text()
        self.assertIn('makeStreamingFile(', src)          # shared by both branches
        self.assertNotIn('await res.blob()', src)         # whole-file download is gone
        self.assertIn('bytes=${begin}-${end - 1}', src)   # Range math intact
        self.assertIn('TypeError', src)                   # per-slice retry present

    def test_reader_margin_is_css_length(self):
        """The paginator consumes margin as a CSS length (minmax(var(--_margin),
        1fr), foliate-js README: 'The unit must be px'). A unitless non-zero
        value invalidates the grid rows and pages render at ~45% height."""
        src = READER.read_text()
        html = (WEB / "reader.html").read_text()
        for v in ("16px", "48px", "96px"):
            self.assertIn(f'value="{v}"', html)
        self.assertIn('get("reader-margin", "48px")', src)
        self.assertIn('+ "px"', src)  # legacy unitless values normalized at write

    def test_pdfjs_range_chunk_size_bumped(self):
        src = (WEB / "foliate-js" / "pdf.js").read_text()
        self.assertIn("rangeChunkSize: 262144", src)

    def test_reader_fonts_dir_exists(self):
        fonts = WEB / "fonts"
        self.assertTrue((fonts / "Literata-VF.woff2").exists())
        self.assertTrue((fonts / "Literata-Italic-VF.woff2").exists())

    def test_reader_vietnamese_font_subset_wired(self):
        """Vietnamese subset @font-face rules must exist in both reader.html and
        reader.js (blob: iframes redeclare fonts), with Google's unicode-range.
        Iowan (old display + 'Iowan Serif' option) lacks U+1EDA-1EF1 — the
        stacks must not lead with it anymore."""
        vr = "U+1EA0-1EF9"
        for src in (self.READER_HTML_SRC, READER.read_text()):
            self.assertIn("Literata-VF-viet.woff2", src)
            self.assertIn("Literata-Italic-VF-viet.woff2", src)
            self.assertIn(vr, src)
        fonts = WEB / "fonts"
        self.assertTrue((fonts / "Literata-VF-viet.woff2").exists())
        self.assertTrue((fonts / "Literata-Italic-VF-viet.woff2").exists())
        # no eager preload for the viet subset — unicode-range must lazy-fetch it
        # only when a book actually contains Vietnamese codepoints
        self.assertNotIn('href="/fonts/Literata-VF-viet.woff2"', self.READER_HTML_SRC)
        # stale/removed stored keys (old 'serif') must heal to the default:
        # otherwise the select goes blank and applyStyles() injects no @font-face
        self.assertIn('if (!FONTS[settings["font-family"]]) put("font-family", "literata")', READER.read_text())
        self.assertNotIn("Iowan", self.READER_HTML_SRC)
        self.assertNotIn("Iowan", READER.read_text())
        self.assertNotIn("Iowan", (WEB / "app.css").read_text())


class TestDocsHtml(unittest.TestCase):
    """web/docs.html is the in-app documentation fragment: app.js fetches it and
    injects it into #docs-body via innerHTML, so it must stay a pure-HTML
    fragment (no <script>), and its contract strings (endpoints, auth, MCP tool
    names) must not drift from the server."""

    DOCS = WEB / "docs.html"
    TOOLS = ["search_library", "get_book", "list_collections", "create_collection",
             "add_to_collection", "search_store", "queue_book", "list_queue",
             "remetadata", "refetch_cover", "update_book"]

    def test_sections_cover_every_tab(self):
        src = self.DOCS.read_text()
        tabs = set(re.findall(r'data-doc="(\w+)"', INDEX.read_text()))
        self.assertTrue(tabs, "docs tabs missing from index.html")
        sections = set(re.findall(r'<section data-doc="(\w+)"', src))
        self.assertEqual(sections, tabs,
                         f"docs.html sections {sorted(sections)} != index.html tabs {sorted(tabs)}")

    def test_contract_strings_present(self):
        src = self.DOCS.read_text()
        for needle in ["/opds", "/mcp", "Authorization: Bearer", "openapi.json",
                       "kindle.com", "@kindle.com", "/api/books", "/api/tokens",
                       "/api/ai/chat", "api.z.ai/api/paas/v4"] + self.TOOLS:
            self.assertIn(needle, src, f"docs.html lost contract string: {needle}")

    def test_no_executable_content_in_fragment(self):
        """docs.html is injected via innerHTML, where <script> never runs but
        on*= handler attributes and javascript: URLs DO execute — block both."""
        src = self.DOCS.read_text()
        self.assertNotIn("<script", src.lower())
        self.assertIsNone(re.search(r"\son[a-z]+\s*=", src, re.IGNORECASE),
                          "docs.html must not use inline event-handler attributes")
        self.assertIsNone(re.search(r"javascript\s*:", src, re.IGNORECASE),
                          "docs.html must not use javascript: URLs")

    def test_docs_view_wired_in_app(self):
        app = (WEB / "app.js").read_text()
        self.assertIn('docs: "#docs-view"', app)
        self.assertIn('fetch("/docs.html")', app)


if __name__ == "__main__":
    unittest.main()

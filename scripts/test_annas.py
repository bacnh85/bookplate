#!/usr/bin/env python3
"""Unit tests for the Anna's Archive adapter — no network, no key needed
(the fixture in scripts/fixtures/ is real search HTML; see
scripts/fetch_annas_fixture.py for how to refresh it).

Run: .venv/bin/python scripts/test_annas.py
"""
import asyncio
import hashlib
import os
import pathlib
import sys
import shutil
import time
import tempfile
import unittest
from unittest import mock

import httpx

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app.annas_client import (Annas, AnnasConfigError, AnnasUnavailable,
                              _ext_from_meta, _is_challenge, _slow_page_info,
                              parse_search_results)

FIXTURE = pathlib.Path(__file__).resolve().parent / "fixtures" / "annas_search.html"
BASE = "https://annas-archive.gd"
MD5 = "ab" * 16


def run(coro):
    return asyncio.run(coro)


def md5of(b: bytes) -> str:
    return hashlib.md5(b).hexdigest()


class FakeAsyncClient:
    """Stands in for httpx.AsyncClient in `async with` blocks; every get/post
    consumes the next scripted item (a Response, or an exception to raise)."""

    def __init__(self, responses, *a, **k):
        # shared, not copied: one script consumed across every AsyncClient()
        # instance a single call flow creates
        self.responses = responses
        self.requests = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def _pop(self):
        nxt = self.responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    async def get(self, url, **k):
        self.requests.append(("GET", url, k.get("headers") or {}))
        return self._pop()

    async def post(self, url, **k):
        self.requests.append(("POST", url, k.get("headers") or {}))
        return self._pop()


def with_client(responses):
    return mock.patch("app.annas_client.httpx.AsyncClient",
                      lambda *a, **k: FakeAsyncClient(responses, *a, **k))


def resp(status=200, json_body=None, text="", headers=None):
    # request attached: httpx cookie parsing scopes via response.request.url
    req = httpx.Request("GET", f"{BASE}/x")
    if json_body is not None:
        return httpx.Response(status, json=json_body, headers=headers or {}, request=req)
    return httpx.Response(status, text=text, headers=headers or {}, request=req)


def login_resp():
    return resp(302, headers=[("Set-Cookie", "aa_account_id2=abc; Path=/"),
                              ("Set-Cookie", "__ddg1_=xyz; Path=/"),
                              ("Location", f"{BASE}/account/")])


def challenge():
    return resp(302, headers={"location": f"{BASE}/search?q=x&check=1",
                              "server": "ddos-guard"})


def xredirect():
    return resp(302, headers={"location": "https://evil.example/next"})


async def _async_bytes(b):
    return b


class ParserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rows = parse_search_results(FIXTURE.read_text(), BASE)

    def test_extracts_rows_from_real_markup(self):
        self.assertGreaterEqual(len(self.rows), 10)
        first = self.rows[0]
        self.assertRegex(first["id"], r"^[a-f0-9]{32}$")
        self.assertEqual(first["name"], "The man test : the Marin Test Series, #1")
        self.assertEqual(first["authors"], "Aksel, Amanda")
        self.assertEqual(first["extension"], "epub")
        self.assertEqual(first["size"], "0.4MB")
        self.assertEqual(first["year"], "2014")
        self.assertEqual(first["language"], "English")
        self.assertEqual(first["url"], f"{BASE}/md5/{first['id']}")
        self.assertEqual(first["source"], "annas")

    def test_every_md5_is_32hex(self):
        for r in self.rows:
            self.assertRegex(r["id"], r"^[a-f0-9]{32}$")

    def test_meta_variants(self):
        meta = "English [en] · EPUB · 0.4MB · 2014 · 📕 Book (fiction) · 🚀/lgli"
        self.assertEqual(_ext_from_meta(meta), "epub")
        self.assertEqual(_ext_from_meta(" · fb2 · 1.2GB · 1999"), "fb2")
        self.assertEqual(_ext_from_meta("Italiano [it] · Book (fiction)"), "")

    def test_non_md5_anchor_is_dropped(self):
        page = ('<a class="js-vim-focus" href="/md5/not-a-md5">x</a>'
                f'<a class="js-vim-focus" href="/md5/{MD5}">Real</a>')
        rows = parse_search_results(page, BASE)
        self.assertEqual([r["id"] for r in rows], [MD5])

    def test_entity_encoded_tags_survive_decoding(self):
        # _clean strips real tags, then decodes entities (same order as the zlib
        # adapter) — decoded angle brackets stay in the stored text. The XSS
        # boundary is the frontend: every row field renders through esc().
        page = f'<a class="js-vim-focus" href="/md5/{MD5}">&lt;script&gt;T&lt;/script&gt;</a>'
        rows = parse_search_results(page, BASE)
        self.assertEqual(rows[0]["name"], "<script>T</script>")


class ChallengeTests(unittest.TestCase):
    def test_403_from_ddos_guard(self):
        self.assertTrue(_is_challenge(httpx.Response(403, headers={"server": "ddos-guard"})))
        self.assertFalse(_is_challenge(httpx.Response(403)))

    def test_check_redirect(self):
        self.assertTrue(_is_challenge(httpx.Response(
            302, headers={"location": f"{BASE}/search?q=x&check=1"})))
        self.assertFalse(_is_challenge(httpx.Response(
            302, headers={"location": f"{BASE}/search?q=x"})))
        self.assertFalse(_is_challenge(httpx.Response(200)))


class LoginTests(unittest.TestCase):
    def setUp(self):
        env = mock.patch.dict(os.environ, {"ANNAS_ARCHIVE_SECRET_KEY": "testkey"})
        env.start()
        self.addCleanup(env.stop)
        self.annas = Annas()  # fresh instance: no cached cookie

    def test_enabled_gates_on_key(self):
        self.assertTrue(self.annas.enabled)
        with mock.patch.dict(os.environ, {"ANNAS_ARCHIVE_SECRET_KEY": ""}):
            self.assertFalse(Annas().enabled)

    def test_missing_key_is_config_error(self):
        with mock.patch.dict(os.environ, {"ANNAS_ARCHIVE_SECRET_KEY": ""}):
            with self.assertRaises(AnnasConfigError):
                run(self.annas._login())

    def test_bad_key_rejected_without_cookie(self):
        with with_client([resp(200, text="<form>login page</form>")]):
            with self.assertRaises(AnnasConfigError):
                run(self.annas._login())

    def test_login_caches_cookies(self):
        with with_client([login_resp()]):
            run(self.annas._login())
        self.assertIn("aa_account_id2=abc", self.annas._cookie)
        self.assertIn("__ddg1_=xyz", self.annas._cookie)

    def test_challenged_login_is_retryable(self):
        # guard challenge on POST /account/ issues no aa_* cookie — must surface
        # the retryable browser hint, not a permanent "bad key" config error
        with with_client([challenge()]):
            with self.assertRaises(AnnasUnavailable) as cm:
                run(self.annas._login())
        self.assertIn("browser", str(cm.exception))
        self.assertEqual(self.annas._cookie, "")

    def test_bad_key_with_ddg_cookies_still_fails(self):
        # the guard sets __ddg* even on the rejected login form — only an aa_*
        # cookie means success, else a dead session gets cached
        r = resp(200, text="<form>login page</form>",
                 headers=[("Set-Cookie", "__ddg1_=xyz; Path=/")])
        with with_client([r]):
            with self.assertRaises(AnnasConfigError):
                run(self.annas._login())
        self.assertEqual(self.annas._cookie, "")  # nothing cached

    def test_unreachable_is_unavailable(self):
        with with_client([httpx.ConnectError("nope")]):
            with self.assertRaises(AnnasUnavailable):
                run(self.annas._login())


class SearchTests(unittest.TestCase):
    def setUp(self):
        env = mock.patch.dict(os.environ, {"ANNAS_ARCHIVE_SECRET_KEY": "testkey"})
        env.start()
        self.addCleanup(env.stop)
        self.annas = Annas()

    def test_search_parses_fixture(self):
        seen = {}

        async def fake_authed(self, url):
            seen["url"] = url
            return resp(200, text=FIXTURE.read_text())
        with mock.patch.object(Annas, "_fetch_authed", fake_authed):
            rows = run(self.annas.search("q"))
        self.assertIn("/search?q=", seen["url"])
        self.assertGreaterEqual(len(rows), 10)

    def test_search_raises_on_non_200(self):
        async def fake_authed(self, url):
            return resp(500, text="boom")
        with mock.patch.object(Annas, "_fetch_authed", fake_authed):
            with self.assertRaises(AnnasUnavailable):
                run(self.annas.search("q"))

    def test_challenge_ladder_recovers_after_relogin(self):
        self.annas._cookie, self.annas._cookie_at = "aa_account_id2=stale", 0.0
        fixture = FIXTURE.read_text()
        with with_client([login_resp(), resp(200, text=fixture)]):
            rows = run(self.annas.search("q"))
        self.assertGreaterEqual(len(rows), 10)

    def test_challenge_then_success_recovers(self):
        # fresh cookie, first hop challenged: the relogin-once ladder must recover
        self.annas._cookie, self.annas._cookie_at = "aa_account_id2=x", time.monotonic()
        with with_client([challenge(), login_resp(), resp(200, text=FIXTURE.read_text())]):
            rows = run(self.annas.search("q"))
        self.assertGreaterEqual(len(rows), 10)

    def test_cross_origin_redirect_drops_session_cookie(self):
        # a hostile/injected redirect must not be followed at all — the server
        # must not GET an attacker-chosen host (cookie or not)
        self.annas._cookie, self.annas._cookie_at = "aa_account_id2=secret", time.monotonic()
        fake = FakeAsyncClient([xredirect(), resp(200, text="leak?")])
        with mock.patch("app.annas_client.httpx.AsyncClient", lambda *a, **k: fake):
            with self.assertRaises(AnnasUnavailable) as cm:
                run(self.annas._fetch_authed(f"{BASE}/search?q=x"))
        self.assertIn("off-mirror", str(cm.exception))
        self.assertEqual(len(fake.requests), 1)  # the evil hop was never issued
        self.assertIn("Cookie", fake.requests[0][2])  # cookie only went to the mirror

    def test_same_host_redirect_keeps_cookie(self):
        self.annas._cookie, self.annas._cookie_at = "aa_account_id2=x", time.monotonic()
        hop = resp(302, headers={"location": f"{BASE}/search?q=x&page=2"})
        fake = FakeAsyncClient([hop, resp(200, text="ok")])
        with mock.patch("app.annas_client.httpx.AsyncClient", lambda *a, **k: fake):
            run(self.annas._fetch_authed(f"{BASE}/search?q=x"))
        self.assertIn("Cookie", fake.requests[1][2])  # same-host hop keeps it

    def test_persistent_challenge_surfaces_hint(self):
        self.annas._cookie, self.annas._cookie_at = "aa_account_id2=x", 0.0
        with with_client([login_resp(), challenge(), login_resp(), challenge()]):
            with self.assertRaises(AnnasUnavailable) as cm:
                run(self.annas.search("q"))
        self.assertIn("browser", str(cm.exception))


class DownloadTests(unittest.TestCase):
    def setUp(self):
        env = mock.patch.dict(os.environ, {"ANNAS_ARCHIVE_SECRET_KEY": "testkey"})
        env.start()
        self.addCleanup(env.stop)
        self.annas = Annas()

    def test_happy_path(self):
        content = b"EPUB!"
        api = resp(200, json_body={"download_url": "https://partner.example/get/book.epub",
                                   "account_fast_download_info": {"downloads_left": 5}})
        with with_client([api]), \
             mock.patch("app.annas_client._fetch_bytes",
                        lambda *a, **k: _async_bytes(content)):
            data, meta = run(self.annas.download(md5of(content)))
        self.assertEqual(data, content)
        self.assertTrue(meta["_filename"].endswith(".epub"))

    def test_not_a_member_falls_back_to_slow(self):
        api = resp(200, json_body={"download_url": None, "error": "Not a member"})

        async def fake_slow(self, md5, on_progress, expected_size):
            return b"SLOW", {"_filename": f"{md5}.epub"}
        with with_client([api]), \
             mock.patch.object(Annas, "_slow_download", fake_slow):
            data, meta = run(self.annas.download(MD5))
        self.assertEqual(data, b"SLOW")
        self.assertTrue(meta["_filename"].endswith(".epub"))

    def test_invalid_md5_is_retryable(self):
        api = resp(200, json_body={"download_url": None, "error": "Invalid md5"})
        with with_client([api]):
            with self.assertRaises(AnnasUnavailable):
                run(self.annas.download(MD5))

    def test_wrong_content_md5_rejected(self):
        # an expired/wrong partner link serving a 200 body that isn't the book
        api = resp(200, json_body={"download_url": "https://partner.example/get/book.epub"})
        with with_client([api]), \
             mock.patch("app.annas_client._fetch_bytes",
                        lambda *a, **k: _async_bytes(b"<html>not the book</html>")):
            with self.assertRaises(AnnasUnavailable) as cm:
                run(self.annas.download(md5of(b"EPUB!")))
        self.assertIn("md5", str(cm.exception))

    def test_bad_key_fails_fast_without_slow_fallback(self):
        api = resp(200, json_body={"download_url": None, "error": "Invalid key"})
        with with_client([api]):
            with self.assertRaises(AnnasConfigError):
                run(self.annas.download(MD5))

    def test_rate_limited_is_retryable(self):
        with with_client([resp(429, json_body={"error": "slow down"})]):
            with self.assertRaises(AnnasUnavailable):
                run(self.annas.download(MD5))

    def test_junk_json_is_retryable(self):
        with with_client([resp(200, text="<html>not json</html>")]):
            with self.assertRaises(AnnasUnavailable):
                run(self.annas.download(MD5))

    def test_non_md5_id_rejected_locally(self):
        with self.assertRaises(AnnasConfigError):
            run(self.annas.download("../../etc/passwd"))

    def test_url_ext_whitelist(self):
        # .php in a query-shaped URL must not become the extension
        content = b"x"
        api = resp(200, json_body={"download_url": "https://x/get.php?md5=ab"})
        with with_client([api]), \
             mock.patch("app.annas_client._fetch_bytes",
                        lambda *a, **k: _async_bytes(content)):
            _, meta = run(self.annas.download(md5of(content)))
        self.assertEqual(meta["_filename"], md5of(content))  # unusable name: job ext wins


class SlowDownloadTests(unittest.TestCase):
    def setUp(self):
        env = mock.patch.dict(os.environ, {"ANNAS_ARCHIVE_SECRET_KEY": "testkey"})
        env.start()
        self.addCleanup(env.stop)
        self.annas = Annas()

    READY = ('<p class="mb-4 text-xl font-bold">\n'
             '  <a href="https://nrzr.li/d3/y/1736300000/10000/'
             'libgen%2Flg%2Ffile%2Fpath~/AbCdEf_-123/Terraform%20Up%20and%20Running.pdf" '
             'target="_blank">Download now</a></p>')
    WAITING = ('<div class="mb-4 font-bold text-xl">Please wait '
               '<span class="js-partner-countdown">123</span> seconds</div>\n'
               '<script>\n  let waitSeconds = 123;\n</script>')
    WAIT600 = WAITING.replace("123", "600")

    def test_page_info_extraction(self):
        url, wait = _slow_page_info(self.READY)
        self.assertTrue(url.startswith("https://nrzr.li/d3/"))
        self.assertIsNone(wait)
        url, wait = _slow_page_info(self.WAITING)
        self.assertIsNone(url)
        self.assertEqual(wait, 123)
        self.assertEqual(_slow_page_info("<html>plain md5 page</html>"), (None, None))

    def test_slow_immediate_link(self):
        ready = self.READY
        content = b"PDFDATA"
        book_md5 = md5of(content)

        async def fake_authed(self, url):
            return resp(200, text=ready)
        with mock.patch.object(Annas, "_fetch_authed", fake_authed), \
             mock.patch("app.annas_client._fetch_bytes",
                        lambda *a, **k: _async_bytes(content)):
            data, meta = run(self.annas._slow_download(book_md5, None, None))
        self.assertEqual(data, content)
        self.assertEqual(meta["_filename"], "Terraform Up and Running.pdf")

    def test_slow_waitlist_reloads(self):
        sleeps = []

        async def fake_sleep(s):
            sleeps.append(s)

        pages = iter([resp(200, text=self.WAITING), resp(200, text=self.READY)])

        async def fake_authed(self, url):
            return next(pages)
        content = b"PDFDATA"
        with mock.patch.object(Annas, "_fetch_authed", fake_authed), \
             mock.patch("app.annas_client.asyncio.sleep", fake_sleep), \
             mock.patch("app.annas_client._fetch_bytes",
                        lambda *a, **k: _async_bytes(content)):
            data, meta = run(self.annas._slow_download(md5of(content), None, None))
        self.assertEqual(data, content)
        self.assertGreaterEqual(sum(sleeps), 123)  # waited out the waitlist

    def test_slow_wait_budget_bounded_with_heartbeat(self):
        # waitlisted servers must not pin the queue ~1h: total wait is capped at
        # SLOW_MAX_WAIT_S and the heartbeat fires during waits
        waits = []

        async def fake_sleep(s):
            waits.append(s)

        beats = []

        def beat(done, total):
            beats.append((done, total))

        wait600 = self.WAIT600

        async def fake_authed(self, url):
            return resp(200, text=wait600)
        with mock.patch.object(Annas, "_fetch_authed", fake_authed), \
             mock.patch("app.annas_client.asyncio.sleep", fake_sleep):
            with self.assertRaises(AnnasUnavailable):
                run(self.annas._slow_download(MD5, beat, 1000))
        self.assertLessEqual(sum(waits), 610)  # was ~1800s+ before the budget
        self.assertGreater(len(beats), 0)      # heartbeat kept the row fresh

    def test_slow_gives_up_readable(self):
        async def fake_authed(self, url):
            return resp(200, text="<html>md5 page, nothing here</html>")
        with mock.patch.object(Annas, "_fetch_authed", fake_authed):
            with self.assertRaises(AnnasUnavailable) as cm:
                run(self.annas._slow_download(MD5, None, None))
        self.assertIn("slow", str(cm.exception))

class MainWiringTests(unittest.TestCase):
    """main.py annas wiring: enqueue validation, retry alias, worker dispatch.
    Runs against a throwaway DB — app.main is imported AFTER the db paths are
    redirected, because main calls db.init() at import time."""

    @classmethod
    def setUpClass(cls):
        import app.db as db
        cls._tmp = pathlib.Path(tempfile.mkdtemp(prefix="annas-wiring-"))
        cls._patches = [
            mock.patch.object(db, "DATA_DIR", cls._tmp),
            mock.patch.object(db, "DB_PATH", cls._tmp / "test.db"),
        ]
        for p in cls._patches:
            p.start()
        import app.main as main  # db.init() now lands in the temp DB
        cls.main = main
        with db.conn() as c:
            c.execute("INSERT OR IGNORE INTO users(id, email, password_hash) "
                      "VALUES(424242, 'wire@t.io', 'x')")
        cls.user = {"id": 424242, "email": "wire@t.io"}

    @classmethod
    def tearDownClass(cls):
        for p in cls._patches:
            p.stop()
        shutil.rmtree(cls._tmp, ignore_errors=True)

    def test_enqueue_rejects_non_md5(self):
        # the id flows into slow_download URLs — arbitrary strings never reach it
        with self.assertRaises(self.main.HTTPException) as cm:
            self.main.annas_enqueue(self.main.ZlibQueueReq(id="../../etc/passwd"),
                                    user=self.user)
        self.assertEqual(cm.exception.status_code, 400)

    def test_enqueue_tags_source(self):
        row = self.main.annas_enqueue(self.main.ZlibQueueReq(id="ab" * 16, name="W"),
                                      user=self.user)
        self.assertEqual(row["source"], "annas")

    def test_retry_alias_route_registered(self):
        paths = {getattr(r, "path", "") for r in self.main.app.routes}
        self.assertIn("/api/annas/queue/{job_id}/retry", paths)

    def test_retry_resets_failed_annas_row(self):
        with self.main.db.conn() as c:
            cur = c.execute("INSERT INTO download_jobs(user_id, zlib_id, title, source, "
                            "status, attempts, error) VALUES(424242, ?, 'R', 'annas', "
                            "'failed', 1, 'boom')", ("ef" * 16,))
            job_id = cur.lastrowid
        r = self.main.zlib_queue_retry(job_id, user=self.user)  # shared handler
        self.assertEqual(r, {"ok": True})
        with self.main.db.conn() as c:
            row = c.execute("SELECT status, attempts, error FROM download_jobs WHERE id=?",
                            (job_id,)).fetchone()
        self.assertEqual((row["status"], row["attempts"], row["error"]), ("queued", 0, ""))

    def test_search_endpoint_maps_config_vs_transient(self):
        with mock.patch.object(self.main.annas, "search",
                               side_effect=AnnasConfigError("no key set")):
            with self.assertRaises(self.main.HTTPException) as cm:
                run(self.main.annas_search(q="x", user=self.user))
        self.assertEqual(cm.exception.status_code, 400)
        self.assertIn("no key", cm.exception.detail)
        with mock.patch.object(self.main.annas, "search",
                               side_effect=AnnasUnavailable("bot check")):
            with self.assertRaises(self.main.HTTPException) as cm:
                run(self.main.annas_search(q="x", user=self.user))
        self.assertEqual(cm.exception.status_code, 503)

    def test_run_job_dispatches_annas_never_zlib(self):
        # a dispatch typo must not silently send AA jobs to the zlib CLI
        with self.main.db.conn() as c:
            cur = c.execute("INSERT INTO download_jobs(user_id, zlib_id, title, source, "
                            "status) VALUES(424242, ?, 'D', 'annas', 'queued')", ("12" * 16,))
            job_id = cur.lastrowid
        job = dict(self.main.db.conn().execute(
            "SELECT * FROM download_jobs WHERE id=?", (job_id,)).fetchone())
        calls = []

        async def fake_annas_download(md5, on_progress=None, expected_size=None):
            calls.append(("annas", md5))
            raise AnnasConfigError("member check failed")

        async def fake_zlib_download(*a, **k):
            calls.append(("zlib",))
            raise AssertionError("zlib CLI used for an annas job")

        with mock.patch.object(self.main.annas, "download", fake_annas_download), \
             mock.patch.object(self.main.zlib, "download", fake_zlib_download), \
             mock.patch.object(self.main.zlib, "limits",
                               side_effect=AssertionError("zlib quota probe ran")):
            run(self.main._run_job(job))
        self.assertEqual(calls, [("annas", "12" * 16)])
        with self.main.db.conn() as c:
            row = c.execute("SELECT status, error FROM download_jobs WHERE id=?",
                            (job_id,)).fetchone()
        self.assertEqual(row["status"], "failed")  # config error: fail fast, no retry
        self.assertIn("member check failed", row["error"])


if __name__ == "__main__":
    unittest.main(verbosity=2)

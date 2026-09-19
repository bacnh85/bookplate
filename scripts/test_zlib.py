#!/usr/bin/env python3
"""Unit tests for the Z-Library adapter (CLI backend) — no network, no creds.

Credentials come from app settings written into an isolated temp DB (never
the developer's data/ebook.db).

Run: .venv/bin/python scripts/test_zlib.py
"""
import asyncio
import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

# isolated DB so test settings never touch a real data dir
_TMP = pathlib.Path(tempfile.mkdtemp(prefix="bookplate-zlib-test-"))
import app.db as _db  # noqa: E402

_db.DATA_DIR = _TMP
_db.DB_PATH = _TMP / "ebook.db"
os.environ.setdefault("BOOKPLATE_ADMIN_PASS", "x")  # keep db.init() bootstrap quiet
_db.init()
from app import settings as _settings  # noqa: E402

from app.zlib_client import Zlib, ZlibUnavailable


def run(coro):
    return asyncio.run(coro)


class ZlibTests(unittest.TestCase):
    def setUp(self):
        # keep the gate open and the real CLI out of the picture
        _settings.set("zlib.email", "e@x")
        _settings.set("zlib.password", "p")
        which = mock.patch("app.zlib_client.shutil.which", return_value="/usr/bin/zlib")
        which.start()
        self.addCleanup(which.stop)
        # hermetic home WITH a session: _ensure_session skips login identically
        # on every host; tests exercising the login path override home locally
        fake_home = pathlib.Path(tempfile.gettempdir()) / "zlib-test-default-home"
        (fake_home / ".config" / "zlib").mkdir(parents=True, exist_ok=True)
        (fake_home / ".config" / "zlib" / "session.json").write_text("{}")
        self.addCleanup(
            lambda: (fake_home / ".config" / "zlib" / "session.json").unlink(missing_ok=True))
        home = mock.patch("pathlib.Path.home", return_value=fake_home)
        home.start()
        self.addCleanup(home.stop)

    def test_enabled_requires_cli(self):
        with mock.patch("app.zlib_client.shutil.which", return_value=None):
            self.assertFalse(Zlib().enabled)

    def test_search_maps_json_tolerantly(self):
        out = json.dumps({"books": [
            {"id": "1:ab", "name": "T", "authors": ["A", "B"], "year": 2020,
             "extension": "PDF", "size": "1 MB", "cover": "c", "rating": "5"},
            {"id": "2:cd"},  # sparse row — must not explode
        ]})

        async def fake_run(self, *args, **k):
            return 0, out, ""

        with mock.patch.object(Zlib, "_run", fake_run):
            rows = run(Zlib().search("q"))
        self.assertEqual(rows[0]["id"], "1:ab")
        self.assertEqual(rows[0]["authors"], "A, B")
        self.assertEqual(rows[0]["extension"], "pdf")
        self.assertEqual(rows[1]["authors"], "")
        self.assertEqual(rows[1]["extension"], "")

    def test_search_maps_detail_fields(self):
        out = json.dumps({"books": [{
            "id": "1:ab", "name": "T", "authors": ["A"], "publisher": "Hollym",
            "year": "2013", "extension": "pdf", "size": "37.09 MB", "cover": "c",
            "language": "English", "rating": "5.0", "quality": "4.0",
            "isbn": "9781565912489", "url": "https://z-lib.gd/book/x.html",
            "description": "Line one.<br>  Line two &amp; three <b>bold</b>",
        }, {"id": "2:cd"}]})  # sparse row: detail fields default to ""

        async def fake_run(self, *args, **k):
            return 0, out, ""

        with mock.patch.object(Zlib, "_run", fake_run):
            rows = run(Zlib().search("q"))
        row = rows[0]
        self.assertEqual(row["publisher"], "Hollym")
        self.assertEqual(row["quality"], "4.0")
        self.assertEqual(row["isbn"], "9781565912489")
        self.assertEqual(row["url"], "https://z-lib.gd/book/x.html")
        self.assertEqual(row["description"], "Line one. Line two & three bold")
        self.assertNotIn("<", row["description"])
        self.assertEqual(rows[1]["publisher"], "")
        self.assertEqual(rows[1]["description"], "")

    def test_search_cli_failure_maps_to_503(self):
        async def fake_run(self, *args, **k):
            return 1, "", "mirror said no"

        with mock.patch.object(Zlib, "_run", fake_run):
            with self.assertRaises(ZlibUnavailable):
                run(Zlib().search("q"))

    def test_non_dict_json_maps_to_503(self):
        async def fake_run(self, *args, **k):
            return 0, "[1,2]", ""  # valid JSON, wrong shape — must 503, not 500

        with mock.patch.object(Zlib, "_run", fake_run):
            with self.assertRaises(ZlibUnavailable):
                run(Zlib().search("q"))
            with self.assertRaises(ZlibUnavailable):
                run(Zlib().limits())

    def test_transient_failure_does_not_relogin(self):
        calls = []

        async def fake_run(self, *args, **k):
            calls.append(args[0])
            return 1, "", "mirror said no"  # not an auth failure — keep the session

        with mock.patch.object(Zlib, "_run", fake_run):
            with self.assertRaises(ZlibUnavailable) as cm:
                run(Zlib().search("q"))
        self.assertEqual(calls, ["search"])
        self.assertIn("mirror said no", str(cm.exception))

    def test_limits_parses_json(self):
        async def fake_run(self, *args, **k):
            return 0, json.dumps({"daily_amount": 1, "daily_allowed": 10, "daily_remaining": 9}), ""

        with mock.patch.object(Zlib, "_run", fake_run):
            self.assertEqual(run(Zlib().limits())["daily_remaining"], 9)

    def test_limits_junk_json_maps_to_503(self):
        async def fake_run(self, *args, **k):
            return 0, "<html>not json</html>", ""

        with mock.patch.object(Zlib, "_run", fake_run):
            with self.assertRaises(ZlibUnavailable):
                run(Zlib().limits())

    def test_search_forwards_count_to_cli(self):
        calls = []

        async def fake_run(self, *args, **k):
            calls.append(args)
            return 0, json.dumps({"books": []}), ""

        with mock.patch.object(Zlib, "_run", fake_run):
            run(Zlib().search("q", count=5))
        self.assertIn("-n", calls[0])
        self.assertEqual(calls[0][calls[0].index("-n") + 1], "5")

    def test_search_relogins_and_retries_when_session_expired(self):
        calls = []

        async def fake_run(self, *args, **k):
            calls.append(args[0])
            if args[0] == "search":
                if calls.count("search") == 1:
                    return 1, "", "session expired"
                return 0, json.dumps({"books": [{"id": "1:ab", "name": "T"}]}), ""
            return 0, "", ""  # logout / login succeed

        fake_config = pathlib.Path(tempfile.gettempdir()) / "zlib-test-home"
        (fake_config / ".config" / "zlib").mkdir(parents=True, exist_ok=True)
        (fake_config / ".config" / "zlib" / "session.json").write_text("{}")  # stale session
        with mock.patch("pathlib.Path.home", return_value=fake_config):
            with mock.patch.object(Zlib, "_run", fake_run):
                rows = run(Zlib().search("q"))
        self.assertEqual(rows[0]["id"], "1:ab")
        self.assertEqual(calls.count("login"), 1)
        self.assertEqual(calls.count("search"), 2)
        (fake_config / ".config" / "zlib" / "session.json").unlink(missing_ok=True)

    def test_download_reads_file_and_cleans_up(self):
        class FakeProc:
            returncode = 0
            async def communicate(self):
                return b"", b""

        async def fake_run(self, *args, **k):
            return 0, "", ""  # logout / login

        async def fake_spawn(self, *args):
            assert args[0] == "download"
            d = pathlib.Path(args[args.index("--dir") + 1])
            (d / "Some Book.pdf").write_bytes(b"data")
            return FakeProc()

        with mock.patch.object(Zlib, "_run", fake_run), \
                mock.patch.object(Zlib, "_spawn", fake_spawn):
            data, meta = run(Zlib().download("1:ab"))
        self.assertEqual(data, b"data")
        self.assertEqual(meta["_filename"], "Some Book.pdf")

    def test_download_failure_maps_to_503(self):
        class FakeProc:
            returncode = 1
            def __init__(self, err):
                self._err = err.encode()
            async def communicate(self):
                return b"", self._err

        async def fake_run(self, *args, **k):
            return 0, "", ""  # logout / login (one re-login retry, then give up)

        async def fake_spawn(self, *args):
            return FakeProc("quota exceeded")

        with mock.patch.object(Zlib, "_run", fake_run), \
                mock.patch.object(Zlib, "_spawn", fake_spawn):
            with self.assertRaises(ZlibUnavailable):
                run(Zlib().download("1:ab"))

    def test_ensure_session_logins_from_env_when_no_session(self):
        calls = []

        async def fake_run(self, *args, **k):
            calls.append(args[0])
            return 0, "", ""

        with mock.patch("pathlib.Path.home", return_value=pathlib.Path("/nonexistent-home")):
            with mock.patch.object(Zlib, "_run", fake_run):
                run(Zlib()._ensure_session())
        self.assertIn("login", calls)

    def test_ensure_session_skips_login_when_session_exists(self):
        calls = []

        async def fake_run(self, *args, **k):
            calls.append(args[0])
            return 0, "", ""

        fake_config = pathlib.Path(tempfile.gettempdir()) / "zlib-test-home"
        (fake_config / ".config" / "zlib").mkdir(parents=True, exist_ok=True)
        (fake_config / ".config" / "zlib" / "session.json").write_text("{}")
        with mock.patch("pathlib.Path.home", return_value=fake_config):
            with mock.patch.object(Zlib, "_run", fake_run):
                run(Zlib()._ensure_session())
        self.assertNotIn("login", calls)
        (fake_config / ".config" / "zlib" / "session.json").unlink(missing_ok=True)

    def test_ensure_session_relogins_when_domain_changed(self):
        """The CLI pins the domain inside session.json — a Settings domain change
        must force a fresh login, else the stored domain silently wins."""
        calls = []

        async def fake_run(self, *args, **k):
            calls.append(args[0])
            return 0, "", ""

        fake_config = pathlib.Path(tempfile.gettempdir()) / "zlib-test-domain-home"
        cfg = fake_config / ".config" / "zlib"
        cfg.mkdir(parents=True, exist_ok=True)
        (cfg / "session.json").write_text(json.dumps({"domain": "https://old.example"}))
        with mock.patch("pathlib.Path.home", return_value=fake_config):
            with mock.patch.object(Zlib, "_run", fake_run):
                with mock.patch("app.zlib_client.settings.get",
                                lambda k, d="": {"zlib.domain": "https://new.example",
                                                "zlib.email": "e@x",
                                                "zlib.password": "p"}.get(k, d)):
                    run(Zlib()._ensure_session())
        self.assertIn("login", calls)
        (cfg / "session.json").unlink(missing_ok=True)

    def test_ensure_session_no_relogin_when_domain_matches(self):
        calls = []

        async def fake_run(self, *args, **k):
            calls.append(args[0])
            return 0, "", ""

        fake_config = pathlib.Path(tempfile.gettempdir()) / "zlib-test-domain2-home"
        cfg = fake_config / ".config" / "zlib"
        cfg.mkdir(parents=True, exist_ok=True)
        (cfg / "session.json").write_text(json.dumps({"domain": "https://same.example"}))
        with mock.patch("pathlib.Path.home", return_value=fake_config):
            with mock.patch.object(Zlib, "_run", fake_run):
                with mock.patch("app.zlib_client.settings.get",
                                lambda k, d="": {"zlib.domain": "https://same.example",
                                                "zlib.email": "e@x",
                                                "zlib.password": "p"}.get(k, d)):
                    run(Zlib()._ensure_session())
        self.assertNotIn("login", calls)
        (cfg / "session.json").unlink(missing_ok=True)




    def test_history_parses_cli_json(self):
        out = json.dumps({"items": [
            {"id": "2746084:7a5288", "name": "War of Art", "extension": "epub",
             "size": "186 KB", "date": ""},
        ], "page": 2, "total_pages": 5})

        async def fake_run(self, *args, **k):
            assert args[0] == "history" and "--json" in args and "-p" in args
            return 0, out, ""

        with mock.patch.object(Zlib, "_run", fake_run):
            h = run(Zlib().history(page=2))
        self.assertEqual(h["page"], 2)
        self.assertEqual(h["total_pages"], 5)
        self.assertEqual(h["items"][0]["id"], "2746084:7a5288")

    def test_history_junk_json_maps_to_503(self):
        async def fake_run(self, *args, **k):
            return 0, "<html>nope</html>", ""

        with mock.patch.object(Zlib, "_run", fake_run):
            with self.assertRaises(ZlibUnavailable):
                run(Zlib().history())


class ZlibEapiTests(unittest.TestCase):
    """app.zlib_eapi with a stubbed httpx transport — no network."""

    def setUp(self):
        import tempfile
        fake_home = pathlib.Path(tempfile.gettempdir()) / "zlib-eapi-test-home"
        cfg = fake_home / ".config" / "zlib"
        cfg.mkdir(parents=True, exist_ok=True)
        (cfg / "session.json").write_text(json.dumps(
            {"cookies": {"remix_userid": "1", "remix_userkey": "k"},
             "domain": "https://z-lib.test", "mode": "eapi"}))
        home = mock.patch("app.zlib_eapi.SESSION_FILE", cfg / "session.json")
        home.start()
        self.addCleanup(home.stop)
        from app import zlib_eapi
        zlib_eapi._winners.clear()
        self.zlib_eapi = zlib_eapi

    def _transport(self, responder):
        import httpx
        return httpx.MockTransport(responder)

    def test_library_probes_candidates_and_normalizes(self):
        import httpx
        seen = []

        def handler(request):
            seen.append(request.url.path)
            if request.url.path == "/eapi/user/book/saved":
                return httpx.Response(200, json={"success": 1, "books": [
                    {"id": "42:ha", "name": "Saved Book", "author": "A", "year": 2001}],
                    "pagination": {"current": 1}})
            return httpx.Response(404, json={"success": 0, "error": "Requested page not found"})

        real_client = httpx.AsyncClient

        def client_factory(*a, **k):
            return real_client(transport=self._transport(handler))

        with mock.patch("app.zlib_eapi.httpx.AsyncClient", side_effect=client_factory):
            lib = run(self.zlib_eapi.library())
        self.assertTrue(lib["available"])
        self.assertEqual(seen, ["/eapi/user/book/bookmarks", "/eapi/user/book/saved",
                                "/eapi/user/book/saved"])  # probe x2, then the fetch
        self.assertEqual(lib["items"][0]["name"], "Saved Book")
        self.assertEqual(lib["items"][0]["authors"], "A")
        # winner cached: second call goes straight to the endpoint
        real_client = httpx.AsyncClient

        def client_factory(*a, **k):
            return real_client(transport=self._transport(handler))

        with mock.patch("app.zlib_eapi.httpx.AsyncClient", side_effect=client_factory):
            lib2 = run(self.zlib_eapi.library())
        self.assertEqual(lib2["items"][0]["id"], "42:ha")

    def test_booklists_negative_probe_reports_unavailable(self):
        import httpx

        def handler(request):
            return httpx.Response(404, json={"success": 0, "error": "Requested page not found"})

        real_client = httpx.AsyncClient

        def client_factory(*a, **k):
            return real_client(transport=self._transport(handler))

        with mock.patch("app.zlib_eapi.httpx.AsyncClient", side_effect=client_factory):
            bl = run(self.zlib_eapi.booklists())
        self.assertEqual(bl, {"available": False})

    def test_auth_error_triggers_relogin_retry(self):
        import httpx
        calls = {"n": 0}

        def handler(request):
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(401, json={"success": 0, "error": "not authorized"})
            return httpx.Response(200, json={"success": 1, "books": [], "pagination": {}})

        # pre-seed the winner: the 401 lands in the fetch phase, exercising its relogin
        self.zlib_eapi._winners["library"] = "/eapi/user/book/saved"

        async def fake_login():
            calls["login"] = True

        real_client = httpx.AsyncClient

        def client_factory(*a, **k):
            return real_client(transport=self._transport(handler))

        with mock.patch("app.zlib_eapi.httpx.AsyncClient", side_effect=client_factory):
            with mock.patch.object(self.zlib_eapi.zlib, "_login", fake_login):
                lib = run(self.zlib_eapi.library())
        self.assertTrue(calls.get("login"))
        self.assertTrue(lib["available"])
        self.assertEqual(calls["n"], 2)

    def test_winner_rejecting_with_success_0_reports_unavailable(self):
        import httpx

        def handler(request):
            return httpx.Response(200, json={"success": 0, "error": "nope"})

        # pre-seed the winner so the probe phase is skipped (endpoint exists, call rejected)
        self.zlib_eapi._winners["library"] = "/eapi/user/book/saved"
        real_client = httpx.AsyncClient

        def client_factory(*a, **k):
            return real_client(transport=self._transport(handler))

        with mock.patch("app.zlib_eapi.httpx.AsyncClient", side_effect=client_factory):
            lib = run(self.zlib_eapi.library())
        self.assertEqual(lib, {"available": False})

    def test_missing_session_logins_then_probes(self):
        import httpx
        # session.json absent (fresh container): the module must attempt a CLI
        # login and then succeed — NOT permanently cache "unavailable"
        missing = self.zlib_eapi.SESSION_FILE.parent / "missing.json"
        missing.unlink(missing_ok=True)
        session = json.dumps({"cookies": {"remix_userid": "1", "remix_userkey": "k"},
                              "domain": "https://z-lib.test", "mode": "eapi"})

        def handler(request):
            return httpx.Response(200, json={"success": 1, "books": [
                {"id": "7:hh", "name": "After Login"}], "pagination": {}})

        async def fake_ensure():
            missing.write_text(session)  # simulate zlib CLI login persisting a session

        real_client = httpx.AsyncClient

        def client_factory(*a, **k):
            return real_client(transport=self._transport(handler))

        with mock.patch("app.zlib_eapi.SESSION_FILE", missing):
            with mock.patch.object(self.zlib_eapi.zlib, "_ensure_session", fake_ensure):
                with mock.patch("app.zlib_eapi.httpx.AsyncClient", side_effect=client_factory):
                    lib = run(self.zlib_eapi.library())
        self.assertTrue(lib["available"])
        self.assertEqual(lib["items"][0]["name"], "After Login")
        # winner cached from the successful probe: a second call still works
        with mock.patch("app.zlib_eapi.httpx.AsyncClient", side_effect=client_factory):
            lib2 = run(self.zlib_eapi.library())
        self.assertTrue(lib2["available"])

    def test_missing_session_without_creds_not_cached(self):
        missing = self.zlib_eapi.SESSION_FILE.parent / "absent.json"
        missing.unlink(missing_ok=True)

        async def refuse():  # no creds configured -> CLI login refuses
            raise ZlibUnavailable("Z-Library login needs creds")

        with mock.patch("app.zlib_eapi.SESSION_FILE", missing):
            with mock.patch.object(self.zlib_eapi.zlib, "_ensure_session", refuse):
                lib = run(self.zlib_eapi.library())
        self.assertEqual(lib, {"available": False})
        self.assertNotIn("library", self.zlib_eapi._winners)  # retryable: NOT cached


if __name__ == "__main__":
    unittest.main(verbosity=2)

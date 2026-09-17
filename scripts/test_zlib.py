#!/usr/bin/env python3
"""Unit tests for the Z-Library adapter (CLI backend) — no network, no creds.

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

from app.zlib_client import Zlib, ZlibUnavailable


def run(coro):
    return asyncio.run(coro)


class ZlibTests(unittest.TestCase):
    def setUp(self):
        # keep the gate open and the real CLI out of the picture
        env = mock.patch.dict(os.environ, {"ZLIB_EMAIL": "e@x", "ZLIB_PASSWORD": "p"})
        env.start()
        self.addCleanup(env.stop)
        which = mock.patch("app.zlib_client.shutil.which", return_value="/usr/bin/zlib")
        which.start()
        self.addCleanup(which.stop)

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

    def test_search_cli_failure_maps_to_503(self):
        async def fake_run(self, *args, **k):
            return 1, "", "mirror said no"

        with mock.patch.object(Zlib, "_run", fake_run):
            with self.assertRaises(ZlibUnavailable):
                run(Zlib().search("q"))

    def test_limits_parses_json(self):
        async def fake_run(self, *args, **k):
            return 0, json.dumps({"daily_amount": 1, "daily_allowed": 10, "daily_remaining": 9}), ""

        with mock.patch.object(Zlib, "_run", fake_run):
            self.assertEqual(run(Zlib().limits())["daily_remaining"], 9)

    def test_download_reads_file_and_cleans_up(self):
        async def fake_run(self, *args, **k):
            if args[0] == "download":
                d = pathlib.Path(args[args.index("--dir") + 1])
                (d / "Some Book.pdf").write_bytes(b"data")
                return 0, "Saved", ""
            raise AssertionError(f"unexpected command {args}")

        with mock.patch.object(Zlib, "_run", fake_run):
            data, meta = run(Zlib().download("1:ab"))
        self.assertEqual(data, b"data")
        self.assertEqual(meta["_filename"], "Some Book.pdf")

    def test_download_failure_maps_to_503(self):
        async def fake_run(self, *args, **k):
            if args[0] == "download":
                d = pathlib.Path(args[args.index("--dir") + 1])
                return 1, "", "quota exceeded"
            raise AssertionError(f"unexpected command {args}")

        with mock.patch.object(Zlib, "_run", fake_run):
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

        real_home = pathlib.Path.home()
        fake_config = pathlib.Path(tempfile.gettempdir()) / "zlib-test-home"
        (fake_config / ".config" / "zlib").mkdir(parents=True, exist_ok=True)
        (fake_config / ".config" / "zlib" / "session.json").write_text("{}")
        with mock.patch("pathlib.Path.home", return_value=fake_config):
            with mock.patch.object(Zlib, "_run", fake_run):
                run(Zlib()._ensure_session())
        self.assertNotIn("login", calls)
        (fake_config / ".config" / "zlib" / "session.json").unlink(missing_ok=True)
        self.assertEqual(real_home.exists(), True)




if __name__ == "__main__":
    unittest.main(verbosity=2)

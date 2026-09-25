#!/usr/bin/env python3
"""Unit tests for the Z-Library account pool (rotation, ledger, per-account
CLI sessions) — no network, no real CLI, isolated temp DB.

Run: .venv/bin/python scripts/test_zlib_accounts.py
"""
import asyncio
import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

# isolated DB so test rows never touch a real data dir
_TMP = pathlib.Path(tempfile.mkdtemp(prefix="bookplate-zacc-test-"))
import app.db as _db  # noqa: E402

_db.DATA_DIR = _TMP
_db.DB_PATH = _TMP / "ebook.db"
import os  # noqa: E402

os.environ.setdefault("BOOKPLATE_ADMIN_PASS", "x")  # keep db.init() bootstrap quiet
_db.init()
from app import settings as _settings  # noqa: E402

from app import zlib_accounts  # noqa: E402
from app.zlib_client import ZlibConfigError, ZlibUnavailable, with_account  # noqa: E402


def run(coro):
    return asyncio.run(coro)


class Base(unittest.TestCase):
    def setUp(self):
        which = mock.patch("app.zlib_client.shutil.which", return_value="/usr/bin/zlib")
        which.start()
        self.addCleanup(which.stop)
        zlib_accounts._QUOTA.clear()
        zlib_accounts._RR["next"] = 0
        self.addCleanup(self._wipe_accounts)

    def _wipe_accounts(self):
        import app.db as db
        with db.conn() as con:
            con.execute("DELETE FROM zlib_usage")
            con.execute("DELETE FROM zlib_accounts")
        _settings.set("zlib.email", "")
        _settings.set("zlib.password", "")
        _settings.set("zlib.domain", "")

    def _acc(self, email="a@x", label="", domain="https://z-lib.gd", enabled=1):
        return zlib_accounts.create(label, email, "pw", domain) if enabled else (
            (lambda a: (zlib_accounts.update(a["id"], enabled=0), a)[1])(
                zlib_accounts.create(label, email, "pw", domain)))


class SeedTests(Base):
    def test_seed_from_legacy_settings(self):
        _settings.set("zlib.email", "legacy@x")
        _settings.set("zlib.password", "pw")
        _settings.set("zlib.domain", "https://z-lib.gd")
        # re-run the migration body directly (init() already ran once)
        import sqlite3
        with _db.conn() as con:
            _db._seed_zlib_accounts(con)
        accs = zlib_accounts.accounts()
        self.assertEqual(len(accs), 1)
        self.assertEqual(accs[0]["email"], "legacy@x")
        # second run must not duplicate
        with _db.conn() as con:
            _db._seed_zlib_accounts(con)
        self.assertEqual(len(zlib_accounts.accounts()), 1)

    def test_configured_needs_enabled_account(self):
        self.assertFalse(zlib_accounts.configured())
        a = self._acc()
        self.assertTrue(zlib_accounts.configured())
        zlib_accounts.update(a["id"], enabled=0)
        self.assertFalse(zlib_accounts.configured())


class RotationTests(Base):
    def test_pick_next_round_robins(self):
        a, b = self._acc("a@x"), self._acc("b@x")
        picks = [zlib_accounts.pick_next()["id"], zlib_accounts.pick_next()["id"],
                 zlib_accounts.pick_next()["id"]]
        self.assertEqual(picks, [a["id"], b["id"], a["id"]])

    def test_pick_skips_exhausted_and_disabled(self):
        a, b, c = self._acc("a@x"), self._acc("b@x"), self._acc("c@x")
        zlib_accounts.update(c["id"], enabled=0)
        zlib_accounts.snapshot(b["id"], {"daily_allowed": 10, "daily_remaining": 0})
        picks = {zlib_accounts.pick_next()["id"], zlib_accounts.pick_next()["id"]}
        self.assertEqual(picks, {a["id"]})

    def test_pick_excludes_tried(self):
        a, b = self._acc("a@x"), self._acc("b@x")
        self.assertEqual(zlib_accounts.pick_next(exclude={a["id"]})["id"], b["id"])

    def test_pick_none_when_all_dry(self):
        a = self._acc()
        zlib_accounts.snapshot(a["id"], {"daily_allowed": 10, "daily_remaining": 0})
        self.assertIsNone(zlib_accounts.pick_next())


class SnapshotLedgerTests(Base):
    def test_snapshot_first_and_exhausted(self):
        a = self._acc()
        zlib_accounts.snapshot(a["id"], {"daily_allowed": 10, "daily_remaining": 10})
        zlib_accounts.snapshot(a["id"], {"daily_allowed": 10, "daily_remaining": 0})
        events = [r["event"] for r in zlib_accounts.usage()]
        self.assertEqual(sorted(events), ["exhausted", "snapshot"])
        self.assertEqual(zlib_accounts.cached_quota(a["id"])["remaining"], 0)

    def test_reset_on_day_roll_and_remaining_up(self):
        a = self._acc()
        zlib_accounts.snapshot(a["id"], {"daily_allowed": 10, "daily_remaining": 0})
        zlib_accounts.snapshot(a["id"], {"daily_allowed": 10, "daily_remaining": 10})
        events = [r["event"] for r in zlib_accounts.usage()]
        self.assertIn("reset", events)

    def test_pool_exhausted_earliest_reset(self):
        a, b = self._acc("a@x"), self._acc("b@x")
        zlib_accounts.snapshot(a["id"], {"daily_allowed": 10, "daily_remaining": 0,
                                         "daily_reset": "23:59"})
        zlib_accounts.snapshot(b["id"], {"daily_allowed": 10, "daily_remaining": 0})
        reset = zlib_accounts.pool_exhausted()
        self.assertTrue(reset)
        # one account with quota -> not exhausted
        zlib_accounts.snapshot(a["id"], {"daily_allowed": 10, "daily_remaining": 3})
        self.assertIsNone(zlib_accounts.pool_exhausted())

    def test_mask_email_variants(self):
        # long name -> 2 chars + ellipsis; short name kept whole + ellipsis;
        # no host -> untouched
        self.assertEqual(zlib_accounts._mask_email("someone@example.com"), "so\u2026@example.com")
        self.assertEqual(zlib_accounts._mask_email("ab@x.io"), "ab\u2026@x.io")
        self.assertEqual(zlib_accounts._mask_email("no-at-sign"), "no-at-sign")

    def test_reset_at_fallback_and_parsed(self):
        import re
        self.assertRegex(zlib_accounts.reset_at(""), r"\d{2}:00:00$")
        m = zlib_accounts.reset_at("05:30")
        self.assertTrue(m.endswith("05:30:00"))


class WorkerRotationTests(Base):
    """The full _run_job pool path with the CLI seam stubbed."""

    def setUp(self):
        super().setUp()
        import app.main as main
        self.main = main
        self.data, self.meta = b"PDF", {"_filename": "x.pdf"}

    def _job(self, jid=1):
        return {"id": jid, "zlib_id": "42", "title": "T", "authors": "", "cover_url": "",
                "ext": "pdf", "size_text": "", "source": "zlibrary", "attempts": 0,
                "user_id": 1}

    def _ctx(self, acc):
        return {"id": acc["id"], "dir": str(zlib_accounts.account_dir(acc["id"])),
                "domain": acc["domain"], "creds": (acc["email"], acc["password"])}

    def test_rotates_to_second_account_when_first_dry(self):
        a, b = self._acc("a@x"), self._acc("b@x")
        # a is already known-dry from cache; b must be picked and downloaded
        zlib_accounts.snapshot(a["id"], {"daily_allowed": 10, "daily_remaining": 0})

        async def fake_limits():
            return {"daily_amount": 10, "daily_allowed": 10, "daily_remaining": 5}

        async def fake_download(book_id, on_progress=None, expected_size=None):
            return self.data, self.meta

        with mock.patch.object(self.main.zlib, "limits", fake_limits), \
             mock.patch.object(self.main.zlib, "download", fake_download), \
             mock.patch.object(self.main, "_job_update"):
            data, meta = run(self.main._zlib_download(self._job(), 1))
        self.assertEqual(data, b"PDF")
        events = [(r["account_id"], r["event"]) for r in zlib_accounts.usage()]
        self.assertIn((a["id"], "exhausted"), events)
        self.assertIn((b["id"], "download_ok"), events)

    def test_all_dry_waits_for_earliest_reset(self):
        a, b = self._acc("a@x"), self._acc("b@x")

        async def fake_limits():
            return {"daily_amount": 10, "daily_allowed": 10, "daily_remaining": 0}

        updates = []
        with mock.patch.object(self.main.zlib, "limits", fake_limits), \
             mock.patch.object(self.main, "_job_update",
                               lambda jid, **kw: updates.append((jid, kw))):
            with self.assertRaises(self.main._StopJob):
                run(self.main._zlib_download(self._job(), 1))
        st = [kw for _, kw in updates if "status" in kw][-1]
        self.assertEqual(st["status"], "waiting_quota")
        self.assertTrue(st["next_attempt_at"])  # scheduled at pool-wide earliest reset
        events = [r["event"] for r in zlib_accounts.usage()]
        self.assertIn("cooldown", events)

    def test_cli_no_file_link_rotates(self):
        # REAL dry-account CLI error (verified live): no quota words, but the
        # account is done for the day — must rotate to the next account
        import app.zlib_accounts as za_mod
        a, b = self._acc("a@x"), self._acc("b@x")
        self.assertIn("no file link", za_mod.QUOTA_ERR.pattern)
        self.assertTrue(za_mod.QUOTA_ERR.search(
            "Z-Library download failed: zlibrary: download failed: EAPI returned no file link"))

    def test_transient_error_does_not_rotate(self):
        a = self._acc()

        async def fake_limits():
            return {"daily_amount": 10, "daily_allowed": 10, "daily_remaining": 5}

        async def boom(book_id, on_progress=None, expected_size=None):
            raise ZlibUnavailable("connection reset by peer")

        with mock.patch.object(self.main.zlib, "limits", fake_limits), \
             mock.patch.object(self.main.zlib, "download", boom), \
             mock.patch.object(self.main, "_job_update"):
            with self.assertRaises(ZlibUnavailable):
                run(self.main._zlib_download(self._job(), 1))
        events = [r["event"] for r in zlib_accounts.usage()]
        self.assertIn("download_fail", events)

    def test_legacy_path_when_no_pool(self):
        self.assertFalse(zlib_accounts.configured())

        async def fake_limits():
            return {"daily_amount": 10, "daily_allowed": 10, "daily_remaining": 9}

        async def fake_download(book_id, on_progress=None, expected_size=None):
            return b"PDF", {"_filename": "x.pdf"}

        with mock.patch.object(self.main.zlib, "limits", fake_limits), \
             mock.patch.object(self.main.zlib, "download", fake_download), \
             mock.patch.object(self.main, "_job_update"):
            data, _ = run(self.main._zlib_download(self._job(), 1))
        self.assertEqual(data, b"PDF")
        self.assertEqual(zlib_accounts.usage(), [])  # nothing ledgered in legacy mode

    def test_legacy_waits_quota_when_dry(self):
        self.assertFalse(zlib_accounts.configured())

        async def fake_limits():
            return {"daily_amount": 10, "daily_allowed": 10, "daily_remaining": 0}

        updates = []
        with mock.patch.object(self.main.zlib, "limits", fake_limits), \
             mock.patch.object(self.main, "_job_update",
                               lambda jid, **kw: updates.append((jid, kw))):
            with self.assertRaises(self.main._StopJob):
                run(self.main._zlib_download(self._job(), 1))
        st = [kw for _, kw in updates if "status" in kw][-1]
        self.assertEqual(st["status"], "waiting_quota")


class SessionIsolationTests(Base):
    """with_account + _spawn must give each account its own CLI environment."""

    def test_spawn_env_is_per_account(self):
        captured = {}

        class _FakeProc:
            returncode = 0
            async def communicate(self):
                return b"{}", b""

        async def fake_exec(cmd, *args, stdout=None, stderr=None, env=None):
            captured["cmd"], captured["args"], captured["env"] = cmd, args, env
            return _FakeProc()

        a = self._acc("a@x", domain="https://acc-a.example")
        ctx = {"id": a["id"], "dir": "/tmp/acc-a", "domain": "https://acc-a.example",
               "creds": ("a@x", "pw")}
        with mock.patch("app.zlib_client.os.environ", {"PATH": "/usr/bin"}), \
             mock.patch("asyncio.create_subprocess_exec", fake_exec):
            from app import zlib_client
            run(with_account(ctx, zlib_client.zlib._run, "search", "q"))
        self.assertEqual(captured["cmd"], "zlib")
        self.assertIn("search", captured["args"])
        self.assertEqual(captured["env"]["HOME"], "/tmp/acc-a")
        self.assertEqual(captured["env"]["XDG_CONFIG_HOME"], "/tmp/acc-a/.config")
        self.assertEqual(captured["env"]["ZLIB_DOMAIN"], "https://acc-a.example")
        self.assertEqual(captured["env"]["PATH"], "/usr/bin")  # rest of host env kept

    def test_cfg_dir_follows_active_account(self):
        from app.zlib_client import _cfg_dir
        self.assertEqual(_cfg_dir(), pathlib.Path.home() / ".config" / "zlib")
        import app.zlib_client as zc
        ctx = {"id": 9, "dir": "/tmp/acc-9", "domain": "d", "creds": ("e", "p")}
        prev = zc._active
        zc._active = ctx
        try:
            self.assertEqual(_cfg_dir(), pathlib.Path("/tmp/acc-9/.config/zlib"))
        finally:
            zc._active = prev

    def test_account_dir_layout(self):
        a = self._acc()
        d = zlib_accounts.account_dir(a["id"])
        self.assertEqual(d, _db.DATA_DIR / "zlib_accounts" / str(a["id"]))

    def test_eapi_session_per_account(self):
        from app import zlib_eapi
        a = self._acc("e@x")
        d = zlib_accounts.account_dir(a["id"])
        cfg = d / ".config" / "zlib"
        cfg.mkdir(parents=True, exist_ok=True)
        (cfg / "session.json").write_text(json.dumps(
            {"cookies": {"remix_userid": "1"}, "domain": "https://acc.example"}))
        s = zlib_eapi._session(a["id"])
        self.assertEqual(s["domain"], "https://acc.example")
        self.assertIsNone(zlib_eapi._session(a["id"] + 100))  # other account: none

    def test_delete_removes_row_usage_and_dir(self):
        a = self._acc("gone@x")
        d = zlib_accounts.account_dir(a["id"])
        (d / ".config" / "zlib").mkdir(parents=True, exist_ok=True)
        zlib_accounts.record_event(a["id"], "snapshot",
                                   {"daily_allowed": 10, "daily_remaining": 5})
        zlib_accounts.delete(a["id"])
        self.assertIsNone(zlib_accounts.get(a["id"]))
        self.assertFalse(d.exists())
        self.assertEqual([u for u in zlib_accounts.usage() if u["account_id"] == a["id"]], [])


class CrudEndpointTests(Base):
    """Account CRUD + masking via the API layer (no HTTP server)."""

    def setUp(self):
        super().setUp()
        import app.main as main
        self.main = main

    def test_create_and_mask(self):
        admin = {"role": "admin"}
        req = self.main.ZlibAccountReq(email="u@x", password="secret", label="L",
                                       domain="https://z-lib.gd")
        # zlib CLI absent on CI -> create() skips the verify probe but still records
        with mock.patch("app.zlib_client.shutil.which", return_value=None):
            out = run(self.main.admin_zlib_account_add(req, admin=admin))
        accs = self.main.admin_zlib_accounts(admin=admin)  # sync endpoint
        self.assertEqual(len(accs), 1)
        a = accs[0]
        self.assertNotIn("password", a)  # secret never leaves the server
        self.assertEqual(a["email_masked"], "u…@x")
        self.assertEqual(a["label"], "L")

    def test_duplicate_email_409(self):
        self._acc("dup@x")
        req = self.main.ZlibAccountReq(email="dup@x", password="p")
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as cm:
            run(self.main.admin_zlib_account_add(req, admin={"role": "admin"}))
        self.assertEqual(cm.exception.status_code, 409)

    def test_patch_enabled_and_domain(self):
        a = self._acc()
        run(self.main.admin_zlib_account_patch(
            a["id"], self.main.ZlibAccountPatch(enabled=False, domain="https://new.example"),
            admin={"role": "admin"}))
        row = zlib_accounts.get(a["id"])
        self.assertEqual(row["enabled"], 0)
        self.assertEqual(row["domain"], "https://new.example")

    def test_delete_endpoint(self):
        a = self._acc()
        self.main.admin_zlib_account_delete(a["id"], admin={"role": "admin"})
        self.assertIsNone(zlib_accounts.get(a["id"]))

    def test_verify_records_ledger_and_clears_error(self):
        a = self._acc()
        zlib_accounts.note_failure(a["id"], "old failure")

        async def fake_limits_ctx(ctx):
            return {"daily_amount": 10, "daily_allowed": 10, "daily_remaining": 7}

        with mock.patch.object(self.main, "_account_limits", fake_limits_ctx):
            out = run(self.main.admin_zlib_account_verify(a["id"], admin={"role": "admin"}))
        self.assertEqual(out["daily_remaining"], 7)
        self.assertEqual(zlib_accounts.get(a["id"])["last_error"], "")

    def test_verify_failure_records_error(self):
        a = self._acc()

        async def boom(ctx):
            raise ZlibUnavailable("login failed: Incorrect email or password")

        with mock.patch.object(self.main, "_account_limits", boom):
            from fastapi import HTTPException
            with self.assertRaises(HTTPException) as cm:
                run(self.main.admin_zlib_account_verify(a["id"], admin={"role": "admin"}))
        self.assertEqual(cm.exception.status_code, 503)
        self.assertIn("Incorrect email", zlib_accounts.get(a["id"])["last_error"])


class AggregateLimitsTests(Base):
    def setUp(self):
        super().setUp()
        import app.main as main
        self.main = main

    def test_aggregate_shape_and_masking(self):
        a, b = self._acc("one@x", label="Primary"), self._acc("two@x")
        zlib_accounts.snapshot(a["id"], {"daily_allowed": 10, "daily_remaining": 6})
        zlib_accounts.snapshot(b["id"], {"daily_allowed": 10, "daily_remaining": 1})
        out = run(self.main.zlib_limits(user={"id": 1}))
        self.assertEqual(out["total_remaining"], 7)
        self.assertEqual(out["total_allowed"], 20)
        self.assertEqual(out["accounts"][0]["email"], "on…@x")
        self.assertNotIn("password", out["accounts"][0])

    def test_no_pool_falls_back_to_legacy(self):
        async def legacy():
            return {"daily_amount": 10, "daily_allowed": 10, "daily_remaining": 9}
        with mock.patch.object(self.main.zlib, "limits", legacy):
            out = run(self.main.zlib_limits(user={"id": 1}))
        self.assertEqual(out["daily_remaining"], 9)


if __name__ == "__main__":
    unittest.main(verbosity=2)

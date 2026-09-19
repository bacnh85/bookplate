#!/usr/bin/env python3
"""Offline tests for app-managed settings + roles/approval + admin endpoints.

No TestClient and no live server (the lifespan worker and dev DB stay out of
the picture): patch db paths BEFORE importing app.main (db.init runs at
import), then call route functions directly with fake user dicts.

Run: .venv/bin/python scripts/test_admin.py
"""
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

# isolate the DB before anything imports app.*
_TMP = pathlib.Path(tempfile.mkdtemp(prefix="bookplate-admin-test-"))
import app.db as db  # noqa: E402

db.DATA_DIR = _TMP
db.DB_PATH = _TMP / "ebook.db"

from fastapi import HTTPException, Response  # noqa: E402

from app import auth, main, settings  # noqa: E402  (runs db.init on the temp dir)
from app.auth import hash_password, verify_password  # noqa: E402


def admin_row(uid=1):
    with db.conn() as con:
        r = con.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    return r


def mk_user(email, role="user", status="active"):
    with db.conn() as con:
        cur = con.execute(
            "INSERT INTO users(email, password_hash, role, status) VALUES(?,?,?,?)",
            (email, hash_password("secret6"), role, status))
        return cur.lastrowid


class SettingsTests(unittest.TestCase):
    def tearDown(self):
        with db.conn() as con:
            con.execute("DELETE FROM settings")

    def test_db_value_wins_over_env(self):
        settings.set("zlib.email", "db@x")
        with mock.patch.dict(os.environ, {"ZLIB_EMAIL": "env@x"}):
            self.assertEqual(settings.get("zlib.email"), "db@x")

    def test_env_fallback_when_db_empty(self):
        with mock.patch.dict(os.environ, {"ZLIB_EMAIL": "env@x"}):
            self.assertEqual(settings.get("zlib.email"), "env@x")  # key absent
            settings.set("zlib.email", "")  # cleared in UI -> back to env
            self.assertEqual(settings.get("zlib.email"), "env@x")

    def test_default_when_neither(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(settings.get("annas.base_url", "dft"), "dft")

    def test_registration_has_no_env_fallback(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(settings.get("registration", "approval"), "approval")


class RegisterApprovalTests(unittest.TestCase):
    def setUp(self):
        with db.conn() as con:
            con.execute("DELETE FROM users")
        settings.set("registration", "approval")

    def tearDown(self):
        with db.conn() as con:
            con.execute("DELETE FROM users")
            con.execute("DELETE FROM settings")

    def test_first_user_is_active_admin(self):
        out = main.register(main.Credentials(email="a@x", password="hunter22"), Response())
        self.assertEqual(out["status"], "active")
        self.assertIn("token", out)
        a = admin_row(1)
        self.assertEqual(a["role"], "admin")
        self.assertEqual(a["status"], "active")

    def test_second_user_pending_without_token(self):
        main.register(main.Credentials(email="a@x", password="hunter22"), Response())
        out = main.register(main.Credentials(email="b@x", password="hunter22"), Response())
        self.assertEqual(out, {"status": "pending"})
        self.assertEqual(admin_row(2)["status"], "pending")

    def test_pending_cannot_login_then_approved_can(self):
        main.register(main.Credentials(email="a@x", password="hunter22"), Response())
        main.register(main.Credentials(email="b@x", password="hunter22"), Response())
        with self.assertRaises(HTTPException) as cm:
            main.login(main.Credentials(email="b@x", password="hunter22"), Response())
        self.assertEqual(cm.exception.status_code, 403)
        main._set_status(2, "active")
        out = main.login(main.Credentials(email="b@x", password="hunter22"), Response())
        self.assertIn("token", out)

    def test_disabled_cannot_login(self):
        main.register(main.Credentials(email="a@x", password="hunter22"), Response())
        mk_user("d@x")
        main._set_status(2, "disabled")
        with self.assertRaises(HTTPException) as cm:
            main.login(main.Credentials(email="d@x", password="secret6"), Response())
        self.assertEqual(cm.exception.status_code, 403)

    def test_closed_registration_rejects(self):
        settings.set("registration", "closed")
        main.register(main.Credentials(email="a@x", password="hunter22"), Response())
        with self.assertRaises(HTTPException) as cm:
            main.register(main.Credentials(email="b@x", password="hunter22"), Response())
        self.assertEqual(cm.exception.status_code, 403)

    def test_first_user_bypasses_closed_registration(self):
        settings.set("registration", "closed")
        out = main.register(main.Credentials(email="a@x", password="hunter22"), Response())
        self.assertEqual(out["status"], "active")  # bootstrap must always be possible

    def test_concurrent_first_registrations_yield_one_admin(self):
        import threading
        results = {}
        barrier = threading.Barrier(2)

        def go(email):
            barrier.wait()  # maximize the chance both see an empty table
            try:
                results[email] = main.register(
                    main.Credentials(email=email, password="hunter22"), Response())
            except HTTPException as e:
                results[email] = {"status": f"http-{e.status_code}"}

        threads = [threading.Thread(target=go, args=(e,))
                   for e in ("race1@x", "race2@x")]
        for t in threads:
            t.start()
        for t in threads:
            t.join(15)
        with db.conn() as con:
            admins = con.execute(
                "SELECT COUNT(*) c FROM users WHERE role='admin' AND status='active'").fetchone()["c"]
        self.assertEqual(admins, 1, str(results))
        # the loser fell back to the normal approval path
        statuses = sorted(u["status"] for u in
                          results.values() if u.get("status"))
        self.assertIn("pending", statuses)


class AdminRouteTests(unittest.TestCase):
    def setUp(self):
        with db.conn() as con:
            con.execute("DELETE FROM users")
        self.admin = admin_row(mk_user("admin@x", role="admin"))
        self.plain = admin_row(mk_user("plain@x"))

    def tearDown(self):
        with db.conn() as con:
            con.execute("DELETE FROM users")
            con.execute("DELETE FROM settings")

    def test_non_admin_gets_403(self):
        # route functions called directly skip Depends() — assert the guard itself
        with self.assertRaises(HTTPException) as cm:
            auth.admin_required(self.plain)
        self.assertEqual(cm.exception.status_code, 403)

    def test_create_user_and_reset_password(self):
        out = main.admin_create_user(main.AdminUserReq(
            email="new@x", password="hunter22", role="user"), admin=self.admin)
        self.assertEqual(out["status"], "active")
        main.admin_reset_password(out["id"], main.PasswordReq(password="newpass1"),
                                  admin=self.admin)
        self.assertTrue(verify_password("newpass1", admin_row(out["id"])["password_hash"]))
        with self.assertRaises(HTTPException):
            main.admin_reset_password(out["id"], main.PasswordReq(password="abc"),
                                      admin=self.admin)  # too short

    def test_cannot_demote_or_disable_last_admin(self):
        uid = self.admin["id"]
        with self.assertRaises(HTTPException) as cm:
            main.admin_set_role(uid, main.SetRoleReq(role="user"), admin=self.admin)
        self.assertEqual(cm.exception.status_code, 409)
        with self.assertRaises(HTTPException) as cm:
            main._set_status(uid, "disabled")
        self.assertEqual(cm.exception.status_code, 409)
        # with a second active admin it works
        second = mk_user("admin2@x", role="admin")
        main.admin_set_role(uid, main.SetRoleReq(role="user"), admin=self.admin)
        self.assertEqual(admin_row(uid)["role"], "user")
        main.admin_enable(second, admin=self.admin)  # status transitions on an admin OK

    def test_approve_flow(self):
        pending = mk_user("p@x", status="pending")
        main.admin_approve(pending, admin=self.admin)
        self.assertEqual(admin_row(pending)["status"], "active")


class AdminSettingsApiTests(unittest.TestCase):
    def setUp(self):
        with db.conn() as con:
            con.execute("DELETE FROM users")
            con.execute("DELETE FROM settings")
        self.admin = admin_row(mk_user("admin@x", role="admin"))

    def tearDown(self):
        with db.conn() as con:
            con.execute("DELETE FROM users")
            con.execute("DELETE FROM settings")

    def test_get_masks_secrets(self):
        settings.set("zlib.password", "supersecret")
        out = main.admin_get_settings(admin=self.admin)
        self.assertEqual(out["zlib.password"], {"set": True, "hint": "…cret"})
        self.assertNotIn("supersecret", str(out))
        self.assertEqual(out["registration"], "approval")

    def test_put_writes_and_validates(self):
        out = main.admin_put_settings(main.SettingsReq(values={
            "zlib.email": "z@x", "registration": "closed"}), admin=self.admin)
        self.assertEqual(out, {"ok": True})
        self.assertEqual(settings.get("zlib.email"), "z@x")
        self.assertEqual(settings.get("registration"), "closed")
        with self.assertRaises(HTTPException):
            main.admin_put_settings(main.SettingsReq(values={"nope.key": "v"}),
                                    admin=self.admin)
        with self.assertRaises(HTTPException):
            main.admin_put_settings(main.SettingsReq(values={"registration": "wild"}),
                                    admin=self.admin)

    def test_put_clear_reverts_to_env(self):
        settings.set("zlib.domain", "https://db.example")
        main.admin_put_settings(main.SettingsReq(values={"zlib.domain": ""}),
                                admin=self.admin)
        with mock.patch.dict(os.environ, {"ZLIB_DOMAIN": "https://env.example"}):
            self.assertEqual(settings.get("zlib.domain"), "https://env.example")


class TokenLockoutTests(unittest.TestCase):
    def setUp(self):
        with db.conn() as con:
            con.execute("DELETE FROM users")

    def tearDown(self):
        with db.conn() as con:
            con.execute("DELETE FROM users")

    @staticmethod
    def _request_with_token(token: str):
        from starlette.requests import Request
        return Request(scope={"type": "http", "headers": [
            (b"authorization", b"Bearer " + token.encode())]})

    def test_valid_token_rejected_after_disable(self):
        uid = mk_user("tok@x")
        req = self._request_with_token(auth.make_token(uid))
        self.assertEqual(auth.current_user(req)["id"], uid)  # active: works
        main._set_status(uid, "disabled")
        with self.assertRaises(HTTPException) as cm:
            auth.current_user(req)  # same valid JWT, now disabled
        self.assertEqual(cm.exception.status_code, 403)

    def test_valid_token_rejected_while_pending(self):
        uid = mk_user("pend@x", status="pending")
        req = self._request_with_token(auth.make_token(uid))
        with self.assertRaises(HTTPException) as cm:
            auth.current_user(req)
        self.assertEqual(cm.exception.status_code, 403)
        main.admin_approve(uid)
        self.assertEqual(auth.current_user(req)["id"], uid)  # approved: works


class SecretPathTests(unittest.TestCase):
    def test_secret_follows_data_dir_and_migrates(self):
        import pathlib, tempfile
        from app import auth
        legacy = pathlib.Path(tempfile.mkdtemp(prefix="bp-legacy-")) / ".secret"
        legacy.write_text("legacysecret")
        newdir = pathlib.Path(tempfile.mkdtemp(prefix="bp-newdata-"))
        new_secret = newdir / ".secret"
        with mock.patch.object(auth, "_LEGACY_SECRET", legacy), \
                mock.patch.object(auth, "_SECRET_FILE", new_secret):
            self.assertEqual(auth._secret(), "legacysecret")   # migrated from legacy path
            self.assertEqual(auth._secret(), "legacysecret")   # now read from new path
        self.assertTrue(new_secret.exists())
        self.assertEqual(oct(new_secret.stat().st_mode & 0o777), oct(0o600))


class AtLeastOneAdminTests(unittest.TestCase):
    def tearDown(self):
        with db.conn() as con:
            con.execute("DELETE FROM users")

    def test_migration_promotes_earliest_active_user(self):
        # the earliest account is disabled (e.g. selftest debris) and must be
        # skipped in favour of the earliest ACTIVE account
        with db.conn() as con:
            con.execute("DELETE FROM users")
        mk_user("old1@x", status="disabled")
        mk_user("old2@x")
        with mock.patch.dict(os.environ, {}, clear=True):
            db.init()
        with db.conn() as con:
            roles = {r["email"]: r["role"] for r in con.execute("SELECT email, role FROM users")}
        self.assertEqual(roles, {"old1@x": "user", "old2@x": "admin"})

    def test_admin_email_override_wins(self):
        with db.conn() as con:
            con.executescript("""
                DROP TABLE users;
                CREATE TABLE users(
                  id INTEGER PRIMARY KEY,
                  email TEXT UNIQUE NOT NULL,
                  password_hash TEXT NOT NULL,
                  created_at TEXT DEFAULT (datetime('now'))
                );
                INSERT INTO users(email, password_hash) VALUES
                  ('old1@x', 'h'), ('owner@x', 'h');
            """)
        with mock.patch.dict(os.environ, {"ADMIN_EMAIL": "owner@x"}, clear=True):
            db.init()
        with db.conn() as con:
            roles = {r["email"]: r["role"] for r in con.execute("SELECT email, role FROM users")}
        self.assertEqual(roles, {"old1@x": "admin", "owner@x": "admin"})

    def test_admin_email_skips_disabled_accounts(self):
        with db.conn() as con:
            con.execute("DELETE FROM users")
        mk_user("owner@x", status="disabled")
        mk_user("other@x")
        with mock.patch.dict(os.environ, {"ADMIN_EMAIL": "owner@x"}, clear=True):
            db.init()
        with db.conn() as con:
            roles = {r["email"]: (r["role"], r["status"])
                     for r in con.execute("SELECT email, role, status FROM users")}
        # disabled owner is never revived; earliest ACTIVE user got promoted instead
        self.assertEqual(roles, {"owner@x": ("user", "disabled"),
                                 "other@x": ("admin", "active")})

    def test_demotion_survives_reinit_without_admin_email(self):
        with db.conn() as con:
            con.execute("DELETE FROM users")
        admin_id = mk_user("boss@x", role="admin")
        mk_user("peer@x", role="admin")
        main.admin_set_role(admin_id, main.SetRoleReq(role="user"), admin=admin_row(admin_id))
        with mock.patch.dict(os.environ, {}, clear=True):
            db.init()  # must not resurrect the demoted account
        with db.conn() as con:
            roles = {r["email"]: r["role"] for r in con.execute("SELECT email, role FROM users")}
        self.assertEqual(roles, {"boss@x": "user", "peer@x": "admin"})

    def test_no_promotion_when_admin_exists(self):
        with db.conn() as con:
            con.execute("DELETE FROM users")
        mk_user("a@x", role="admin")  # separate connection: outer DELETE held the write lock
        with mock.patch.dict(os.environ, {}, clear=True):
            db.init()  # idempotent: must not touch anything
        with db.conn() as con:
            n = con.execute("SELECT COUNT(*) c FROM users WHERE role='admin'").fetchone()["c"]
        self.assertEqual(n, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)

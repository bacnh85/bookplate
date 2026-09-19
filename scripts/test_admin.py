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
# deterministic bootstrap (db.init runs at import of app.main below; no password file)
os.environ["BOOKPLATE_ADMIN_USER"] = "rootadmin"
os.environ["BOOKPLATE_ADMIN_PASS"] = "bootpass1"

from fastapi import HTTPException, Response  # noqa: E402

from app import auth, main, settings  # noqa: E402  (runs db.init on the temp dir)
from app.auth import hash_password, verify_password  # noqa: E402


def admin_row(uid=1):
    with db.conn() as con:
        r = con.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    return r


def mk_user(username, role="user", status="active"):
    with db.conn() as con:
        cur = con.execute(
            "INSERT INTO users(username, password_hash, role, status) VALUES(?,?,?,?)",
            (username, hash_password("secret6"), role, status))
        return cur.lastrowid


class SettingsTests(unittest.TestCase):
    def tearDown(self):
        with db.conn() as con:
            con.execute("DELETE FROM settings")

    def test_set_then_get(self):
        settings.set("zlib.email", "db@x")
        self.assertEqual(settings.get("zlib.email"), "db@x")

    def test_empty_value_falls_back_to_default(self):
        settings.set("zlib.email", "")  # cleared in UI
        self.assertEqual(settings.get("zlib.email", "dft"), "dft")

    def test_default_when_missing(self):
        self.assertEqual(settings.get("annas.base_url", "dft"), "dft")

    def test_registration_default_approval(self):
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

    def test_register_always_pending_no_token(self):
        out = main.register(main.Credentials(username="a@x", password="hunter22"))
        self.assertEqual(out, {"status": "pending"})
        self.assertEqual(admin_row(1)["role"], "user")
        self.assertEqual(admin_row(1)["status"], "pending")

    def test_duplicate_username_409(self):
        main.register(main.Credentials(username="a@x", password="hunter22"))
        with self.assertRaises(HTTPException) as cm:
            main.register(main.Credentials(username="a@x", password="hunter22"))
        self.assertEqual(cm.exception.status_code, 409)

    def test_pending_cannot_login_then_approved_can(self):
        main.register(main.Credentials(username="b@x", password="hunter22"))
        with self.assertRaises(HTTPException) as cm:
            main.login(main.Credentials(username="b@x", password="hunter22"), Response())
        self.assertEqual(cm.exception.status_code, 403)
        main._set_status(1, "active")
        out = main.login(main.Credentials(username="b@x", password="hunter22"), Response())
        self.assertIn("token", out)

    def test_login_is_case_insensitive(self):
        main.register(main.Credentials(username="Bob@x", password="hunter22"))
        main._set_status(1, "active")
        out = main.login(main.Credentials(username="BOB@X", password="hunter22"), Response())
        self.assertIn("token", out)

    def test_disabled_cannot_login(self):
        uid = mk_user("d@x")
        main._set_status(uid, "disabled")
        with self.assertRaises(HTTPException) as cm:
            main.login(main.Credentials(username="d@x", password="secret6"), Response())
        self.assertEqual(cm.exception.status_code, 403)

    def test_closed_registration_rejects(self):
        settings.set("registration", "closed")
        with self.assertRaises(HTTPException) as cm:
            main.register(main.Credentials(username="b@x", password="hunter22"))
        self.assertEqual(cm.exception.status_code, 403)


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
            username="new@x", password="hunter22", role="user"), admin=self.admin)
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

    def test_put_clear_empties_value(self):
        settings.set("zlib.domain", "https://db.example")
        main.admin_put_settings(main.SettingsReq(values={"zlib.domain": ""}),
                                admin=self.admin)
        self.assertEqual(settings.get("zlib.domain", "https://default.example"),
                         "https://default.example")


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
        db.init()
        with db.conn() as con:
            roles = {r["username"]: r["role"]
                     for r in con.execute("SELECT username, role FROM users")}
        self.assertEqual(roles, {"old1@x": "user", "old2@x": "admin"})

    def test_demotion_survives_reinit(self):
        with db.conn() as con:
            con.execute("DELETE FROM users")
        admin_id = mk_user("boss@x", role="admin")
        mk_user("peer@x", role="admin")
        main.admin_set_role(admin_id, main.SetRoleReq(role="user"), admin=admin_row(admin_id))
        db.init()  # must not resurrect the demoted account
        with db.conn() as con:
            roles = {r["username"]: r["role"]
                     for r in con.execute("SELECT username, role FROM users")}
        self.assertEqual(roles, {"boss@x": "user", "peer@x": "admin"})

    def test_no_promotion_when_admin_exists(self):
        with db.conn() as con:
            con.execute("DELETE FROM users")
        mk_user("a@x", role="admin")  # separate connection: outer DELETE held the write lock
        db.init()  # idempotent: must not touch anything
        with db.conn() as con:
            n = con.execute("SELECT COUNT(*) c FROM users WHERE role='admin'").fetchone()["c"]
        self.assertEqual(n, 1)


class BootstrapAdminTests(unittest.TestCase):
    def tearDown(self):
        with db.conn() as con:
            con.execute("DELETE FROM users")
        for k in ("BOOKPLATE_ADMIN_USER", "BOOKPLATE_ADMIN_PASS"):
            os.environ.pop(k, None)

    def test_bootstrap_with_env_pass(self):
        with db.conn() as con:
            con.execute("DELETE FROM users")
        with mock.patch.dict(os.environ, {"BOOKPLATE_ADMIN_USER": "Boss",
                                          "BOOKPLATE_ADMIN_PASS": "bootpass1"}):
            db.init()
        with db.conn() as con:
            row = con.execute("SELECT username, role, status FROM users").fetchone()
        self.assertEqual((row["username"], row["role"], row["status"]),
                         ("boss", "admin", "active"))
        self.assertFalse((db.DATA_DIR / "initial_admin_password").exists())

    def test_bootstrap_generated_password_file(self):
        with db.conn() as con:
            con.execute("DELETE FROM users")
        f = db.DATA_DIR / "initial_admin_password"
        f.unlink(missing_ok=True)
        with mock.patch.dict(os.environ, {}, clear=True):
            db.init()
        self.assertTrue(f.exists())
        self.assertEqual(oct(f.stat().st_mode & 0o777), oct(0o600))
        pw = f.read_text().strip()
        with db.conn() as con:
            row = con.execute("SELECT username, password_hash FROM users").fetchone()
        self.assertEqual(row["username"], "admin")
        self.assertTrue(verify_password(pw, row["password_hash"]))
        # idempotent: a re-init must not touch existing accounts or rewrite the file
        mtime = f.stat().st_mtime_ns
        with mock.patch.dict(os.environ, {}, clear=True):
            db.init()
        self.assertEqual(f.stat().st_mtime_ns, mtime)
        with db.conn() as con:
            n = con.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
        self.assertEqual(n, 1)
        f.unlink()

    def test_old_db_renames_email_column_no_bootstrap(self):
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
                  ('keeper@x', 'h'), ('boss@x', 'h');
            """)
        with mock.patch.dict(os.environ, {}, clear=True):
            db.init()  # renames the column; populated table → no bootstrap
        with db.conn() as con:
            rows = con.execute("SELECT username, role FROM users ORDER BY id").fetchall()
        self.assertEqual([dict(r) for r in rows],
                         [{"username": "keeper@x", "role": "admin"},
                          {"username": "boss@x", "role": "user"}])
        self.assertFalse((db.DATA_DIR / "initial_admin_password").exists())


class SeedLegacyEnvTests(unittest.TestCase):
    def tearDown(self):
        with db.conn() as con:
            con.execute("DELETE FROM settings")

    def test_seeds_never_configured_keys_from_legacy_env(self):
        with mock.patch.dict(os.environ, {"ZLIB_EMAIL": "env@x",
                                          "ANNAS_ARCHIVE_SECRET_KEY": "k5"}):
            with db.conn() as con:
                settings.seed_legacy_env(con)
        self.assertEqual(settings.get("zlib.email"), "env@x")
        self.assertEqual(settings.get("annas.secret_key"), "k5")

    def test_never_overwrites_existing_values(self):
        settings.set("zlib.email", "db@x")
        with mock.patch.dict(os.environ, {"ZLIB_EMAIL": "env@x"}):
            with db.conn() as con:
                settings.seed_legacy_env(con)
        self.assertEqual(settings.get("zlib.email"), "db@x")

    def test_respects_explicit_clear(self):
        with mock.patch.dict(os.environ, {"ZLIB_EMAIL": "env@x"}):
            with db.conn() as con:
                settings.seed_legacy_env(con)   # first boot after upgrade
            settings.set("zlib.email", "")     # admin clears it in the UI
            with db.conn() as con:
                settings.seed_legacy_env(con)   # a later boot must not resurrect env
        self.assertEqual(settings.get("zlib.email", ""), "")


class BasicAuthTests(unittest.TestCase):
    """OPDS Basic auth: same username normalization as the JSON login path —
    critical on migrated DBs whose username column lacks COLLATE NOCASE."""

    def tearDown(self):
        with db.conn() as con:
            con.execute("DELETE FROM users")

    @staticmethod
    def _basic_request(user, pw):
        import base64 as b64
        from starlette.requests import Request
        token = b64.b64encode(f"{user}:{pw}".encode()).decode()
        return Request(scope={"type": "http", "headers": [
            (b"authorization", b"Basic " + token.encode())]})

    def test_basic_auth_is_case_insensitive(self):
        uid = mk_user("alice@x.io")  # stored lowercase, like every app write path
        row = auth._user_from_basic(self._basic_request("Alice@X.IO", "secret6"))
        self.assertEqual(row["id"], uid)

    def test_basic_auth_rejects_pending_and_bad_password(self):
        mk_user("p@x", status="pending")
        with self.assertRaises(HTTPException) as cm:  # same lockout as JWT/cookie paths
            auth._user_from_basic(self._basic_request("p@x", "secret6"))
        self.assertEqual(cm.exception.status_code, 403)
        mk_user("q@x")
        self.assertIsNone(auth._user_from_basic(self._basic_request("q@x", "wrong")))


if __name__ == "__main__":
    unittest.main(verbosity=2)

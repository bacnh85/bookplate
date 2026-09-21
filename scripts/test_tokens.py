#!/usr/bin/env python3
"""Offline tests for per-user API tokens: CRUD, hash-only storage, and the
`bp_` bearer path through current_user (auth gating incl. pending users).

Run: .venv/bin/python scripts/test_tokens.py
"""
import json
import os
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

_TMP = pathlib.Path(tempfile.mkdtemp(prefix="bookplate-token-test-"))
import app.db as db  # noqa: E402

db.DATA_DIR = _TMP
db.DB_PATH = _TMP / "ebook.db"
os.environ["BOOKPLATE_ADMIN_USER"] = "rootadmin"
os.environ["BOOKPLATE_ADMIN_PASS"] = "bootpass1"

from fastapi import HTTPException  # noqa: E402
from starlette.requests import Request  # noqa: E402

from app import auth, db as _db, main, settings  # noqa: E402  (runs db.init)


def mk_user(username, status="active"):
    with db.conn() as con:
        cur = con.execute(
            "INSERT INTO users(username, password_hash, role, status) VALUES(?,?, 'user', ?)",
            (username, auth.hash_password("secret6"), status))
        return cur.lastrowid


def bearer_request(raw):
    return Request(scope={"type": "http", "headers": [
        (b"authorization", b"Bearer " + raw.encode())]})


class TokenTests(unittest.TestCase):
    def setUp(self):
        self.uid = mk_user("eve")

    def tearDown(self):
        with db.conn() as con:
            con.execute("DELETE FROM api_tokens")
            con.execute("DELETE FROM users WHERE id>1")

    def test_create_list_delete(self):
        out = main.token_create(main.TokenReq(label=" Claude "), user={"id": self.uid})
        self.assertTrue(out["token"].startswith("bp_"))
        toks = main.token_list(user={"id": self.uid})
        self.assertEqual(len(toks), 1)
        self.assertEqual(toks[0]["label"], "Claude")
        self.assertNotIn("token", toks[0])  # never listed
        main.token_delete(toks[0]["id"], user={"id": self.uid})
        self.assertEqual(main.token_list(user={"id": self.uid}), [])

    def test_hash_only_at_rest(self):
        out = main.token_create(main.TokenReq(label="x"), user={"id": self.uid})
        with db.conn() as con:
            row = con.execute("SELECT token_hash FROM api_tokens").fetchone()
        self.assertNotIn(out["token"], row["token_hash"])
        self.assertNotEqual(out["token"], row["token_hash"])
        import hashlib
        self.assertEqual(row["token_hash"],
                         hashlib.sha256(out["token"].encode()).hexdigest())

    def test_bearer_token_authenticates_as_owner(self):
        out = main.token_create(main.TokenReq(label="agent"), user={"id": self.uid})
        req = bearer_request(out["token"])
        user = auth.current_user(req)
        self.assertEqual(user["id"], self.uid)

    def test_revoked_token_rejected(self):
        out = main.token_create(main.TokenReq(label="agent"), user={"id": self.uid})
        main.token_delete(out["id"], user={"id": self.uid})
        with self.assertRaises(HTTPException) as cm:
            auth.current_user(bearer_request(out["token"]))
        self.assertEqual(cm.exception.status_code, 401)

    def test_garbage_token_rejected(self):
        with self.assertRaises(HTTPException) as cm:
            auth.current_user(bearer_request("bp_totally-made-up"))
        self.assertEqual(cm.exception.status_code, 401)

    def test_pending_user_token_locked_out(self):
        pending = mk_user("pending1", status="pending")
        out = main.token_create(main.TokenReq(label="t"), user={"id": pending})
        with self.assertRaises(HTTPException) as cm:
            auth.current_user(bearer_request(out["token"]))
        self.assertEqual(cm.exception.status_code, 403)

    def test_tokens_scoped_to_owner(self):
        other = mk_user("frank")
        out = main.token_create(main.TokenReq(label="mine"), user={"id": self.uid})
        with self.assertRaises(HTTPException) as cm:
            main.token_delete(out["id"], user={"id": other})
        self.assertEqual(cm.exception.status_code, 404)
        self.assertEqual(len(main.token_list(user={"id": other})), 0)


if __name__ == "__main__":
    unittest.main()

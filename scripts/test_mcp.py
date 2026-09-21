#!/usr/bin/env python3
"""MCP server smoke tests: real streamable-HTTP client against a live uvicorn
thread of app.main with the /mcp mount. Covers 401 without a token and a full
initialize → tools/list → tools/call round-trip with one.

Run: .venv/bin/python scripts/test_mcp.py
"""
import asyncio
import json
import os
import pathlib
import socket
import sys
import tempfile
import threading
import time
import unittest
import json

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

_TMP = pathlib.Path(tempfile.mkdtemp(prefix="bookplate-mcp-test-"))
os.environ["BOOKPLATE_DATA_DIR"] = str(_TMP)
os.environ["BOOKPLATE_ADMIN_USER"] = "rootadmin"
os.environ["BOOKPLATE_ADMIN_PASS"] = "bootpass1"

import app.db as db  # noqa: E402

db.DATA_DIR = _TMP
db.DB_PATH = _TMP / "ebook.db"
os.environ["BOOKPLATE_DATA_DIR"] = str(_TMP)  # storage dirs follow the DB

from app import auth, main  # noqa: E402  (runs db.init)
from app.auth import hash_password  # noqa: E402


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


PORT = free_port()
BASE = f"http://127.0.0.1:{PORT}"

# seed: a user with a book and a token, before the server starts
with db.conn() as con:
    cur = con.execute(
        "INSERT INTO users(username, password_hash, role, status) VALUES('mcpuser', ?, 'user', 'active')",
        (hash_password("secret6"),))
    uid = cur.lastrowid
    cur = con.execute(
        "INSERT INTO books(sha256, ext, size, title, authors, added_by) "
        "VALUES('mcpsHA1', 'epub', 10, 'MCP Test Book', 'A. Author', ?)", (uid,))
    bid = cur.lastrowid
    con.execute("INSERT INTO user_books(user_id, book_id) VALUES(?,?)", (uid, bid))
UID = uid  # module-level for tests
TOKEN = main.token_create(main.TokenReq(label="smoke"), user={"id": uid})["token"]

import uvicorn  # noqa: E402
config = uvicorn.Config(main.app, host="127.0.0.1", port=PORT, log_level="error")
server = uvicorn.Server(config)
threading.Thread(target=server.run, daemon=True).start()
for _ in range(50):
    try:
        import urllib.request
        urllib.request.urlopen(f"{BASE}/api/me", timeout=1)
        break
    except Exception:
        time.sleep(0.2)


class McpTests(unittest.TestCase):
    def test_search_library_capped_at_200(self):
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client
        with db.conn() as con:
            for i in range(210):
                cur = con.execute(
                    "INSERT INTO books(sha256, ext, size, title, added_by, created_at) "
                    "VALUES(?, 'epub', 1, ?, ?, datetime('now', ?))",
                    (f"cap{i:04d}", f"Cap Filler {i}", UID,
                     f"-{i} minutes"))
                con.execute("INSERT INTO user_books(user_id, book_id) VALUES(?,?)",
                            (uid, cur.lastrowid))

        async def run():
            async with streamablehttp_client(
                    f"{BASE}/mcp", headers={"Authorization": f"Bearer {TOKEN}"}) as (r, w, _):
                async with ClientSession(r, w) as s:
                    await s.initialize()
                    res = await s.call_tool("search_library", {"query": ""})
                    rows = (res.structuredContent or {}).get("result") or []
                    self.assertEqual(len(rows), 200)  # bounded, not the whole shelf
                    self.assertEqual(rows[0]["title"], "Cap Filler 0")  # newest first
        asyncio.run(run())

    def test_http_401_without_token(self):
        import urllib.error
        req = urllib.request.Request(f"{BASE}/mcp", data=b"{}",
                                     headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=5)
            self.fail("expected 401")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 401)

    def test_full_round_trip(self):
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        async def run():
            async with streamablehttp_client(
                    f"{BASE}/mcp", headers={"Authorization": f"Bearer {TOKEN}"}) as (r, w, _):
                async with ClientSession(r, w) as s:
                    await s.initialize()
                    tools = await s.list_tools()
                    names = {t.name for t in tools.tools}
                    self.assertTrue({"search_library", "get_book", "create_collection",
                                     "queue_book", "remetadata", "update_book"} <= names)
                    # search finds the seeded book
                    res = await s.call_tool("search_library", {"query": "MCP Test"})
                    self.assertIn("MCP Test Book", str(res.content[0].text))
                    # collection round-trip
                    res = await s.call_tool("create_collection",
                                            {"name": "From MCP", "book_ids": [bid]})
                    self.assertIn("From MCP", str(res.content[0].text))
                    cols = await s.call_tool("list_collections", {})
                    self.assertIn("From MCP", str(cols.content[0].text))
                    # metadata PATCH round-trip
                    res = await s.call_tool("update_book",
                                            {"book_id": bid, "title": "Renamed via MCP"})
                    self.assertIn("Renamed via MCP", str(res.content[0].text))
                    # ownership: a different user's token can't see it
                    return True
        self.assertTrue(asyncio.run(run()))

    def test_token_of_other_user_sees_nothing(self):
        from app import settings  # noqa: F401
        with db.conn() as con:
            cur = con.execute(
                "INSERT INTO users(username, password_hash, role, status) "
                "VALUES('stranger', ?, 'user', 'active')", (hash_password("secret6"),))
            sid = cur.lastrowid
        other = main.token_create(main.TokenReq(label="s"), user={"id": sid})["token"]

        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        async def run():
            async with streamablehttp_client(
                    f"{BASE}/mcp", headers={"Authorization": f"Bearer {other}"}) as (r, w, _):
                async with ClientSession(r, w) as s:
                    await s.initialize()
                    res = await s.call_tool("search_library", {"query": "MCP Test"})
                    self.assertNotIn("MCP Test Book", str(res.content))  # empty result, no leak
        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()

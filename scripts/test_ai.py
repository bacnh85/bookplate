#!/usr/bin/env python3
"""Offline tests for the AI assistant: actions parser, tool loop (against a stub
OpenAI-compatible endpoint), and the /api/ai/chat endpoint (auth-free route call
with fake user dicts — real auth is UserDep, exercised by the e2e suite).

Run: .venv/bin/python scripts/test_ai.py
"""
import asyncio
import json
import os
import pathlib
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

# isolate the DB before anything imports app.*
_TMP = pathlib.Path(tempfile.mkdtemp(prefix="bookplate-ai-test-"))
import app.db as db  # noqa: E402

db.DATA_DIR = _TMP
db.DB_PATH = _TMP / "ebook.db"
os.environ["BOOKPLATE_ADMIN_USER"] = "rootadmin"
os.environ["BOOKPLATE_ADMIN_PASS"] = "bootpass1"

from app import ai, main, settings  # noqa: E402  (runs db.init on the temp dir)
from app.auth import hash_password  # noqa: E402


def mk_user(username):
    with db.conn() as con:
        cur = con.execute(
            "INSERT INTO users(username, password_hash, role, status) VALUES(?,?,?,'active')",
            (username, hash_password("secret6"), "user"))
        return cur.lastrowid


def mk_book(title, author=""):
    with db.conn() as con:
        cur = con.execute(
            "INSERT INTO books(sha256, ext, size, title, authors) VALUES(?,?,?,?,?)",
            (f"sha-{title}-{author}", "epub", 1000, title, author))
        return cur.lastrowid


def own_book(uid, bid):
    with db.conn() as con:
        con.execute("INSERT OR IGNORE INTO user_books(user_id, book_id) VALUES(?,?)", (uid, bid))


# ---------- stub OpenAI-compatible endpoint ----------

class StubAI:
    """Scripted /chat/completions: each call pops a step. Steps are dicts:
    {"tool": {...args...}}  → assistant message with a tool_call
    {"text": "..."}         → plain assistant message
    {"status": 400, "body": "..."} → error response
    Every request body is recorded in .requests."""

    def __init__(self):
        self.steps = []
        self.requests = []
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.srv.server_address[1]}/v1"

    def _handler(self):
        stub = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                stub.requests.append(body)
                step = stub.steps.pop(0) if stub.steps else {"text": "stub default"}
                self.send_response(step.get("status", 200))
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                if "status" in step:
                    self.wfile.write(json.dumps({"error": step["body"]}).encode())
                    return
                if "tool" in step:
                    msg = {"role": "assistant", "content": None, "tool_calls": [{
                        "id": "call_1", "type": "function",
                        "function": {"name": "search_store",
                                     "arguments": json.dumps(step["tool"])}}]}
                else:
                    msg = {"role": "assistant", "content": step["text"]}
                self.wfile.write(json.dumps(
                    {"choices": [{"message": msg}]}).encode())

            def log_message(self, *a):  # silence
                pass

        return H

    def stop(self):
        self.srv.shutdown()


class SplitActionsTests(unittest.TestCase):
    def test_no_block(self):
        self.assertEqual(ai.split_actions("Just prose."), ("Just prose.", []))

    def test_array_block(self):
        text = 'Look:\n```actions\n[{"type":"queue","source":"annas","id":" md5 ","extension":"EPUB"}]\n```'
        clean, acts = ai.split_actions(text)
        self.assertEqual(clean, "Look:")
        self.assertEqual(acts, [{"type": "queue", "source": "annas", "id": "md5",
                                 "name": "", "authors": "", "cover": "",
                                 "extension": "epub", "size": ""}])

    def test_object_block_and_last_wins(self):
        text = ('```actions\n[{"type":"queue","id":"a"}]\n```\n'
                '```actions\n{"actions":[{"type":"collection_create","name":" Sci-Fi ","book_ids":["1","x",2]}]}\n```')
        _, acts = ai.split_actions(text)
        self.assertEqual(acts, [{"type": "collection_create", "name": "Sci-Fi",
                                 "book_ids": [1, 2]}])

    def test_malformed_and_unknown_types_dropped(self):
        text = '```actions\nnot json\n```'
        self.assertEqual(ai.split_actions(text)[1], [])
        text = '```actions\n[{"type":"delete_everything"},{"type":"queue"},{"type":"collection_add","collection_id":"5","book_ids":[3]}]\n```'
        acts = ai.split_actions(text)[1]
        self.assertEqual([a["type"] for a in acts], ["collection_add"])
        self.assertEqual(acts[0]["collection_id"], 5)

    def test_queue_without_id_dropped(self):
        text = '```actions\n[{"type":"queue","source":"zlibrary"}]\n```'
        self.assertEqual(ai.split_actions(text)[1], [])

    def test_queue_without_source_dropped(self):
        # a source-less id must NOT default to a store — it would queue a doomed
        # job that burns a real download attempt
        text = '```actions\n[{"type":"queue","id":"md5hash"},{"type":"queue","id":"1","source":"goodreads"}]\n```'
        self.assertEqual(ai.split_actions(text)[1], [])

    def test_meta_action_parsing(self):
        text = ('```actions\n[{"type":"remetadata","book_id":"3","query":"A - T"},'
                '{"type":"refetch_cover","book_id":5},'
                '{"type":"update_meta","book_id":"7","fields":{"title":"New",'
                '"year":"2001","hack":"x","description":"  "}}]\n```')
        acts = ai.split_actions(text)[1]
        self.assertEqual([a["type"] for a in acts], ["remetadata", "refetch_cover", "update_meta"])
        self.assertEqual(acts[0], {"type": "remetadata", "book_id": 3, "query": "A - T"})
        self.assertEqual(acts[1], {"type": "refetch_cover", "book_id": 5})
        self.assertEqual(acts[2]["fields"], {"title": "New", "year": 2001})  # hack+blank dropped

    def test_meta_action_invalid_dropped(self):
        text = ('```actions\n[{"type":"remetadata"},{"type":"refetch_cover","book_id":"x"},'
                '{"type":"update_meta","book_id":1,"fields":{}},'
                '{"type":"update_meta","book_id":1,"fields":{"title":""}}]\n```')
        self.assertEqual(ai.split_actions(text)[1], [])


class AichatLoopTests(unittest.TestCase):
    def setUp(self):
        settings.set("ai.enabled", "1")
        settings.set("ai.api_key", "test-key")
        self.stub = StubAI()
        settings.set("ai.base_url", self.stub.url)

    def tearDown(self):
        self.stub.stop()
        with db.conn() as con:
            con.execute("DELETE FROM settings")

    async def _chat(self, system="s", msgs=None, search=None):
        async def _default(q, s):
            return [{"id": "99", "name": "Row"}]  # async, per the documented contract

        return await ai.ai_chat(system, msgs or [{"role": "user", "content": "hi"}],
                                search or _default)

    def test_tool_loop_then_actions(self):
        self.stub.steps = [
            {"tool": {"query": "le guin", "source": "zlibrary"}},
            {"text": "Found it.\n```actions\n[{\"type\":\"queue\",\"source\":\"zlibrary\","
                     "\"id\":\"99\",\"name\":\"The Dispossessed\"}]\n```"},
        ]
        seen = {}

        async def search(query, source):
            seen["args"] = (query, source)
            return [{"id": "99", "name": "The Dispossessed", "authors": "U. Le Guin"}]

        out = asyncio.run(self._chat(search=search))
        self.assertEqual(seen["args"], ("le guin", "zlibrary"))
        # second request carried the tool result
        second = self.stub.requests[1]["messages"]
        self.assertEqual([m["role"] for m in second][-1], "tool")
        self.assertIn("Dispossessed", second[-1]["content"])
        self.assertEqual(out["reply"], "Found it.")
        self.assertEqual(out["actions"][0]["id"], "99")
        # first request had the tool schema
        self.assertIn("tools", self.stub.requests[0])

    def test_unsupported_tools_falls_back(self):
        self.stub.steps = [
            {"status": 400, "body": "function calling is not supported"},  # no 'tool' substring
            {"text": "Plain answer."},
        ]
        out = asyncio.run(self._chat())
        self.assertEqual(out, {"reply": "Plain answer.", "actions": []})
        self.assertNotIn("tools", self.stub.requests[1])

    def test_http_error_maps_to_unavailable(self):
        self.stub.steps = [{"status": 401, "body": "bad key"}]
        with self.assertRaises(ai.AIUnavailable):
            asyncio.run(self._chat())

    def test_round_exhaustion_forces_plain_answer(self):
        self.stub.steps = [
            {"tool": {"query": "a"}}, {"tool": {"query": "b"}}, {"tool": {"query": "c"}},
            {"tool": {"query": "d"}},  # 4th tool_call — round budget exhausted
            {"text": "Final prose answer."},
        ]
        out = asyncio.run(self._chat())
        self.assertEqual(out["reply"], "Final prose answer.")
        self.assertEqual(out["actions"], [])
        self.assertNotIn("tools", self.stub.requests[-1])  # forced no-tools call
        # the async stub's real rows (not {"error":…}) must reach the relay
        self.assertIn('"Row"', self.stub.requests[1]["messages"][-1]["content"])

    def test_not_configured(self):
        settings.set("ai.api_key", "")
        with self.assertRaises(ai.AIUnavailable):
            asyncio.run(self._chat())

    def test_key_is_stripped(self):
        settings.set("ai.api_key", " k123.abc ")
        self.assertEqual(ai._cfg()[0], "k123.abc")

    def test_whitespace_key_counts_as_unconfigured(self):
        settings.set("ai.enabled", "1")
        settings.set("ai.api_key", "   ")
        self.assertFalse(ai.ai_enabled())


class EndpointTests(unittest.TestCase):
    def setUp(self):
        settings.set("ai.enabled", "1")
        settings.set("ai.api_key", "test-key")
        self.stub = StubAI()
        settings.set("ai.base_url", self.stub.url)
        self.uid = mk_user("alice")
        self.other = mk_user("bob")

    def tearDown(self):
        self.stub.stop()
        with db.conn() as con:
            con.execute("DELETE FROM settings")
            con.execute("DELETE FROM user_books")
            con.execute("DELETE FROM shares")
            con.execute("DELETE FROM books")
            con.execute("DELETE FROM users WHERE id>1")

    def _chat(self, text, user=None):
        req = main.ChatReq(messages=[{"role": "user", "content": text}])
        return asyncio.run(main.ai_chat_endpoint(req, user={"id": user or self.uid}))

    def test_requires_config(self):
        settings.set("ai.api_key", "")
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as cm:
            self._chat("hi")
        self.assertEqual(cm.exception.status_code, 403)

    def test_last_message_must_be_user(self):
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as cm:
            req = main.ChatReq(messages=[{"role": "assistant", "content": "hi"}])
            asyncio.run(main.ai_chat_endpoint(req, user={"id": self.uid}))
        self.assertEqual(cm.exception.status_code, 400)

    def test_context_visible_books_only(self):
        b1 = mk_book("Own Book", "A. Author")
        b2 = mk_book("Foreign Book")
        own_book(self.uid, b1)
        own_book(self.other, b2)
        self.stub.steps = [{"text": "ok"}]
        out = self._chat("what do I have?")
        sys_prompt = self.stub.requests[0]["messages"][0]["content"]
        self.assertIn("Own Book", sys_prompt)
        self.assertNotIn("Foreign Book", sys_prompt)
        self.assertEqual(out["reply"], "ok")

    def test_shared_books_are_visible(self):
        b1 = mk_book("Shared With Me")
        own_book(self.other, b1)
        with db.conn() as con:
            con.execute("INSERT INTO shares(book_id, from_user, to_user) VALUES(?,?,?)",
                        (b1, self.other, self.uid))
        self.stub.steps = [{"text": "ok"}]
        self._chat("hi")
        self.assertIn("Shared With Me", self.stub.requests[0]["messages"][0]["content"])

    def test_queue_action_end_to_end(self):
        import unittest.mock as mock
        b1 = mk_book("Own Book")
        own_book(self.uid, b1)
        self.stub.steps = [{"text": "On it.\n```actions\n["
                           "{\"type\":\"queue\",\"source\":\"zlibrary\",\"id\":\"42\","
                           "\"name\":\"Result\",\"authors\":\"R. Auth\",\"extension\":\"epub\"}]\n```"}]
        async def fake_zlib_search(q, count=20):
            return [{"id": "42", "name": "Result"}]
        with mock.patch.object(main.zlib, "search", fake_zlib_search):
            out = self._chat("find Result")
        self.assertEqual(out["actions"][0]["type"], "queue")
        with db.conn() as con:
            row = con.execute("SELECT * FROM download_jobs WHERE user_id=? AND zlib_id='42'",
                              (self.uid,)).fetchone()
        self.assertIsNone(row)  # endpoint NEVER applies actions itself

    def test_history_trimmed(self):
        self.stub.steps = [{"text": "ok"}]
        msgs = [{"role": "user" if i % 2 == 0 else "assistant", "content": f"m{i}"}
                for i in range(31)]  # ends on a user turn
        req = main.ChatReq(messages=msgs)
        asyncio.run(main.ai_chat_endpoint(req, user={"id": self.uid}))
        sent = self.stub.requests[0]["messages"]
        self.assertEqual(len(sent), 17)  # system + last 16

    def test_message_too_long_rejected(self):
        from pydantic import ValidationError
        with self.assertRaises(ValidationError):
            main.ChatReq(messages=[{"role": "user", "content": "x" * 8001}])

    def test_throttle_10_per_minute(self):
        with db.conn() as con:
            con.execute("DELETE FROM settings")
        uid = 999
        self.assertTrue(all(main._ai_throttle(uid) for _ in range(10)))
        self.assertFalse(main._ai_throttle(uid))
        main._AI_RATE.pop(uid)

    def test_sourceless_search_prefers_configured_store(self):
        import unittest.mock as mock
        # zlib creds unset (even though the CLI binary exists on this machine)
        settings.set("zlib.email", "")
        settings.set("annas.secret_key", "aa-key")
        self.stub.steps = [{"tool": {"query": "anything"}}]  # model omits source
        calls = []

        async def fake_annas_search(q, count=20):
            calls.append(("annas", q))
            return []

        async def fail_zlib_search(q, count=20):
            calls.append(("zlib", q))
            return []

        with mock.patch.object(main.annas, "search", fake_annas_search), \
             mock.patch.object(main.zlib, "search", fail_zlib_search):
            self._chat("find anything")
        self.assertEqual(calls, [("annas", "anything")])


class MetadataEndpointTests(unittest.TestCase):
    """PATCH / remetadata / cover endpoints (offline, enrichment stubbed)."""

    def setUp(self):
        settings.set("ai.enabled", "1")
        settings.set("ai.api_key", "test-key")
        self.stub = StubAI()
        settings.set("ai.base_url", self.stub.url)
        self.uid = mk_user("carol")
        self.mate = mk_user("dave")
        self.bid = mk_book("Junk Title", "W. Writer")
        own_book(self.uid, self.bid)
        with db.conn() as con:
            con.execute("UPDATE books SET added_by=? WHERE id=?", (self.uid, self.bid))

    def tearDown(self):
        self.stub.stop()
        with db.conn() as con:
            con.execute("DELETE FROM settings")
            con.execute("DELETE FROM user_books")
            con.execute("DELETE FROM shares")
            con.execute("DELETE FROM books")
            con.execute("DELETE FROM users WHERE id>1")

    def _patch(self, fields, user=None):
        return main.update_book(self.bid, main.BookPatch(**fields), user={"id": user or self.uid, "role": "user"})

    def test_patch_updates_and_fts(self):
        out = self._patch({"title": " Real New Name ", "year": 2011})
        self.assertEqual(out["title"], "Real New Name")
        self.assertEqual(out["year"], 2011)
        with db.conn() as con:
            hit = con.execute(
                "SELECT b.title FROM books_fts f JOIN books b ON b.id=f.rowid "
                "WHERE books_fts MATCH ?", ('"Real New"*',)).fetchall()
        self.assertEqual(len(hit), 1)

    def test_patch_empty_body_rejected(self):
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as cm:
            self._patch({})
        self.assertEqual(cm.exception.status_code, 400)

    def test_shared_nonowner_gets_403(self):
        from fastapi import HTTPException
        own_book(self.mate, self.bid)
        with db.conn() as con:
            con.execute("INSERT OR IGNORE INTO shares(book_id, from_user, to_user) VALUES(?,?,?)",
                        (self.bid, self.uid, self.mate))
        with self.assertRaises(HTTPException) as cm:
            self._patch({"title": "X"}, user=self.mate)
        self.assertEqual(cm.exception.status_code, 403)
        with self.assertRaises(HTTPException) as cm:
            asyncio.run(main.remetadata_book(
                self.bid, main.RemetaReq(query="Q"), user={"id": self.mate, "role": "user"}))
        self.assertEqual(cm.exception.status_code, 403)

    def test_invisible_book_404(self):
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as cm:
            self._patch({"title": "X"}, user=self.mate)  # dave can't see it at all
        self.assertEqual(cm.exception.status_code, 404)

    def test_remetadata_enriches_and_keeps_blanks(self):
        import unittest.mock as mock
        async def fake_google(title, author):
            return {"title": "Real Book Title", "year": 1999}  # authors stays blank
        with mock.patch.object(main.metadata, "_google", fake_google), \
             mock.patch.object(main, "_write_cover", return_value=None) as wc:
            out = asyncio.run(main.remetadata_book(
                self.bid, main.RemetaReq(query="Kleppmann - Real Book"), user={"id": self.uid, "role": "user"}))
        wc.assert_called_once()  # cover from the chain is persisted too
        self.assertEqual(out["before"]["title"], "Junk Title")
        self.assertEqual(out["after"]["title"], "Real Book")  # from the query hint's parse
        self.assertEqual(out["after"]["year"], 1999)
        self.assertIn("title", out["changed"])
        self.assertEqual(out["after"]["authors"], "Kleppmann")  # non-blank hint wins

    def test_cover_helper_writes_and_swaps_ext(self):
        import app.storage as storage
        sha = f"sha-{self.bid}"
        with db.conn() as con:
            con.execute("UPDATE books SET cover_ext='svg' WHERE id=?", (self.bid,))
        old = storage.cover_path(sha, "svg")
        old.write_bytes(b"old-svg")
        ext = main._write_cover(sha, {"cover": b"png-bytes", "cover_ext": "png"}, "svg")
        self.assertEqual(ext, "png")
        self.assertEqual(storage.cover_path(sha, "png").read_bytes(), b"png-bytes")
        self.assertFalse(old.exists())  # old extension unlinked

    def _sha(self):
        with db.conn() as con:
            return con.execute("SELECT sha256 FROM books WHERE id=?", (self.bid,)).fetchone()[0]

    def test_refetch_cover_nothing_found_leaves_existing(self):
        import unittest.mock as mock
        import app.storage as storage
        sha = self._sha()
        with db.conn() as con:
            con.execute("UPDATE books SET cover_ext='jpg' WHERE id=?", (self.bid,))
        storage.cover_path(sha, "jpg").write_bytes(b"existing-jpg")  # a real cover on disk
        async def fake_google(title, author):
            return {}  # no cover found anywhere
        with mock.patch.object(main.metadata, "_google", fake_google):
            out = asyncio.run(main.refetch_cover(self.bid, user={"id": self.uid, "role": "user"}))
        self.assertEqual(out, {"updated": False})  # real cover left alone

    def test_refetch_cover_generates_when_file_missing(self):
        import unittest.mock as mock
        import app.storage as storage
        sha = self._sha()
        async def fake_google(title, author):
            return {}
        with mock.patch.object(main.metadata, "_google", fake_google):
            out = asyncio.run(main.refetch_cover(self.bid, user={"id": self.uid, "role": "user"}))
        self.assertEqual(out["updated"], True)
        self.assertEqual(out["cover_ext"], "svg")  # app guarantee: every book has a cover
        import app.storage as storage
        self.assertTrue(storage.cover_path(sha, "svg").exists())

class RemetadataOffloadTests(unittest.TestCase):
    """The chain must run off the event loop: while a slow remetadata executes,
    the loop keeps serving. Regression for the sync-blocked-loop finding."""

    def setUp(self):
        self.uid = mk_user("hank")
        self.bid = mk_book("Loop Book", "L. Auth")
        own_book(self.uid, self.bid)
        with db.conn() as con:
            con.execute("UPDATE books SET added_by=? WHERE id=?", (self.uid, self.bid))

    def tearDown(self):
        with db.conn() as con:
            con.execute("DELETE FROM user_books")
            con.execute("DELETE FROM books")
            con.execute("DELETE FROM users WHERE id>1")

    def test_slow_chain_does_not_block_loop(self):
        import unittest.mock as mock
        import time
        async def slow_build(path, orig):
            await asyncio.sleep(0.8)  # simulate the slow chain
            return {}
        with mock.patch.object(main.metadata, "build_metadata", slow_build):
            async def go():
                task = asyncio.ensure_future(main.remetadata_book(
                    self.bid, main.RemetaReq(query="Q"),
                    user={"id": self.uid, "role": "user"}))
                await asyncio.sleep(0.1)   # loop must stay responsive meanwhile
                dt = time.monotonic()
                await asyncio.sleep(0)     # schedule: proves the loop is not blocked
                waited = time.monotonic() - dt
                done = task.done()
                await task
                return waited, done
            waited, done = asyncio.run(go())
            self.assertLess(waited, 0.5)   # 0.1s sleep fired promptly
            self.assertFalse(done)         # chain still running in its thread


class OwnershipBoundaryTests(unittest.TestCase):
    """_editable_book is the whole authz surface of the new mutation endpoints:
    share-recipient 403, invisible 404, owner 200, admin 200 — a regression that
    drops the added_by check must fail here."""

    def setUp(self):
        self.owner = mk_user("owner1")
        self.mate = mk_user("mate1")
        self.outsider = mk_user("out1")
        self.bid = mk_book("Shared Book", "S. Auth")
        own_book(self.owner, self.bid)
        with db.conn() as con:
            con.execute("UPDATE books SET added_by=? WHERE id=?", (self.owner, self.bid))
            con.execute("INSERT INTO shares(book_id, from_user, to_user) VALUES(?,?,?)",
                        (self.bid, self.owner, self.mate))

    def tearDown(self):
        with db.conn() as con:
            con.execute("DELETE FROM shares")
            con.execute("DELETE FROM user_books")
            con.execute("DELETE FROM books")
            con.execute("DELETE FROM users WHERE id>1")

    def _u(self, uid, role="user"):
        return {"id": uid, "role": role}

    def test_patch_boundary(self):
        from fastapi import HTTPException
        for user, code in ((self.mate, 403), (self.outsider, 404), (self.owner, 200)):
            if code == 200:
                out = main.update_book(self.bid, main.BookPatch(year=2020), user=self._u(user))
                self.assertEqual(out["year"], 2020)
            else:
                with self.assertRaises(HTTPException) as cm:
                    main.update_book(self.bid, main.BookPatch(year=2020), user=self._u(user))
                self.assertEqual(cm.exception.status_code, code)

    def test_admin_bypasses_on_all_three(self):
        import unittest.mock as mock
        with db.conn() as con:
            admin_id = con.execute("SELECT id FROM users WHERE role='admin'").fetchone()[0]
            con.execute("INSERT OR IGNORE INTO user_books(user_id, book_id) VALUES(?,?)",
                        (admin_id, self.bid))  # admin must at least see the book
        admin = self._u(admin_id, role="admin")
        with mock.patch.object(main.metadata, "build_metadata",
                               mock.AsyncMock(return_value={})):
            out = main.update_book(self.bid, main.BookPatch(title="Admin Edit"), user=admin)
            self.assertEqual(out["title"], "Admin Edit")
            out = asyncio.run(main.remetadata_book(
                self.bid, main.RemetaReq(), user=admin))  # no raise, no real network
            self.assertIn("after", out)
            asyncio.run(main.refetch_cover(self.bid, user=admin))  # must not raise

    def test_remetadata_and_cover_boundary(self):
        import unittest.mock as mock
        from fastapi import HTTPException
        with mock.patch.object(main.metadata, "build_metadata",
                               mock.AsyncMock(return_value={})):
            for user, code in ((self.mate, 403), (self.outsider, 404), (self.owner, 200)):
                u = self._u(user)
                if code == 200:
                    out = asyncio.run(main.remetadata_book(
                        self.bid, main.RemetaReq(), user=u))
                    self.assertIn("after", out)
                    out = asyncio.run(main.refetch_cover(self.bid, user=u))
                    self.assertTrue(out["updated"])
                    continue
                for call in (("remetadata",
                              lambda: main.remetadata_book(
                                  self.bid, main.RemetaReq(), user=u)),
                             ("cover",
                              lambda: main.refetch_cover(self.bid, user=u))):
                    with self.assertRaises(HTTPException) as cm:
                        asyncio.run(call[1]())
                    self.assertEqual(cm.exception.status_code, code, f"{call[0]} {code}")



if __name__ == "__main__":
    unittest.main()

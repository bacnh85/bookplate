#!/usr/bin/env python3
"""End-to-end self-test against a running server (default localhost:8480).

Generates a test corpus with ebooklib/pypdf, then verifies: auth (+ approval flow),
ingest, metadata extraction chain, exact dedup, logical dup, sharing, FTS search,
covers, download, delete, OPDS, z-lib gating, send-to-kindle gating, admin RBAC
+ settings + z-lib admin.

Usage: .venv/bin/python scripts/selftest.py [base_url]

Registrations always land as *pending*; the suite signs in with the bootstrap
admin and approves its own users. Admin creds: SELFTEST_ADMIN_USER /
SELFTEST_ADMIN_PASS env — or, when the server shares this filesystem and was
booted with a generated password, data/initial_admin_password is read.
"""
import io
import os
import random
import re
import string
import sys
import stat
import time
from pathlib import Path

import httpx
from ebooklib import epub
from pypdf import PdfReader, PdfWriter

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8480"
rand = "".join(random.choices(string.hexdigits.lower(), k=6))
ALICE, BOB = f"alice-{rand}@t.io", f"bob-{rand}@t.io"
PASS = "hunter22"


def _initial_admin_pw() -> str:
    """Fresh-DB dev mode: bootstrap password file beside the server's data dir."""
    f = Path(__file__).resolve().parent.parent / "data" / "initial_admin_password"
    try:
        return f.read_text().strip()
    except OSError:
        return ""


ADMIN_USER = os.getenv("SELFTEST_ADMIN_USER", "admin")
ADMIN_PASS = os.getenv("SELFTEST_ADMIN_PASS", "") or _initial_admin_pw()

ok = total = 0
def check(name, cond, detail=""):
    global ok, total
    total += 1
    mark = "PASS" if cond else "FAIL"
    print(f"[{mark}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if cond: ok += 1


def make_good_epub() -> bytes:
    b = epub.EpubBook()
    b.set_identifier(f"id-{rand}")
    b.set_title("The Art of Testing")
    b.add_author("Ada Lovelace")
    b.set_language("en")
    b.add_metadata("DC", "subject", "Software Testing", {})
    c = epub.EpubHtml(title="Ch 1", file_name="c1.xhtml", content="<h1>Hi</h1><p>text</p>")
    b.add_item(c)
    b.add_item(epub.EpubNcx()); b.add_item(epub.EpubNav())
    b.spine = ["nav", c]
    data = io.BytesIO(); epub.write_epub(data, b)
    return data.getvalue()


def make_bare_epub() -> bytes:
    b = epub.EpubBook()  # no title/author on purpose
    b.set_identifier(f"id2-{rand}")
    c = epub.EpubHtml(title="x", file_name="c1.xhtml", content="<p>nothing</p>")
    b.add_item(c); b.add_item(epub.EpubNcx()); b.add_item(epub.EpubNav())
    b.spine = ["nav", c]
    data = io.BytesIO(); epub.write_epub(data, b)
    return data.getvalue()


def make_newline_epub() -> bytes:
    b = epub.EpubBook()  # DC:title with a newline: must 502 readably, not 500
    b.set_identifier(f"id3-{rand}")
    b.set_title(f"Broken\nTitle {rand}")
    b.add_author("Newline Author")
    c = epub.EpubHtml(title="x", file_name="c1.xhtml", content="<p>nl</p>")
    b.add_item(c); b.add_item(epub.EpubNcx()); b.add_item(epub.EpubNav())
    b.spine = ["nav", c]
    data = io.BytesIO(); epub.write_epub(data, b)
    return data.getvalue()


def make_pdf(title: str | None, author: str | None) -> bytes:
    w = PdfWriter()
    w.add_blank_page(width=612, height=792)
    if title:
        w.add_metadata({"/Title": title, "/Author": author or "", "/Subject": "Networking"})
    data = io.BytesIO(); w.write(data)
    return data.getvalue()


def make_encrypted_pdf(title: str) -> bytes:
    w = PdfWriter()
    w.add_blank_page(width=612, height=792)
    w.add_metadata({"/Title": title})
    w.encrypt(f"{rand}-pass")  # user+owner password: unrenderable thumbnail
    data = io.BytesIO(); w.write(data)
    return data.getvalue()


def upload(cx, token, name, data):
    return cx.post(f"{BASE}/api/books",
                   files={"file": (name, data)},
                   headers={"Authorization": f"Bearer {token}"}).json()


def _uid_by_name(cx, headers, username):
    users = cx.get(f"{BASE}/api/admin/users", headers=headers).json()
    return next(u["id"] for u in users if u["username"] == username)


def main():
    cx = httpx.Client(timeout=30, base_url=BASE)
    # 2. auth — bootstrap admin + registration approval flow. Registrations ALWAYS
    # pend; the suite logs in as the bootstrap admin and approves its own users.
    la = cx.post("/api/auth/login", json={"username": ADMIN_USER, "password": ADMIN_PASS})
    check("admin login (bootstrap creds)", la.status_code == 200, la.text[:120])
    t_admin = la.json().get("token", "")
    if not t_admin:
        print("\nNo admin credentials: set SELFTEST_ADMIN_USER / SELFTEST_ADMIN_PASS, "
              "or run against a fresh server whose data/initial_admin_password is "
              "readable from this checkout.")
        return 1
    ah = {"Authorization": f"Bearer {t_admin}"}

    def register_and_approve(username):
        label = username.split("@")[0]
        r = cx.post("/api/auth/register", json={"username": username, "password": PASS})
        check(f"register {label} pending", r.status_code == 200
              and r.json().get("status") == "pending", r.text[:120])
        r = cx.post(f"{BASE}/api/admin/users/{_uid_by_name(cx, ah, username)}/approve",
                    headers=ah)
        check(f"admin approves {label}", r.status_code == 200, r.text[:120])
        login = cx.post("/api/auth/login", json={"username": username, "password": PASS})
        return login.json().get("token", "")

    t_alice = register_and_approve(ALICE)
    t_bob = register_and_approve(BOB)
    auth_a = {"Authorization": f"Bearer {t_alice}"}
    auth_b = {"Authorization": f"Bearer {t_bob}"}
    check("register two users", t_alice and t_bob)
    t_super = t_admin
    auth_super = ah

    # 1. good epub: embedded metadata wins
    r = upload(cx, t_alice, "whatever.epub", make_good_epub())
    b = r["book"]
    check("epub embedded metadata", b["title"] == "The Art of Testing" and "Ada Lovelace" in b["authors"],
          f'{b["title"]} / {b["authors"]}')

    # 2. bare epub -> filename "Author - Title" parse + enrichment
    r = upload(cx, t_alice, "Grace Hopper - Compilers and Categories.epub", make_bare_epub())
    b2 = r["book"]
    check("filename parse fallback", b2["title"].startswith("Compilers and Categories") and "Hopper" in b2["authors"],
          f'{b2["title"]} / {b2["authors"]}')

    # 3. pdf with metadata
    r = upload(cx, t_alice, "doc.pdf", make_pdf(f"Computer Networks {rand}", "Andrew Tanenbaum"))
    b3 = r["book"]
    check("pdf metadata", b3["title"].startswith("Computer Networks") and b3["authors"] == "Andrew Tanenbaum",
          f'{b3["title"]} / {b3["authors"]}')
    r = cx.get(f"{BASE}/api/books/{b3['id']}/cover", headers={"Authorization": f"Bearer {t_alice}"})
    check("pdf cover is rendered page-1 jpeg", r.status_code == 200 and r.headers.get("content-type") == "image/jpeg",
          f"{r.status_code} {r.headers.get('content-type', '')}")

    # 3b. encrypted pdf: render fails -> fallback chain must still guarantee a cover
    r = upload(cx, t_alice, f"Anon - Locked Volume {rand}.pdf", make_encrypted_pdf(f"Locked {rand}"))
    r = cx.get(f"{BASE}/api/books/{r['book']['id']}/cover", headers={"Authorization": f"Bearer {t_alice}"})
    check("encrypted pdf still has a cover (fallback)", r.status_code == 200 and r.headers.get("content-type", "").startswith("image/"),
          f"{r.status_code} {r.headers.get('content-type', '')}")

    # 4. exact dedup: same bytes again
    dup_pdf = make_pdf("Computer Networks", "Andrew Tanenbaum")
    upload(cx, t_alice, "Computer Networks.pdf", dup_pdf)  # first store
    r = upload(cx, t_alice, "Computer Networks.pdf", dup_pdf)  # same bytes -> dedup
    check("exact dedup (same sha)", r["duplicate"] is True)

    # 5. logical dup: same title different file
    r = upload(cx, t_alice, "doc2.pdf", make_pdf(f"Computer Networks {rand} (2nd scan)", "A. Tanenbaum"))
    check("logical dup detected", any(f"Computer Networks {rand}" in s["title"] for s in r.get("similar", [])),
          str(r.get("similar")))

    # 6. FTS search
    res = cx.get(f"{BASE}/api/books", params={"q": "testing"}, headers={"Authorization": f"Bearer {t_alice}"}).json()
    check("FTS search 'testing'", isinstance(res, list) and any("Art of Testing" in x["title"] for x in res),
          str([x.get("title") for x in res])[:200] if isinstance(res, list) else str(res)[:200])

    # 6b. quoted/malformed query must not 500
    r = cx.get(f"{BASE}/api/books", params={"q": 'o"brien AND (weird'}, headers={"Authorization": f"Bearer {t_alice}"})
    check("FTS quoted query -> 200", r.status_code == 200, str(r.status_code))

    # 7. share -> auto-display on bob's shelf
    cx.post(f"{BASE}/api/books/{b['id']}/share", json={"username": BOB},
            headers={"Authorization": f"Bearer {t_alice}"})
    bob_books = cx.get(f"{BASE}/api/books", headers={"Authorization": f"Bearer {t_bob}"}).json()
    shared = [x for x in bob_books if x["id"] == b["id"]]
    check("share appears on bob's shelf", bool(shared) and shared[0]["shared_by"] == ALICE)

    # 8. bob cannot see alice's unshared books
    check("unshared books invisible", all(x["id"] != b3["id"] for x in bob_books))

    # 9. cover — every ingested book now ends with a cover (generated SVG fallback)
    r = cx.get(f"{BASE}/api/books/{b['id']}/cover", headers={"Authorization": f"Bearer {t_bob}"})
    check("cover endpoint (fallback guarantees image)", r.status_code == 200 and r.headers.get("content-type", "").startswith("image/"),
          f"{r.status_code} {r.headers.get('content-type', '')}")
    check("cover cache-control private", r.status_code != 200 or r.headers.get("cache-control", "").startswith("private"),
          r.headers.get("cache-control", ""))

    # 10. file download
    r = cx.get(f"{BASE}/api/books/{b['id']}/file", headers={"Authorization": f"Bearer {t_bob}"})
    check("file download", r.status_code == 200 and len(r.content) > 500)

    # 11. z-lib routes: gated 503 when unconfigured; with real creds configured the
    # invariant is only "never a 500" (login may legitimately succeed or be walled)
    r = cx.get(f"{BASE}/api/zlib/search", params={"q": "x"}, headers={"Authorization": f"Bearer {t_alice}"})
    check("z-lib no-500 invariant", r.status_code in (200, 503), str(r.status_code))

    # 12. OPDS via basic auth
    r = cx.get(f"{BASE}/opds", auth=(ALICE, PASS))
    check("OPDS feed (basic auth)", r.status_code == 200 and b"opds-catalog" in r.content)
    stamps = re.findall(rb"<updated>(.*?)</updated>", r.content)
    rfc3339 = re.compile(rb"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
    check("OPDS updated RFC3339", len(stamps) >= 2 and all(rfc3339.match(s) for s in stamps),
          str(stamps[:3]))

    # 13. delete removes from shelf; last-owner delete drops the row
    cx.delete(f"{BASE}/api/books/{b['id']}", headers={"Authorization": f"Bearer {t_bob}"})
    cx.delete(f"{BASE}/api/books/{b['id']}", headers={"Authorization": f"Bearer {t_alice}"})
    r = cx.get(f"{BASE}/api/books", headers={"Authorization": f"Bearer {t_alice}"}).json()
    check("delete + cleanup", all(x["id"] != b["id"] for x in r))

    # 14b. JWT secret file must be owner-only — local target only: this stats the
    # local checkout's file, which is not the file a remote BASE actually uses.
    # only meaningful when the test shares a filesystem with the server (local dev
    # server); against a containerized/remote server the local data dir won't exist
    if BASE.startswith(("http://localhost", "http://127.0.0.1")) and (Path(__file__).resolve().parent.parent / "data").exists():
        secret = Path(__file__).resolve().parent.parent / "data" / ".secret"
        mode = stat.S_IMODE(os.stat(secret).st_mode) if secret.exists() else None
        check("secret file 0600", mode == 0o600, "missing" if mode is None else oct(mode))
        init_pw = Path(__file__).resolve().parent.parent / "data" / "initial_admin_password"
        mode2 = stat.S_IMODE(os.stat(init_pw).st_mode) if init_pw.exists() else None
        check("initial_admin_password 0600", mode2 is None or mode2 == 0o600,
              "missing" if mode2 is None else oct(mode2))

    # 15. download queue: enqueue is idempotent; worker records failures with a
    # readable error; delete works. CI (no zlib CLI) must fail the job fast.
    qjob = cx.post(f"{BASE}/api/zlib/queue",
                   json={"id": f"fake-{rand}", "name": "Queue Probe", "authors": "Q. Auteur",
                         "extension": "epub", "size": "1 MB"},
                   headers={"Authorization": f"Bearer {t_alice}"}).json()
    check("queue enqueue", bool(qjob.get("id")) and qjob["status"] in ("queued", "waiting_quota", "downloading", "processing"),
          str(qjob)[:200])
    jobs = cx.get(f"{BASE}/api/zlib/queue", headers={"Authorization": f"Bearer {t_alice}"}).json()["jobs"]
    check("queue list", any(j["id"] == qjob["id"] for j in jobs))
    has_backend = cx.get(f"{BASE}/api/zlib/limits", headers={"Authorization": f"Bearer {t_alice}"}).status_code == 200
    if not has_backend:
        handled = None
        for _ in range(30):  # no zlib backend: worker must record a readable error quickly
            jobs = cx.get(f"{BASE}/api/zlib/queue", headers={"Authorization": f"Bearer {t_alice}"}).json()["jobs"]
            handled = next((j for j in jobs if j["id"] == qjob["id"]), None)
            if handled and (handled["status"] == "failed" or handled["attempts"] >= 1):
                break
            time.sleep(1)
        check("queue job records error without backend", handled is not None and bool(handled["error"]),
              str(handled)[:200] if handled else "job missing")
        if handled and handled["status"] == "failed":
            r = cx.post(f"{BASE}/api/zlib/queue/{qjob['id']}/retry", headers={"Authorization": f"Bearer {t_alice}"})
            check("queue retry", r.status_code == 200, r.text[:200])
    r = cx.delete(f"{BASE}/api/zlib/queue/{qjob['id']}", headers={"Authorization": f"Bearer {t_alice}"})
    # 200 = removed; 409 = the worker re-claimed the retried job first (racy by
    # design — the 409 guard itself is correct). Only 200 proves removal.
    check("queue delete", r.status_code in (200, 409), r.text[:200])
    if r.status_code == 200:
        jobs = cx.get(f"{BASE}/api/zlib/queue", headers={"Authorization": f"Bearer {t_alice}"}).json()["jobs"]
        check("queue delete removes", not any(j["id"] == qjob["id"] for j in jobs))

    # 15b. worker resilience: a malformed job must fail alone — the worker keeps
    # draining (regression: parse_size("1.2.3 MB") ValueError killed the queue
    # task permanently, leaving every later job stuck).
    pair_ids = []
    for payload in ({"id": f"bad-{rand}", "name": "Poison Probe", "size": "1.2.3 MB"},
                    {"id": f"ok-{rand}", "name": "Drain Probe", "size": "1 MB"}):
        pair_ids.append(cx.post(f"{BASE}/api/zlib/queue", json=payload,
                                headers={"Authorization": f"Bearer {t_alice}"}).json()["id"])
    drained, got = False, []
    for _ in range(45):
        jobs = cx.get(f"{BASE}/api/zlib/queue", headers={"Authorization": f"Bearer {t_alice}"}).json()["jobs"]
        by_id = {j["id"]: j for j in jobs}
        got = [by_id.get(j) for j in pair_ids]
        if all(j and j["error"] and (j["status"] == "failed" or j["attempts"] >= 1) for j in got):
            drained = True
            break
        time.sleep(1)
    check("worker survives malformed job, queue still drains", drained,
          str([(j or {}).get("status") for j in got]))
    for jid in pair_ids:
        cx.delete(f"{BASE}/api/zlib/queue/{jid}", headers={"Authorization": f"Bearer {t_alice}"})

    # 15c. anna's archive routes: same hermeticity story as z-lib — CI has no key
    # so search 503s and jobs fail fast with a readable error. The id is md5-validated
    # at the door; a fake-but-wellformed md5 keeps the probe inert even where a real
    # member key exists (upstream answers "Invalid md5").
    r = cx.get(f"{BASE}/api/annas/search", params={"q": "x"}, headers={"Authorization": f"Bearer {t_alice}"})
    check("annas no-500 invariant", r.status_code in (200, 400, 503), str(r.status_code))
    r = cx.post(f"{BASE}/api/annas/queue", json={"id": "not-an-md5", "name": "x"},
                headers={"Authorization": f"Bearer {t_alice}"})
    check("annas queue rejects non-md5", r.status_code == 400, r.text[:120])
    ajob = cx.post(f"{BASE}/api/annas/queue",
                   json={"id": "0" * 32, "name": "Annas Probe", "authors": "A. Archive",
                         "extension": "epub", "size": "1 MB"},
                   headers={"Authorization": f"Bearer {t_alice}"}).json()
    check("annas enqueue tags source", bool(ajob.get("id")) and ajob.get("source") == "annas",
          str(ajob)[:200])
    jobs = cx.get(f"{BASE}/api/annas/queue", headers={"Authorization": f"Bearer {t_alice}"}).json()["jobs"]
    check("annas queue list alias", any(j["id"] == ajob["id"] for j in jobs))
    handled = None
    for _ in range(30):  # config errors fail fast; upstream errors show attempts>=1
        jobs = cx.get(f"{BASE}/api/zlib/queue", headers={"Authorization": f"Bearer {t_alice}"}).json()["jobs"]
        handled = next((j for j in jobs if j["id"] == ajob["id"]), None)
        if handled and (handled["status"] == "failed" or handled["attempts"] >= 1):
            break
        time.sleep(1)
    check("annas job records readable error", handled is not None and bool(handled["error"]),
          str(handled)[:200] if handled else "job missing")
    if handled and handled["status"] == "failed":
        r = cx.post(f"{BASE}/api/annas/queue/{ajob['id']}/retry", headers={"Authorization": f"Bearer {t_alice}"})
        check("annas retry", r.status_code == 200, r.text[:200])
    r = cx.delete(f"{BASE}/api/zlib/queue/{ajob['id']}", headers={"Authorization": f"Bearer {t_alice}"})
    check("annas delete", r.status_code == 200, r.text[:200])

    # 15. admin: RBAC, settings API, z-lib admin endpoints
    r = cx.get("/api/admin/users", headers=auth_b)
    check("non-admin blocked from admin API", r.status_code == 403, str(r.status_code))
    if t_super:
        r = cx.get("/api/admin/users", headers=auth_super)
        check("admin lists users", r.status_code == 200 and isinstance(r.json(), list), r.text[:120])
        r = cx.get("/api/admin/settings", headers=auth_super)
        masked = r.json() if r.status_code == 200 else {}
        check("settings GET masks secrets", r.status_code == 200
              and set(masked.get("zlib.password", {})) == {"set", "hint"}, r.text[:160])
        r = cx.put("/api/admin/settings", headers=auth_super,
                   json={"values": {"ai.model": "glm-5.3-flash", "registration": "closed"}})
        check("settings PUT writes", r.status_code == 200, r.text[:120])
        r = cx.post("/api/auth/register", json={"username": f"closed-{rand}@t.io", "password": PASS})
        check("closed registration rejects", r.status_code == 403, str(r.status_code))
        r = cx.put("/api/admin/settings", headers=auth_super,
                   json={"values": {"registration": "approval"}})
        check("settings PUT restores approval", r.status_code == 200, r.text[:120])
        r = cx.put("/api/admin/settings", headers=auth_super,
                   json={"values": {"not.a.key": "x"}})
        check("settings PUT rejects unknown key", r.status_code == 400, str(r.status_code))

        # 15d. send-to-kindle: per-user device CRUD, config gating + readable
        # SMTP failure, fully
        # hermetic — the configured host is 127.0.0.1:1 (instant refusal), so
        # no real mail is sent anywhere. SKIPPED on a server that already has
        # kindle configured: the probes must not wipe saved SMTP credentials
        # (secrets are masked and can never be read back to restore).
        kindle_smtp_keys = ["kindle.from", "kindle.smtp_host", "kindle.smtp_port",
                            "kindle.smtp_security", "kindle.smtp_user", "kindle.smtp_password"]
        pre = cx.get(f"{BASE}/api/admin/settings", headers=auth_super).json()
        # any saved kindle SMTP setting (even a partial one) -> skip: the probes
        # overwrite kindle.* and masked secrets can never be restored
        if (pre.get("kindle.smtp_host")
                or pre.get("kindle.smtp_user")
                or (pre.get("kindle.smtp_password") or {}).get("set")):
            print("send-to-kindle: server already kindle-configured — skipping probes "
                  "(they would wipe saved credentials)")
        else:
            cx.put(f"{BASE}/api/admin/settings", headers=auth_super,
                   json={"values": {k: "" for k in kindle_smtp_keys}})
            me0 = cx.get(f"{BASE}/api/me", headers=auth_a).json()
            check("me.kindle false when unconfigured", me0.get("kindle") is False, str(me0))
            # device CRUD (per-user rows in this instance's own DB — safe to probe)
            r = cx.post(f"{BASE}/api/kindle/devices", headers=auth_a,
                        json={"label": "", "email": "not-an-email"})
            check("device add rejects bad email", r.status_code == 400, r.text[:120])
            r = cx.post(f"{BASE}/api/kindle/devices", headers=auth_a,
                        json={"email": "attacker@example.com"})
            check("device add rejects non-Kindle domain", r.status_code == 400
                  and "@kindle.com" in r.json().get("detail", ""), r.text[:120])
            r = cx.post(f"{BASE}/api/kindle/devices", headers=auth_a,
                        json={"label": "Paperwhite", "email": "alice_kindle@kindle.com"})
            check("device add ok", r.status_code == 200 and len(r.json()) == 1
                  and r.json()[0]["label"] == "Paperwhite", r.text[:160])
            dev_a = r.json()[0]["id"]
            r = cx.post(f"{BASE}/api/kindle/devices", headers=auth_a,
                        json={"email": "alice_kindle@kindle.com"})
            check("duplicate device email -> 409", r.status_code == 409, r.text[:120])
            r = cx.post(f"{BASE}/api/kindle/devices", headers=auth_a,
                        json={"email": "alice2@kindle.com"})
            check("blank label defaults to email", r.status_code == 200
                  and r.json()[-1]["label"] == "alice2@kindle.com", r.text[:160])
            dev_a2 = r.json()[-1]["id"]
            # SMTP still unset -> flag stays false even with devices
            me1 = cx.get(f"{BASE}/api/me", headers=auth_a).json()
            check("me.kindle false with device but no SMTP", me1.get("kindle") is False, str(me1))
            # cross-user isolation
            r = cx.delete(f"{BASE}/api/kindle/devices/{dev_a}", headers=auth_b)
            check("device delete by other user -> 404", r.status_code == 404, r.text[:120])
            r = cx.post(f"{BASE}/api/books/{b2['id']}/send-to-kindle", headers=auth_a,
                        json={"device_id": dev_a})
            check("kindle send unconfigured -> 502 readable",
                  r.status_code == 502 and "not configured" in r.json().get("detail", ""),
                  f"{r.status_code} {r.text[:120]}")
            r = cx.put(f"{BASE}/api/admin/settings", headers=auth_super,
                       json={"values": {"kindle.from": "selftest_sender@localhost",
                                        "kindle.smtp_host": "127.0.0.1", "kindle.smtp_port": "1"}})
            check("kindle settings accepted", r.status_code == 200, r.text[:120])
            me2 = cx.get(f"{BASE}/api/me", headers=auth_a).json()
            check("me.kindle true with device+SMTP", me2.get("kindle") is True, str(me2))
            me_b = cx.get(f"{BASE}/api/me", headers=auth_b).json()
            check("me.kindle false for user without devices", me_b.get("kindle") is False, str(me_b))
            r = cx.post(f"{BASE}/api/books/{b2['id']}/send-to-kindle", headers=auth_a,
                        json={"device_id": dev_a})
            check("kindle send smtp failure -> 502 readable",
                  r.status_code == 502 and "SMTP" in r.json().get("detail", ""),
                  f"{r.status_code} {r.text[:160]}")
            r = cx.post(f"{BASE}/api/books/{b2['id']}/send-to-kindle", headers=auth_b,
                        json={"device_id": dev_a})
            check("send with another user's device -> 404", r.status_code == 404,
                  f"{r.status_code} {r.text[:120]}")
            nl = upload(cx, t_alice, "newline title.epub", make_newline_epub())
            r = cx.post(f"{BASE}/api/books/{nl['book']['id']}/send-to-kindle", headers=auth_a,
                        json={"device_id": dev_a2})
            check("kindle newline title -> 502 readable (not 500)",
                  r.status_code == 502 and "SMTP" in r.json().get("detail", ""),
                  f"{r.status_code} {r.text[:160]}")
            mobi = upload(cx, t_alice, "Kindle Reject Probe.mobi", b"BOOKMOBI" + b"\x00" * 64)
            r = cx.post(f"{BASE}/api/books/{mobi['book']['id']}/send-to-kindle", headers=auth_a,
                        json={"device_id": dev_a})
            check("kindle rejects non-epub/pdf",
                  r.status_code == 502 and "only EPUB and PDF" in r.json().get("detail", ""),
                  f"{r.status_code} {r.text[:160]}")
            r = cx.delete(f"{BASE}/api/kindle/devices/{dev_a}", headers=auth_a)
            check("device delete by owner", r.status_code == 200, r.text[:120])
            r = cx.delete(f"{BASE}/api/kindle/devices/{dev_a2}", headers=auth_a)
            check("second device delete by owner", r.status_code == 200, r.text[:120])
            cx.put(f"{BASE}/api/admin/settings", headers=auth_super,
                   json={"values": {k: "" for k in kindle_smtp_keys}})  # leave nothing configured

        for path, label in [("/api/admin/zlib/limits", "zlib admin limits"),
                            ("/api/admin/zlib/history", "zlib admin history"),
                            ("/api/admin/zlib/library", "zlib admin library"),
                            ("/api/admin/zlib/booklists", "zlib admin booklists")]:
            r = cx.get(path, headers=auth_super)
            # 200 = backend answered (library/booklists carry 'available');
            # 503 = CLI/session unavailable (CI) — never a 5xx crash or 404 route
            check(label, r.status_code in (200, 503), str(r.status_code))
        r = cx.get("/api/admin/zlib/history", headers=auth_b)
        check("non-admin blocked from zlib admin", r.status_code == 403, str(r.status_code))

    # 16. static frontend must serve (a bad catch-all route 404s reader.html
    # — shipped once because nothing checked it) and revalidate on load
    for path, label in [("/", "index served"), ("/reader.html", "reader page served"),
                        ("/app.js", "app.js served"), ("/app.css", "app.css served"),
                        ("/foliate-js/epub.js", "foliate-js served")]:
        with httpx.Client(base_url=BASE) as cx:
            r = cx.get(path)
            check(label, r.status_code == 200 and r.headers.get("cache-control") == "no-cache",
                  f"{r.status_code} {r.headers.get('cache-control')}")

    print(f"\n{ok} passed, {total - ok} failed")
    return 0 if ok == total else 1


if __name__ == "__main__":
    sys.exit(main())

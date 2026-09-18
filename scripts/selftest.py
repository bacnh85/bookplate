#!/usr/bin/env python3
"""End-to-end self-test against a running server (default localhost:8480).

Generates a test corpus with ebooklib/pypdf, then verifies: auth, ingest,
metadata extraction chain, exact dedup, logical dup, sharing, FTS search,
covers, download, delete, OPDS, z-lib gating.

Usage: .venv/bin/python scripts/selftest.py [base_url]
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


def make_pdf(title: str | None, author: str | None) -> bytes:
    w = PdfWriter()
    w.add_blank_page(width=612, height=792)
    if title:
        w.add_metadata({"/Title": title, "/Author": author or "", "/Subject": "Networking"})
    data = io.BytesIO(); w.write(data)
    return data.getvalue()


def upload(cx, token, name, data):
    return cx.post(f"{BASE}/api/books",
                   files={"file": (name, data)},
                   headers={"Authorization": f"Bearer {token}"}).json()


def main():
    cx = httpx.Client(timeout=30)
    t_alice = cx.post(f"{BASE}/api/auth/register", json={"email": ALICE, "password": PASS}).json()["token"]
    t_bob = cx.post(f"{BASE}/api/auth/register", json={"email": BOB, "password": PASS}).json()["token"]
    check("register two users", t_alice and t_bob)

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
    cx.post(f"{BASE}/api/books/{b['id']}/share", json={"email": BOB},
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
    check("queue delete", r.status_code == 200, r.text[:200])
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

    # 15. static frontend must serve (a bad catch-all route 404s reader.html
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

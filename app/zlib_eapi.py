"""Z-Library EAPI probe — My Library / Booklists, NOT exposed by the zlib CLI (0.0.8).

The CLI only wraps /eapi/user/login, /eapi/book/search, /eapi/user/profile and
/eapi/user/book/downloaded (verified against heartleo/zlib source). This module
calls further undocumented /eapi endpoints directly, reusing the CLI's session
cookies (remix_userid/remix_userkey in ~/.config/zlib/session.json) — the same
EAPI transport the CLI itself uses, so DiamWall is not in the way.

SPIKE OUTCOME (probed live 2026-09-19 against https://z-lib.gd):
- My Library: FOUND — GET /eapi/user/book/saved -> {success:1, books:[...],
  pagination:{limit,current,next,total_items,total_pages}}. Page param: ?page=N.
  (This account had 0 saved books at probe time, so item field names are
  best-effort normalized — see _normalize_book.)
- Booklists: NOT FOUND — 12 /eapi candidates (eapi/booklists*, /eapi/user/booklists*,
  /eapi/lists) all answer the EAPI router 404 {"success":0,"error":"Requested page
  not found"}. Reported as {"available": false}; UI shows the section as
  unavailable. Upstream request tracked at heartleo/zlib.

Add/remove-to-library is deliberately not implemented: without a saved item's
real field shape, the mutation contract is unguessable — add it after observing
one real saved book.

If no candidate endpoint answers, callers get {"available": false} and the UI
shows the section as unavailable; upstream request tracked at heartleo/zlib.
"""
import json
import re
from pathlib import Path

import httpx

from .zlib_client import ZlibUnavailable, zlib

# endpoint candidates probed in order — first success=1 envelope wins
LIBRARY_CANDIDATES = [
    "/eapi/user/book/bookmarks",
    "/eapi/user/book/saved",
    "/eapi/user/savedbooks",
    "/eapi/bookmarks",
    "/eapi/user/book/library",
]
BOOKLIST_CANDIDATES = [
    "/eapi/booklists",
    "/eapi/user/booklists",
    "/eapi/booklists/user",
]

# kind -> winning endpoint (per process); a probed-negative kind caches as None
_winners: dict[str, str | None] = {}


def _session_file(account_id: int | None = None) -> Path:
    """Account pool: each account's CLI session lives in its own dir.
    None = legacy host session (~/.config/zlib)."""
    if account_id is not None:
        from .zlib_accounts import account_dir
        return account_dir(account_id) / ".config" / "zlib" / "session.json"
    return Path.home() / ".config" / "zlib" / "session.json"


def _session(account_id: int | None = None) -> dict | None:
    try:
        return json.loads(_session_file(account_id).read_text())
    except (OSError, ValueError):
        return None


def _looks_auth_error(body: str, status: int) -> bool:
    if status in (401, 403):
        return True
    return bool(re.search(r"\"(?:error|message)\"?\s*:?\s*\"?[^\"]*(?:auth|login|session|authorized)",
                          body, re.I))


def _normalize_book(b: dict) -> dict:
    """Best-effort map of an unknown-shape EAPI book object onto the search-row shape
    the frontend already renders. Unknown keys fall back to ''."""
    get = lambda *keys: next((b[k] for k in keys if b.get(k) not in (None, "")), "")
    return {
        "id": str(get("id", "book_id", "documentId")),
        "hash": str(get("hash", "hashId")),
        "name": get("name", "title", "book_title"),
        "authors": get("author", "authors"),
        "year": str(get("year", "year_of_book")),
        "extension": str(get("extension", "format", "file_type")).lower(),
        "size": str(get("size", "file_size")),
        "cover": get("cover", "image", "cover_url"),
        "language": get("language", "lang"),
    }


async def _call(endpoint: str, page: int, account_id: int | None = None) -> dict:
    """GET a known endpoint; on an auth error, re-login once and retry.
    The re-login and the retry run under the same account ctx."""
    from .zlib_client import with_account
    for attempt in (1, 2):
        s = _session(account_id)
        if not s or not s.get("domain"):
            return {"available": False}
        try:
            async with httpx.AsyncClient(timeout=20, follow_redirects=False) as cx:
                r = await cx.get(s["domain"].rstrip("/") + endpoint,
                                 params={"page": max(1, page)}, cookies=s["cookies"],
                                 headers={"User-Agent": "bookplate-eapi"})
        except httpx.HTTPError as e:
            raise ZlibUnavailable(f"Z-Library unreachable: {e}") from e
        if r.status_code == 200 and not _looks_auth_error(r.text, r.status_code):
            try:
                body = r.json()
            except ValueError:
                return {"available": False}
            if isinstance(body, dict) and body.get("success") == 1:
                raw = body.get("items") or body.get("books") or body.get("data") or []
                return {"available": True,
                        "items": [_normalize_book(b) for b in raw if isinstance(b, dict)],
                        "pagination": body.get("pagination") or {}}
            return {"available": False}  # endpoint answered but rejected the call
        if attempt == 1:
            try:
                if account_id is not None:
                    from . import zlib_accounts
                    acc = zlib_accounts.get(account_id)
                    if not acc:
                        return {"available": False}
                    ctx = {"id": account_id, "dir": str(zlib_accounts.account_dir(account_id)),
                           "domain": acc["domain"], "creds": (acc["email"], acc["password"])}
                    await with_account(ctx, zlib._login)
                else:
                    await zlib._login()  # fresh session, then re-read session.json
            except ZlibUnavailable:
                return {"available": False}
    return {"available": False}


async def _fetch(kind: str, candidates: list[str], page: int = 1,
                 account_id: int | None = None) -> dict:
    """Probe candidates for one feature; winner cached per process.
    Returns {"available": True, "items": [...], "pagination": {...}} or
    {"available": False}. A missing session triggers one CLI login (when creds
    exist) before giving up; a missing-session outcome is NOT cached (the next
    call retries after a login), only a probed-negative endpoint list is."""
    if kind in _winners:
        if _winners[kind]:
            return await _call(_winners[kind], page, account_id)
        return {"available": False}
    s = _session(account_id)
    if not s or not s.get("domain") or not s.get("cookies"):
        try:
            if account_id is not None:
                from . import zlib_accounts
                from .zlib_client import with_account
                acc = zlib_accounts.get(account_id)
                if not acc:
                    return {"available": False}
                ctx = {"id": account_id, "dir": str(zlib_accounts.account_dir(account_id)),
                       "domain": acc["domain"], "creds": (acc["email"], acc["password"])}
                await with_account(ctx, zlib._ensure_session)
            else:
                await zlib._ensure_session()  # creds from settings/env -> fresh session.json
            s = _session(account_id)
        except ZlibUnavailable:
            return {"available": False}  # no CLI/creds: report unavailable, don't cache
        if not s or not s.get("domain") or not s.get("cookies"):
            return {"available": False}
    for attempt in (1, 2):
        winner, auth_err = None, False
        try:
            async with httpx.AsyncClient(timeout=20, follow_redirects=False) as cx:
                for path in candidates:
                    r = await cx.get(s["domain"].rstrip("/") + path, cookies=s["cookies"],
                                     headers={"User-Agent": "bookplate-eapi"})
                    if r.status_code == 200 and not _looks_auth_error(r.text, r.status_code):
                        try:
                            body = r.json()
                        except ValueError:
                            continue
                        if isinstance(body, dict) and body.get("success") == 1:
                            winner = path
                            break
                    elif _looks_auth_error(r.text, r.status_code):
                        auth_err = True
        except httpx.HTTPError as e:
            raise ZlibUnavailable(f"Z-Library unreachable: {e}") from e
        if winner:
            _winners[kind] = winner
            return await _call(winner, page, account_id)
        if auth_err and attempt == 1:
            try:
                if account_id is not None:
                    from . import zlib_accounts
                    from .zlib_client import with_account
                    acc = zlib_accounts.get(account_id)
                    if not acc:
                        break
                    ctx = {"id": account_id, "dir": str(zlib_accounts.account_dir(account_id)),
                           "domain": acc["domain"], "creds": (acc["email"], acc["password"])}
                    await with_account(ctx, zlib._login)
                else:
                    await zlib._login()
                continue
            except ZlibUnavailable:
                break
        break
    _winners[kind] = None
    return {"available": False}


async def library(page: int = 1, account_id: int | None = None) -> dict:
    return await _fetch("library", LIBRARY_CANDIDATES, page, account_id)


async def booklists(account_id: int | None = None) -> dict:
    return await _fetch("booklists", BOOKLIST_CANDIDATES, 1, account_id)

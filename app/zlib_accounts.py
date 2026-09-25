"""Z-Library account pool: rotation, quota cache, cooldown, usage ledger.

The pool is server-wide (like the old single account); download jobs are NOT
bound to an account — the worker picks whatever enabled account still has
daily quota at claim time. One global lock serializes all CLI spawns (the
worker is sequential anyway; this also guards the /api/admin verify endpoint).

T&C posture: sessions persist per account dir (`data/zlib_accounts/<id>/` —
one-time login), downloads are sequential, `daily_allowed` is never exceeded.
"""
import asyncio
import json
import re
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .db import DATA_DIR, conn

# one CLI at a time, process-wide (single uvicorn process — see Dockerfile CMD)
POOL_LOCK = asyncio.Lock()
PROFILE_TTL = 600.0  # re-probe an account's profile at most every 10 min
# Dry-account downloads fail with the CLI's "EAPI returned no file link" (verified
# live against a 0-remaining account) — zero quota words in it, so "no file link"
# must count as quota-shaped or the worker backoffs instead of rotating.
QUOTA_ERR = re.compile(r"limit|quota|exceeded|daily|no file link", re.I)

# account_id -> {allowed, remaining, reset_at, checked_at, day}
_QUOTA: dict[int, dict] = {}
_RR = {"next": 0}  # round-robin start index (module state, single process)


def _mask_email(e: str) -> str:
    name, _, host = e.partition("@")
    if not host:
        return e
    return (name[:2] + "…" if len(name) > 2 else name + "…") + "@" + host


def accounts() -> list[sqlite3.Row]:
    with conn() as con:
        return con.execute(
            "SELECT * FROM zlib_accounts ORDER BY ord, id").fetchall()


def enabled_accounts() -> list[sqlite3.Row]:
    with conn() as con:
        return con.execute(
            "SELECT * FROM zlib_accounts WHERE enabled=1 ORDER BY ord, id").fetchall()


def configured() -> bool:
    """Z-Library usable = at least one enabled account with credentials."""
    with conn() as con:
        return bool(con.execute(
            "SELECT 1 FROM zlib_accounts WHERE enabled=1 LIMIT 1").fetchone())


def get(account_id: int) -> sqlite3.Row | None:
    with conn() as con:
        return con.execute("SELECT * FROM zlib_accounts WHERE id=?",
                           (account_id,)).fetchone()


def create(label: str, email: str, password: str, domain: str) -> sqlite3.Row:
    with conn() as con:
        try:
            cur = con.execute(
                "INSERT INTO zlib_accounts(label, email, password, domain) VALUES(?,?,?,?)",
                (label, email, password, domain))
        except sqlite3.IntegrityError:
            raise ValueError(f"account {email} already exists")
        return con.execute("SELECT * FROM zlib_accounts WHERE id=?",
                           (cur.lastrowid,)).fetchone()


def update(account_id: int, **fields) -> None:
    if not fields:
        return
    fields["updated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    cols = ", ".join(f"{k}=?" for k in fields)
    with conn() as con:
        con.execute(f"UPDATE zlib_accounts SET {cols} WHERE id=?",
                    (*fields.values(), account_id))


def delete(account_id: int) -> None:
    """Drop the row and its CLI session dir (revokes nothing server-side —
    the z-lib account itself keeps existing)."""
    with conn() as con:
        con.execute("DELETE FROM zlib_accounts WHERE id=?", (account_id,))
        con.execute("DELETE FROM zlib_usage WHERE account_id=?", (account_id,))
    shutil.rmtree(account_dir(account_id), ignore_errors=True)
    _QUOTA.pop(account_id, None)


def account_dir(account_id: int) -> Path:
    return DATA_DIR / "zlib_accounts" / str(account_id)


def record_event(account_id: int, event: str, limits: dict | None = None,
                 detail: str = "") -> None:
    with conn() as con:
        con.execute(
            "INSERT INTO zlib_usage(account_id, event, daily_amount, daily_allowed,"
            " daily_remaining, detail) VALUES(?,?,?,?,?,?)",
            (account_id, event,
             (limits or {}).get("daily_amount"), (limits or {}).get("daily_allowed"),
             (limits or {}).get("daily_remaining"), detail[:300]))


def usage(limit: int = 100) -> list[dict]:
    with conn() as con:
        rows = con.execute(
            "SELECT u.*, COALESCE(a.label, a.email) AS account"
            " FROM zlib_usage u LEFT JOIN zlib_accounts a ON a.id=u.account_id"
            " ORDER BY u.id DESC LIMIT ?", (max(1, min(500, limit)),)).fetchall()
    return [dict(r) for r in rows]


def _next_utc_midnight() -> str:
    """Daily windows reset at 00:00 UTC. ponytail: fallback for an empty
    daily_reset field — once the ledger accumulates real reset evidence, this
    can learn the true offset."""
    now = datetime.now(timezone.utc)
    nxt = now.replace(hour=0, minute=0, second=0, microsecond=0)
    try:
        nxt = nxt.replace(day=nxt.day + 1)
    except ValueError:  # month end: last second of month, next snapshot rolls over
        import calendar
        last = calendar.monthrange(now.year, now.month)[1]
        nxt = now.replace(day=last, hour=23, minute=59, second=59)
    return nxt.strftime("%Y-%m-%d %H:%M:%S")


def reset_at(parsed: str, day: str | None = None) -> str:
    """Best-effort reset time: the account's own daily_reset when it parses,
    else the next 00:00 UTC."""
    if parsed:
        m = re.match(r"(\d{2}):(\d{2})", parsed.strip())
        if m:
            now = datetime.now(timezone.utc)
            hh, mm = int(m.group(1)), int(m.group(2))
            try:
                nxt = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
                if nxt <= now:
                    nxt = nxt.replace(day=nxt.day + 1)
                return nxt.strftime("%Y-%m-%d %H:%M:%S")
            except ValueError:
                pass  # month-end overflow — fall through to the UTC-midnight fallback
    return _next_utc_midnight()


def snapshot(account_id: int, limits: dict) -> dict:
    """Store a fresh profile result in the quota cache; ledger the interesting
    transitions (reset = remaining went UP, or a new UTC day)."""
    try:
        allowed = int(limits.get("daily_allowed"))
        remaining = int(limits.get("daily_remaining"))
    except (TypeError, ValueError):
        return _QUOTA.get(account_id, {})
    prev = _QUOTA.get(account_id, {})
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if prev.get("day") != day and prev:
        record_event(account_id, "reset", limits, f"day rolled {prev.get('day')} -> {day}")
    elif remaining > prev.get("remaining", remaining) and prev:
        record_event(account_id, "reset", limits)
    elif remaining == 0 and prev.get("remaining", 1) > 0:
        record_event(account_id, "exhausted", limits)
    reset = reset_at(str(limits.get("daily_reset") or ""))
    _QUOTA[account_id] = {"allowed": allowed, "remaining": remaining,
                          "reset_at": reset, "checked_at": _now(),
                          "day": day, "amount": limits.get("daily_amount")}
    if not prev:
        record_event(account_id, "snapshot", limits)
    return _QUOTA[account_id]


def cached_quota(account_id: int, ttl: float | None = PROFILE_TTL) -> dict | None:
    q = _QUOTA.get(account_id)
    if q and (ttl is None or _now() - q["checked_at"] < ttl):
        return q
    return None


def _now() -> float:
    import time
    return time.monotonic()


def pool_exhausted() -> str | None:
    """Earliest reset time across cached quotas when every enabled account has
    remaining=0; None if any account (or the cache) says otherwise."""
    accs = enabled_accounts()
    if not accs:
        return None
    resets = []
    for a in accs:
        q = cached_quota(a["id"])
        if not q or q["remaining"] > 0:
            return None
        resets.append(q["reset_at"])
    return min(resets) if resets else None


def pick_next(exclude: set[int] = ()) -> sqlite3.Row | None:
    """Round-robin over enabled accounts (excluding `exclude`): the next one
    whose cached quota is not known-exhausted. Account with no cache yet counts
    as available (worker probes its profile before downloading)."""
    accs = [a for a in enabled_accounts() if a["id"] not in exclude]
    if not accs:
        return None
    n = len(accs)
    for i in range(n):
        a = accs[(_RR["next"] + i) % n]
        q = cached_quota(a["id"])
        if not q or q["remaining"] > 0:
            _RR["next"] = (_RR["next"] + i + 1) % n
            return a
    return None


def note_failure(account_id: int, err: str) -> None:
    update(account_id, last_error=err[:300])


def clear_error(account_id: int) -> None:
    if get(account_id) and get(account_id)["last_error"]:
        update(account_id, last_error="")


def delete_session_cache(account_id: int) -> None:
    """Credentials/domain changed: drop the CLI session dir + quota cache so the
    next call does a one-time login with the new values."""
    shutil.rmtree(account_dir(account_id) / ".config", ignore_errors=True)
    _QUOTA.pop(account_id, None)

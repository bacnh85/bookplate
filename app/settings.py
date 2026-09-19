"""App-managed settings (DB) — the DB is the only source, no env fallback.

Secrets (z-lib password, Anna's Archive key, AI api key) sit verbatim in
data/ebook.db — the same on-disk trust boundary .env.local had. They must be
replayable to third-party services, so they are stored as-is and only masked
in the admin API (main.py SECRET_SETTINGS/_mask).
"""
import os

from .db import conn

# valid keys (also the documentation of what the admin UI may write)
KEYS = (
    "zlib.email",
    "zlib.password",
    "zlib.domain",
    "annas.secret_key",
    "annas.base_url",
    "ai.enabled",
    "ai.api_key",
    "ai.base_url",
    "ai.model",
    # UI-only toggle
    "registration",
)

# env vars from the pre-DB-only era; seeded into the DB once on upgrade
LEGACY_ENV = {
    "zlib.email": "ZLIB_EMAIL",
    "zlib.password": "ZLIB_PASSWORD",
    "zlib.domain": "ZLIB_DOMAIN",
    "annas.secret_key": "ANNAS_ARCHIVE_SECRET_KEY",
    "annas.base_url": "ANNAS_BASE_URL",
    "ai.enabled": "ZAI_ENABLED",
    "ai.api_key": "ZAI_API_KEY",
    "ai.base_url": "ZAI_BASE_URL",
    "ai.model": "ZAI_MODEL",
}


def seed_legacy_env(con) -> None:
    """One-time import for deployments upgrading from the env-fallback era: a set
    legacy env var fills settings keys that have NEVER been configured, so
    z-lib/AA/AI keep working after the upgrade. Keys with a row (including an
    explicit "" from a UI "clear") are left alone; afterwards the DB is the sole
    source and env is never re-read. Runs on db.init()'s connection — WAL allows
    one writer, so a second connection would deadlock against the schema txn."""
    imported = []
    for key, var in LEGACY_ENV.items():
        val = os.getenv(var, "")
        if not val:
            continue
        row = con.execute("SELECT 1 FROM settings WHERE key=?", (key,)).fetchone()
        if row is None:
            con.execute("INSERT INTO settings(key, value) VALUES(?,?)", (key, val))
            imported.append(var)
    if imported:
        print("bookplate: imported legacy env into settings: " + ", ".join(imported)
              + " — env vars are no longer read; manage values in Admin → Settings",
              flush=True)


def get(key: str, default: str = "") -> str:
    with conn() as con:
        row = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row and row["value"] != "" else default


def set(key: str, value: str) -> None:
    with conn() as con:
        con.execute(
            "INSERT INTO settings(key, value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value))

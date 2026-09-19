"""App-managed settings (DB) with env fallback.

Precedence: a non-empty DB value wins; empty/missing falls back to the env var.
This keeps existing env-only deployments (Docker, .env.local) working, while the
admin UI writes DB values that then take over. Clearing a setting in the UI
(empty string) reverts to env. Secrets sit in data/ebook.db — the same on-disk
trust boundary .env.local had.
"""
import os

from .db import conn

# key -> env var used as fallback (also the documentation of valid keys)
ENV_FALLBACK = {
    "zlib.email": "ZLIB_EMAIL",
    "zlib.password": "ZLIB_PASSWORD",
    "zlib.domain": "ZLIB_DOMAIN",
    "annas.secret_key": "ANNAS_ARCHIVE_SECRET_KEY",
    "annas.base_url": "ANNAS_BASE_URL",
    "ai.enabled": "ZAI_ENABLED",
    "ai.api_key": "ZAI_API_KEY",
    "ai.base_url": "ZAI_BASE_URL",
    "ai.model": "ZAI_MODEL",
    # no env fallback: UI-only toggles
    "registration": "",
}


def get(key: str, default: str = "") -> str:
    with conn() as con:
        row = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    if row and row["value"] != "":
        return row["value"]
    env = ENV_FALLBACK.get(key, "")
    return os.getenv(env, default) if env else default


def set(key: str, value: str) -> None:
    with conn() as con:
        con.execute(
            "INSERT INTO settings(key, value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value))

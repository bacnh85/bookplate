#!/usr/bin/env python3
"""Refresh the Anna's Archive search-HTML fixture for scripts/test_annas.py.

Uses the real account key, seeding it into app settings (data/ebook.db) so the
app's own client can authenticate — same effect as saving it in Admin → Settings:
  ANNAS_ARCHIVE_SECRET_KEY=... .venv/bin/python scripts/fetch_annas_fixture.py

Run it again whenever Anna's Archive changes their search markup — the offline
parser tests then tell you exactly what drifted.
"""
import asyncio
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app.annas_client import annas

if os.getenv("ANNAS_ARCHIVE_SECRET_KEY"):
    from app import settings as _s
    _s.set("annas.secret_key", os.environ["ANNAS_ARCHIVE_SECRET_KEY"])

FIXTURE = pathlib.Path(__file__).resolve().parent / "fixtures" / "annas_search.html"


async def main() -> None:
    r = await annas._fetch_authed(f"{annas._base()}/search?q=terraform")
    print(f"search HTTP {r.status_code}, {len(r.text)} bytes")
    if r.status_code != 200:
        sys.exit("no HTML — aborting, fixture not touched")
    FIXTURE.parent.mkdir(exist_ok=True)
    FIXTURE.write_text(r.text)
    print(f"fixture written: {FIXTURE}")
    rows = await annas.search("terraform")
    print(f"parsed rows: {len(rows)}")
    for row in rows[:3]:
        print(" ", {k: row[k] for k in ("name", "authors", "extension", "size", "year", "language")})


if __name__ == "__main__":
    asyncio.run(main())

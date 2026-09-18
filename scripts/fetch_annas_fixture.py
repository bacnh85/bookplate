#!/usr/bin/env python3
"""Refresh the Anna's Archive search-HTML fixture for scripts/test_annas.py.

Uses the real account key from the environment (never echoed, never saved):
  set -a; . ./.env.local; set +a
  .venv/bin/python scripts/fetch_annas_fixture.py

Run it again whenever Anna's Archive changes their search markup — the offline
parser tests then tell you exactly what drifted.
"""
import asyncio
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app.annas_client import annas

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

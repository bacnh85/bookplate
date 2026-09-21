#!/usr/bin/env python3
"""One-off: backfill books.cover_color for existing covers (3D book 'case').

Usage: .venv/bin/python scripts/backfill_cover_colors.py
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import app.db as db  # noqa: E402
from app.metadata import cover_color  # noqa: E402
from app.storage import cover_path  # noqa: E402


def main():
    db.init()  # adds books.cover_color on DBs the upgraded server hasn't opened yet
    with db.conn() as con:
        rows = con.execute("SELECT id, sha256, cover_ext FROM books WHERE cover_ext IS NOT NULL").fetchall()
        done = 0
        for r in rows:  # recomputes ALL covers — safe to re-run after algorithm changes
            path = cover_path(r["sha256"], r["cover_ext"])
            if not path.exists():
                continue
            color = cover_color(path.read_bytes(), r["cover_ext"])
            if color:
                con.execute("UPDATE books SET cover_color=? WHERE id=?", (color, r["id"]))
                done += 1
        print(f"backfilled {done}/{len(rows)} cover colours")


if __name__ == "__main__":
    main()

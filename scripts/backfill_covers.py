#!/usr/bin/env python3
"""One-time backfill: give every cover-less book a thumbnail.

Per book, in order: PDF page-1 embedded image -> Google/OpenLibrary cover by
title -> deterministic generated SVG cover. Idempotent; run any time.

Usage: .venv/bin/python scripts/backfill_covers.py
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db, metadata
from app.storage import book_path, cover_path


async def main() -> None:
    with db.conn() as con:
        rows = [dict(r) for r in con.execute("SELECT * FROM books WHERE cover_ext IS NULL")]
    print(f"{len(rows)} books without covers")
    fixed = 0
    for b in rows:
        cover, ext = None, None
        if b["ext"] == "pdf":  # 1. embedded image on page 1
            path = book_path(b["sha256"], b["ext"])
            if path.exists():
                m = metadata.blank()
                metadata.from_pdf(path, m)
                if m["cover"]:
                    cover, ext = m["cover"], m["cover_ext"]
        if not cover and b["title"] and b["title"] != "Unknown title":  # 2. API cover
            m = metadata.blank()
            m["title"], m["authors"] = b["title"], b["authors"]
            await metadata.enrich(m)
            if m["cover"]:
                cover, ext = m["cover"], m["cover_ext"]
        if not cover:  # 3. generated SVG — guaranteed
            cover, ext = metadata.generated_cover(b["title"], b["authors"], b["sha256"]), "svg"
        cover_path(b["sha256"], ext).write_bytes(cover)
        with db.conn() as con:
            con.execute("UPDATE books SET cover_ext=? WHERE id=?", (ext, b["id"]))
        fixed += 1
        print(f"  + {b['title'][:60]} -> {ext}")
    print(f"done: {fixed}/{len(rows)} covered")


if __name__ == "__main__":
    asyncio.run(main())

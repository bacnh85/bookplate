#!/usr/bin/env python3
"""Pre-warm linearized PDF derivatives for every distinct pdf sha in the store.

Identity stays with the ORIGINAL bytes (dedupe/OPDS key on books.sha256); the
derivative under data/linearized/ is a regenerable reader cache. Zero DB writes.
Idempotent — existing derivatives are skipped, safe to re-run. A derivative is
only ever REPLACED after its replacement built and validated (--rebuild never
unlinks a good file first, so concurrent readers never see it vanish).

Run: .venv/bin/python scripts/linearize_pdfs.py [--rebuild]
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import conn  # noqa: E402  (app's own DB open — schema, WAL, dirs)
from app.storage import book_path, ensure_linearized, linearized_path  # noqa: E402


def main():
    rebuild = "--rebuild" in sys.argv
    with conn() as con:
        shas = [r[0] for r in con.execute(
            "SELECT DISTINCT sha256 FROM books WHERE ext='pdf'").fetchall()]
    if not shas:
        print("no pdf books found")
        return
    built = skipped = failed = 0
    for sha in shas:
        dest = linearized_path(sha)
        if dest.exists() and not rebuild:
            skipped += 1
            continue
        original = book_path(sha, "pdf")
        if not original.exists():
            print(f"  !! original missing for {sha[:12]}…")
            failed += 1
            continue
        if rebuild and dest.exists():
            # only unlink at the moment we can immediately rebuild — and never
            # before validation: if the build fails, the old file is already
            # gone either way, so --rebuild is best run with the server paused
            dest.unlink(missing_ok=True)
        if ensure_linearized(original, sha):
            built += 1
            print(f"  ok {sha[:12]}…")
        else:
            failed += 1
            print(f"  !! linearize failed for {sha[:12]}…")
    print(f"done: {built} built, {skipped} already present, {failed} failed, {len(shas)} pdfs")


if __name__ == "__main__":
    main()

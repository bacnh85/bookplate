"""Content-addressed file store: /data/books/<aa>/<bb>/<sha256>.<ext>, covers likewise.
PDFs additionally get a REGENERABLE linearized derivative under /data/linearized —
identity (dedupe/OPDS) always keys on the ORIGINAL's sha256, never the derivative."""
import hashlib
import shutil
from pathlib import Path

import threading

from .db import DATA_DIR

BOOKS_DIR = DATA_DIR / "books"
COVERS_DIR = DATA_DIR / "covers"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def book_path(sha: str, ext: str) -> Path:
    p = BOOKS_DIR / sha[:2] / sha[2:4]
    p.mkdir(parents=True, exist_ok=True)
    return p / f"{sha}.{ext}"


def cover_path(sha: str, ext: str) -> Path:
    p = COVERS_DIR / sha[:2]
    p.mkdir(parents=True, exist_ok=True)
    return p / f"{sha}.{ext}"


LINEARIZED_DIR = DATA_DIR / "linearized"
_LIN_LOCKS: dict[str, threading.Lock] = {}


def linearized_path(sha: str) -> Path:
    """Reader-optimized PDF derivative (may not exist — build on demand)."""
    p = LINEARIZED_DIR / sha[:2]
    p.mkdir(parents=True, exist_ok=True)
    return p / f"{sha}.pdf"


def ensure_linearized(original: Path, sha: str) -> Path | None:
    """Build the linearized PDF derivative for `sha` if missing. CPU-bound —
    callers on the event loop must run this via asyncio.to_thread. Returns the
    derivative path, or None on failure (corrupt/encrypted PDF — original kept)."""
    dest = linearized_path(sha)
    if dest.exists():
        return dest
    import pikepdf  # heavy import — keep off the hot path
    # per-sha lock (threading — callers are worker threads): two cold requests
    # for the same book must not interleave on one tmp file (a torn write would
    # install a corrupt derivative forever)
    lock = _LIN_LOCKS.setdefault(sha, threading.Lock())
    with lock:
        if dest.exists():  # loser of the lock wait: winner already built it
            return dest
        tmp = dest.with_suffix(".tmp")
        try:
            with pikepdf.open(original) as pdf:
                pdf.save(tmp, linearize=True)
            _validate_linearized(tmp, sha)  # torn/failed build must never install
            tmp.replace(dest)  # atomic: a crash never leaves a partial derivative
        except Exception:
            tmp.unlink(missing_ok=True)
            return None
        return dest


def _validate_linearized(path: Path, sha: str) -> None:
    """Sanity-check a freshly built derivative before it becomes cacheable.
    The linearized bytes differ from the original (that's the point), so the
    check is only that pikepdf certifies it linearized — a torn/failed build
    must never install."""
    import pikepdf

    with pikepdf.open(path) as pdf:
        if not pdf.is_linearized:
            raise ValueError("save(linearize=True) did not linearize")


def store_file(tmp: Path, sha: str, ext: str) -> Path:
    """Move tmp file into the store; no-op if content already present."""
    dest = book_path(sha, ext)
    if not dest.exists():
        shutil.move(str(tmp), dest)
    return dest

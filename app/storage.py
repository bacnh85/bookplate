"""Content-addressed file store: /data/books/<aa>/<bb>/<sha256>.<ext>, covers likewise."""
import hashlib
import shutil
from pathlib import Path

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


def store_file(tmp: Path, sha: str, ext: str) -> Path:
    """Move tmp file into the store; no-op if content already present."""
    dest = book_path(sha, ext)
    if not dest.exists():
        shutil.move(str(tmp), dest)
    return dest

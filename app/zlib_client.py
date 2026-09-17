"""Z-Library adapter over the `zlib` CLI (brew: heartleo/tap/zlib, EAPI mobile-app API).

Why the CLI: z-lib's clearnet mirrors wall raw HTTP clients behind DiamWall
(JS proof-of-work) and the onion host sits behind Cloudflare — see plan Finding 2.
The CLI solves both automatically and exposes --json for search/profile.

One-time setup on the host:
  brew install heartleo/tap/zlib
  zlib login --eapi --email you@x --password ... --domain https://z-lib.gd
Session persists in ~/.config/zlib. If it expires and ZLIB_EMAIL/ZLIB_PASSWORD
are set (.env.local), this adapter re-logins automatically.
"""
import asyncio
import json
import os
import shutil
import tempfile
from pathlib import Path

from .db import DATA_DIR


class ZlibUnavailable(Exception):
    pass


class Zlib:
    async def _run(self, *args: str, timeout: float = 90) -> tuple[int, str, str]:
        proc = await asyncio.create_subprocess_exec(
            "zlib", *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout)
        except asyncio.TimeoutError:
            proc.kill()
            raise ZlibUnavailable(f"zlib {args[0]} timed out after {int(timeout)}s")
        return (proc.returncode or 0,
                out.decode(errors="replace"),
                err.decode(errors="replace"))

    def _require_cli(self) -> None:
        if not shutil.which("zlib"):
            raise ZlibUnavailable(
                "zlib CLI not installed — brew install heartleo/tap/zlib, then "
                "zlib login --eapi --email ... --password ... --domain https://z-lib.gd")

    @property
    def enabled(self) -> bool:
        return shutil.which("zlib") is not None

    async def _ensure_session(self) -> None:
        """Re-login from .env.local creds if the CLI has no session yet."""
        self._require_cli()
        cfg = Path.home() / ".config" / "zlib"
        if cfg.is_dir() and any(cfg.iterdir()):
            return  # CLI already manages state here (session.json/.env/config.json)
        if os.getenv("ZLIB_EMAIL") and os.getenv("ZLIB_PASSWORD"):
            rc, out, err = await self._run(
                "login", "--eapi", "--email", os.getenv("ZLIB_EMAIL"),
                "--password", os.getenv("ZLIB_PASSWORD"), timeout=120)
            if rc != 0:
                raise ZlibUnavailable(f"Z-Library login failed: {(err or out).strip()[:200]}")

    async def search(self, q: str, count: int = 20) -> list[dict]:
        await self._ensure_session()
        rc, out, err = await self._run("search", q, "--json", timeout=90)
        if rc != 0:
            raise ZlibUnavailable(f"Z-Library search failed: {(err or out).strip()[:200]}")
        try:
            books = json.loads(out).get("books", [])
        except json.JSONDecodeError as e:
            raise ZlibUnavailable(f"Z-Library search returned junk: {e}") from e
        return [{
            "id": str(b.get("id", "")), "name": b.get("name", ""),
            "authors": ", ".join(b.get("authors") or []),
            "year": str(b.get("year") or ""),
            "extension": (b.get("extension") or "").lower(),
            "size": b.get("size", ""), "cover": b.get("cover", ""),
            "language": b.get("language", ""), "rating": str(b.get("rating", "")),
        } for b in books[:count]]

    async def limits(self) -> dict:
        await self._ensure_session()
        rc, out, err = await self._run("profile", "--json", timeout=60)
        if rc != 0:
            raise ZlibUnavailable(f"Z-Library profile failed: {(err or out).strip()[:200]}")
        return json.loads(out)

    async def download(self, book_id: str) -> tuple[bytes, dict]:
        """Returns (file_bytes, meta) — meta['_filename'] is the CLI's own filename."""
        await self._ensure_session()
        tmp = DATA_DIR / "tmp"
        tmp.mkdir(exist_ok=True)
        out_dir = Path(tempfile.mkdtemp(dir=tmp))
        try:
            rc, out, err = await self._run(
                "download", str(book_id), "--dir", str(out_dir), timeout=600)
            files = list(out_dir.iterdir())
            if rc != 0 or not files:
                hint = (err or out).strip()[:200]
                raise ZlibUnavailable(f"Z-Library download failed: {hint or 'no file produced'}")
            f = files[0]
            return f.read_bytes(), {"_filename": f.name}
        finally:
            shutil.rmtree(out_dir, ignore_errors=True)


zlib = Zlib()

"""Z-Library adapter over the `zlib` CLI (brew: heartleo/tap/zlib, EAPI mobile-app API).

Why the CLI: z-lib's clearnet mirrors wall raw HTTP clients behind DiamWall
(JS proof-of-work) and the onion host sits behind Cloudflare — see plan Finding 2.
The CLI solves both automatically and exposes --json for search/profile.

One-time setup on the host:
  brew install heartleo/tap/zlib
  zlib login --eapi --email you@x --password ... --domain https://z-lib.gd
Session persists in ~/.config/zlib. If a call fails because the session is missing
or expired and ZLIB_EMAIL/ZLIB_PASSWORD are set (.env.local), this adapter re-logins
and retries once. NOTE: the CLI has no stdin/env password input, so the password
transits argv for the duration of a login (ps-visible on multi-user hosts).
"""
import asyncio
import html
import json
import os
import re
import shutil
import tempfile
from pathlib import Path

from .db import DATA_DIR


class ZlibUnavailable(Exception):
    pass


def _clean_desc(s: str) -> str:
    """z-lib descriptions are third-party HTML — reduce to plain text.
    Tags are stripped before entity decoding; the frontend renders this as
    textContent, so nothing here can execute."""
    text = re.sub(r"<[^>]+>", " ", s or "")
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


class Zlib:
    async def _run(self, *args: str, timeout: float = 90) -> tuple[int, str, str]:
        proc = await asyncio.create_subprocess_exec(
            "zlib", *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()  # reap the killed process (else it lingers as a zombie)
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

    def _creds(self) -> tuple[str, str] | None:
        email, password = os.getenv("ZLIB_EMAIL"), os.getenv("ZLIB_PASSWORD")
        return (email, password) if email and password else None

    async def _login(self) -> None:
        """Fresh login, overwriting any stale session."""
        creds = self._creds()
        if not creds:
            raise ZlibUnavailable("Z-Library login needs ZLIB_EMAIL/ZLIB_PASSWORD")
        domain = os.getenv("ZLIB_DOMAIN", "https://z-lib.gd")
        rc, out, err = await self._run(
            "login", "--eapi", "--email", creds[0], "--password", creds[1],
            "--domain", domain, timeout=120)
        if rc != 0:
            raise ZlibUnavailable(f"Z-Library login failed: {(err or out).strip()[:200]}")

    async def _ensure_session(self) -> None:
        """Login from env creds if the CLI has no session yet (expiry is handled
        by the re-login+retry in _run_authed)."""
        self._require_cli()
        cfg = Path.home() / ".config" / "zlib"
        if cfg.is_dir() and any(cfg.iterdir()):
            return  # CLI manages state here; a stale session is handled on failure
        if self._creds():
            await self._login()

    async def _run_authed(self, *args: str, timeout: float = 90):
        """Run a session-requiring command; on failure, force one re-login and retry
        (covers sessions that expired on disk — _ensure_session can't see that).
        Exactly one retry: the follow-up runs via plain _run, not recursively."""
        rc, out, err = await self._run(*args, timeout=timeout)
        if rc == 0 or not self._creds():
            return rc, out, err
        await self._run("logout", timeout=30)  # best effort: drop the stale session
        await self._login()
        return await self._run(*args, timeout=timeout)

    async def search(self, q: str, count: int = 20) -> list[dict]:
        await self._ensure_session()
        rc, out, err = await self._run_authed("search", q, "--json", "-n", str(count), timeout=90)
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
            "publisher": b.get("publisher", ""), "isbn": b.get("isbn", ""),
            "quality": str(b.get("quality", "")), "url": b.get("url", ""),
            "description": _clean_desc(b.get("description", "")),
        } for b in books[:count]]

    async def limits(self) -> dict:
        await self._ensure_session()
        rc, out, err = await self._run_authed("profile", "--json", timeout=60)
        if rc != 0:
            raise ZlibUnavailable(f"Z-Library profile failed: {(err or out).strip()[:200]}")
        try:
            return json.loads(out)
        except json.JSONDecodeError as e:
            raise ZlibUnavailable(f"Z-Library profile returned junk: {e}") from e

    async def download(self, book_id: str) -> tuple[bytes, dict]:
        """Returns (file_bytes, meta) — meta['_filename'] is the CLI's own filename."""
        await self._ensure_session()
        tmp = DATA_DIR / "tmp"
        tmp.mkdir(exist_ok=True)
        out_dir = Path(tempfile.mkdtemp(dir=tmp))
        try:
            rc, out, err = await self._run_authed(
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

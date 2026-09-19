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
from . import settings


class ZlibUnavailable(Exception):
    pass


class ZlibConfigError(ZlibUnavailable):
    """Permanent configuration problem (CLI missing, no credentials) — retries can't help."""


def _clean_desc(s: str) -> str:
    """z-lib descriptions are third-party HTML — reduce to plain text.
    Tags are stripped before entity decoding; the frontend renders this as
    textContent, so nothing here can execute."""
    text = re.sub(r"<[^>]+>", " ", s or "")
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def parse_size(s: str) -> int | None:
    """'1.8 MB' -> bytes (approximate; display strings round). None if unparseable."""
    try:
        m = re.match(r"([\d.]+)\s*(B|kB|MB|GB|TB)", (s or "").strip(), re.I)
        if not m:
            return None
        mult = {"b": 1, "kb": 10**3, "mb": 10**6, "gb": 10**9, "tb": 10**12}[m.group(2).lower()]
        return int(float(m.group(1)) * mult)
    except ValueError:  # e.g. "1.2.3 MB" — malformed input must not crash the worker
        return None


class Zlib:
    async def _spawn(self, *args: str):
        """Process seam (unit tests patch this): one zlib CLI invocation."""
        return await asyncio.create_subprocess_exec(
            "zlib", *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)

    async def _run(self, *args: str, timeout: float = 90) -> tuple[int, str, str]:
        proc = await self._spawn(*args)
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
            raise ZlibConfigError(
                "zlib CLI not installed — brew install heartleo/tap/zlib, then "
                "zlib login --eapi --email ... --password ... --domain https://z-lib.gd")

    @property
    def enabled(self) -> bool:
        return shutil.which("zlib") is not None

    def _creds(self) -> tuple[str, str] | None:
        email, password = settings.get("zlib.email"), settings.get("zlib.password")
        return (email, password) if email and password else None

    async def _login(self) -> None:
        """Fresh login, overwriting any stale session."""
        creds = self._creds()
        if not creds:
            raise ZlibConfigError("Z-Library login needs ZLIB_EMAIL/ZLIB_PASSWORD")
        domain = settings.get("zlib.domain", "https://z-lib.gd")
        rc, out, err = await self._run(
            "login", "--eapi", "--email", creds[0], "--password", creds[1],
            "--domain", domain, timeout=120)
        if rc != 0:
            raise ZlibUnavailable(f"Z-Library login failed: {(err or out).strip()[:200]}")

    async def _ensure_session(self) -> None:
        """Login from env/DB creds when the CLI has no session, and RE-login when
        the configured domain changed since the stored session (the CLI pins the
        domain inside session.json — a Settings change alone would be ignored).
        Session expiry is handled by the re-login+retry in _run_authed."""
        self._require_cli()
        cfg = Path.home() / ".config" / "zlib"
        if cfg.is_dir() and any(cfg.iterdir()):
            if self._creds():
                try:
                    sess = json.loads((cfg / "session.json").read_text())
                except (OSError, ValueError):
                    sess = {}
                want = settings.get("zlib.domain", "https://z-lib.gd").rstrip("/")
                have = str(sess.get("domain", "")).rstrip("/")
                if have and want and have != want:
                    await self._login()
            return
        if self._creds():
            await self._login()

    async def _run_authed(self, *args: str, timeout: float = 90):
        """Run a session-requiring command; on failure, force one re-login and retry
        when the failure looks like an auth/session problem (covers sessions that
        expired on disk — _ensure_session can't see that). Other failures return
        unchanged: no re-auth on transient mirror errors (a logout would wipe a
        still-valid session). Exactly one retry: the follow-up runs via plain
        _run, not recursively."""
        rc, out, err = await self._run(*args, timeout=timeout)
        if rc == 0 or not self._creds():
            return rc, out, err
        if not any(s in (err + out).lower() for s in ("session", "login", "auth", "401")):
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
            parsed = json.loads(out)
            books = parsed.get("books", []) if isinstance(parsed, dict) else None
            if books is None:
                raise ValueError("unexpected JSON shape")
        except (json.JSONDecodeError, ValueError) as e:
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
            parsed = json.loads(out)
            if not isinstance(parsed, dict):
                raise ValueError("unexpected JSON shape")
            return parsed
        except (json.JSONDecodeError, ValueError) as e:
            raise ZlibUnavailable(f"Z-Library profile returned junk: {e}") from e

    async def history(self, page: int = 1, fmt: str = "") -> dict:
        """Account download history (CLI: /eapi/user/book/downloaded).
        Items carry id in 'id:hash' EAPI form — directly queueable via zlib.download."""
        await self._ensure_session()
        args = ["history", "--json", "-p", str(max(1, page))]
        if fmt:
            args += ["-f", fmt]
        rc, out, err = await self._run_authed(*args, timeout=60)
        if rc != 0:
            raise ZlibUnavailable(f"Z-Library history failed: {(err or out).strip()[:200]}")
        try:
            parsed = json.loads(out)
            items = parsed.get("items") if isinstance(parsed, dict) else None
            if items is None:
                raise ValueError("unexpected JSON shape")
        except (json.JSONDecodeError, ValueError) as e:
            raise ZlibUnavailable(f"Z-Library history returned junk: {e}") from e
        return {"items": items, "page": parsed.get("page", page),
                "total_pages": parsed.get("total_pages", 1)}

    async def download(self, book_id: str,
                       on_progress=None, expected_size: int | None = None) -> tuple[bytes, dict]:
        """Returns (file_bytes, meta) — meta['_filename'] is the CLI's own filename.
        The CLI emits no progress output; on_progress(done, total) polls the file
        growing in the download dir each second (total is the approximate size
        parsed from the search row, may be None). Auth failures re-login and
        retry once, same contract as _run_authed."""
        await self._ensure_session()
        tmp = DATA_DIR / "tmp"
        tmp.mkdir(exist_ok=True)
        out_dir = Path(tempfile.mkdtemp(dir=tmp))

        async def once() -> tuple[int, str, str]:
            proc = await self._spawn("download", str(book_id), "--dir", str(out_dir))
            comm = asyncio.ensure_future(proc.communicate())
            loop = asyncio.get_event_loop()
            deadline = loop.time() + 600
            while not comm.done():
                if loop.time() > deadline:
                    proc.kill()
                    await proc.wait()
                    raise ZlibUnavailable("zlib download timed out after 600s")
                await asyncio.wait({comm}, timeout=1.0)
                if on_progress:
                    done = sum(f.stat().st_size for f in out_dir.iterdir() if f.is_file())
                    on_progress(done, expected_size)
            out, err = comm.result()
            return (proc.returncode or 0,
                    out.decode(errors="replace"), err.decode(errors="replace"))

        try:
            rc, out, err = await once()
            if rc != 0 and self._creds() and any(
                    s in (err + out).lower() for s in ("session", "login", "auth", "401")):
                await self._run("logout", timeout=30)  # best effort: drop the stale session
                await self._login()
                for f in out_dir.iterdir():  # drop partial files from the failed attempt
                    if f.is_file():
                        f.unlink()
                rc, out, err = await once()
            files = list(out_dir.iterdir())
            if rc != 0 or not files:
                hint = (err or out).strip()[:200]
                raise ZlibUnavailable(f"Z-Library download failed: {hint or 'no file produced'}")
            f = max(files, key=lambda p: p.stat().st_mtime)  # newest = the delivered file
            if on_progress:
                on_progress(f.stat().st_size, f.stat().st_size)
            return f.read_bytes(), {"_filename": f.name}
        finally:
            shutil.rmtree(out_dir, ignore_errors=True)


zlib = Zlib()

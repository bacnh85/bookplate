"""Anna's Archive adapter (httpx, member secret key) — search + fast download.

Verified against live mirrors 2026-09:
- No search API. /search is DDoS-Guard-challenged for anonymous clients (302 to
  the same path with check=1); logging in via POST /account/ (form field `key`)
  yields aa_* session cookies that skip the challenge.
- Official download API: /dyn/api/fast_download.json?md5=&key= -> 200/204 with
  `download_url` (documentation is served by the endpoint itself). The URL
  redirects to partner servers — fetched via app/webfetch.py (pinned, re-validated hops).
- Mirrors rotate; the base URL is admin-Settings- or ANNAS_BASE_URL-overridable (default https://annas-archive.gd).

The secret key lives in admin Settings (DB) or the env fallback; the session cookie is
cached in RAM only and
never logged or persisted. Search HTML parsing is per-card isolated so one
malformed result can't poison the rest; md5s are validated 32-hex (untrusted input).
"""
import asyncio
import hashlib
import html
import os
import re
import time
from urllib.parse import quote_plus, unquote, urljoin, urlsplit

import httpx

from . import metadata, settings
from .webfetch import _fetch_bytes

# ponytail: book bytes buffered in RAM like the z-lib CLI path; stream to disk
# if real books ever approach this.
BOOK_MAX_BYTES = 512 * 1024 * 1024

# A browser-like UA keeps DDoS-Guard from serving a challenge page.
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}

SESSION_TTL_S = 6 * 3600  # optimization only; expiry is really handled on challenge
MAX_HOPS = 5

# Free (no-membership) slow downloads: /slow_download/<md5>/<path>/<domain> pages.
# Preferred indices first — index 2 serves an immediate link (no waitlist) in AA's
# current config; waitlisted ones need one reload after wait_seconds (the site's
# own countdown JS is literally a page reload). Indices beyond AA's current 3 are
# tried so the loop survives their server-count changes. ponytail: probed
# linearly; switch to parsing the /md5/ page's server list if this ever misfires.
SLOW_DOMAIN_INDICES = (2, 3, 1, 0, 4, 5)
SLOW_MAX_WAIT_S = 600   # AA waitlists run on a 10-minute modulo
SLOW_POLL_S = 30        # keep the worker's job row fresh during long waits

_D3_URL_RE = re.compile(r'href="(https?://[^"]+/d3/[^"]+)"')
_WAIT_RE = re.compile(r'waitSeconds\s*=\s*(\d+)')
# only formats the in-browser reader can open — anything else is refused rather
# than downloaded and then stranded on the shelf (see app/metadata.py EXTS)
_GOOD_FILE_EXTS = metadata.EXTS


def _guard_hint(base: str) -> str:
    return (f"Anna's Archive bot check (DDoS-Guard): open {base} in a browser on this "
            "server's network and complete the check once — after that, search and "
            "downloads work (members' fast downloads work even without this)")


class AnnasUnavailable(Exception):
    pass


class AnnasConfigError(AnnasUnavailable):
    """Permanent configuration problem (missing/rejected key) — retries can't help."""


class _Challenge(Exception):
    """DDoS-Guard interstitial: 403 from ddos-guard, or a redirect with check=1."""


def _is_challenge(r: httpx.Response) -> bool:
    if r.status_code == 403 and "ddos-guard" in (r.headers.get("server") or "").lower():
        return True
    loc = r.headers.get("location") or ""
    return 300 <= r.status_code < 400 and "check=1" in loc


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s))).strip()


_ANCHOR_RE = re.compile(r'<a\b([^>]*\bjs-vim-focus\b[^>]*)>(.*?)</a>', re.S)


def _anchor_md5(attrs: str) -> str | None:
    m = re.search(r'href="/md5/([a-f0-9]{32})"', attrs)
    return m.group(1) if m else None


def _ext_from_meta(meta_text: str) -> str:
    """'English [en] · EPUB · 0.4MB · 2014 · 📕 Book (fiction) · …' -> 'epub'.
    Scan ·-separated tokens; first short alphanumeric token starting with a
    letter and without '[' wins (that skips language '[en]' and reaches the
    extension before 'Book (…)'). Digits allowed after char 1 (fb2, azw3)."""
    for seg in meta_text.split("·"):
        tok = seg.strip().lower()
        if tok and "[" not in tok and len(tok) <= 6 and re.fullmatch(r"[a-z][a-z0-9-]*", tok):
            return tok
    return ""


def _parse_card(card: str, base: str) -> dict | None:
    """One result slice (from its title anchor to the next). Untrusted input —
    every extracted value is a plain string; md5 was validated by the anchor regex."""
    m = _ANCHOR_RE.search(card)
    if not m:
        return None
    md5 = _anchor_md5(m.group(1))
    if not md5:
        return None
    title = _clean(m.group(2))

    # author: the anchor CONTAINS the user-edit icon, name is the text after it
    author = ""
    im = re.search(r'icon-\[mdi--user-edit\][^>]*>\s*</span>', card)
    if im:
        am = re.search(r'(.*?)</a>', card[im.end():], re.S)
        if am:
            author = _clean(am.group(1))

    # cover: img inside the per-record cover div
    cover = ""
    cm = re.search(r'id="list_cover_aarecord_id__', card)
    if cm:
        im2 = re.search(r'<img\b[^>]*\bsrc="([^"]+)"', card[cm.end():])
        if im2:
            src = html.unescape(im2.group(1))
            cover = src if src.startswith("http") else urljoin(base + "/", src.lstrip("/"))

    # meta line: <div class="font-semibold text-sm leading-[1.2] mt-2">English [en] · EPUB ·
    # 0.4MB · 2014 · 📕 Book (fiction) · 🚀/… · <a…> — leaf div, text to first </div>.
    # Anchored on the div tag: author anchors also carry leading-[1.2].
    meta_text = ""
    dm = re.search(r'<div[^>]*font-semibold[^>]*leading-\[1\.2\][^>]*>', card)
    if dm:
        tm = re.search(r'>(.*?)</div>', card[dm.end() - 1:], re.S)
        if tm:
            meta_text = _clean(tm.group(1))

    size = ""
    sm = re.search(r'\b[\d.]+\s*[kMGT]?B\b', meta_text)
    if sm:
        size = sm.group(0)
    ym = re.search(r'\b(1[89]\d{2}|20\d{2})\b', meta_text)
    year = ym.group(1) if ym else ""
    lang = ""
    lm = re.match(r'([A-Za-z][A-Za-z ]*?)\s*\[', meta_text)
    if lm:
        lang = lm.group(1).strip()

    return {"id": md5, "name": title, "authors": author, "year": year,
            "extension": _ext_from_meta(meta_text), "size": size, "cover": cover,
            "language": lang, "rating": "", "publisher": "", "isbn": "",
            "quality": "", "url": f"{base}/md5/{md5}", "description": "",
            "source": "annas"}


def parse_search_results(page: str, base: str) -> list[dict]:
    """Slice the page at each result title anchor so card parsing stays scoped
    and isolated (one malformed card can't leak into the next)."""
    rows: list[dict] = []
    starts = [m.start() for m in _ANCHOR_RE.finditer(page)]
    for i, start in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else len(page)
        row = _parse_card(page[start:end], base)
        if row:
            rows.append(row)
    return rows


class Annas:
    def __init__(self) -> None:
        self._cookie = ""
        self._cookie_at = 0.0

    @property
    def enabled(self) -> bool:
        return bool(settings.get("annas.secret_key", "").strip())

    def _base(self) -> str:
        return settings.get("annas.base_url", "https://annas-archive.gd").rstrip("/")

    def _key(self) -> str:
        key = settings.get("annas.secret_key", "").strip()
        if not key:
            raise AnnasConfigError(
                "Anna's Archive needs a secret key (admin Settings, from your AA account page)")
        return key

    async def _login(self) -> None:
        """POST the secret key to /account/ — an aa_* session cookie comes back.
        A bad key returns the login form with no cookie."""
        try:
            async with httpx.AsyncClient(timeout=30, follow_redirects=False) as cx:
                r = await cx.post(f"{self._base()}/account/", data={"key": self._key()},
                                  headers=HEADERS)
        except httpx.HTTPError as e:
            raise AnnasUnavailable(f"Anna's Archive unreachable: {e}") from e
        if _is_challenge(r):
            # a guard-challenged login issues no aa_* cookie — without this check
            # it would masquerade as "rejected the secret key" (permanent) instead
            # of the retryable bot-check hint
            raise AnnasUnavailable(_guard_hint(self._base()))
        # success REQUIRES an aa_* session cookie — the guard issues __ddg* even on
        # the bad-key form, so accepting any cookie would cache a dead session and
        # resurface as a misleading "bot check" later
        if not any(c.name.startswith("aa_") for c in r.cookies.jar):
            raise AnnasConfigError("Anna's Archive rejected the secret key")
        # keep ALL cookies (aa_* session + __ddg* guard state) — a browser would
        # carry them all; the guard clears on the pair, not the session alone
        cookie = "; ".join(f"{c.name}={c.value}" for c in r.cookies.jar)
        self._cookie, self._cookie_at = cookie, time.monotonic()

    async def _fetch_authed(self, url: str) -> httpx.Response:
        """GET with the session cookie, following redirects by hand so a
        check=1 challenge hop is reported, not followed. On challenge: one
        forced re-login and retry (covers expired cached cookies)."""
        if not self._cookie or time.monotonic() - self._cookie_at > SESSION_TTL_S:
            await self._login()

        async def once() -> httpx.Response:
            hop, cur = 0, url
            base_host = httpx.URL(self._base()).host
            while True:
                try:
                    # the member session cookie never leaves the configured
                    # mirror's host — a hostile/injected redirect can't exfiltrate it
                    headers = {**HEADERS}
                    if httpx.URL(cur).host == base_host:
                        headers["Cookie"] = self._cookie
                    async with httpx.AsyncClient(timeout=30, follow_redirects=False) as cx:
                        r = await cx.get(cur, headers=headers)
                except httpx.HTTPError as e:
                    raise AnnasUnavailable(f"Anna's Archive unreachable: {e}") from e
                if _is_challenge(r):
                    raise _Challenge()
                if 300 <= r.status_code < 400 and r.headers.get("location"):
                    nxt = urljoin(cur, r.headers["location"])
                    if httpx.URL(nxt).host != base_host:
                        # same threat model as webfetch: a compromised mirror must
                        # not point the server at arbitrary hosts (the cookie is
                        # withheld, but the GET itself still leaks intent)
                        raise AnnasUnavailable(
                            "Anna's Archive redirected off-mirror "
                            f"({httpx.URL(nxt).host}) — refusing to follow")
                    if (hop := hop + 1) > MAX_HOPS:
                        raise AnnasUnavailable("too many redirects from Anna's Archive")
                    cur = nxt
                    continue
                return r

        try:
            return await once()
        except _Challenge:
            await self._login()
            try:
                return await once()
            except _Challenge:
                raise AnnasUnavailable(_guard_hint(self._base()))

    async def search(self, q: str, count: int = 20) -> list[dict]:
        r = await self._fetch_authed(f"{self._base()}/search?q={quote_plus(q)}")
        if r.status_code != 200:
            raise AnnasUnavailable(f"Anna's Archive search HTTP {r.status_code}")
        rows = [row for row in parse_search_results(r.text, self._base())
                if not row["extension"] or row["extension"] in _GOOD_FILE_EXTS]
        return rows[:count]

    async def download(self, md5: str, on_progress=None,
                       expected_size: int | None = None) -> tuple[bytes, dict]:
        """Fast-download via the official member API; on "Not a member" fall back
        to the free slow partner servers (no membership needed, up to a ~10 min
        waitlist). Returns (bytes, meta) with meta['_filename'] like the zlib adapter."""
        if not re.fullmatch(r"[a-f0-9]{32}", md5 or ""):
            raise AnnasConfigError(f"not an md5: {md5!r}")
        try:
            async with httpx.AsyncClient(timeout=30, follow_redirects=False) as cx:
                r = await cx.get(f"{self._base()}/dyn/api/fast_download.json",
                                 params={"md5": md5, "key": self._key()}, headers=HEADERS)
        except httpx.HTTPError as e:
            raise AnnasUnavailable(f"Anna's Archive unreachable: {e}") from e
        if r.status_code == 429:
            raise AnnasUnavailable("Anna's Archive rate-limited the API (429)")
        if _is_challenge(r):
            raise AnnasUnavailable(_guard_hint(self._base()))
        if r.status_code != 200:  # includes the documented 204: no body to parse
            raise AnnasUnavailable(f"Anna's Archive fast_download HTTP {r.status_code}")
        try:
            data = r.json()
            if not isinstance(data, dict):
                raise ValueError
        except ValueError as e:
            raise AnnasUnavailable(f"fast_download returned junk (HTTP {r.status_code})") from e
        url = data.get("download_url")
        if not url:
            err = str(data.get("error") or f"HTTP {r.status_code}")
            if re.search(r"\bkey\b", err, re.I):
                raise AnnasConfigError(f"Anna's Archive: {err}")
            if re.search(r"member", err, re.I):
                # membership-gated ("Not a member") — the free slow servers need
                # no account at all, so use them instead of failing the job
                return await self._slow_download(md5, on_progress, expected_size)
            raise AnnasUnavailable(f"Anna's Archive: {err}")
        return await self._fetch_file(url, on_progress, expected_size, md5)

    async def _slow_download(self, md5: str, on_progress,
                             expected_size: int | None) -> tuple[bytes, dict]:
        target = f"{self._base()}/slow_download/{md5}/0/{{}}"
        last = "no slow server offered a link"
        wait_budget = SLOW_MAX_WAIT_S  # cumulative across indices: one slow job
        for domain_index in SLOW_DOMAIN_INDICES:  # must not pin the queue ~1h
            r = await self._fetch_authed(target.format(domain_index))
            if r.status_code != 200:
                last = f"slow_download HTTP {r.status_code}"
                continue
            url, wait_s = _slow_page_info(r.text)
            if url is None and wait_s is not None and wait_s <= min(SLOW_MAX_WAIT_S, wait_budget):
                waited = 0
                while waited < wait_s:
                    chunk = min(SLOW_POLL_S, wait_s - waited)
                    await asyncio.sleep(chunk)
                    waited += chunk
                    if on_progress:  # heartbeat: keeps the job row under the
                        on_progress(0, expected_size)  # 15-min stale reclaim
                wait_budget -= wait_s
                r = await self._fetch_authed(target.format(domain_index))  # the site's JS just reloads
                url, _ = _slow_page_info(r.text)
            if url:
                return await self._fetch_file(url, on_progress, expected_size, md5)
        raise AnnasUnavailable(f"Anna's Archive slow download: {last}")

    async def _fetch_file(self, url: str, on_progress, expected_size: int | None,
                          md5: str) -> tuple[bytes, dict]:
        def _prog(done: int, total: int | None) -> None:
            if on_progress:
                on_progress(done, total if total else expected_size)

        data_bytes = await _fetch_bytes(url, max_bytes=BOOK_MAX_BYTES, on_progress=_prog)
        if not data_bytes:
            raise AnnasUnavailable("Anna's Archive file fetch failed (redirect/cap/size)")
        # an expired/wrong partner link can serve a 200 body that isn't the book —
        # verify it actually hashes to the requested md5 before ingesting
        if hashlib.md5(data_bytes).hexdigest() != md5:
            raise AnnasUnavailable("Anna's Archive file failed md5 check (stale/wrong link)")
        # slow-server URLs end in the real filename ("Title (2017).pdf"); fast
        # ones don't — an unusable name falls back to md5 + the job's own ext
        name = unquote(urlsplit(url).path.rsplit("/", 1)[-1])
        if "~" in name or name.rsplit(".", 1)[-1].lower() not in _GOOD_FILE_EXTS:
            name = ""
        return data_bytes, {"_filename": name or md5}


def _slow_page_info(page: str) -> tuple[str | None, int | None]:
    """From a /slow_download/ page: (direct d3-link, wait_seconds). A ready page
    carries the pre-signed partner URL; a waitlisted one the countdown; a wrong
    domain index redirects to /md5/ which matches neither."""
    m = _D3_URL_RE.search(page)
    w = _WAIT_RE.search(page)
    return (html.unescape(m.group(1)) if m else None,
            int(w.group(1)) if w else None)


annas = Annas()

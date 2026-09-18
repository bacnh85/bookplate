"""SSRF-pinned HTTP GET helpers — every redirect hop is re-validated.

Moved verbatim from app/main.py when the Anna's Archive client needed the same
discipline for partner-server file downloads (the fast-download URL redirects
to arbitrary public hosts, so a compromised mirror could not be allowed to
point us at internal services).
"""
import asyncio
import ipaddress

import httpx

MAX_COVER_BYTES = 5 * 1024 * 1024


async def _resolve_public_ip(host: str) -> str | None:
    """THE single DNS resolution for a fetch — validation and dial use the
    same answer, so rebinding can't diverge them. Non-blocking (loop executor).
    None unless every resolved address is global (no loopback/private/link-local)."""
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, None)
    except Exception:
        return None
    if not infos or not all(ipaddress.ip_address(i[4][0]).is_global for i in infos):
        return None
    return infos[0][4][0]


async def _fetch_bytes(url: str, max_bytes: int = MAX_COVER_BYTES,
                       on_progress=None) -> bytes | None:
    try:
        for _ in range(5):  # follow redirects manually: re-validate every hop
            u = httpx.URL(url)
            if u.scheme not in ("http", "https") or not u.host:
                return None
            ip = await _resolve_public_ip(u.host)
            if ip is None:
                return None
            # dial the validated IP directly; restore Host + TLS identity.
            # Fresh client per hop: pools key on the pinned origin, so a hop to
            # another host on the same IP must not reuse another name's TLS.
            pinned = u.copy_with(host=ip)
            ext = {"sni_hostname": u.host} if u.scheme == "https" else {}
            try:
                async with httpx.AsyncClient(timeout=10, follow_redirects=False) as cx:
                    async with cx.stream("GET", str(pinned),
                                         headers={"Host": u.netloc.decode("ascii")},
                                         extensions=ext) as r:
                        if r.status_code in (301, 302, 303, 307, 308):
                            loc = r.headers.get("location", "")
                            if not loc:
                                return None  # 3xx without Location: don't spin 5 hops
                            url = str(u.join(loc))
                            continue  # next hop: fresh client, re-validated pin
                        if r.status_code != 200:
                            return None
                        if int(r.headers.get("content-length") or 0) > max_bytes:
                            return None
                        buf = bytearray()  # streamed: a lying Content-Length can't balloon RAM
                        async for chunk in r.aiter_bytes(1 << 16):
                            buf += chunk
                            if len(buf) > max_bytes:
                                return None
                            if on_progress:
                                on_progress(len(buf),
                                            int(r.headers.get("content-length") or 0) or None)
                        return bytes(buf) or None
            except httpx.HTTPError:
                return None
    except Exception:
        return None
    return None

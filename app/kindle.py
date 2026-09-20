"""Send to Kindle: email the book file to the Kindle address via SMTP.

Amazon's email gateway converts/delivers EPUB+PDF attachments sent from an
approved sender to the account's @kindle.com address — the same mechanism
Calibre uses. Config lives in the DB-only settings (kindle.* keys).
"""
import re
import smtplib
import unicodedata
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import format_datetime
from pathlib import Path

from . import settings

MAX_SIZE = 50 * 1024 * 1024  # Amazon's send-to-kindle attachment cap
SENDEXTS = {"epub", "pdf"}
_PORTS = {"starttls": 587, "ssl": 465, "none": 25}


class KindleError(Exception):
    """User-facing failure (config or SMTP); surfaced verbatim via HTTP 502."""


def configured() -> bool:
    return bool(settings.get("kindle.to") and settings.get("kindle.smtp_host"))


def _ascii_name(title: str, authors: str, ext: str) -> str:
    """Attachment filename: Kindle titles a PDF by filename, so a raw sha256
    would look terrible. Non-Latin chars are dropped (EPUB titles come from
    embedded metadata; only PDF display names are affected)."""
    s = unicodedata.normalize("NFKD", " - ".join(x for x in (authors, title) if x))
    s = re.sub(r"[^A-Za-z0-9() ._-]+", " ", s.encode("ascii", "ignore").decode())
    return (re.sub(r"\s+", " ", s).strip().strip("-") or "book") + f".{ext}"


def send(path: Path, title: str, authors: str) -> None:
    to = settings.get("kindle.to")
    host = settings.get("kindle.smtp_host")
    if not (to and host):
        raise KindleError("Kindle delivery is not configured (Admin → Settings)")
    ext = path.suffix.lstrip(".").lower()
    if ext not in SENDEXTS:
        raise KindleError(f"Kindle accepts only EPUB and PDF (got {ext.upper()}) — "
                          "download the EPUB version instead")
    if path.stat().st_size > MAX_SIZE:
        raise KindleError("file exceeds Amazon's 50 MB email attachment limit")
    sec = settings.get("kindle.smtp_security") or "starttls"
    port_raw = settings.get("kindle.smtp_port")
    try:
        port = int(port_raw) if port_raw else _PORTS[sec]
    except (ValueError, KeyError):
        raise KindleError(f"bad SMTP port/security: {port_raw!r}/{sec!r}")
    frm = settings.get("kindle.from") or settings.get("kindle.smtp_user") or "bookplate@localhost"
    # header values must not carry newlines (email.policy raises ValueError ->
    # unhandled 500); titles come verbatim from EPUB metadata / filenames
    title, frm, to = (re.sub(r"[\r\n]+", " ", v) for v in (title, frm, to))

    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = frm, to, title
    msg["Date"] = format_datetime(datetime.now(timezone.utc))
    msg.set_content("Sent from Bookplate.")
    msg.add_attachment(path.read_bytes(), maintype="application",
                       subtype="epub+zip" if ext == "epub" else "pdf",
                       filename=_ascii_name(title, authors, ext))
    user, pw = settings.get("kindle.smtp_user"), settings.get("kindle.smtp_password")
    try:
        # ponytail: synchronous SMTP send; promote to a download_jobs-style
        # queue only if real-world sends get slow.
        with (smtplib.SMTP_SSL(host, port, timeout=20) if sec == "ssl"
              else smtplib.SMTP(host, port, timeout=20)) as s:
            if sec == "starttls":
                s.starttls()
            if user and pw:
                s.login(user, pw)
            s.send_message(msg)
    except (smtplib.SMTPException, OSError) as e:
        raise KindleError(f"SMTP: {e}")

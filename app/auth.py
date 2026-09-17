"""Auth: pbkdf2 password hashing, JWT bearer tokens, Basic-auth fallback (for OPDS apps)."""
import base64
import hashlib
import os
import secrets
import time
from pathlib import Path

import jwt
from fastapi import Depends, HTTPException, Request

from .db import conn

_ITERATIONS = 200_000
_SECRET_FILE = Path(__file__).resolve().parent.parent / "data" / ".secret"


def _secret() -> str:
    if _SECRET_FILE.exists():
        os.chmod(_SECRET_FILE, 0o600)  # heal perms of files created by older versions
        return _SECRET_FILE.read_text().strip()
    s = secrets.token_hex(32)
    _SECRET_FILE.parent.mkdir(exist_ok=True)
    try:
        fd = os.open(_SECRET_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(s)
    except FileExistsError:  # racing first boot — use the winner's secret
        return _SECRET_FILE.read_text().strip()
    return s


def hash_password(pw: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, _ITERATIONS)
    return f"{salt.hex()}:{dk.hex()}"


def verify_password(pw: str, stored: str) -> bool:
    try:
        salt_hex, dk_hex = stored.split(":")
    except ValueError:
        return False
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt_hex), _ITERATIONS)
    return secrets.compare_digest(dk.hex(), dk_hex)


def make_token(user_id: int) -> str:
    return jwt.encode(
        {"sub": str(user_id), "exp": int(time.time()) + 30 * 86400}, _secret()
    )


def _user_from_basic(request: Request):
    header = request.headers.get("Authorization", "")
    if not header.startswith("Basic "):
        return None
    try:
        email, _, pw = base64.b64decode(header[6:]).decode().partition(":")
    except Exception:
        return None
    row = conn().execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    return row if row and verify_password(pw, row["password_hash"]) else None


def current_user(request: Request):
    """Bearer JWT header, `session` cookie (set at login, used by <img>/OPDS), or Basic auth."""
    token_str = ""
    header = request.headers.get("Authorization", "")
    if header.startswith("Bearer "):
        token_str = header[7:]
    elif request.cookies.get("session"):
        token_str = request.cookies["session"]
    if token_str:
        try:
            payload = jwt.decode(token_str, _secret(), algorithms=["HS256"])
            user_id = int(payload["sub"])
        except Exception:
            raise HTTPException(401, "invalid token")
        row = conn().execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        if not row:
            raise HTTPException(401, "user not found")
        return row
    user = _user_from_basic(request)
    if user:
        return user
    raise HTTPException(401, "unauthenticated", headers={"WWW-Authenticate": "Basic"})


UserDep = Depends(current_user)

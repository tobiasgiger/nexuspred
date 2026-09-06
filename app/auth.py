"""Dashboard authentication: local user accounts (email + password), invite-only.

Login state is a small **HMAC-signed, HTTP-only cookie** carrying the user id,
an expiry and a fingerprint of the user's current password hash — no
server-side session store, no external dependency. The fingerprint means a
password change or reset invalidates every other session of that user. User records,
password hashing and invites live in :mod:`app.db`; this module handles the
session cookie and request → user resolution.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from typing import Any, Optional

from . import db

COOKIE = "fb_session"
SESSION_TTL = 30 * 24 * 3600  # 30 days


_KEY: Optional[bytes] = None


def _secret_key() -> bytes:
    """The cookie-signing key, resolved once (env, else persisted in ``meta``)."""
    global _KEY
    if _KEY is None:
        key = os.environ.get("SESSION_SECRET") or db.meta_get("session_secret")
        if not key:
            key = secrets.token_urlsafe(48)
            db.meta_set("session_secret", key)
        _KEY = key.encode()
    return _KEY


def _b64e(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _sign(body: str) -> str:
    return _b64e(hmac.new(_secret_key(), body.encode(), hashlib.sha256).digest())


def make_session(user_id: int) -> str:
    body = _b64e(json.dumps({"uid": int(user_id), "exp": int(time.time()) + SESSION_TTL,
                             "pv": db.password_version(int(user_id))}).encode())
    return f"{body}.{_sign(body)}"


def read_session(cookie: Optional[str]) -> Optional[int]:
    """Return the user id from a valid, unexpired session cookie whose password
    fingerprint still matches the account, else None."""
    if not cookie or "." not in cookie:
        return None
    body, _, sig = cookie.partition(".")
    try:
        if not hmac.compare_digest(sig, _sign(body)):
            return None
        payload = json.loads(_b64d(body))
        if int(payload.get("exp", 0)) < time.time():
            return None
        uid = int(payload["uid"])
        pv = str(payload.get("pv", ""))
        if not pv or not hmac.compare_digest(pv, db.password_version(uid)):
            return None
        return uid
    except Exception:  # noqa: BLE001 - any malformed cookie is simply "not logged in"
        return None


def current_user(request) -> Optional[dict[str, Any]]:
    """The logged-in user for a request, or None."""
    uid = read_session(request.cookies.get(COOKIE))
    if uid is None:
        return None
    return db.get_user(uid)

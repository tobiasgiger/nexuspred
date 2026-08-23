"""Dashboard authentication: Sign in with Google (OAuth), email allowlist.

When a Google **client id + secret** and at least one **allowed email** are
configured (via Settings or the ``GOOGLE_CLIENT_ID`` / ``GOOGLE_CLIENT_SECRET`` /
``GOOGLE_ALLOWED_EMAILS`` env vars), the dashboard requires "Sign in with
Google" and only those emails get in — the legacy ``dashboard_password`` is then
ignored. Until that's configured, the bridge falls back to the old behaviour
(password if set, otherwise open) so a deploy is never accidentally locked out.

The signed-in state is a small **HMAC-signed cookie** (email + expiry) — no
server-side session store, no extra dependency. Token exchange + user info use
the existing ``httpx`` client; the OAuth flow is the standard Authorization Code
flow against Google's OpenID Connect endpoints.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from typing import Optional

from . import config

COOKIE = "fb_session"
STATE_COOKIE = "fb_oauth_state"
SESSION_TTL = 7 * 24 * 3600  # 7 days

AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
USERINFO_ENDPOINT = "https://openidconnect.googleapis.com/v1/userinfo"


# --------------------------------------------------------------- configuration
def client_id() -> str:
    return (os.environ.get("GOOGLE_CLIENT_ID") or config.load_settings().get("google_client_id") or "").strip()


def client_secret() -> str:
    return (os.environ.get("GOOGLE_CLIENT_SECRET") or config.load_settings().get("google_client_secret") or "").strip()


def allowed_emails() -> set[str]:
    raw = os.environ.get("GOOGLE_ALLOWED_EMAILS")
    if raw is None:
        raw = config.load_settings().get("google_allowed_emails") or []
    if isinstance(raw, str):
        raw = raw.replace(",", " ").split()
    return {str(e).strip().lower() for e in raw if str(e).strip()}


def configured() -> bool:
    """Google login is active only when we have a client id + secret AND at least
    one allowed email (so an incomplete setup never locks everyone out)."""
    return bool(client_id() and client_secret() and allowed_emails())


def email_allowed(email: Optional[str]) -> bool:
    return bool(email) and email.strip().lower() in allowed_emails()


def base_url(request) -> str:
    """Public base URL for building the OAuth redirect, honouring a configured
    ``public_url`` / ``PUBLIC_URL`` and common proxy headers."""
    pub = os.environ.get("PUBLIC_URL") or config.load_settings().get("public_url")
    if pub:
        return pub.rstrip("/")
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
    return f"{proto}://{host}"


def redirect_uri(request) -> str:
    return base_url(request) + "/auth/callback"


# --------------------------------------------------------------- session cookie
def _secret_key() -> bytes:
    key = os.environ.get("SESSION_SECRET") or config.load_settings().get("session_secret")
    if not key:
        key = secrets.token_urlsafe(48)
        config.save_settings({"session_secret": key})
    return key.encode()


def _b64e(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _sign(body: str) -> str:
    return _b64e(hmac.new(_secret_key(), body.encode(), hashlib.sha256).digest())


def make_session(email: str) -> str:
    body = _b64e(json.dumps({"email": email.strip().lower(), "exp": int(time.time()) + SESSION_TTL}).encode())
    return f"{body}.{_sign(body)}"


def read_session(cookie: Optional[str]) -> Optional[str]:
    """Return the email from a valid, unexpired session cookie, else None."""
    if not cookie or "." not in cookie:
        return None
    body, _, sig = cookie.partition(".")
    try:
        if not hmac.compare_digest(sig, _sign(body)):
            return None
        payload = json.loads(_b64d(body))
        if int(payload.get("exp", 0)) < time.time():
            return None
        return payload.get("email")
    except Exception:  # noqa: BLE001 - any malformed cookie is simply "not logged in"
        return None


def new_state() -> str:
    return secrets.token_urlsafe(24)


def google_auth_url(request, state: str) -> str:
    from urllib.parse import urlencode

    params = {
        "client_id": client_id(),
        "redirect_uri": redirect_uri(request),
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "access_type": "online",
        "prompt": "select_account",
    }
    return f"{AUTH_ENDPOINT}?{urlencode(params)}"

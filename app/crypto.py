"""Encryption at rest for the secrets inside area settings.

Tradovate access tokens, the Discord user token, the SMTP password, the alert
webhook URL, the webhook passphrase and Discord-target secrets are stored in
the ``areas.settings`` JSON. They are written as ``enc:v1:<fernet token>`` and
transparently decrypted on load, so every caller above :mod:`app.db` keeps
seeing plain values and the SQLite file alone no longer yields usable tokens.

Key resolution (first match wins):

1. ``NEXUSPRED_ENCRYPTION_KEY`` — any string; recommended on every deployment.
2. ``SESSION_SECRET`` (env) — already required to pin logins across deploys.
3. The auto-generated session secret stored in the database's ``meta`` table.
   This still works, but key and ciphertext then live in the same file — it
   protects against casual reads of a DB dump, not against someone who has the
   whole file. A warning is logged at startup in that case.

Values written before this module existed are plain strings; they read back
unchanged and are encrypted by :func:`app.db.encrypt_existing_settings` on
the first start after the upgrade.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import os
from typing import Any, Optional

from cryptography.fernet import Fernet, InvalidToken

log = logging.getLogger(__name__)

PREFIX = "enc:v1:"

# Top-level secret settings keys (mirrors config.SECRET_FIELDS, kept here so
# db → crypto never needs config at import time).
SECRET_KEYS = ("webhook_passphrase", "alert_discord_webhook_url", "alert_smtp_password",
               "discord_user_token")
TOKEN_ACCOUNT_KEYS = ("access_token", "md_token")

_fernet: Optional[Fernet] = None
_source: str = ""


def _derive(material: str) -> bytes:
    return base64.urlsafe_b64encode(hashlib.sha256(material.encode("utf-8")).digest())


def key_source() -> str:
    """Where the key came from: ``env:encryption``, ``env:session`` or ``db``."""
    _get()
    return _source


def _get() -> Fernet:
    global _fernet, _source
    if _fernet is not None:
        return _fernet
    material = os.environ.get("NEXUSPRED_ENCRYPTION_KEY")
    if material:
        _source = "env:encryption"
    else:
        material = os.environ.get("SESSION_SECRET")
        if material:
            _source = "env:session"
        else:
            from . import db  # local: db imports this module
            material = db.meta_get("session_secret")
            if not material:
                import secrets
                material = secrets.token_urlsafe(48)
                db.meta_set("session_secret", material)
            _source = "db"
    _fernet = Fernet(_derive(material))
    return _fernet


def reset() -> None:
    """Forget the cached key (tests, key rotation)."""
    global _fernet, _source
    _fernet = None
    _source = ""


def is_encrypted(value: Any) -> bool:
    return isinstance(value, str) and value.startswith(PREFIX)


def encrypt(value: Any) -> Any:
    """Encrypt a non-empty string; other values (empty, None, already
    encrypted, non-strings) pass through unchanged."""
    if not isinstance(value, str) or not value or is_encrypted(value):
        return value
    return PREFIX + _get().encrypt(value.encode("utf-8")).decode("ascii")


def decrypt(value: Any) -> Any:
    """Inverse of :func:`encrypt`. A token that cannot be decrypted (the key
    changed) yields an empty string and a logged error instead of an
    exception — a broken secret must never take the whole area's settings down."""
    if not is_encrypted(value):
        return value
    try:
        return _get().decrypt(value[len(PREFIX):].encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError, TypeError):
        log.error("Cannot decrypt a stored secret — NEXUSPRED_ENCRYPTION_KEY / SESSION_SECRET "
                  "changed? Re-enter the affected token in the dashboard.")
        return ""


def _map_settings(settings: dict[str, Any], fn) -> dict[str, Any]:
    out = dict(settings)
    for key in SECRET_KEYS:
        if key in out:
            out[key] = fn(out[key])
    if isinstance(out.get("token_accounts"), list):
        out["token_accounts"] = [
            {**a, **{k: fn(a.get(k)) for k in TOKEN_ACCOUNT_KEYS if k in a}} if isinstance(a, dict) else a
            for a in out["token_accounts"]
        ]
    if isinstance(out.get("discord_channels"), list):
        out["discord_channels"] = [
            {**c, "targets": [
                {**t, "secret": fn(t.get("secret"))} if isinstance(t, dict) and "secret" in t else t
                for t in (c.get("targets") or [])
            ]} if isinstance(c, dict) else c
            for c in out["discord_channels"]
        ]
    return out


def encrypt_settings(settings: dict[str, Any]) -> dict[str, Any]:
    """A copy of ``settings`` with every secret field encrypted."""
    return _map_settings(settings, encrypt)


def decrypt_settings(settings: dict[str, Any]) -> dict[str, Any]:
    """A copy of ``settings`` with every secret field in plain text."""
    return _map_settings(settings, decrypt)


def has_plaintext_secret(settings: dict[str, Any]) -> bool:
    """True when at least one secret field holds a non-empty, unencrypted value."""
    found = False

    def probe(v: Any) -> Any:
        nonlocal found
        if isinstance(v, str) and v and not is_encrypted(v):
            found = True
        return v

    _map_settings(settings, probe)
    return found

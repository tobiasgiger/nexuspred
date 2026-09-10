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
_legacy: Optional[list[Fernet]] = None


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


def _legacy_keys() -> list[Fernet]:
    """Keys a secret may still be encrypted with after a key change: the
    explicit previous key (``NEXUSPRED_ENCRYPTION_KEY_PREVIOUS``), the session
    secret from the environment and the one stored in the database — in that
    order, excluding whichever is the current key."""
    global _legacy
    if _legacy is not None:
        return _legacy
    _get()
    materials: list[str] = []
    prev = os.environ.get("NEXUSPRED_ENCRYPTION_KEY_PREVIOUS")
    if prev:
        materials.append(prev)
    if _source != "env:session" and os.environ.get("SESSION_SECRET"):
        materials.append(os.environ["SESSION_SECRET"])
    if _source != "db":
        try:
            from . import db
            stored = db.meta_get("session_secret")
            if stored:
                materials.append(stored)
        except Exception:  # noqa: BLE001 - no DB yet
            pass
    _legacy = [Fernet(_derive(m)) for m in materials]
    return _legacy


def reset() -> None:
    """Forget the cached keys (tests, key rotation)."""
    global _fernet, _source, _legacy
    _fernet = None
    _source = ""
    _legacy = None


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
    token = value[len(PREFIX):].encode("ascii")
    try:
        return _get().decrypt(token).decode("utf-8")
    except (InvalidToken, ValueError, TypeError):
        pass
    # Key changed? Try the previous keys — the startup pass re-encrypts with
    # the current one (see db.encrypt_existing_settings).
    for f in _legacy_keys():
        try:
            return f.decrypt(token).decode("utf-8")
        except (InvalidToken, ValueError, TypeError):
            continue
    log.error("Cannot decrypt a stored secret — NEXUSPRED_ENCRYPTION_KEY / SESSION_SECRET "
              "changed? Set NEXUSPRED_ENCRYPTION_KEY_PREVIOUS to the old value once, or re-enter "
              "the affected token in the dashboard.")
    return ""


def is_current(value: Any) -> bool:
    """True when ``value`` is not a secret that would need re-encryption
    (plain/empty, or encrypted with the current key)."""
    if not is_encrypted(value):
        return not (isinstance(value, str) and value)
    try:
        _get().decrypt(value[len(PREFIX):].encode("ascii"))
        return True
    except (InvalidToken, ValueError, TypeError):
        return False


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


def _undecryptable(value: Any) -> bool:
    return is_encrypted(value) and decrypt(value) == ""


def keep_undecryptable(settings: dict[str, Any], previous: dict[str, Any]) -> dict[str, Any]:
    """``settings`` (plain text, about to be saved) with every *empty* secret
    field replaced by the stored cipher text of ``previous`` when that cipher
    text cannot be decrypted with the current key — the value the caller got
    was the empty placeholder, not a deliberate clearing. Logins are matched by
    their stable id (``lid``), falling back to the list position; Discord
    targets by position within a channel of the same id."""
    if not isinstance(previous, dict) or not previous:
        return settings
    out = dict(settings)
    for key in SECRET_KEYS:
        if key in out and not out[key] and _undecryptable(previous.get(key)):
            out[key] = previous[key]
    prev_logins = [a for a in (previous.get("token_accounts") or []) if isinstance(a, dict)]
    by_lid = {a.get("lid"): a for a in prev_logins if a.get("lid")}
    if isinstance(out.get("token_accounts"), list):
        merged = []
        for i, a in enumerate(out["token_accounts"]):
            if isinstance(a, dict):
                old = by_lid.get(a.get("lid")) if a.get("lid") else (prev_logins[i] if i < len(prev_logins) else None)
                if old:
                    a = {**a, **{k: old[k] for k in TOKEN_ACCOUNT_KEYS if k in a and not a[k] and _undecryptable(old.get(k))}}
            merged.append(a)
        out["token_accounts"] = merged
    prev_channels = {c.get("id"): c for c in (previous.get("discord_channels") or []) if isinstance(c, dict) and c.get("id")}
    if isinstance(out.get("discord_channels"), list):
        channels = []
        for c in out["discord_channels"]:
            old = prev_channels.get(c.get("id")) if isinstance(c, dict) else None
            if old and isinstance(c.get("targets"), list):
                old_targets = old.get("targets") or []
                targets = []
                for j, t in enumerate(c["targets"]):
                    ot = old_targets[j] if j < len(old_targets) and isinstance(old_targets[j], dict) else None
                    if isinstance(t, dict) and "secret" in t and not t["secret"] and ot and _undecryptable(ot.get("secret")):
                        t = {**t, "secret": ot["secret"]}
                    targets.append(t)
                c = {**c, "targets": targets}
            channels.append(c)
        out["discord_channels"] = channels
    return out


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


def needs_reencrypt(settings: dict[str, Any]) -> bool:
    """True when a secret is plain text or encrypted with a previous key that
    still decrypts — i.e. a re-save with the current key would fix it."""
    found = False

    def probe(v: Any) -> Any:
        nonlocal found
        if isinstance(v, str) and v and not is_current(v) and decrypt(v) != "":
            found = True
        return v

    _map_settings(settings, probe)
    return found

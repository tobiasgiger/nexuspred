"""Two-factor authentication: TOTP (RFC 6238, what Google Authenticator, Authy,
1Password, Aegis… speak) plus ten single-use backup codes.

* New accounts (first-run setup and invite registration) must enrol before
  they can use the dashboard; existing accounts may enable it under Account.
* The secret is stored encrypted (``crypto``); backup codes are stored as
  salted SHA-256 hashes and deleted when used. A fresh set can be requested at
  any time (lost or all used) — it replaces the old one.
* Recovery when the authenticator *and* the codes are gone: an admin resets
  the user's 2FA (or a password reset does it), and the user enrols again.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time
from typing import Any, Optional

ISSUER = "Fluxbridge"
DIGITS = 6
PERIOD = 30
WINDOW = 1                  # ±1 step (30 s) of clock drift
BACKUP_CODES = 10
BACKUP_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"   # no 0/O, 1/I


def new_secret() -> str:
    """160-bit secret, base32 without padding (what authenticator apps expect)."""
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")


def _key(secret: str) -> bytes:
    s = secret.strip().replace(" ", "").upper()
    return base64.b32decode(s + "=" * (-len(s) % 8))


def hotp(secret: str, counter: int, digits: int = DIGITS) -> str:
    mac = hmac.new(_key(secret), struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = mac[-1] & 0x0F
    code = (struct.unpack(">I", mac[offset:offset + 4])[0] & 0x7FFFFFFF) % (10 ** digits)
    return f"{code:0{digits}d}"


def totp(secret: str, at: Optional[float] = None) -> str:
    return hotp(secret, int((at if at is not None else time.time()) // PERIOD))


def verify_totp(secret: str, code: str, *, at: Optional[float] = None, last_counter: int = -1) -> Optional[int]:
    """The counter the code matched (to store as ``last_counter``), else None.
    A counter at or below ``last_counter`` is refused: a code is single-use even
    inside its 30-second step (replay protection)."""
    digits = "".join(ch for ch in str(code or "") if ch.isdigit())
    if len(digits) != DIGITS:
        return None
    now = int((at if at is not None else time.time()) // PERIOD)
    for step in range(-WINDOW, WINDOW + 1):
        counter = now + step
        if counter <= last_counter:
            continue
        if hmac.compare_digest(hotp(secret, counter), digits):
            return counter
    return None


def provisioning_uri(secret: str, account: str, issuer: str = ISSUER) -> str:
    from urllib.parse import quote
    label = quote(f"{issuer}:{account}", safe="")
    return f"otpauth://totp/{label}?secret={secret}&issuer={quote(issuer, safe='')}&algorithm=SHA1&digits={DIGITS}&period={PERIOD}"


def qr_data_uri(uri: str) -> str:
    """The provisioning URI as an SVG data URI (rendered inline; nothing leaves the server)."""
    import segno
    return segno.make(uri, error="m").svg_data_uri(scale=5, border=2, dark="#111827", light="#ffffff")


def pretty_secret(secret: str) -> str:
    """Grouped for typing by hand: ABCD EFGH …"""
    return " ".join(secret[i:i + 4] for i in range(0, len(secret), 4))


# ------------------------------------------------------------ backup codes
def new_backup_codes(n: int = BACKUP_CODES) -> list[str]:
    def one() -> str:
        raw = "".join(secrets.choice(BACKUP_ALPHABET) for _ in range(10))
        return f"{raw[:5]}-{raw[5:]}"
    return [one() for _ in range(n)]


def normalize_backup_code(code: str) -> str:
    return "".join(ch for ch in str(code or "").upper() if ch.isalnum())


def hash_backup_code(code: str, salt: str) -> str:
    return hashlib.sha256(f"{salt}:{normalize_backup_code(code)}".encode("utf-8")).hexdigest()


def looks_like_backup_code(code: str) -> bool:
    return len(normalize_backup_code(code)) == 10 and not str(code or "").strip().isdigit()


def status_of(user: dict[str, Any], codes_left: int) -> dict[str, Any]:
    return {"enabled": bool(user.get("totp_enabled")), "required": bool(user.get("totp_required")),
            "backup_codes_left": codes_left, "backup_codes_total": BACKUP_CODES}

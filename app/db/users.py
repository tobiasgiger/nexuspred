"""Users, passwords, two-factor, invites, password resets."""
from __future__ import annotations
import asyncio
import hashlib
import hmac
import json
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from .. import crypto
from ..context import DEFAULT_AREA_ID
from .core import _agents_by_hash, _all_features_on, _bump_areas_generation, _connect, _now, _pw_versions, _users, default_area_features, init, reset_caches
from .areas import area_owner, get_area_features, user_primary_area


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200_000)
    return f"pbkdf2$200000${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iters, salt_hex, hash_hex = stored.split("$")
        if algo != "pbkdf2":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), int(iters))
        return hmac.compare_digest(dk.hex(), hash_hex)
    except Exception:  # noqa: BLE001
        return False


def _row_to_user(row: sqlite3.Row) -> dict[str, Any]:
    keys = row.keys()
    return {"id": row["id"], "email": row["email"], "is_admin": bool(row["is_admin"]),
            "created_at": row["created_at"],
            "last_login_at": row["last_login_at"] if "last_login_at" in keys else None,
            "last_login_ip": row["last_login_ip"] if "last_login_ip" in keys else None,
            "totp_enabled": bool(row["totp_enabled"]) if "totp_enabled" in keys else False,
            "totp_required": bool(row["totp_required"]) if "totp_required" in keys else False}


def record_login(user_id: int, ip: str = "") -> None:
    """Stamp a successful sign-in on the user (shown on the Users page)."""
    init()
    with _connect() as c:
        c.execute("UPDATE users SET last_login_at=?, last_login_ip=? WHERE id=?",
                  (_now(), (ip or "")[:64], user_id))
    _users.pop(user_id, None)


def get_user(user_id: int) -> Optional[dict[str, Any]]:
    cached = _users.get(user_id)
    if cached is not None:
        return dict(cached)
    init()
    with _connect() as c:
        row = c.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    if not row:
        return None
    user = _row_to_user(row)
    _users[user_id] = user
    return dict(user)


def get_user_by_email(email: str) -> Optional[dict[str, Any]]:
    init()
    with _connect() as c:
        row = c.execute("SELECT * FROM users WHERE email=?", (email.strip().lower(),)).fetchone()
        return _row_to_user(row) if row else None


_DUMMY_HASH = hash_password("dummy-timing-equaliser")


def authenticate(email: str, password: str) -> Optional[dict[str, Any]]:
    init()
    with _connect() as c:
        row = c.execute("SELECT * FROM users WHERE email=?", (email.strip().lower(),)).fetchone()
    if row and verify_password(password, row["password_hash"]):
        return _row_to_user(row)
    if not row:
        verify_password(password, _DUMMY_HASH)          # an unknown address costs the same time as a wrong password
    return None


def list_users() -> list[dict[str, Any]]:
    init()
    with _connect() as c:
        rows = c.execute("SELECT * FROM users ORDER BY id").fetchall()
        users = [_row_to_user(r) for r in rows]
    for u in users:  # attach each user's primary-area feature flags for the admin UI
        u["features"] = user_features(u["id"])
    return users


def create_user(email: str, password: str, is_admin: bool = False,
                initial_settings: Optional[dict[str, Any]] = None, *, totp_required: bool = False) -> dict[str, Any]:
    """Create a user + their own area + an owner membership. Returns the user.
    ``totp_required`` (sign-up and first-run setup) forces two-factor enrolment
    before the dashboard can be used."""
    init()
    email = email.strip().lower()
    # Default the alert "Notify email" to the owner's own address (unless the
    # migrated/initial settings already carry one), so it's correct per-user.
    init_settings = dict(initial_settings or {})
    if not init_settings.get("alert_email_to"):
        init_settings["alert_email_to"] = email
    settings_json = json.dumps(crypto.encrypt_settings(init_settings))
    with _connect() as c:
        # First user is forced admin; area id of the very first user is DEFAULT_AREA_ID.
        first = c.execute("SELECT COUNT(*) n FROM users").fetchone()["n"] == 0
        cur = c.execute(
            "INSERT INTO users(email,password_hash,is_admin,created_at,totp_required) VALUES(?,?,?,?,?)",
            (email, hash_password(password), 1 if (is_admin or first) else 0, _now(), 1 if totp_required else 0),
        )
        uid = cur.lastrowid
        if first:
            # Seed the first area with the well-known DEFAULT_AREA_ID so migrated
            # single-tenant data keeps a stable home.
            # The bootstrap admin's area gets every feature ON.
            c.execute(
                "INSERT INTO areas(id,name,owner_user_id,settings,features,created_at) VALUES(?,?,?,?,?,?)",
                (DEFAULT_AREA_ID, "My area", uid, settings_json, _all_features_on(), _now()),
            )
            area_id = DEFAULT_AREA_ID
        else:
            cur2 = c.execute(
                "INSERT INTO areas(name,owner_user_id,settings,created_at) VALUES(?,?,?,?)",
                ("My area", uid, settings_json, _now()),
            )
            area_id = cur2.lastrowid
        c.execute(
            "INSERT INTO memberships(user_id,area_id,role,created_at) VALUES(?,?,?,?)",
            (uid, area_id, "owner", _now()),
        )
    _bump_areas_generation()
    reset_caches()
    return get_user(uid)  # type: ignore[return-value]


def set_password(user_id: int, new_password: str) -> None:
    init()
    with _connect() as c:
        c.execute("UPDATE users SET password_hash=? WHERE id=?",
                  (hash_password(new_password), user_id))
    _pw_versions.pop(user_id, None)


def password_version(user_id: int) -> str:
    """A short, stable fingerprint of the user's current password hash and
    session salt — baked into session cookies so changing the password, or
    revoking the sessions (:func:`revoke_sessions`), logs every other session
    out. Empty for an unknown user (which never matches a cookie). Cached."""
    cached = _pw_versions.get(user_id)
    if cached is not None:
        return cached
    init()
    with _connect() as c:
        row = c.execute("SELECT password_hash, session_salt FROM users WHERE id=?", (user_id,)).fetchone()
    if not row:
        return ""
    version = hashlib.sha256(f"{row['password_hash']}|{row['session_salt'] or ''}".encode()).hexdigest()[:16]
    _pw_versions[user_id] = version
    return version


def revoke_sessions(user_id: int) -> None:
    """Invalidate every session cookie of the user (sign out everywhere): the
    cookies are stateless, so the fingerprint they carry is rotated instead."""
    init()
    with _connect() as c:
        c.execute("UPDATE users SET session_salt=? WHERE id=?", (secrets.token_hex(8), user_id))
    _pw_versions.pop(user_id, None)


async def authenticate_async(email: str, password: str) -> Optional[dict[str, Any]]:
    return await asyncio.to_thread(authenticate, email, password)


async def create_user_async(email: str, password: str, is_admin: bool = False,
                            initial_settings: Optional[dict[str, Any]] = None, *, totp_required: bool = False) -> dict[str, Any]:
    return await asyncio.to_thread(lambda: create_user(email, password, is_admin, initial_settings, totp_required=totp_required))


async def set_password_async(user_id: int, new_password: str) -> None:
    await asyncio.to_thread(set_password, user_id, new_password)


async def consume_password_reset_async(token: str, new_password: str) -> Optional[int]:
    return await asyncio.to_thread(consume_password_reset, token, new_password)


def mfa_secret(user_id: int) -> str:
    """The user's TOTP secret (decrypted), '' when none is stored."""
    init()
    with _connect() as c:
        row = c.execute("SELECT totp_secret FROM users WHERE id=?", (user_id,)).fetchone()
    raw = row["totp_secret"] if row else ""
    return crypto.decrypt(raw) if raw else ""


def mfa_begin(user_id: int, secret: str) -> None:
    """Store a fresh, not yet confirmed secret (enrolment step 1)."""
    init()
    with _connect() as c:
        c.execute("UPDATE users SET totp_secret=?, totp_enabled=0, totp_counter=-1 WHERE id=?", (crypto.encrypt(secret), user_id))
    _users.pop(user_id, None)


def mfa_enable(user_id: int, counter: int) -> None:
    """Enrolment confirmed with a valid code."""
    init()
    with _connect() as c:
        c.execute("UPDATE users SET totp_enabled=1, totp_counter=? WHERE id=?", (counter, user_id))
    _users.pop(user_id, None)


def mfa_counter(user_id: int) -> int:
    init()
    with _connect() as c:
        row = c.execute("SELECT totp_counter FROM users WHERE id=?", (user_id,)).fetchone()
    return int(row["totp_counter"]) if row else -1


def mfa_touch_counter(user_id: int, counter: int) -> bool:
    """Record the counter a code was accepted at; False when a newer one is
    already stored (two racing submits of the same code: one wins)."""
    init()
    with _connect() as c:
        cur = c.execute("UPDATE users SET totp_counter=? WHERE id=? AND totp_counter<?", (counter, user_id, counter))
    return cur.rowcount == 1


def mfa_reset(user_id: int, *, required: bool) -> None:
    """Drop the secret and every backup code (disable, or admin recovery)."""
    init()
    with _connect() as c:
        c.execute("UPDATE users SET totp_secret='', totp_enabled=0, totp_required=?, totp_counter=-1, backup_salt='' WHERE id=?",
                  (1 if required else 0, user_id))
        c.execute("DELETE FROM mfa_backup_codes WHERE user_id=?", (user_id,))
    _users.pop(user_id, None)


def mfa_set_backup_codes(user_id: int, codes: list[str]) -> None:
    """Replace the user's backup codes (stored salted-hashed)."""
    from .. import mfa
    init()
    salt = secrets.token_hex(16)
    with _connect() as c:
        c.execute("DELETE FROM mfa_backup_codes WHERE user_id=?", (user_id,))
        c.execute("UPDATE users SET backup_salt=? WHERE id=?", (salt, user_id))
        c.executemany("INSERT INTO mfa_backup_codes(user_id, code_hash, created_at) VALUES(?,?,?)",
                      [(user_id, mfa.hash_backup_code(code, salt), _now()) for code in codes])


def mfa_backup_codes_left(user_id: int) -> int:
    init()
    with _connect() as c:
        return int(c.execute("SELECT COUNT(*) n FROM mfa_backup_codes WHERE user_id=?", (user_id,)).fetchone()["n"])


def mfa_use_backup_code(user_id: int, code: str) -> bool:
    """Burn a backup code atomically; True when it was valid and unused."""
    from .. import mfa
    init()
    with _connect() as c:
        row = c.execute("SELECT backup_salt FROM users WHERE id=?", (user_id,)).fetchone()
        if not row or not row["backup_salt"]:
            return False
        h = mfa.hash_backup_code(code, row["backup_salt"])
        cur = c.execute("DELETE FROM mfa_backup_codes WHERE user_id=? AND code_hash=?", (user_id, h))
    return cur.rowcount == 1


def delete_user(user_id: int) -> None:
    init()
    with _connect() as c:
        area_ids = [r["id"] for r in c.execute("SELECT id FROM areas WHERE owner_user_id=?", (user_id,)).fetchall()]
        c.execute("DELETE FROM memberships WHERE user_id=?", (user_id,))
        c.execute("DELETE FROM mfa_backup_codes WHERE user_id=?", (user_id,))
        for aid in area_ids:
            c.execute("DELETE FROM memberships WHERE area_id=?", (aid,))
            c.execute("DELETE FROM areas WHERE id=?", (aid,))
            # Their subscriptions, and everyone's subscriptions to their webhooks.
            c.execute("DELETE FROM subscriptions WHERE area_id=? OR publisher_area_id=?", (aid, aid))
            # Their execution agents (tokens stop working) and push devices.
            c.execute("DELETE FROM agents WHERE area_id=?", (aid,))
            c.execute("DELETE FROM agent_pairings WHERE area_id=?", (aid,))
            c.execute("DELETE FROM push_subscriptions WHERE area_id=?", (aid,))
            # Their history, journal and copy-trading state: nothing of a deleted
            # workspace stays behind in the database.
            for table in ("signal_log", "order_log", "journal_fills", "journal_trades", "journal_snapshots",
                          "journal_seen", "journal_imports", "copy_events", "copy_state", "copy_twins"):
                c.execute(f"DELETE FROM {table} WHERE area_id=?", (aid,))
        c.execute("DELETE FROM push_subscriptions WHERE user_id=?", (user_id,))
        c.execute("DELETE FROM password_resets WHERE user_id=?", (user_id,))
        c.execute("DELETE FROM users WHERE id=?", (user_id,))
    _bump_areas_generation()
    _agents_by_hash.clear()
    reset_caches()


def area_owner_email(area_id: int) -> Optional[str]:
    owner = area_owner(area_id)
    user = get_user(owner) if owner else None
    return user["email"] if user else None


def user_features(user_id: int) -> dict[str, bool]:
    aid = user_primary_area(user_id)
    return get_area_features(aid) if aid else default_area_features()


def create_invite(created_by: Optional[int], email: str = "", is_admin: bool = False) -> str:
    init()
    code = secrets.token_urlsafe(16)
    with _connect() as c:
        c.execute(
            "INSERT INTO invites(code,email,is_admin,created_by,created_at) VALUES(?,?,?,?,?)",
            (code, (email or "").strip().lower(), 1 if is_admin else 0, created_by, _now()),
        )
    return code


def get_invite(code: str) -> Optional[dict[str, Any]]:
    init()
    with _connect() as c:
        row = c.execute("SELECT * FROM invites WHERE code=?", (code,)).fetchone()
        if not row:
            return None
        return {"code": row["code"], "email": row["email"], "is_admin": bool(row["is_admin"]),
                "used_by": row["used_by"], "used_at": row["used_at"]}


def list_invites() -> list[dict[str, Any]]:
    init()
    with _connect() as c:
        rows = c.execute("SELECT * FROM invites ORDER BY created_at DESC").fetchall()
        return [{"code": r["code"], "email": r["email"], "is_admin": bool(r["is_admin"]),
                 "used_by": r["used_by"], "used_at": r["used_at"], "created_at": r["created_at"]}
                for r in rows]


def consume_invite(code: str, user_id: int) -> bool:
    """Mark an invite used — atomically, so two racing registrations with the
    same code can never both succeed."""
    init()
    with _connect() as c:
        cur = c.execute("UPDATE invites SET used_by=?, used_at=? WHERE code=? AND used_by IS NULL",
                        (user_id, _now(), code))
        return cur.rowcount == 1


def delete_invite(code: str) -> None:
    init()
    with _connect() as c:
        c.execute("DELETE FROM invites WHERE code=?", (code,))


def create_password_reset(user_id: int, ttl_hours: int = 24) -> str:
    init()
    token = secrets.token_urlsafe(24)
    now = datetime.now(timezone.utc)
    with _connect() as c:
        c.execute(
            "INSERT INTO password_resets(token,user_id,created_at,expires_at) VALUES(?,?,?,?)",
            (token, user_id, now.isoformat(), (now + timedelta(hours=ttl_hours)).isoformat()),
        )
    return token


def get_password_reset(token: str) -> Optional[dict[str, Any]]:
    """A valid (unused, unexpired) reset record, else None."""
    init()
    if not token:
        return None
    with _connect() as c:
        row = c.execute("SELECT * FROM password_resets WHERE token=?", (token,)).fetchone()
    if not row or row["used_at"]:
        return None
    try:
        if datetime.fromisoformat(row["expires_at"]) < datetime.now(timezone.utc):
            return None
    except ValueError:
        return None
    return {"token": row["token"], "user_id": row["user_id"]}


def consume_password_reset(token: str, new_password: str) -> Optional[int]:
    """Set the user's new password and mark the token used. Returns the user id."""
    rec = get_password_reset(token)
    if not rec:
        return None
    with _connect() as c:
        # burn the token first, atomically: two racing submits set one password, not two
        cur = c.execute("UPDATE password_resets SET used_at=? WHERE token=? AND used_at IS NULL AND expires_at>?",
                        (_now(), token, _now()))
        if cur.rowcount != 1:
            return None
        c.execute("UPDATE users SET password_hash=? WHERE id=?",
                  (hash_password(new_password), rec["user_id"]))
        # the link changes the password only: the second factor stays (a reset
        # link in the wrong hands must not be a 2FA bypass); a lost authenticator
        # is recovered by an admin's 2FA reset
    _pw_versions.pop(rec["user_id"], None)
    _users.pop(rec["user_id"], None)
    return rec["user_id"]

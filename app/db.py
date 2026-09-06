"""SQLite persistence for users, areas (per-user isolated workspaces),
memberships and invites.

Design for the current phase (per-user isolation) and the next one (shared
areas):

* **users** — login accounts (email + salted password hash; ``is_admin``).
* **areas** — an isolated workspace; ``settings`` is the JSON config blob that
  used to live in ``data/settings.json`` (token accounts, webhooks, Discord
  config, symbol map, alerts, …). Every user owns exactly one area today.
* **memberships** — (user, area, role). One row per user↔area. Today each user
  has a single ``owner`` membership; sharing later just adds more rows/roles.
* **invites** — single-use invite codes (registration is invite-only).
* **meta** — small key/value store (e.g. the session-cookie signing secret).

Kept dependency-free (stdlib ``sqlite3``) with WAL mode and a short busy timeout.
v5 keeps one connection per thread (v4 opened — and never closed — one per
call) and caches the hot auth lookups (``user_count``, ``get_user``,
``user_primary_area``) so a warm request path needs no SQLite at all. Password
hashing (PBKDF2, ~100 ms) has ``*_async`` wrappers that run it in a worker
thread instead of stalling the event loop.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from .context import DEFAULT_AREA_ID

ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("NEXUSPRED_DATA_DIR") or (ROOT_DIR / "data"))
DB_FILE = DATA_DIR / "fluxbridge.db"

_init_lock = threading.Lock()
_initialized = False
# Bumped whenever an area is created or deleted, so in-memory indexes keyed by
# area (see config.find_webhook) know when to rescan without querying.
_areas_generation = 0


def areas_generation() -> int:
    return _areas_generation

# Per-area feature entitlements. Admins turn these on/off per user (area); the
# feature stays off by default for a freshly created area, so an admin decides
# who gets it. `label` is what the admin UI shows.
FEATURES: dict[str, dict[str, Any]] = {
    "discord_signals": {"label": "Discord Signals", "default": False},
}


def default_area_features() -> dict[str, bool]:
    return {k: bool(v["default"]) for k, v in FEATURES.items()}


def _all_features_on() -> str:
    return json.dumps({k: True for k in FEATURES})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


_local = threading.local()


def _connect() -> sqlite3.Connection:
    """This thread's connection, (re)opened when ``DB_FILE`` changes.

    Callers use it as ``with _connect() as c:`` — that commits / rolls back the
    statement block but never closes, so the connection (and its WAL/pragma
    setup) is reused for the thread's lifetime."""
    conn = getattr(_local, "conn", None)
    if conn is not None and getattr(_local, "path", None) == str(DB_FILE):
        return conn
    if conn is not None:
        try:
            conn.close()
        except sqlite3.Error:
            pass
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_FILE), timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    _local.conn = conn
    _local.path = str(DB_FILE)
    return conn


# --- hot-path caches (auth middleware) --------------------------------------
# Invalidated by the only writers that can change them (create/delete user).
_user_count: Optional[int] = None
_users: dict[int, dict[str, Any]] = {}
_primary_area: dict[int, Optional[int]] = {}


def reset_caches() -> None:
    global _user_count
    _user_count = None
    _users.clear()
    _primary_area.clear()


def init() -> None:
    global _initialized
    with _init_lock:
        if _initialized:
            return
        with _connect() as c:
            c.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    email TEXT UNIQUE NOT NULL,
                    password_hash TEXT NOT NULL,
                    is_admin INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS areas (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    owner_user_id INTEGER NOT NULL,
                    settings TEXT NOT NULL DEFAULT '{}',
                    features TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS memberships (
                    user_id INTEGER NOT NULL,
                    area_id INTEGER NOT NULL,
                    role TEXT NOT NULL DEFAULT 'owner',
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (user_id, area_id)
                );
                CREATE TABLE IF NOT EXISTS invites (
                    code TEXT PRIMARY KEY,
                    email TEXT,
                    is_admin INTEGER NOT NULL DEFAULT 0,
                    created_by INTEGER,
                    created_at TEXT NOT NULL,
                    used_by INTEGER,
                    used_at TEXT
                );
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    actor_user_id INTEGER,
                    actor_email TEXT,
                    action TEXT NOT NULL,
                    target TEXT,
                    detail TEXT
                );
                CREATE TABLE IF NOT EXISTS password_resets (
                    token TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    used_at TEXT
                );
                """
            )
            # --- migrations for databases created before a column existed ---
            area_cols = {r["name"] for r in c.execute("PRAGMA table_info(areas)").fetchall()}
            if "features" not in area_cols:
                c.execute("ALTER TABLE areas ADD COLUMN features TEXT NOT NULL DEFAULT '{}'")
                # Preserve behavior for existing deployments: areas that predate
                # feature gating keep every feature ON, so nobody loses Discord.
                c.execute("UPDATE areas SET features=?", (_all_features_on(),))
        _initialized = True


# --------------------------------------------------------------------- meta
def meta_get(key: str) -> Optional[str]:
    init()
    with _connect() as c:
        row = c.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None


def meta_set(key: str, value: str) -> None:
    init()
    with _connect() as c:
        c.execute(
            "INSERT INTO meta(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )


# --------------------------------------------------------------- passwords
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


# --------------------------------------------------------------- users
def user_count() -> int:
    global _user_count
    if _user_count is None:
        init()
        with _connect() as c:
            _user_count = c.execute("SELECT COUNT(*) n FROM users").fetchone()["n"]
    return _user_count


def _row_to_user(row: sqlite3.Row) -> dict[str, Any]:
    return {"id": row["id"], "email": row["email"], "is_admin": bool(row["is_admin"]),
            "created_at": row["created_at"]}


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


def authenticate(email: str, password: str) -> Optional[dict[str, Any]]:
    init()
    with _connect() as c:
        row = c.execute("SELECT * FROM users WHERE email=?", (email.strip().lower(),)).fetchone()
    if row and verify_password(password, row["password_hash"]):
        return _row_to_user(row)
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
                initial_settings: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Create a user + their own area + an owner membership. Returns the user."""
    global _areas_generation
    init()
    email = email.strip().lower()
    # Default the alert "Notify email" to the owner's own address (unless the
    # migrated/initial settings already carry one), so it's correct per-user.
    init_settings = dict(initial_settings or {})
    if not init_settings.get("alert_email_to"):
        init_settings["alert_email_to"] = email
    settings_json = json.dumps(init_settings)
    with _connect() as c:
        # First user is forced admin; area id of the very first user is DEFAULT_AREA_ID.
        first = c.execute("SELECT COUNT(*) n FROM users").fetchone()["n"] == 0
        cur = c.execute(
            "INSERT INTO users(email,password_hash,is_admin,created_at) VALUES(?,?,?,?)",
            (email, hash_password(password), 1 if (is_admin or first) else 0, _now()),
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
    _areas_generation += 1
    reset_caches()
    return get_user(uid)  # type: ignore[return-value]


def set_password(user_id: int, new_password: str) -> None:
    init()
    with _connect() as c:
        c.execute("UPDATE users SET password_hash=? WHERE id=?",
                  (hash_password(new_password), user_id))


# Async wrappers: PBKDF2 (200k rounds) takes ~100 ms of CPU; run it in a
# worker thread so a login never stalls order processing on the event loop.
async def authenticate_async(email: str, password: str) -> Optional[dict[str, Any]]:
    return await asyncio.to_thread(authenticate, email, password)


async def create_user_async(email: str, password: str, is_admin: bool = False,
                            initial_settings: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    return await asyncio.to_thread(create_user, email, password, is_admin, initial_settings)


async def set_password_async(user_id: int, new_password: str) -> None:
    await asyncio.to_thread(set_password, user_id, new_password)


async def consume_password_reset_async(token: str, new_password: str) -> Optional[int]:
    return await asyncio.to_thread(consume_password_reset, token, new_password)


def delete_user(user_id: int) -> None:
    global _areas_generation
    init()
    with _connect() as c:
        area_ids = [r["id"] for r in c.execute("SELECT id FROM areas WHERE owner_user_id=?", (user_id,)).fetchall()]
        c.execute("DELETE FROM memberships WHERE user_id=?", (user_id,))
        for aid in area_ids:
            c.execute("DELETE FROM memberships WHERE area_id=?", (aid,))
            c.execute("DELETE FROM areas WHERE id=?", (aid,))
        c.execute("DELETE FROM users WHERE id=?", (user_id,))
    _areas_generation += 1
    reset_caches()


# --------------------------------------------------------------- areas
def user_primary_area(user_id: int) -> Optional[int]:
    if user_id in _primary_area:
        return _primary_area[user_id]
    init()
    with _connect() as c:
        row = c.execute(
            "SELECT area_id FROM memberships WHERE user_id=? ORDER BY area_id LIMIT 1",
            (user_id,),
        ).fetchone()
    area = row["area_id"] if row else None
    if area is not None:  # never cache a miss (the user may be mid-creation)
        _primary_area[user_id] = area
    return area


def user_area_ids(user_id: int) -> list[int]:
    init()
    with _connect() as c:
        return [r["area_id"] for r in c.execute(
            "SELECT area_id FROM memberships WHERE user_id=? ORDER BY area_id", (user_id,)).fetchall()]


def all_area_ids() -> list[int]:
    init()
    with _connect() as c:
        return [r["id"] for r in c.execute("SELECT id FROM areas ORDER BY id").fetchall()]


def get_area(area_id: int) -> Optional[dict[str, Any]]:
    init()
    with _connect() as c:
        row = c.execute("SELECT * FROM areas WHERE id=?", (area_id,)).fetchone()
        if not row:
            return None
        return {"id": row["id"], "name": row["name"], "owner_user_id": row["owner_user_id"],
                "created_at": row["created_at"]}


def get_area_settings(area_id: int) -> dict[str, Any]:
    init()
    with _connect() as c:
        row = c.execute("SELECT settings FROM areas WHERE id=?", (area_id,)).fetchone()
    if not row:
        return {}
    try:
        return json.loads(row["settings"] or "{}")
    except json.JSONDecodeError:
        return {}


def save_area_settings(area_id: int, settings: dict[str, Any]) -> None:
    init()
    with _connect() as c:
        c.execute("UPDATE areas SET settings=? WHERE id=?", (json.dumps(settings), area_id))


def area_owner(area_id: int) -> Optional[int]:
    a = get_area(area_id)
    return a["owner_user_id"] if a else None


def _load_features(area_id: int) -> dict[str, Any]:
    with _connect() as c:
        row = c.execute("SELECT features FROM areas WHERE id=?", (area_id,)).fetchone()
    if not row:
        return {}
    try:
        return json.loads(row["features"] or "{}")
    except json.JSONDecodeError:
        return {}


def get_area_features(area_id: int) -> dict[str, bool]:
    """Effective feature flags for an area (stored values merged over defaults)."""
    init()
    stored = _load_features(area_id)
    merged = default_area_features()
    for key in FEATURES:
        if key in stored:
            merged[key] = bool(stored[key])
    return merged


def set_area_feature(area_id: int, feature: str, enabled: bool) -> dict[str, bool]:
    if feature not in FEATURES:
        raise ValueError(f"unknown feature: {feature}")
    init()
    with _connect() as c:
        row = c.execute("SELECT features FROM areas WHERE id=?", (area_id,)).fetchone()
        stored: dict[str, Any] = {}
        if row:
            try:
                stored = json.loads(row["features"] or "{}")
            except json.JSONDecodeError:
                stored = {}
        stored[feature] = bool(enabled)
        c.execute("UPDATE areas SET features=? WHERE id=?", (json.dumps(stored), area_id))
    return get_area_features(area_id)


def user_features(user_id: int) -> dict[str, bool]:
    aid = user_primary_area(user_id)
    return get_area_features(aid) if aid else default_area_features()


def backfill_alert_emails() -> int:
    """Set each area's alert 'Notify email' to its owner's address where unset.

    Makes the per-user default correct for areas created before that behavior
    (or before multi-tenancy), without touching areas where the user chose an
    address. Returns how many areas were updated. Safe to run repeatedly."""
    init()
    updated = 0
    with _connect() as c:
        rows = c.execute(
            "SELECT a.id AS id, a.settings AS settings, u.email AS email "
            "FROM areas a JOIN users u ON u.id = a.owner_user_id").fetchall()
        for r in rows:
            try:
                s = json.loads(r["settings"] or "{}")
            except json.JSONDecodeError:
                s = {}
            if not s.get("alert_email_to") and r["email"]:
                s["alert_email_to"] = r["email"]
                c.execute("UPDATE areas SET settings=? WHERE id=?", (json.dumps(s), r["id"]))
                updated += 1
    return updated


# --------------------------------------------------------------- invites
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
    init()
    with _connect() as c:
        row = c.execute("SELECT * FROM invites WHERE code=? AND used_by IS NULL", (code,)).fetchone()
        if not row:
            return False
        c.execute("UPDATE invites SET used_by=?, used_at=? WHERE code=?", (user_id, _now(), code))
        return True


def delete_invite(code: str) -> None:
    init()
    with _connect() as c:
        c.execute("DELETE FROM invites WHERE code=?", (code,))


# --------------------------------------------------------------- audit log
def log_action(actor_user_id: Optional[int], actor_email: str, action: str,
               target: str = "", detail: str = "") -> None:
    """Record an admin action. Never raises — auditing must not break the action."""
    try:
        init()
        with _connect() as c:
            c.execute(
                "INSERT INTO audit_log(created_at,actor_user_id,actor_email,action,target,detail) "
                "VALUES(?,?,?,?,?,?)",
                (_now(), actor_user_id, actor_email, action, target, detail),
            )
    except Exception:  # noqa: BLE001
        pass


def list_audit(limit: int = 100) -> list[dict[str, Any]]:
    init()
    with _connect() as c:
        rows = c.execute(
            "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall()
        return [{"id": r["id"], "created_at": r["created_at"], "actor_email": r["actor_email"],
                 "action": r["action"], "target": r["target"], "detail": r["detail"]}
                for r in rows]


# ------------------------------------------------------- password resets
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
        c.execute("UPDATE users SET password_hash=? WHERE id=?",
                  (hash_password(new_password), rec["user_id"]))
        c.execute("UPDATE password_resets SET used_at=? WHERE token=?", (_now(), token))
    return rec["user_id"]

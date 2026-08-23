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

Kept dependency-free (stdlib ``sqlite3``) with WAL mode and a short busy timeout;
connections are opened per operation (low traffic).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .context import DEFAULT_AREA_ID

ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("NEXUSPRED_DATA_DIR") or (ROOT_DIR / "data"))
DB_FILE = DATA_DIR / "fluxbridge.db"

_init_lock = threading.Lock()
_initialized = False


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_FILE), timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


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
                """
            )
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
    init()
    with _connect() as c:
        return c.execute("SELECT COUNT(*) n FROM users").fetchone()["n"]


def _row_to_user(row: sqlite3.Row) -> dict[str, Any]:
    return {"id": row["id"], "email": row["email"], "is_admin": bool(row["is_admin"]),
            "created_at": row["created_at"]}


def get_user(user_id: int) -> Optional[dict[str, Any]]:
    init()
    with _connect() as c:
        row = c.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        return _row_to_user(row) if row else None


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
        return [_row_to_user(r) for r in rows]


def create_user(email: str, password: str, is_admin: bool = False,
                initial_settings: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Create a user + their own area + an owner membership. Returns the user."""
    init()
    email = email.strip().lower()
    settings_json = json.dumps(initial_settings or {})
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
            c.execute(
                "INSERT INTO areas(id,name,owner_user_id,settings,created_at) VALUES(?,?,?,?,?)",
                (DEFAULT_AREA_ID, "My area", uid, settings_json, _now()),
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
    return get_user(uid)  # type: ignore[return-value]


def delete_user(user_id: int) -> None:
    init()
    with _connect() as c:
        area_ids = [r["id"] for r in c.execute("SELECT id FROM areas WHERE owner_user_id=?", (user_id,)).fetchall()]
        c.execute("DELETE FROM memberships WHERE user_id=?", (user_id,))
        for aid in area_ids:
            c.execute("DELETE FROM memberships WHERE area_id=?", (aid,))
            c.execute("DELETE FROM areas WHERE id=?", (aid,))
        c.execute("DELETE FROM users WHERE id=?", (user_id,))


# --------------------------------------------------------------- areas
def user_primary_area(user_id: int) -> Optional[int]:
    init()
    with _connect() as c:
        row = c.execute(
            "SELECT area_id FROM memberships WHERE user_id=? ORDER BY area_id LIMIT 1",
            (user_id,),
        ).fetchone()
        return row["area_id"] if row else None


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

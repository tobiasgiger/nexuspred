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

from . import crypto
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
    conn.execute("PRAGMA synchronous=NORMAL")   # WAL + NORMAL: durable across crashes, no fsync per commit
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    _local.conn = conn
    _local.path = str(DB_FILE)
    return conn


# --- hot-path caches (auth middleware) --------------------------------------
# Invalidated by the only writers that can change them (create/delete user).
_user_count: Optional[int] = None
_users: dict[int, dict[str, Any]] = {}
_pw_versions: dict[int, str] = {}  # user id -> fingerprint of the password hash
_primary_area: dict[int, Optional[int]] = {}
_area_ids: Optional[tuple[int, list[int]]] = None  # (areas_generation, ids)


def reset_caches() -> None:
    global _user_count, _area_ids
    _user_count = None
    _area_ids = None
    _users.clear()
    _primary_area.clear()
    _pw_versions.clear()
    _agents_by_hash.clear()
    _agent_touch_at.clear()
    _subs_changed()


def init() -> None:
    global _initialized
    if _initialized:
        return                                  # hot path: no lock once the schema is in place
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
                CREATE TABLE IF NOT EXISTS signal_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    area_id INTEGER NOT NULL,
                    ts TEXT NOT NULL,
                    result TEXT NOT NULL DEFAULT '',
                    webhook TEXT NOT NULL DEFAULT '',
                    payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_signal_log_area ON signal_log(area_id, id);
                CREATE INDEX IF NOT EXISTS ix_signal_log_ts ON signal_log(ts);
                CREATE TABLE IF NOT EXISTS order_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    area_id INTEGER NOT NULL,
                    ts TEXT NOT NULL,
                    action TEXT NOT NULL DEFAULT '',
                    symbol TEXT NOT NULL DEFAULT '',
                    account TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT '',
                    data TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_order_log_area ON order_log(area_id, id);
                CREATE INDEX IF NOT EXISTS ix_order_log_ts ON order_log(ts);
                CREATE TABLE IF NOT EXISTS journal_fills (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    area_id INTEGER NOT NULL,
                    fill_id INTEGER NOT NULL,
                    order_id INTEGER NOT NULL DEFAULT 0,
                    account_id INTEGER NOT NULL DEFAULT 0,
                    contract_id INTEGER NOT NULL DEFAULT 0,
                    symbol TEXT NOT NULL DEFAULT '',
                    ts TEXT NOT NULL,
                    action TEXT NOT NULL DEFAULT '',
                    qty INTEGER NOT NULL DEFAULT 0,
                    price REAL NOT NULL DEFAULT 0,
                    fees REAL NOT NULL DEFAULT 0,
                    UNIQUE(area_id, fill_id)
                );
                CREATE TABLE IF NOT EXISTS journal_trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    area_id INTEGER NOT NULL,
                    pair_id TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT '',
                    account_id INTEGER NOT NULL DEFAULT 0,
                    account_spec TEXT NOT NULL DEFAULT '',
                    account_name TEXT NOT NULL DEFAULT '',
                    environment TEXT NOT NULL DEFAULT '',
                    contract_id INTEGER NOT NULL DEFAULT 0,
                    symbol TEXT NOT NULL DEFAULT '',
                    root TEXT NOT NULL DEFAULT '',
                    side TEXT NOT NULL DEFAULT '',
                    qty INTEGER NOT NULL DEFAULT 0,
                    entry_price REAL NOT NULL DEFAULT 0,
                    exit_price REAL NOT NULL DEFAULT 0,
                    entry_ts TEXT NOT NULL DEFAULT '',
                    exit_ts TEXT NOT NULL DEFAULT '',
                    entry_fill_id INTEGER NOT NULL DEFAULT 0,
                    exit_fill_id INTEGER NOT NULL DEFAULT 0,
                    points REAL NOT NULL DEFAULT 0,
                    value_per_point REAL NOT NULL DEFAULT 1,
                    gross_pnl REAL NOT NULL DEFAULT 0,
                    fees REAL NOT NULL DEFAULT 0,
                    net_pnl REAL NOT NULL DEFAULT 0,
                    note TEXT NOT NULL DEFAULT '',
                    tags TEXT NOT NULL DEFAULT '',
                    imported_at TEXT NOT NULL,
                    UNIQUE(area_id, pair_id)
                );
                CREATE INDEX IF NOT EXISTS ix_journal_trades_exit ON journal_trades(area_id, exit_ts);
                CREATE INDEX IF NOT EXISTS ix_journal_trades_acct_exit ON journal_trades(area_id, account_id, exit_ts);
                CREATE INDEX IF NOT EXISTS ix_journal_trades_fills ON journal_trades(area_id, account_id, entry_fill_id, exit_fill_id);
                CREATE TABLE IF NOT EXISTS journal_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    area_id INTEGER NOT NULL,
                    account_id INTEGER NOT NULL,
                    account_spec TEXT NOT NULL DEFAULT '',
                    day TEXT NOT NULL,
                    total_cash REAL NOT NULL DEFAULT 0,
                    realized_pnl REAL NOT NULL DEFAULT 0,
                    open_pnl REAL NOT NULL DEFAULT 0,
                    week_realized_pnl REAL NOT NULL DEFAULT 0,
                    total_pnl REAL NOT NULL DEFAULT 0,
                    taken_at TEXT NOT NULL,
                    UNIQUE(area_id, account_id, day)
                );
                CREATE TABLE IF NOT EXISTS journal_seen (
                    area_id INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    ref TEXT NOT NULL,
                    UNIQUE(area_id, kind, ref)
                );
                CREATE TABLE IF NOT EXISTS journal_imports (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    area_id INTEGER NOT NULL,
                    ts TEXT NOT NULL,
                    trigger TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT '',
                    by TEXT NOT NULL DEFAULT '',
                    logins INTEGER NOT NULL DEFAULT 0,
                    accounts INTEGER NOT NULL DEFAULT 0,
                    fills INTEGER NOT NULL DEFAULT 0,
                    fills_new INTEGER NOT NULL DEFAULT 0,
                    trades INTEGER NOT NULL DEFAULT 0,
                    trades_new INTEGER NOT NULL DEFAULT 0,
                    snapshots INTEGER NOT NULL DEFAULT 0,
                    duration_ms INTEGER NOT NULL DEFAULT 0,
                    error TEXT NOT NULL DEFAULT '',
                    history_new INTEGER NOT NULL DEFAULT 0,
                    detail TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS agents (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    area_id INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    token_hash TEXT UNIQUE NOT NULL,
                    version TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    last_seen_at TEXT,
                    last_ip TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS agent_pairings (
                    code TEXT PRIMARY KEY,
                    area_id INTEGER NOT NULL,
                    name TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    used_at TEXT
                );
                CREATE TABLE IF NOT EXISTS push_subscriptions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    area_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    endpoint TEXT UNIQUE NOT NULL,
                    p256dh TEXT NOT NULL,
                    auth TEXT NOT NULL,
                    device TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    last_used_at TEXT,
                    failures INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS copy_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    area_id INTEGER NOT NULL,
                    group_id TEXT NOT NULL,
                    ts TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    leader TEXT NOT NULL DEFAULT '',
                    follower TEXT NOT NULL DEFAULT '',
                    symbol TEXT NOT NULL DEFAULT '',
                    detail TEXT NOT NULL DEFAULT '',
                    latency_ms INTEGER
                );
                CREATE INDEX IF NOT EXISTS copy_events_area ON copy_events(area_id, group_id, id);
                CREATE TABLE IF NOT EXISTS copy_state (
                    area_id INTEGER NOT NULL,
                    group_id TEXT NOT NULL,
                    contract_id INTEGER NOT NULL,
                    symbol TEXT NOT NULL DEFAULT '',
                    leader_net INTEGER NOT NULL DEFAULT 0,
                    unit INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(area_id, group_id, contract_id)
                );
                CREATE TABLE IF NOT EXISTS copy_twins (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    area_id INTEGER NOT NULL,
                    group_id TEXT NOT NULL,
                    spec TEXT NOT NULL,
                    leader_order_id INTEGER NOT NULL,
                    follower_order_id INTEGER NOT NULL,
                    contract_id INTEGER NOT NULL DEFAULT 0,
                    symbol TEXT NOT NULL DEFAULT '',
                    action TEXT NOT NULL DEFAULT '',
                    qty INTEGER NOT NULL DEFAULT 0,
                    order_type TEXT NOT NULL DEFAULT '',
                    price REAL,
                    stop_price REAL,
                    version_id INTEGER NOT NULL DEFAULT 0,
                    oco_with INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(area_id, group_id, spec, leader_order_id)
                );
                CREATE TABLE IF NOT EXISTS subscriptions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    area_id INTEGER NOT NULL,
                    publisher_area_id INTEGER NOT NULL,
                    webhook_id TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    accounts TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(area_id, publisher_area_id, webhook_id)
                );
                """
            )
            # --- migrations for databases created before a column existed ---
            user_cols = {r["name"] for r in c.execute("PRAGMA table_info(users)").fetchall()}
            if "last_login_at" not in user_cols:
                c.execute("ALTER TABLE users ADD COLUMN last_login_at TEXT")
                c.execute("ALTER TABLE users ADD COLUMN last_login_ip TEXT")
            if "session_salt" not in user_cols:
                c.execute("ALTER TABLE users ADD COLUMN session_salt TEXT NOT NULL DEFAULT ''")
            imp_cols = {r["name"] for r in c.execute("PRAGMA table_info(journal_imports)").fetchall()}
            if imp_cols and "history_new" not in imp_cols:
                c.execute("ALTER TABLE journal_imports ADD COLUMN history_new INTEGER NOT NULL DEFAULT 0")
            if imp_cols and "detail" not in imp_cols:
                c.execute("ALTER TABLE journal_imports ADD COLUMN detail TEXT NOT NULL DEFAULT ''")
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
    keys = row.keys()
    return {"id": row["id"], "email": row["email"], "is_admin": bool(row["is_admin"]),
            "created_at": row["created_at"],
            "last_login_at": row["last_login_at"] if "last_login_at" in keys else None,
            "last_login_ip": row["last_login_ip"] if "last_login_ip" in keys else None}


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
    settings_json = json.dumps(crypto.encrypt_settings(init_settings))
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
    _areas_generation += 1
    _agents_by_hash.clear()
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
    """Every area id, ascending. Cached until an area is created or deleted
    (the health loops ask every cycle)."""
    global _area_ids
    if _area_ids is not None and _area_ids[0] == _areas_generation:
        return list(_area_ids[1])
    init()
    with _connect() as c:
        ids = [r["id"] for r in c.execute("SELECT id FROM areas ORDER BY id").fetchall()]
    _area_ids = (_areas_generation, ids)
    return list(ids)


def get_area(area_id: int) -> Optional[dict[str, Any]]:
    init()
    with _connect() as c:
        row = c.execute("SELECT * FROM areas WHERE id=?", (area_id,)).fetchone()
        if not row:
            return None
        return {"id": row["id"], "name": row["name"], "owner_user_id": row["owner_user_id"],
                "created_at": row["created_at"]}


def get_area_settings_raw(area_id: int) -> dict[str, Any]:
    """The stored settings JSON as-is (secrets still encrypted)."""
    init()
    with _connect() as c:
        row = c.execute("SELECT settings FROM areas WHERE id=?", (area_id,)).fetchone()
    if not row:
        return {}
    try:
        data = json.loads(row["settings"] or "{}")
    except json.JSONDecodeError as exc:
        # corrupt JSON must surface: returning {} would let the next save wipe the area
        raise ValueError(f"settings of area {area_id} are not valid JSON: {exc}") from exc
    return data if isinstance(data, dict) else {}


def get_area_settings(area_id: int) -> dict[str, Any]:
    """An area's settings with every secret decrypted (see :mod:`app.crypto`)."""
    return crypto.decrypt_settings(get_area_settings_raw(area_id))


def save_area_settings(area_id: int, settings: dict[str, Any]) -> None:
    """Persist an area's settings; secret fields are encrypted on the way in.

    A secret the current key cannot decrypt reads as an empty string; when the
    caller hands such an empty value back for a field whose stored cipher text
    is undecryptable, the stored cipher text is kept — a settings save must
    never destroy a token that a corrected key could still recover."""
    init()
    with _connect() as c:
        row = c.execute("SELECT settings FROM areas WHERE id=?", (area_id,)).fetchone()
        previous: dict[str, Any] = {}
        if row:
            try:
                loaded = json.loads(row["settings"] or "{}")
                previous = loaded if isinstance(loaded, dict) else {}
            except json.JSONDecodeError:
                previous = {}
        c.execute("UPDATE areas SET settings=? WHERE id=?",
                  (json.dumps(crypto.encrypt_settings(crypto.keep_undecryptable(settings, previous))), area_id))


def encrypt_existing_settings() -> int:
    """One-shot upgrade: re-save every area whose stored settings still hold a
    plain-text secret. Returns how many areas were rewritten. Idempotent."""
    init()
    rewritten = 0
    for aid in all_area_ids():
        try:
            raw = get_area_settings_raw(aid)
        except ValueError:
            continue                                # corrupt row: leave it for the operator
        if crypto.needs_reencrypt(raw):  # plain text, or readable only with a previous key
            save_area_settings(aid, crypto.decrypt_settings(raw))
            rewritten += 1
    return rewritten


def area_owner(area_id: int) -> Optional[int]:
    a = get_area(area_id)
    return a["owner_user_id"] if a else None


def area_owner_email(area_id: int) -> Optional[str]:
    owner = area_owner(area_id)
    user = get_user(owner) if owner else None
    return user["email"] if user else None


# --------------------------------------------------------------- subscriptions
# A subscription = one area following a webhook another area has published on
# the marketplace, executed on the subscriber's own accounts (see app.marketplace
# and signals.forward_to_subscribers).
_active_subs: dict[tuple[int, str], list[dict[str, Any]]] = {}
_sub_counts: dict[int, dict[str, int]] = {}  # publisher area → {webhook_id: count}


def _subs_changed() -> None:
    _active_subs.clear()
    _sub_counts.clear()


def _row_to_sub(r: sqlite3.Row) -> dict[str, Any]:
    try:
        accounts = json.loads(r["accounts"] or "[]")
    except json.JSONDecodeError:
        accounts = []
    return {"id": r["id"], "area_id": r["area_id"], "publisher_area_id": r["publisher_area_id"],
            "webhook_id": r["webhook_id"], "enabled": bool(r["enabled"]), "accounts": accounts,
            "created_at": r["created_at"], "updated_at": r["updated_at"]}


def upsert_subscription(area_id: int, publisher_area_id: int, webhook_id: str,
                        accounts: list[dict[str, Any]], enabled: bool = True) -> dict[str, Any]:
    """Create or update the subscriber area's subscription to a published webhook."""
    init()
    now = _now()
    with _connect() as c:
        c.execute(
            "INSERT INTO subscriptions(area_id,publisher_area_id,webhook_id,enabled,accounts,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(area_id,publisher_area_id,webhook_id) DO UPDATE SET "
            "enabled=excluded.enabled, accounts=excluded.accounts, updated_at=excluded.updated_at",
            (area_id, publisher_area_id, webhook_id, 1 if enabled else 0, json.dumps(accounts), now, now),
        )
        row = c.execute("SELECT * FROM subscriptions WHERE area_id=? AND publisher_area_id=? AND webhook_id=?",
                        (area_id, publisher_area_id, webhook_id)).fetchone()
    _subs_changed()
    return _row_to_sub(row)


def get_subscription(sub_id: int, area_id: Optional[int] = None) -> Optional[dict[str, Any]]:
    init()
    with _connect() as c:
        row = c.execute("SELECT * FROM subscriptions WHERE id=?", (sub_id,)).fetchone()
    if not row or (area_id is not None and row["area_id"] != area_id):
        return None
    return _row_to_sub(row)


def update_subscription(sub_id: int, area_id: int, *, enabled: Optional[bool] = None,
                        accounts: Optional[list[dict[str, Any]]] = None) -> Optional[dict[str, Any]]:
    """Update a subscriber's own subscription (enabled flag and/or routed accounts)."""
    cur = get_subscription(sub_id, area_id)
    if not cur:
        return None
    init()
    with _connect() as c:
        c.execute("UPDATE subscriptions SET enabled=?, accounts=?, updated_at=? WHERE id=?",
                  (1 if (cur["enabled"] if enabled is None else enabled) else 0,
                   json.dumps(cur["accounts"] if accounts is None else accounts), _now(), sub_id))
    _subs_changed()
    return get_subscription(sub_id, area_id)


def delete_subscription(sub_id: int, *, area_id: Optional[int] = None,
                        publisher_area_id: Optional[int] = None) -> Optional[dict[str, Any]]:
    """Remove a subscription — by its subscriber (``area_id``) or by the publisher
    (``publisher_area_id``, "kick"). Returns the removed row or None."""
    init()
    with _connect() as c:
        row = c.execute("SELECT * FROM subscriptions WHERE id=?", (sub_id,)).fetchone()
        if not row:
            return None
        if area_id is not None and row["area_id"] != area_id:
            return None
        if publisher_area_id is not None and row["publisher_area_id"] != publisher_area_id:
            return None
        c.execute("DELETE FROM subscriptions WHERE id=?", (sub_id,))
    _subs_changed()
    return _row_to_sub(row)


def delete_subscriptions_for_webhook(publisher_area_id: int, webhook_id: str) -> int:
    init()
    with _connect() as c:
        cur = c.execute("DELETE FROM subscriptions WHERE publisher_area_id=? AND webhook_id=?",
                        (publisher_area_id, webhook_id))
        n = cur.rowcount
    _subs_changed()
    return n


def list_subscriptions(area_id: int) -> list[dict[str, Any]]:
    """The subscriptions an area holds (as a subscriber)."""
    init()
    with _connect() as c:
        rows = c.execute("SELECT * FROM subscriptions WHERE area_id=? ORDER BY id", (area_id,)).fetchall()
    return [_row_to_sub(r) for r in rows]


def list_subscribers(publisher_area_id: int, webhook_id: str) -> list[dict[str, Any]]:
    """Everyone subscribed to one published webhook, with the subscriber's email."""
    init()
    with _connect() as c:
        rows = c.execute(
            "SELECT s.*, u.email AS email FROM subscriptions s "
            "JOIN areas a ON a.id = s.area_id JOIN users u ON u.id = a.owner_user_id "
            "WHERE s.publisher_area_id=? AND s.webhook_id=? ORDER BY s.id",
            (publisher_area_id, webhook_id)).fetchall()
    return [{**_row_to_sub(r), "email": r["email"]} for r in rows]


def subscriber_counts(publisher_area_id: int) -> dict[str, int]:
    """webhook_id → number of subscriptions (enabled or not) for a publisher area.
    Cached (the webhook list asks on every load) until a subscription changes."""
    cached = _sub_counts.get(publisher_area_id)
    if cached is not None:
        return dict(cached)
    init()
    with _connect() as c:
        rows = c.execute("SELECT webhook_id, COUNT(*) n FROM subscriptions WHERE publisher_area_id=? GROUP BY webhook_id",
                         (publisher_area_id,)).fetchall()
    counts = {r["webhook_id"]: r["n"] for r in rows}
    _sub_counts[publisher_area_id] = counts
    return dict(counts)


def active_subscriptions(publisher_area_id: int, webhook_id: str) -> list[dict[str, Any]]:
    """Enabled subscriptions to a published webhook — the hot path of the signal
    fan-out, cached until any subscription changes."""
    key = (publisher_area_id, webhook_id)
    cached = _active_subs.get(key)
    if cached is not None:
        return [dict(s) for s in cached]
    init()
    with _connect() as c:
        rows = c.execute("SELECT * FROM subscriptions WHERE publisher_area_id=? AND webhook_id=? AND enabled=1 ORDER BY id",
                         key).fetchall()
    subs = [_row_to_sub(r) for r in rows]
    _active_subs[key] = subs
    return [dict(s) for s in subs]


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


# ---------------------------------------------------------------- history
def _json(value: Any) -> str:
    try:
        return json.dumps(value, default=str)
    except (TypeError, ValueError):
        return json.dumps(str(value))


def insert_signal(area_id: int, entry: dict[str, Any]) -> int:
    init()
    with _connect() as c:
        cur = c.execute(
            "INSERT INTO signal_log(area_id,ts,result,webhook,payload) VALUES(?,?,?,?,?)",
            (area_id, entry.get("ts") or _now(), str(entry.get("result") or "")[:200],
             str(entry.get("webhook") or "")[:200], _json(entry.get("payload"))))
        return int(cur.lastrowid or 0)


def insert_order(area_id: int, entry: dict[str, Any]) -> int:
    init()
    data = {k: v for k, v in entry.items() if k != "ts"}
    with _connect() as c:
        cur = c.execute(
            "INSERT INTO order_log(area_id,ts,action,symbol,account,status,data) VALUES(?,?,?,?,?,?,?)",
            (area_id, entry.get("ts") or _now(), str(entry.get("action") or "")[:40],
             str(entry.get("symbol") or "")[:40], str(entry.get("account") or "")[:120],
             str(entry.get("status") or "")[:60], _json(data)))
        return int(cur.lastrowid or 0)


def _signal_row(r: sqlite3.Row) -> dict[str, Any]:
    try:
        payload = json.loads(r["payload"])
    except (TypeError, ValueError):
        payload = {"raw": r["payload"]}
    return {"id": r["id"], "ts": r["ts"], "result": r["result"], "webhook": r["webhook"], "payload": payload}


def _order_row(r: sqlite3.Row) -> dict[str, Any]:
    try:
        data = json.loads(r["data"])
    except (TypeError, ValueError):
        data = {}
    return {"id": r["id"], "ts": r["ts"], **data,
            "action": r["action"], "symbol": r["symbol"], "account": r["account"], "status": r["status"]}


def list_signals(area_id: int, *, limit: int = 100, before: Optional[int] = None,
                 result: str = "", q: str = "") -> dict[str, Any]:
    """Newest-first page of an area's signals. ``before`` = id cursor from the
    previous page's ``next_before``; ``result`` = prefix filter (``ok``,
    ``error``…); ``q`` = substring of the payload / webhook name."""
    init()
    limit = max(1, min(int(limit), 500))
    where = ["area_id=?"]
    params: list[Any] = [area_id]
    if before:
        where.append("id<?"); params.append(int(before))
    if result:
        where.append("result LIKE ?"); params.append(f"{result}%")
    if q:
        where.append("(payload LIKE ? OR webhook LIKE ?)"); params += [f"%{q}%", f"%{q}%"]
    with _connect() as c:
        rows = c.execute(f"SELECT * FROM signal_log WHERE {' AND '.join(where)} ORDER BY id DESC LIMIT ?",
                         (*params, limit + 1)).fetchall()
    items = [_signal_row(r) for r in rows[:limit]]
    return {"items": items, "next_before": items[-1]["id"] if len(rows) > limit else None}


def list_orders(area_id: int, *, limit: int = 100, before: Optional[int] = None,
                symbol: str = "", account: str = "") -> dict[str, Any]:
    init()
    limit = max(1, min(int(limit), 500))
    where = ["area_id=?"]
    params: list[Any] = [area_id]
    if before:
        where.append("id<?"); params.append(int(before))
    if symbol:
        where.append("symbol LIKE ?"); params.append(f"{symbol}%")
    if account:
        where.append("account LIKE ?"); params.append(f"%{account}%")
    with _connect() as c:
        rows = c.execute(f"SELECT * FROM order_log WHERE {' AND '.join(where)} ORDER BY id DESC LIMIT ?",
                         (*params, limit + 1)).fetchall()
    items = [_order_row(r) for r in rows[:limit]]
    return {"items": items, "next_before": items[-1]["id"] if len(rows) > limit else None}


def history_stats(area_id: int, since_ts: str) -> dict[str, Any]:
    """Signal outcomes and order counts since ``since_ts`` (ISO), per day and in total."""
    init()
    with _connect() as c:
        sig = c.execute(
            "SELECT substr(ts,1,10) day, "
            "SUM(CASE WHEN result='received' THEN 1 ELSE 0 END) received, "
            "SUM(CASE WHEN result LIKE 'error%' THEN 1 ELSE 0 END) errors, "
            "SUM(CASE WHEN result='skipped' THEN 1 ELSE 0 END) skipped, "
            "SUM(CASE WHEN result NOT IN ('received','skipped','test','simulated') "
            "         AND result NOT LIKE 'error%' THEN 1 ELSE 0 END) executed "
            "FROM signal_log WHERE area_id=? AND ts>=? GROUP BY day ORDER BY day",
            (area_id, since_ts)).fetchall()
        orders = c.execute(
            "SELECT substr(ts,1,10) day, COUNT(*) n, "
            "SUM(CASE WHEN status LIKE '%reject%' THEN 1 ELSE 0 END) rejected "
            "FROM order_log WHERE area_id=? AND ts>=? GROUP BY day ORDER BY day",
            (area_id, since_ts)).fetchall()
    days: dict[str, dict[str, int]] = {}
    for r in sig:
        days.setdefault(r["day"], {})
        days[r["day"]].update(received=r["received"] or 0, executed=r["executed"] or 0,
                              errors=r["errors"] or 0, skipped=r["skipped"] or 0)
    for r in orders:
        days.setdefault(r["day"], {})
        days[r["day"]].update(orders=r["n"] or 0, rejected=r["rejected"] or 0)
    keys = ("received", "executed", "errors", "skipped", "orders", "rejected")
    totals = {k: sum(d.get(k, 0) for d in days.values()) for k in keys}
    return {"since": since_ts, "totals": totals,
            "days": [{"day": d, **{k: v.get(k, 0) for k in keys}} for d, v in sorted(days.items())]}


# ------------------------------------------------------------ copy trading
def insert_copy_event(area_id: int, rec: dict[str, Any]) -> int:
    """Append one copy-trading event (mirror, reject, drift, feed up/lost …)."""
    init()
    with _connect() as c:
        cur = c.execute(
            "INSERT INTO copy_events(area_id, group_id, ts, kind, leader, follower, symbol, detail, latency_ms) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (area_id, str(rec.get("group_id") or ""), rec.get("ts") or _now(), str(rec.get("kind") or ""),
             str(rec.get("leader") or ""), str(rec.get("follower") or ""), str(rec.get("symbol") or ""),
             str(rec.get("detail") or "")[:400], rec.get("latency_ms")))
        return int(cur.lastrowid or 0)


def list_copy_events(area_id: int, group_id: str = "", limit: int = 100) -> list[dict[str, Any]]:
    init()
    limit = max(1, min(int(limit), 1000))
    with _connect() as c:
        if group_id:
            rows = c.execute("SELECT * FROM copy_events WHERE area_id=? AND group_id=? ORDER BY id DESC LIMIT ?",
                             (area_id, group_id, limit)).fetchall()
        else:
            rows = c.execute("SELECT * FROM copy_events WHERE area_id=? ORDER BY id DESC LIMIT ?",
                             (area_id, limit)).fetchall()
    return [dict(r) for r in rows]


_TWIN_COLS = ("contract_id", "symbol", "action", "qty", "order_type", "price", "stop_price", "version_id", "oco_with")


def save_copy_twin(area_id: int, group_id: str, spec: str, leader_order_id: int, follower_order_id: int, **fields: Any) -> None:
    init()
    now = _now()
    vals = {k: fields.get(k) for k in _TWIN_COLS if k in fields}
    with _connect() as c:
        c.execute(
            "INSERT INTO copy_twins(area_id, group_id, spec, leader_order_id, follower_order_id, contract_id, symbol, action, qty, "
            "order_type, price, stop_price, version_id, oco_with, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(area_id, group_id, spec, leader_order_id) DO UPDATE SET follower_order_id=excluded.follower_order_id, "
            "contract_id=excluded.contract_id, symbol=excluded.symbol, action=excluded.action, qty=excluded.qty, order_type=excluded.order_type, "
            "price=excluded.price, stop_price=excluded.stop_price, version_id=excluded.version_id, oco_with=excluded.oco_with, updated_at=excluded.updated_at",
            (area_id, group_id, spec, int(leader_order_id), int(follower_order_id), int(vals.get("contract_id") or 0),
             str(vals.get("symbol") or ""), str(vals.get("action") or ""), int(vals.get("qty") or 0), str(vals.get("order_type") or ""),
             vals.get("price"), vals.get("stop_price"), int(vals.get("version_id") or 0), int(vals.get("oco_with") or 0), now, now))


def delete_copy_twin(area_id: int, group_id: str, spec: str, leader_order_id: int) -> None:
    init()
    with _connect() as c:
        c.execute("DELETE FROM copy_twins WHERE area_id=? AND group_id=? AND spec=? AND leader_order_id=?",
                  (area_id, group_id, spec, int(leader_order_id)))


def delete_copy_twins(area_id: int, group_id: str) -> None:
    init()
    with _connect() as c:
        c.execute("DELETE FROM copy_twins WHERE area_id=? AND group_id=?", (area_id, group_id))


def list_copy_twins(area_id: int, group_id: str) -> list[dict[str, Any]]:
    init()
    with _connect() as c:
        return [dict(r) for r in c.execute("SELECT * FROM copy_twins WHERE area_id=? AND group_id=? ORDER BY id",
                                           (area_id, group_id)).fetchall()]


def save_copy_state(area_id: int, group_id: str, contract_id: int, symbol: str, leader_net: int, unit: int) -> None:
    """Remember a mirrored contract's leader position (survives a restart)."""
    init()
    with _connect() as c:
        c.execute("INSERT INTO copy_state(area_id, group_id, contract_id, symbol, leader_net, unit, updated_at) VALUES(?,?,?,?,?,?,?) "
                  "ON CONFLICT(area_id, group_id, contract_id) DO UPDATE SET symbol=excluded.symbol, leader_net=excluded.leader_net, "
                  "unit=excluded.unit, updated_at=excluded.updated_at",
                  (area_id, group_id, int(contract_id), str(symbol or ""), int(leader_net), int(unit), _now()))


def delete_copy_state(area_id: int, group_id: str, contract_id: int | None = None) -> None:
    init()
    with _connect() as c:
        if contract_id is None:
            c.execute("DELETE FROM copy_state WHERE area_id=? AND group_id=?", (area_id, group_id))
        else:
            c.execute("DELETE FROM copy_state WHERE area_id=? AND group_id=? AND contract_id=?", (area_id, group_id, int(contract_id)))


def list_copy_state(area_id: int, group_id: str) -> list[dict[str, Any]]:
    init()
    with _connect() as c:
        return [dict(r) for r in c.execute("SELECT * FROM copy_state WHERE area_id=? AND group_id=?", (area_id, group_id)).fetchall()]


def prune_copy_events(days: int = 7) -> int:
    init()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with _connect() as c:
        return int(c.execute("DELETE FROM copy_events WHERE ts<?", (cutoff,)).rowcount or 0)


def prune_history(cutoff_ts: str) -> int:
    init()
    with _connect() as c:
        a = c.execute("DELETE FROM signal_log WHERE ts<?", (cutoff_ts,)).rowcount
        b = c.execute("DELETE FROM order_log WHERE ts<?", (cutoff_ts,)).rowcount
    return int(a or 0) + int(b or 0)


# ---------------------------------------------------------------- journal
_TRADE_COLS = ("pair_id", "source", "account_id", "account_spec", "account_name", "environment",
               "contract_id", "symbol", "root", "side", "qty", "entry_price", "exit_price", "entry_ts",
               "exit_ts", "entry_fill_id", "exit_fill_id", "points", "value_per_point", "gross_pnl",
               "fees", "net_pnl")


def upsert_journal_fill(area_id: int, f: dict[str, Any]) -> int:
    """Insert a fill (idempotent by Tradovate fill id). Returns 1 when new."""
    init()
    with _connect() as c:
        cur = c.execute(
            "INSERT OR IGNORE INTO journal_fills(area_id,fill_id,order_id,account_id,contract_id,symbol,ts,action,qty,price,fees) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (area_id, f["fill_id"], f.get("order_id", 0), f.get("account_id", 0), f.get("contract_id", 0),
             f.get("symbol", ""), f.get("ts", ""), f.get("action", ""), f.get("qty", 0), f.get("price", 0), f.get("fees", 0)))
        return int(cur.rowcount or 0)


def upsert_journal_trade(area_id: int, t: dict[str, Any]) -> int:
    """Insert a round-trip trade (idempotent by pair id). Returns 1 when new."""
    init()
    cols = ",".join(_TRADE_COLS)
    marks = ",".join("?" * len(_TRADE_COLS))
    with _connect() as c:
        cur = c.execute(
            f"INSERT OR IGNORE INTO journal_trades(area_id,{cols},imported_at) VALUES(?,{marks},?)",
            (area_id, *[t.get(k) for k in _TRADE_COLS], _now()))
        return int(cur.rowcount or 0)


def find_similar_journal_trade(area_id: int, t: dict[str, Any], tolerance_s: int = 5) -> Optional[int]:
    """Id of an already-stored trade that is the same round trip under a key of a
    *different family* (``pair:`` = fill ids from the API or a Performance export,
    ``ord:``/``fill:``/``fifo:`` = FIFO-paired): same account, symbol, side, qty,
    entry/exit price and an exit within ``tolerance_s`` seconds. Same-family
    trades are keyed exactly, so two genuinely identical split fills (two 1-lot
    pairs at the same price and second) are never collapsed."""
    init()
    try:
        exit_dt = datetime.fromisoformat(str(t.get("exit_ts")).replace("Z", "+00:00"))
    except ValueError:
        return None
    lo = (exit_dt - timedelta(seconds=tolerance_s)).isoformat()
    hi = (exit_dt + timedelta(seconds=tolerance_s)).isoformat()
    family = str(t.get("pair_id", "")).split(":", 1)[0] + ":"
    efid, xfid = int(t.get("entry_fill_id") or 0), int(t.get("exit_fill_id") or 0)
    with _connect() as c:
        if efid and xfid:
            # Same broker fill ids under another key family (e.g. the Performance
            # report vs the live fill pairs) → the same round trip, whatever the
            # report's timestamps say.
            r = c.execute(
                "SELECT id FROM journal_trades WHERE area_id=? AND account_id=? AND entry_fill_id=? AND exit_fill_id=? "
                "AND substr(pair_id, 1, instr(pair_id, ':')) <> ? LIMIT 1",
                (area_id, t.get("account_id", 0), efid, xfid, family)).fetchone()
            if r:
                return int(r["id"])
        r = c.execute(
            "SELECT id FROM journal_trades WHERE area_id=? AND account_id=? AND symbol=? AND side=? AND qty=? "
            "AND ABS(entry_price-?)<1e-6 AND ABS(exit_price-?)<1e-6 AND exit_ts BETWEEN ? AND ? "
            "AND substr(pair_id, 1, instr(pair_id, ':')) <> ? LIMIT 1",
            (area_id, t.get("account_id", 0), t.get("symbol", ""), t.get("side", ""), t.get("qty", 0),
             float(t.get("entry_price") or 0), float(t.get("exit_price") or 0), lo, hi, family)).fetchone()
    return int(r["id"]) if r else None


_SOURCE_RANK = {"history": 0, "fillpair": 1, "report": 2, "csv": 3, "fifo": 4}


def dedupe_journal_trades(area_id: int, tolerance_s: int = 5) -> int:
    """Collapse round trips stored more than once because they arrived from
    different imports (live fill pairs, the Performance report, a CSV upload):
    same account, symbol, side, qty, entry/exit price and an exit within
    ``tolerance_s`` seconds, under a *different source or key family*. Two rows
    from the same source and family with different keys are genuine split fills
    and are left alone. Keeps the row from the most authoritative source (the
    book's fill pairs first), carries over a note / tags the survivor lacks, and
    returns how many rows were deleted."""
    init()
    with _connect() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT id, pair_id, source, account_id, symbol, side, qty, entry_price, exit_price, exit_ts, note, tags, "
            "entry_fill_id, exit_fill_id FROM journal_trades WHERE area_id=? ORDER BY exit_ts, id", (area_id,)).fetchall()]
    def fam(r): return str(r["pair_id"]).split(":", 1)[0]
    # pass 1: identical broker fill ids under different sources / families
    by_fills: dict[tuple[int, int, int], list[dict[str, Any]]] = {}
    for r in rows:
        if r["entry_fill_id"] and r["exit_fill_id"]:
            by_fills.setdefault((r["account_id"], r["entry_fill_id"], r["exit_fill_id"]), []).append(r)
    fill_groups = [g for g in by_fills.values() if len(g) > 1 and len({(m["source"], fam(m)) for m in g}) > 1]
    grouped_ids = {m["id"] for g in fill_groups for m in g}
    rows = [r for r in rows if r["id"] not in grouped_ids]
    def ts(r):
        try:
            return datetime.fromisoformat(str(r["exit_ts"]).replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    def same_trip(a, b):
        return (a["account_id"] == b["account_id"] and a["symbol"] == b["symbol"] and a["side"] == b["side"]
                and a["qty"] == b["qty"] and abs(float(a["entry_price"]) - float(b["entry_price"])) < 1e-6
                and abs(float(a["exit_price"]) - float(b["exit_price"])) < 1e-6
                and (a["source"] != b["source"] or fam(a) != fam(b)))
    groups: list[list[dict[str, Any]]] = []
    for r in rows:
        t = ts(r)
        placed = False
        if t is not None:
            for g in reversed(groups):
                gt = ts(g[0])
                if gt is None or t - gt > tolerance_s:
                    break
                if all(same_trip(r, m) for m in g):
                    g.append(r); placed = True
                    break
        if not placed:
            groups.append([r])
    removed = 0
    with _connect() as c:
        for g in fill_groups + groups:
            if len(g) < 2:
                continue
            g.sort(key=lambda r: (_SOURCE_RANK.get(r["source"], 9), r["id"]))
            keep, drop = g[0], g[1:]
            note = keep["note"] or next((d["note"] for d in drop if d["note"]), "")
            tags = keep["tags"] or next((d["tags"] for d in drop if d["tags"]), "")
            if (note, tags) != (keep["note"], keep["tags"]):
                c.execute("UPDATE journal_trades SET note=?, tags=? WHERE id=?", (note, tags, keep["id"]))
            c.execute(f"DELETE FROM journal_trades WHERE area_id=? AND id IN ({','.join('?' * len(drop))})",
                      (area_id, *[d["id"] for d in drop]))
            removed += len(drop)
    return removed


def _trade_row(r: sqlite3.Row) -> dict[str, Any]:
    d = dict(r)
    d["tags"] = [x for x in (d.get("tags") or "").split(",") if x]
    return d


def list_journal_trades(area_id: int, *, frm: str = "", to: str = "", account: str = "",
                        symbol: str = "", side: str = "", limit: int = 0,
                        before: Optional[int] = None) -> list[dict[str, Any]]:
    """Trades closed in [frm, to) (ISO-UTC; empty = open-ended), newest first
    when ``limit`` is set, else chronological (for aggregation)."""
    init()
    where = ["area_id=?"]
    params: list[Any] = [area_id]
    if frm:
        where.append("exit_ts>=?"); params.append(frm)
    if to:
        where.append("exit_ts<?"); params.append(to)
    if account:
        where.append("(account_spec=? OR account_name=? OR CAST(account_id AS TEXT)=?)"); params += [account, account, account]
    if symbol:
        where.append("(root=? OR symbol=?)"); params += [symbol, symbol]
    if side:
        where.append("side=?"); params.append(side)
    if before:
        where.append("id<?"); params.append(int(before))
    order = "ORDER BY exit_ts DESC, id DESC" if limit else "ORDER BY exit_ts ASC, id ASC"
    lim = f" LIMIT {int(limit)}" if limit else ""
    with _connect() as c:
        rows = c.execute(f"SELECT * FROM journal_trades WHERE {' AND '.join(where)} {order}{lim}", params).fetchall()
    return [_trade_row(r) for r in rows]


def get_journal_trade(area_id: int, trade_id: int) -> Optional[dict[str, Any]]:
    init()
    with _connect() as c:
        r = c.execute("SELECT * FROM journal_trades WHERE area_id=? AND id=?", (area_id, trade_id)).fetchone()
    return _trade_row(r) if r else None


def update_journal_trade_note(area_id: int, trade_id: int, note: str, tags: list[str]) -> bool:
    init()
    clean = ",".join(sorted({t.strip().lower()[:30] for t in tags if t and t.strip()}))
    with _connect() as c:
        cur = c.execute("UPDATE journal_trades SET note=?, tags=? WHERE area_id=? AND id=?",
                        (note[:2000], clean, area_id, trade_id))
    return bool(cur.rowcount)


def journal_accounts(area_id: int) -> list[dict[str, Any]]:
    init()
    with _connect() as c:
        rows = c.execute(
            "SELECT account_id, account_spec, account_name, environment, COUNT(*) n, MIN(exit_ts) first_ts, MAX(exit_ts) last_ts "
            "FROM journal_trades WHERE area_id=? GROUP BY account_id ORDER BY account_name", (area_id,)).fetchall()
    return [dict(r) for r in rows]


def journal_symbols(area_id: int) -> list[str]:
    init()
    with _connect() as c:
        return [r["root"] for r in c.execute(
            "SELECT DISTINCT root FROM journal_trades WHERE area_id=? ORDER BY root", (area_id,)).fetchall()]


def upsert_journal_snapshot(area_id: int, s: dict[str, Any]) -> None:
    init()
    with _connect() as c:
        c.execute(
            "INSERT INTO journal_snapshots(area_id,account_id,account_spec,day,total_cash,realized_pnl,open_pnl,week_realized_pnl,total_pnl,taken_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(area_id,account_id,day) DO UPDATE SET "
            "total_cash=excluded.total_cash, realized_pnl=excluded.realized_pnl, open_pnl=excluded.open_pnl, "
            "week_realized_pnl=excluded.week_realized_pnl, total_pnl=excluded.total_pnl, taken_at=excluded.taken_at",
            (area_id, s["account_id"], s.get("account_spec", ""), s["day"], s.get("total_cash", 0), s.get("realized_pnl", 0),
             s.get("open_pnl", 0), s.get("week_realized_pnl", 0), s.get("total_pnl", 0), _now()))


def list_journal_snapshots(area_id: int, *, days: int = 90, account: str = "") -> list[dict[str, Any]]:
    init()
    since = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
    where, params = "area_id=? AND day>=?", [area_id, since]
    if account:
        where += " AND (account_spec=? OR CAST(account_id AS TEXT)=?)"; params += [account, account]
    with _connect() as c:
        return [dict(r) for r in c.execute(
            f"SELECT * FROM journal_snapshots WHERE {where} ORDER BY day, account_id", params).fetchall()]


def insert_journal_import(area_id: int, rec: dict[str, Any]) -> int:
    init()
    with _connect() as c:
        cur = c.execute(
            "INSERT INTO journal_imports(area_id,ts,trigger,status,by,logins,accounts,fills,fills_new,trades,trades_new,snapshots,duration_ms,error,history_new,detail) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (area_id, rec.get("ts") or _now(), rec.get("trigger", ""), rec.get("status", ""), rec.get("by", ""),
             rec.get("logins", 0), rec.get("accounts", 0), rec.get("fills", 0), rec.get("fills_new", 0),
             rec.get("trades", 0), rec.get("trades_new", 0), rec.get("snapshots", 0), rec.get("duration_ms", 0),
             rec.get("error", ""), rec.get("history_new", 0), rec.get("detail", "")))
        return int(cur.lastrowid or 0)


def journal_unseen(area_id: int, kind: str, refs: list[Any]) -> list[Any]:
    """The subset of ``refs`` not yet marked as processed for ``kind``."""
    init()
    refs = [r for r in refs if r is not None]
    if not refs:
        return []
    out: list[Any] = []
    with _connect() as c:
        for i in range(0, len(refs), 400):
            chunk = refs[i:i + 400]
            marks = ",".join("?" * len(chunk))
            seen = {r["ref"] for r in c.execute(
                f"SELECT ref FROM journal_seen WHERE area_id=? AND kind=? AND ref IN ({marks})",
                (area_id, kind, *[str(x) for x in chunk])).fetchall()}
            out.extend(x for x in chunk if str(x) not in seen)
    return out


def journal_mark_seen(area_id: int, kind: str, refs: list[Any]) -> None:
    init()
    with _connect() as c:
        c.executemany("INSERT OR IGNORE INTO journal_seen(area_id,kind,ref) VALUES(?,?,?)",
                      [(area_id, kind, str(x)) for x in refs if x is not None])


def list_journal_imports(area_id: int, limit: int = 30) -> list[dict[str, Any]]:
    init()
    with _connect() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM journal_imports WHERE area_id=? ORDER BY id DESC LIMIT ?", (area_id, int(limit))).fetchall()]


# ------------------------------------------------------- execution agents
AGENT_PAIRING_TTL_S = 15 * 60
_agents_by_hash: dict[str, dict[str, Any]] = {}  # hot path: every relay poll authenticates


def _agent_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _row_to_agent(r: sqlite3.Row) -> dict[str, Any]:
    return {"id": r["id"], "area_id": r["area_id"], "name": r["name"], "version": r["version"],
            "created_at": r["created_at"], "last_seen_at": r["last_seen_at"], "last_ip": r["last_ip"]}


def create_agent_pairing(area_id: int, name: str = "") -> str:
    """A one-time, short-lived pairing code (8 chars, unambiguous alphabet)."""
    init()
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    code = "".join(secrets.choice(alphabet) for _ in range(8))
    now = datetime.now(timezone.utc)
    with _connect() as c:
        c.execute("DELETE FROM agent_pairings WHERE expires_at<? OR used_at IS NOT NULL", (now.isoformat(),))
        c.execute("INSERT INTO agent_pairings(code,area_id,name,created_at,expires_at) VALUES(?,?,?,?,?)",
                  (code, area_id, name, now.isoformat(), (now + timedelta(seconds=AGENT_PAIRING_TTL_S)).isoformat()))
    return code


def consume_agent_pairing(code: str) -> Optional[dict[str, Any]]:
    init()
    now = datetime.now(timezone.utc).isoformat()
    with _connect() as c:
        r = c.execute("SELECT * FROM agent_pairings WHERE code=? AND used_at IS NULL AND expires_at>?",
                      (code, now)).fetchone()
        if not r:
            return None
        # single use, atomically: a second caller with the same code loses the race
        cur = c.execute("UPDATE agent_pairings SET used_at=? WHERE code=? AND used_at IS NULL AND expires_at>?",
                        (now, code, now))
        if cur.rowcount != 1:
            return None
    return {"area_id": r["area_id"], "name": r["name"]}


def create_agent(area_id: int, name: str, *, version: str = "", ip: str = "") -> tuple[str, dict[str, Any]]:
    """Create an agent; returns (plain token — shown once, agent record)."""
    init()
    token = "fba_" + secrets.token_urlsafe(32)
    with _connect() as c:
        cur = c.execute(
            "INSERT INTO agents(area_id,name,token_hash,version,created_at,last_seen_at,last_ip) VALUES(?,?,?,?,?,?,?)",
            (area_id, name, _agent_hash(token), version, _now(), _now(), ip))
        r = c.execute("SELECT * FROM agents WHERE id=?", (cur.lastrowid,)).fetchone()
    _agents_by_hash.clear()
    return token, _row_to_agent(r)


def get_agent_by_token(token: str) -> Optional[dict[str, Any]]:
    h = _agent_hash(token)
    cached = _agents_by_hash.get(h)
    if cached is not None:
        return dict(cached)
    init()
    with _connect() as c:
        r = c.execute("SELECT * FROM agents WHERE token_hash=?", (h,)).fetchone()
    if not r:
        return None  # misses are never cached: unauthenticated guesses must not grow memory
    agent = _row_to_agent(r)
    if len(_agents_by_hash) > 256:
        _agents_by_hash.clear()
    _agents_by_hash[h] = agent
    return dict(agent)


def get_agent(area_id: int, agent_id: int) -> Optional[dict[str, Any]]:
    init()
    with _connect() as c:
        r = c.execute("SELECT * FROM agents WHERE area_id=? AND id=?", (area_id, agent_id)).fetchone()
    return _row_to_agent(r) if r else None


def list_agents(area_id: int) -> list[dict[str, Any]]:
    init()
    with _connect() as c:
        return [_row_to_agent(r) for r in c.execute(
            "SELECT * FROM agents WHERE area_id=? ORDER BY id", (area_id,)).fetchall()]


_agent_touch_at: dict[int, float] = {}


def touch_agent(agent_id: int, *, ip: str = "", version: str = "") -> None:
    """Record a poll (throttled to one write per 20 s per agent)."""
    import time as _time
    now = _time.monotonic()
    if now - _agent_touch_at.get(agent_id, -1e9) < 20:
        return
    _agent_touch_at[agent_id] = now
    init()
    with _connect() as c:
        if version:
            c.execute("UPDATE agents SET last_seen_at=?, last_ip=?, version=? WHERE id=?", (_now(), ip[:64], version[:40], agent_id))
        else:
            c.execute("UPDATE agents SET last_seen_at=?, last_ip=? WHERE id=?", (_now(), ip[:64], agent_id))


def rename_agent(area_id: int, agent_id: int, name: str) -> bool:
    init()
    with _connect() as c:
        cur = c.execute("UPDATE agents SET name=? WHERE area_id=? AND id=?", (name, area_id, agent_id))
    _agents_by_hash.clear()
    return bool(cur.rowcount)


def delete_agent(area_id: int, agent_id: int) -> bool:
    init()
    with _connect() as c:
        cur = c.execute("DELETE FROM agents WHERE area_id=? AND id=?", (area_id, agent_id))
    _agents_by_hash.clear()
    return bool(cur.rowcount)


# ------------------------------------------------------ push subscriptions
def _row_to_push(r: sqlite3.Row, public: bool = False) -> dict[str, Any]:
    d = {"id": r["id"], "area_id": r["area_id"], "user_id": r["user_id"], "endpoint": r["endpoint"],
         "device": r["device"], "created_at": r["created_at"], "last_used_at": r["last_used_at"],
         "failures": r["failures"], "last_error": r["last_error"]}
    if public:
        # The endpoint is a capability URL (anyone holding it can push to the
        # device); the browser only needs enough to recognise "this device".
        d["endpoint_host"] = r["endpoint"].split("//", 1)[-1].split("/", 1)[0]
        d.pop("endpoint")
    else:
        d["p256dh"] = r["p256dh"]
        d["auth"] = r["auth"]
    return d


def upsert_push_subscription(area_id: int, user_id: int, endpoint: str, p256dh: str, auth: str, *,
                             device: str = "") -> dict[str, Any]:
    """Register (or refresh) a device push subscription. Endpoints are globally unique."""
    init()
    with _connect() as c:
        row = c.execute("SELECT * FROM push_subscriptions WHERE endpoint=?", (endpoint,)).fetchone()
        if row and row["area_id"] != area_id:
            raise ValueError("this push endpoint is registered to another workspace")
        if row:
            c.execute("UPDATE push_subscriptions SET area_id=?, user_id=?, p256dh=?, auth=?, device=?, "
                      "failures=0, last_error='' WHERE id=?",
                      (area_id, user_id, p256dh, auth, device or row["device"], row["id"]))
            sub_id = row["id"]
        else:
            cur = c.execute("INSERT INTO push_subscriptions (area_id, user_id, endpoint, p256dh, auth, device, created_at) "
                            "VALUES (?,?,?,?,?,?,?)", (area_id, user_id, endpoint, p256dh, auth, device, _now()))
            sub_id = cur.lastrowid
        r = c.execute("SELECT * FROM push_subscriptions WHERE id=?", (sub_id,)).fetchone()
    return _row_to_push(r)


def list_push_subscriptions(area_id: int, *, public: bool = False) -> list[dict[str, Any]]:
    init()
    with _connect() as c:
        rows = c.execute("SELECT * FROM push_subscriptions WHERE area_id=? ORDER BY id", (area_id,)).fetchall()
    return [_row_to_push(r, public=public) for r in rows]


def delete_push_subscription(area_id: int, sub_id: int) -> bool:
    init()
    with _connect() as c:
        cur = c.execute("DELETE FROM push_subscriptions WHERE area_id=? AND id=?", (area_id, sub_id))
    return bool(cur.rowcount)


def delete_push_subscription_by_endpoint(area_id: int, endpoint: str) -> bool:
    if not endpoint:
        return False
    init()
    with _connect() as c:
        cur = c.execute("DELETE FROM push_subscriptions WHERE area_id=? AND endpoint=?", (area_id, endpoint))
    return bool(cur.rowcount)


def touch_push_subscription(sub_id: int, *, ok: bool, error: str = "") -> None:
    init()
    with _connect() as c:
        if ok:
            c.execute("UPDATE push_subscriptions SET last_used_at=?, failures=0, last_error='' WHERE id=?", (_now(), sub_id))
        else:
            c.execute("UPDATE push_subscriptions SET failures=failures+1, last_error=? WHERE id=?", (error[:200], sub_id))


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


LOGIN_ACTIONS = ("login_ok", "login_failed", "login_blocked")


def list_audit(limit: int = 100, *, logins: Optional[bool] = None) -> list[dict[str, Any]]:
    """Newest audit rows. ``logins=True`` → only sign-in events, ``False`` →
    everything but sign-ins (the admin-actions view), ``None`` → all."""
    init()
    marks = ",".join("?" * len(LOGIN_ACTIONS))
    where = ""
    params: list[Any] = []
    if logins is True:
        where, params = f"WHERE action IN ({marks})", list(LOGIN_ACTIONS)
    elif logins is False:
        where, params = f"WHERE action NOT IN ({marks})", list(LOGIN_ACTIONS)
    with _connect() as c:
        rows = c.execute(
            f"SELECT * FROM audit_log {where} ORDER BY id DESC LIMIT ?", (*params, int(limit))).fetchall()
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
        # burn the token first, atomically: two racing submits set one password, not two
        cur = c.execute("UPDATE password_resets SET used_at=? WHERE token=? AND used_at IS NULL AND expires_at>?",
                        (_now(), token, _now()))
        if cur.rowcount != 1:
            return None
        c.execute("UPDATE users SET password_hash=? WHERE id=?",
                  (hash_password(new_password), rec["user_id"]))
    _pw_versions.pop(rec["user_id"], None)
    return rec["user_id"]

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
import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


ROOT_DIR = Path(__file__).resolve().parent.parent


DATA_DIR = Path(os.environ.get("NEXUSPRED_DATA_DIR") or (ROOT_DIR / "data"))


DB_FILE = DATA_DIR / "fluxbridge.db"


_init_lock = threading.Lock()


_initialized = False


_areas_generation = 0


def areas_generation() -> int:
    return _areas_generation


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


_user_count: Optional[int] = None


_users: dict[int, dict[str, Any]] = {}


_pw_versions: dict[int, str] = {}  # user id -> fingerprint of the password hash


_primary_area: dict[int, Optional[int]] = {}


_area_ids: Optional[tuple[int, list[int]]] = None  # (areas_generation, ids)


def reset_caches() -> None:
    _features_cache.clear()
    global _user_count, _area_ids
    _user_count = None
    _area_ids = None
    _users.clear()
    _primary_area.clear()
    _pw_versions.clear()
    _agents_by_hash.clear()
    _agent_touch_at.clear()
    _active_subs.clear()
    _sub_counts.clear()


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
                CREATE INDEX IF NOT EXISTS ix_order_log_area_ts ON order_log(area_id, ts);
                CREATE INDEX IF NOT EXISTS ix_signal_log_area_ts ON signal_log(area_id, ts);
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
            if "totp_secret" not in user_cols:
                # two-factor authentication (app/mfa.py): encrypted secret, enrolment
                # state, replay counter, salt of the backup-code hashes
                c.execute("ALTER TABLE users ADD COLUMN totp_secret TEXT NOT NULL DEFAULT ''")
                c.execute("ALTER TABLE users ADD COLUMN totp_enabled INTEGER NOT NULL DEFAULT 0")
                c.execute("ALTER TABLE users ADD COLUMN totp_required INTEGER NOT NULL DEFAULT 0")
                c.execute("ALTER TABLE users ADD COLUMN totp_counter INTEGER NOT NULL DEFAULT -1")
                c.execute("ALTER TABLE users ADD COLUMN backup_salt TEXT NOT NULL DEFAULT ''")
            c.execute("""CREATE TABLE IF NOT EXISTS mfa_backup_codes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    code_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )""")
            c.execute("CREATE INDEX IF NOT EXISTS idx_mfa_codes_user ON mfa_backup_codes(user_id)")
            sig_cols = {r["name"] for r in c.execute("PRAGMA table_info(signal_log)").fetchall()}
            if "webhook_id" not in sig_cols:
                # alpha.77: per-webhook signal statistics (track record, subscription journal)
                c.execute("ALTER TABLE signal_log ADD COLUMN webhook_id TEXT NOT NULL DEFAULT ''")
            if "latency_ms" not in sig_cols:
                # alpha.78: wall time from acceptance to the broker's answer (latency fairness)
                c.execute("ALTER TABLE signal_log ADD COLUMN latency_ms INTEGER")
            sub_cols = {r["name"] for r in c.execute("PRAGMA table_info(subscriptions)").fetchall()}
            if "status" not in sub_cols:
                # alpha.78: publisher controls (approval, pause) and subscriber controls
                c.execute("ALTER TABLE subscriptions ADD COLUMN status TEXT NOT NULL DEFAULT 'active'")
                c.execute("ALTER TABLE subscriptions ADD COLUMN controls TEXT NOT NULL DEFAULT '{}'")
            for stmt in ("CREATE INDEX IF NOT EXISTS ix_journal_imports_area ON journal_imports(area_id, id)",
                         "CREATE INDEX IF NOT EXISTS ix_signal_log_wh ON signal_log(area_id, webhook_id, id)",
                         "CREATE INDEX IF NOT EXISTS ix_copy_events_ts ON copy_events(ts)",
                         "CREATE INDEX IF NOT EXISTS ix_audit_action ON audit_log(action, id)",
                         "CREATE INDEX IF NOT EXISTS ix_push_subs_area ON push_subscriptions(area_id)",
                         "CREATE INDEX IF NOT EXISTS ix_agents_area ON agents(area_id)",
                         "CREATE INDEX IF NOT EXISTS ix_subscriptions_pub ON subscriptions(publisher_area_id, webhook_id)"):
                c.execute(stmt)
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


def user_count() -> int:
    global _user_count
    if _user_count is None:
        init()
        with _connect() as c:
            _user_count = c.execute("SELECT COUNT(*) n FROM users").fetchone()["n"]
    return _user_count


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


_active_subs: dict[tuple[int, str], list[dict[str, Any]]] = {}


_sub_counts: dict[int, dict[str, int]] = {}  # publisher area → {webhook_id: count}


_features_cache: dict[int, dict[str, bool]] = {}     # area → effective flags (features change only via set_area_feature)


_agents_by_hash: dict[str, dict[str, Any]] = {}  # hot path: every relay poll authenticates


_agent_touch_at: dict[int, float] = {}


def _bump_areas_generation() -> None:
    global _areas_generation
    _areas_generation += 1


def set_db_file(path: Any) -> None:
    """Point the persistence layer at another SQLite file (tests, the CLI's
    restore). The facade attribute ``app.db.DB_FILE`` follows so the backup
    endpoint sees the same path."""
    global DB_FILE
    import sys
    from pathlib import Path as _P
    DB_FILE = _P(path)
    pkg = sys.modules.get("app.db")
    if pkg is not None:
        pkg.DB_FILE = DB_FILE


def mark_uninitialized() -> None:
    """Tests: force the next ``init()`` to (re)create the schema on a fresh file."""
    global _initialized
    _initialized = False

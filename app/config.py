"""Settings management.

Settings are persisted to ``data/settings.json`` so they survive restarts and are
editable from the dashboard. Sensitive credentials never leave the local machine.
"""
from __future__ import annotations

import copy
import json
import logging
import os
import pickle
import secrets
import threading
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger(__name__)

# Repository root (one level up from the ``app`` package).
ROOT_DIR = Path(__file__).resolve().parent.parent
# Where runtime settings live. On hosts with an ephemeral filesystem (e.g. Render)
# set NEXUSPRED_DATA_DIR to a mounted persistent disk so settings/tokens survive
# restarts and deploys.
DATA_DIR = Path(os.environ.get("NEXUSPRED_DATA_DIR") or (ROOT_DIR / "data"))
SETTINGS_FILE = DATA_DIR / "settings.json"
VERSION_FILE = ROOT_DIR / "VERSION"

# GitHub repo used by the auto-updater.
GITHUB_OWNER = "tobiasgiger"
GITHUB_REPO = "nexuspred"
GITHUB_BRANCH = os.environ.get("NEXUSPRED_BRANCH", "main")
# Canonical public origin (e.g. "https://bridge.example.com"). When set, the
# dashboard shows webhook URLs on this host whichever hostname it was opened on,
# and emailed invite / reset links are built on it instead of the request's
# Host header. Trailing slashes are ignored.
PUBLIC_URL = (os.environ.get("NEXUSPRED_PUBLIC_URL") or "").strip().rstrip("/")

DEFAULT_SETTINGS: dict[str, Any] = {
    # --- Tradovate connection -------------------------------------------------
    # --- Tradovate connection -------------------------------------------------
    # Token-only, multi-account. Each entry is one Tradovate login with its OWN
    # access token, renewed via /auth/renewaccesstoken (access token, then check
    # token). No username/password. A single login can expose several trade
    # accounts; each is independently toggled for execution. Every signal is sent
    # to all enabled trade accounts in parallel. Each entry:
    #   {"name": str, "environment": "demo"|"live", "access_token": str,
    #    "md_token": str, "enabled": bool, "qty_multiplier": float,
    #    "account_spec": str, "account_id": int, "token_expires": str,
    #    "accounts": [{"spec": str, "id": int, "enabled": bool,
    #                  "qty_multiplier": float}]}  # discovered on Connect & Verify
    "token_accounts": [],

    # --- Trading behaviour ----------------------------------------------------
    "trading_enabled": False,        # master kill switch (safety: off by default)
    "default_qty": 3,                # contracts for the initial market entry
    "tp_qty": 1,                     # contracts per take-profit limit order
    "entry_order_type": "Market",    # initial buy/sell are market orders
    "tp_order_type": "Limit",        # take-profits are resting limit orders
    "sl_order_type": "Stop",         # stop-loss as a protective stop order
    # On a break-even move_sl (TP1 / "breakeven" message), set the stop to the
    # original entry price instead of the signal's new_sl. Trailing move_sl
    # updates still use the signal's new_sl.
    "breakeven_to_entry": True,

    # Current symbol mapping: TradingView symbol -> exact Tradovate contract.
    # Use the dated contract (e.g. "MNQU6") and update it after each rollover.
    # A bare root (e.g. "MNQ") still works — the bridge auto-picks the front month.
    "symbol_map": {
        "NQ1!": "NQU6",
        "MNQ1!": "MNQU6",
        "ES1!": "ESU6",
        "MES1!": "MESU6",
        "GC1!": "GCM6",
        "MGC1!": "MGCM6",
    },
    "allowed_symbols": ["NQ", "MNQ", "ES", "MES", "GC", "MGC"],

    # --- Webhooks ---------------------------------------------------------------
    # Each strategy gets its own webhook (URL token, strategy type, and which
    # trade accounts it routes to). Entry:
    #   {"id": str, "name": str, "token": str, "enabled": bool,
    #    "strategy": "simple" | "bracket" | "ts_hunter", "default_qty": int, "tp_qty": int,
    #    "accounts": [{"token_idx": int, "spec": str, "enabled": bool,
    #                  "qty_multiplier": float}]}
    # token_idx/spec address a trade account exposed by token_accounts above.
    "webhooks": [],
    # One-shot flag: on first startup after upgrading, the legacy single
    # webhook_secret + every currently-enabled trade account are folded into a
    # "Default" webhook so existing TradingView alerts keep working unchanged.
    "webhooks_migrated": False,

    # --- Webhook security -----------------------------------------------------
    # Access to the dashboard is handled by the user/login system (see app.db /
    # app.auth), not per-area settings. These two are trading-related only.
    "webhook_secret": "change-me",   # legacy single-webhook token (migrated)
    "webhook_passphrase": "",        # optional passphrase checked in JSON body

    # --- Alerts -----------------------------------------------------------------
    # Two channels (each independently toggled) and three triggers (each with
    # its own on/off switch). Trade-executed is Discord-only by design; the
    # other two also go to email.
    "alert_discord_enabled": False,
    "alert_discord_webhook_url": "",       # Discord "Webhook URL" from channel settings
    "alert_discord_mention_everyone": True,  # prefix messages with @everyone
    "alert_email_enabled": False,
    # Empty by default; the app fills this with the area owner's own email (see
    # db.create_user / db.backfill_alert_emails and the /api/settings fallback),
    # unless the user has set a different address.
    "alert_email_to": "",
    "alert_smtp_host": "smtp.gmail.com",
    "alert_smtp_port": 587,
    "alert_smtp_username": "",             # e.g. your Gmail address
    "alert_smtp_password": "",             # Gmail: use an App Password, not your login password
    # Web Push to the installed dashboard app (per-device subscriptions live in
    # the push_subscriptions table; this is the area-wide master switch).
    "alert_push_enabled": True,
    "alert_on_connection_lost": True,
    "alert_on_connection_restored": True,
    "alert_on_trade_executed": True,
    # Position watcher (broker-side view, catches stop / target fills and manual
    # trades too): opened / added, closed / reduced with the realised P&L.
    "alert_on_trade_opened": True,
    # Trade-account specs that may trigger account-level alerts (position
    # opened / closed, signal executed, daily summary). Empty = every account.
    "alert_accounts": [],
    # Trailing-drawdown tracker state per trade account (peaks, EOD candidates,
    # user-pinned thresholds) — maintained by app.drawdown, not user-editable.
    "dd_state": {},
    "alert_on_trade_closed": True,
    # Execution agents (VPS helpers) going offline / back online.
    "alert_on_agent_lost": True,
    "alert_on_agent_restored": True,
    # Copy trading: rejected mirror orders and groups paused after a feed loss.
    "alert_on_copy": True,
    # Per-account risk guard (daily loss / profit limit, flatten time) fired.
    "alert_on_risk": True,
    # Risk-guard locks per trade account (spec → {day, kind, reason, pnl, at}); app.risk.
    "risk_state": {},
    # Economic-calendar lock (app.news): no new entries around high-impact releases.
    "news_lock": {"enabled": False, "currencies": ["USD"], "impacts": ["High"], "before": 5, "after": 5,
                  "action": "block", "manual": [], "alert": True},
    # Copy-trading groups (leader → followers); managed by /api/copy.
    "copy_groups": [],
    # One summary per day (local time in journal_timezone) with realised P&L.
    "alert_daily_summary": True,
    "daily_summary_time": "22:05",
    # Discord listener health (self-bot Gateway connection).
    "alert_on_discord_lost": True,
    "alert_on_discord_restored": True,
    # Webhook → Tradovate delivery failures (a signal arrived but execution failed).
    "alert_on_webhook_failed": True,
    # A dated contract in symbol_map is close to (or past) its roll date.
    "alert_on_rollover": True,
    "rollover_warn_days": 10,          # days before the estimated roll date to warn
    "rollover_notified": {},           # internal: contract -> stage already alerted
    # Seconds the Discord listener may be "wanted but not connected" before it
    # counts as an outage (avoids alerting on the library's transient reconnects).
    "discord_health_grace": 90,

    # --- Discord signal listener (self-bot module) ----------------------------
    # Watches Discord channels via the Gateway using a personal USER token
    # (self-bot) and fans parsed signals out to per-channel webhook targets.
    # Read live on every incoming event, so changes apply without a restart.
    "discord_enabled": False,          # master switch for the listener
    "discord_user_token": "",          # personal Discord user token (SECRET)
    "discord_dry_run": False,          # parse+display but send to NO webhook
    # Each channel: {"id": "<channel_id>", "label": str, "enabled": bool,
    #   "targets": [{"label": str, "url": str, "secret": str, "enabled": bool}]}
    # A channel may fan out to several targets; each target is toggled and may
    # carry a secret sent as the X-Webhook-Secret header.
    "discord_channels": [],

    # --- Live P&L (Overview) --------------------------------------------------
    # Seconds between cash-balance snapshots while a dashboard is open (0 = off).
    # Idle areas are polled once a minute regardless.
    "pnl_poll_seconds": 5,

    # --- Trading journal ------------------------------------------------------
    "journal_auto_import": True,        # import fills/trades from Tradovate once a day
    "journal_import_time": "23:30",     # local time (journal_timezone) — after the CME close
    "journal_timezone": "Europe/Zurich",  # for the daily schedule and day/week/month buckets
    "journal_last_import": "",          # internal: ISO timestamp of the last import run
    "journal_history_days": 365,        # how far back the first history import reaches
    "journal_fee_per_side": 0.0,        # $ per contract per side for report/CSV trades (exports carry no fees)
    "journal_report_cursor": {},        # internal: per account, last day covered by the Performance report
    "journal_report_window": 30,        # internal: days per report request the service accepts (auto-tuned)

    # --- Auto-updater ---------------------------------------------------------
    "auto_check_updates": True,
    "ui_language": "auto",           # dashboard language: auto (browser), de, en

    # --- Connection health ----------------------------------------------------
    # How often (seconds) the bridge verifies the Tradovate session is alive and
    # renews the access token before it expires. 0 disables the background check.
    "health_check_interval": 60,
}

LEGACY_SETTINGS_FILE = DATA_DIR / "settings.json"

# Per-area settings cache. Reentrant lock: save_settings() calls load_settings().
_lock = threading.RLock()
_cache: dict[int, dict[str, Any]] = {}
_snapshots: dict[int, bytes] = {}    # pickled copy of _cache[aid]: unpickling is ~6x faster than copy.deepcopy (see docs/PERFORMANCE.md)
_degraded: set[int] = set()          # areas whose last database read failed: reads fall back to defaults, writes are refused
_webhooks_generation = 0             # bumped only when an area's webhook list changes (find_webhook's index key)


class SettingsUnavailable(RuntimeError):
    """The area's settings could not be read from the database; nothing is
    written on top of defaults — that would wipe the area's configuration."""
# Bumped on every write / invalidation; derived caches (the webhook-token index)
# compare against it instead of re-reading the DB.
_generation = 0
_webhook_index: tuple[tuple[int, int], dict[str, tuple[int, dict[str, Any]]]] | None = None


_version: str | None = None


def _set_cache(aid: int, settings: dict[str, Any]) -> None:
    """Store an area's settings in the cache (lock held by caller). The pickled
    snapshot is built lazily by :func:`_copy_of` so writes stay cheap."""
    _cache[aid] = settings
    _snapshots.pop(aid, None)


def _copy_of(aid: int) -> dict[str, Any]:
    """A private deep copy of the cached settings (lock held by caller).

    Settings are plain JSON data, so a pickle round trip is an exact deep copy
    and runs in C; ``copy.deepcopy`` walks the tree in Python and dominated the
    per-signal cost (3–4 reads per webhook signal)."""
    blob = _snapshots.get(aid)
    if blob is None:
        blob = _snapshots[aid] = pickle.dumps(_cache[aid], protocol=pickle.HIGHEST_PROTOCOL)
    return pickle.loads(blob)  # noqa: S301 - our own bytes, never external input


def get_version(force: bool = False) -> str:
    """The VERSION file, read once (``force=True`` after a self-update)."""
    global _version
    if _version is None or force:
        try:
            _version = VERSION_FILE.read_text(encoding="utf-8").strip()
        except OSError:
            _version = "0.0.0"
    return _version


def legacy_settings_file() -> dict[str, Any] | None:
    """Read the pre-multi-tenant ``data/settings.json`` if present, so its config
    can seed the first user's area on migration. Returns None if absent/invalid."""
    try:
        if LEGACY_SETTINGS_FILE.exists():
            return json.loads(LEGACY_SETTINGS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        pass
    return None


def _resolve_area(area_id: int | None) -> int:
    from . import context
    return area_id if area_id is not None else context.get_area()


def _new_lid() -> str:
    import secrets as _secrets
    return "lg_" + _secrets.token_urlsafe(6)


def ensure_login_ids(s: dict[str, Any]) -> bool:
    """Give every login a stable id (``lid``). Returns True when one was added."""
    changed = False
    for t in s.get("token_accounts") or []:
        if isinstance(t, dict) and not t.get("lid"):
            t["lid"] = _new_lid()
            changed = True
    return changed


def login_index(s: dict[str, Any], lid: str | None) -> int | None:
    """Position of a login in ``token_accounts`` by its id, or None."""
    if not lid:
        return None
    for i, t in enumerate(s.get("token_accounts") or []):
        if isinstance(t, dict) and t.get("lid") == lid:
            return i
    return None


def _stamp(entry: dict[str, Any], s: dict[str, Any], lids: list[str | None]) -> bool:
    """Make one route entry index-independent: learn the ``lid`` from its
    ``token_idx`` once, afterwards keep ``token_idx`` in line with the ``lid``
    (logins may be reordered or deleted). Returns True when it changed."""
    if not isinstance(entry, dict):
        return False
    changed = False
    lid = entry.get("lid")
    if lid:
        idx = login_index(s, lid)
        if idx is not None and entry.get("token_idx") != idx:
            entry["token_idx"] = idx
            changed = True
        return changed
    try:
        idx = int(entry.get("token_idx"))
    except (TypeError, ValueError):
        return False
    if 0 <= idx < len(lids) and lids[idx]:
        entry["lid"] = lids[idx]
        changed = True
    return changed


def stamp_routes(s: dict[str, Any]) -> bool:
    """Apply :func:`_stamp` to every account route (webhooks, copy groups)."""
    lids = [t.get("lid") if isinstance(t, dict) else None for t in (s.get("token_accounts") or [])]
    changed = False
    for wh in s.get("webhooks") or []:
        for a in (wh.get("accounts") or []) if isinstance(wh, dict) else []:
            changed |= _stamp(a, s, lids)
    for g in s.get("copy_groups") or []:
        if not isinstance(g, dict):
            continue
        if isinstance(g.get("leader"), dict):
            changed |= _stamp(g["leader"], s, lids)
        for f in g.get("followers") or []:
            changed |= _stamp(f, s, lids)
    return changed


def load_settings(area_id: int | None = None, force: bool = False) -> dict[str, Any]:
    """Return an area's settings, merged over defaults (defaults to the current
    context area). Cached per area.

    Callers always get a private **deep** copy: v4 handed out shallow copies, so
    a router appending to a fresh area's ``webhooks`` list silently mutated
    ``DEFAULT_SETTINGS`` for every other area. Mutate freely, then persist via
    :func:`save_settings` / :func:`update`."""
    from . import db

    aid = _resolve_area(area_id)
    with _lock:
        if not force and aid in _cache:
            return _copy_of(aid)
        merged = copy.deepcopy(DEFAULT_SETTINGS)
        try:
            merged.update(db.get_area_settings(aid) or {})
        except Exception as exc:  # noqa: BLE001 - a DB hiccup shouldn't crash a read
            # defaults for this read only: not cached (the next read retries) and
            # never written back (see _persist)
            if aid not in _degraded:
                log.error("settings of area %s could not be read (%s); serving defaults, writes refused until the read succeeds", aid, exc)
            _degraded.add(aid)
            return merged
        _degraded.discard(aid)
        try:
            if ensure_login_ids(merged) | stamp_routes(merged):
                _persist(aid, merged)       # self-healing: ids and indices stay consistent
        except Exception as exc:  # noqa: BLE001 - never let the migration break a read
            log.error("settings migration for area %s failed: %s", aid, exc)
        _set_cache(aid, merged)
        return _copy_of(aid)


def setting(key: str, area_id: int | None = None) -> Any:
    """A private copy of one settings key (defaults applied). Hot paths that
    need a single small key — the risk guard's ``risk_state`` before every
    order, the news lock's ``news_lock`` — use this instead of copying the
    whole area's settings."""
    aid = _resolve_area(area_id)
    with _lock:
        if aid in _cache:
            return copy.deepcopy(_cache[aid].get(key))
    return load_settings(area_id=aid).get(key)


def _load_for_write(aid: int) -> dict[str, Any]:
    """The settings a write starts from: the cache when it is current (every
    write goes through _persist, which refreshes it), else a fresh read. Lock
    held by caller. Raises :class:`SettingsUnavailable` instead of handing out
    defaults that a save would write over the real configuration."""
    if aid in _cache and aid not in _degraded:
        return _copy_of(aid)
    current = load_settings(area_id=aid, force=True)
    if aid in _degraded:
        raise SettingsUnavailable(f"settings of area {aid} could not be read — nothing saved")
    return current


def _persist(aid: int, current: dict[str, Any]) -> None:
    """Write an area's full settings dict and refresh the cache. Lock held by caller."""
    global _generation, _webhooks_generation
    from . import db

    if aid in _degraded:
        raise SettingsUnavailable(f"settings of area {aid} could not be read — nothing saved")
    try:
        ensure_login_ids(current)
        stamp_routes(current)           # every write leaves ids and route indices consistent
    except Exception as exc:  # noqa: BLE001
        log.error("route stamping for area %s failed: %s", aid, exc)
    db.save_area_settings(aid, current)
    before = _cache.get(aid)
    if before is None or before.get("webhooks") != current.get("webhooks"):
        _webhooks_generation += 1       # the token index is rebuilt only when a webhook list changed
    _set_cache(aid, current)
    _generation += 1


def save_settings(updates: dict[str, Any], area_id: int | None = None) -> dict[str, Any]:
    """Merge ``updates`` into an area's settings and persist them (to SQLite).
    Unknown keys are dropped (the settings schema is ``DEFAULT_SETTINGS``)."""
    aid = _resolve_area(area_id)
    with _lock:
        current = _load_for_write(aid)
        for key, value in updates.items():
            if key in DEFAULT_SETTINGS:  # ignore unknown keys
                current[key] = value
        _persist(aid, current)
        return copy.deepcopy(current)


def update(mutator: Callable[[dict[str, Any]], Any], area_id: int | None = None) -> dict[str, Any]:
    """Atomic read-modify-write: ``mutator`` receives the area's settings (a
    private copy), edits them in place, and the result is persisted under the
    settings lock — so two concurrent edits can never clobber each other."""
    aid = _resolve_area(area_id)
    with _lock:
        current = _load_for_write(aid)
        mutator(current)
        current = {k: v for k, v in current.items() if k in DEFAULT_SETTINGS}
        _persist(aid, current)
        return copy.deepcopy(current)


def invalidate(area_id: int | None = None) -> None:
    """Drop an area's cached settings (or all) so the next read re-loads from DB."""
    global _generation, _webhooks_generation
    with _lock:
        if area_id is None:
            _cache.clear()
            _snapshots.clear()
        else:
            _cache.pop(area_id, None)
            _snapshots.pop(area_id, None)
        _generation += 1
        _webhooks_generation += 1


def find_webhook(token: str) -> tuple[int | None, dict[str, Any] | None]:
    """Which area owns a webhook token → ``(area_id, webhook)`` or ``(None, None)``.

    v4 scanned every area's settings in SQLite on each TradingView POST. v5 keeps
    an in-memory ``token → (area, webhook)`` index that is rebuilt only when a
    settings write or an area create/delete has happened since (generation
    counters), so the hot path is a dict lookup. First matching area wins, in
    area-id order — same as the v4 scan."""
    global _webhook_index
    from . import db

    if not token:
        return None, None
    with _lock:
        key = (_webhooks_generation, db.areas_generation())
        idx = _webhook_index
        if idx is None or idx[0] != key:
            table: dict[str, tuple[int, dict[str, Any]]] = {}
            for aid in db.all_area_ids():
                for wh in load_settings(area_id=aid).get("webhooks") or []:
                    t = wh.get("token")
                    if t and t not in table:
                        table[t] = (aid, wh)
            _webhook_index = idx = (key, table)
        hit = idx[1].get(token)
    return (hit[0], copy.deepcopy(hit[1])) if hit else (None, None)


def migrate_legacy_webhook(area_id: int | None = None) -> None:
    """One-shot migration: fold the legacy single webhook_secret + every currently
    enabled trade account into a "Default" webhook, so existing TradingView alerts
    keep working unchanged after upgrading to per-strategy webhooks.

    Runs once per area (guarded by ``webhooks_migrated``). Safe on a fresh area —
    it just creates an empty "Default" webhook to edit.
    """
    aid = _resolve_area(area_id)
    with _lock:
        s = load_settings(area_id=aid, force=True)
        if s.get("webhooks_migrated"):
            return
        accounts: list[dict[str, Any]] = []
        for idx, t in enumerate(s.get("token_accounts") or []):
            for a in (t.get("accounts") or []):
                if a.get("enabled", True):
                    accounts.append({
                        "token_idx": idx,
                        "spec": a.get("spec") or a.get("account_spec") or "",
                        "enabled": True,
                        "qty_multiplier": float(a.get("qty_multiplier", 1) or 1),
                    })
        # Reuse a real legacy webhook_secret only if it was customised; otherwise
        # every fresh area would collide on the default "change-me" token.
        legacy_secret = s.get("webhook_secret")
        token = legacy_secret if (legacy_secret and legacy_secret != "change-me") \
            else secrets.token_urlsafe(16)
        default_webhook = {
            "id": f"wh_{secrets.token_hex(4)}",
            "name": "Default",
            "token": token,
            "enabled": True,
            "strategy": "bracket",
            "default_qty": s.get("default_qty", 3),
            "tp_qty": s.get("tp_qty", 1),
            "accounts": accounts,
        }
        webhooks = list(s.get("webhooks") or [])
        webhooks.append(default_webhook)
        save_settings({"webhooks": webhooks, "webhooks_migrated": True}, area_id=aid)


# Valid webhook strategy types:
#   simple    -> plain buy/sell for the payload's qty, no TP/SL
#   bracket   -> entry + tp1/tp2/tp3/sl bracket (see signals.py)
#   ts_hunter -> TS-Hunter contract: event "signal" opens a market entry sized
#                from risk.value with a protective stop at sl.value; event
#                "management" (action partial_close_percent / full_close)
#                manages it, correlated by trade_id (see signals.py)
STRATEGIES = ("simple", "bracket", "ts_hunter")


def new_webhook(
    name: str = "New Webhook", strategy: str = "simple",
    default_qty: int = 1, tp_qty: int = 1,
) -> dict[str, Any]:
    """Build a fresh webhook dict with a generated id + secret token."""
    return {
        "id": f"wh_{secrets.token_hex(4)}",
        "name": name,
        "token": secrets.token_urlsafe(16),
        "enabled": True,
        "strategy": strategy if strategy in STRATEGIES else "simple",
        "default_qty": max(1, int(default_qty or 1)),
        "tp_qty": max(1, int(tp_qty or 1)),
        "accounts": [],
    }


# Keys the generic ``POST /api/settings`` may write. Everything else has a
# dedicated, validating endpoint (webhooks, token accounts, Discord listener)
# or is internal (migration flags, the legacy webhook secret).
SETTINGS_PROTECTED_KEYS = frozenset({
    "token_accounts", "webhooks", "webhooks_migrated", "webhook_secret",
    "discord_enabled", "discord_user_token", "discord_dry_run", "discord_channels",
    "rollover_notified", "journal_last_import", "journal_report_cursor", "journal_report_window",
    "dd_state", "copy_groups", "risk_state", "news_lock",
})

# Fields that must never be returned to the browser in plain text.
SECRET_FIELDS = {
    "webhook_passphrase",
    "alert_discord_webhook_url", "alert_smtp_password",
    "discord_user_token",
}

# Per-entry secret fields inside the token_accounts list.
_TOKEN_SECRETS = ("access_token", "md_token", "rithmic_password", "px_api_key")


def public_settings(area_id: int | None = None) -> dict[str, Any]:
    """Settings safe to send to the dashboard (secrets masked)."""
    s = load_settings(area_id=area_id)
    out = dict(s)
    for field in SECRET_FIELDS:
        out[field] = "********" if out.get(field) else ""
    # Mask the tokens inside each token-account entry.
    out["token_accounts"] = [
        {**a, **{f: ("********" if a.get(f) else "") for f in _TOKEN_SECRETS}}
        for a in (s.get("token_accounts") or [])
    ]
    # Mask the per-target secrets inside each Discord channel entry.
    out["discord_channels"] = [
        {
            **c,
            "targets": [
                {**t, "secret": ("********" if t.get("secret") else "")}
                for t in (c.get("targets") or [])
            ],
        }
        for c in (s.get("discord_channels") or [])
    ]
    return out


def update_token_account(idx: int, area_id: int | None = None, *, lid: str | None = None, **fields: Any) -> None:
    """Persist fields (e.g. a renewed token) into one login of an area — found by
    its stable ``lid`` when given, else by position. Best-effort, thread-safe
    read-modify-write so concurrent renewals don't clobber."""
    aid = _resolve_area(area_id)
    with _lock:
        current = load_settings(area_id=aid, force=True)
        accounts = list(current.get("token_accounts") or [])
        by_lid = login_index(current, lid)
        if by_lid is not None:
            idx = by_lid
        elif lid and any(a.get("lid") for a in accounts):
            return                          # this login was deleted meanwhile: never write into another one
        if 0 <= idx < len(accounts):
            accounts[idx] = {**accounts[idx], **fields}
            current["token_accounts"] = accounts
            _persist(aid, current)


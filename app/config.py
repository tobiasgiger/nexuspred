"""Settings management.

Settings are persisted to ``data/settings.json`` so they survive restarts and are
editable from the dashboard. Sensitive credentials never leave the local machine.
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import threading
from pathlib import Path
from typing import Any

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
    "webhook_secret": "change-me",   # required in the webhook URL path
    "webhook_passphrase": "",        # optional passphrase checked in JSON body
    # Protect the dashboard + API with HTTP Basic auth when hosted publicly.
    # The DASHBOARD_PASSWORD env var overrides this (use it for the first deploy).
    # The /webhook/<secret> endpoint is never behind this (TradingView can't auth).
    # NOTE: this password is only a FALLBACK — when Google login is configured
    # (client id/secret + at least one allowed email) it takes over entirely.
    "dashboard_password": "",

    # --- Access / login: Sign in with Google (OAuth) ------------------------
    # When a client id + secret AND at least one allowed email are set, the
    # dashboard requires "Sign in with Google" and only these emails get in
    # (the password above is then ignored). Env overrides: GOOGLE_CLIENT_ID,
    # GOOGLE_CLIENT_SECRET, GOOGLE_ALLOWED_EMAILS (comma-separated), PUBLIC_URL.
    "google_oauth_enabled": False,
    "google_client_id": "",
    "google_client_secret": "",
    "google_allowed_emails": [],     # e.g. ["you@gmail.com"]
    # Public base URL of this deploy (e.g. https://app.onrender.com). Used to
    # build the OAuth redirect URI when behind a proxy; auto-derived if blank.
    "public_url": "",
    # Auto-generated signing key for the login session cookie (never shown).
    "session_secret": "",

    # --- Alerts -----------------------------------------------------------------
    # Two channels (each independently toggled) and three triggers (each with
    # its own on/off switch). Trade-executed is Discord-only by design; the
    # other two also go to email.
    "alert_discord_enabled": False,
    "alert_discord_webhook_url": "",       # Discord "Webhook URL" from channel settings
    "alert_discord_mention_everyone": True,  # prefix messages with @everyone
    "alert_email_enabled": False,
    "alert_email_to": "speckbrigade@gmail.com",
    "alert_smtp_host": "smtp.gmail.com",
    "alert_smtp_port": 587,
    "alert_smtp_username": "",             # e.g. your Gmail address
    "alert_smtp_password": "",             # Gmail: use an App Password, not your login password
    "alert_on_connection_lost": True,
    "alert_on_connection_restored": True,
    "alert_on_trade_executed": True,

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

    # --- Auto-updater ---------------------------------------------------------
    "auto_check_updates": True,

    # --- Connection health ----------------------------------------------------
    # How often (seconds) the bridge verifies the Tradovate session is alive and
    # renews the access token before it expires. 0 disables the background check.
    "health_check_interval": 60,
}

# Reentrant: save_settings() holds the lock while calling load_settings().
_lock = threading.RLock()
_cache: dict[str, Any] | None = None


def get_version() -> str:
    try:
        return VERSION_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return "0.0.0"


def _ensure_data_dir() -> None:
    """Create the data dir; if it isn't writable, fall back to a local dir.

    On hosts like Render, NEXUSPRED_DATA_DIR must point at a *mounted* persistent
    disk. If the path can't be created (e.g. the disk wasn't attached), we fall
    back to ``<repo>/data`` so the app keeps working — but that location is
    ephemeral, so settings won't survive a redeploy until the disk is fixed.
    """
    global DATA_DIR, SETTINGS_FILE
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        return
    except OSError as exc:
        fallback = ROOT_DIR / "data"
        if DATA_DIR != fallback:
            logging.getLogger("nexuspred").warning(
                "Data dir %s is not writable (%s). Falling back to %s — settings "
                "will NOT persist across redeploys. Attach a persistent disk at %s.",
                DATA_DIR, exc, fallback, DATA_DIR,
            )
            DATA_DIR = fallback
            SETTINGS_FILE = DATA_DIR / "settings.json"
            DATA_DIR.mkdir(parents=True, exist_ok=True)
        else:
            raise


def load_settings(force: bool = False) -> dict[str, Any]:
    """Return the current settings, merged over defaults."""
    global _cache
    with _lock:
        if _cache is not None and not force:
            return dict(_cache)
        merged = dict(DEFAULT_SETTINGS)
        if SETTINGS_FILE.exists():
            try:
                stored = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
                merged.update(stored)
            except (OSError, json.JSONDecodeError):
                pass
        _cache = merged
        return dict(merged)


def save_settings(updates: dict[str, Any]) -> dict[str, Any]:
    """Merge ``updates`` into the stored settings and persist them."""
    global _cache
    with _lock:
        _ensure_data_dir()
        current = load_settings(force=True)
        # Only accept keys we know about to avoid junk creeping in.
        for key, value in updates.items():
            if key in DEFAULT_SETTINGS:
                current[key] = value
        SETTINGS_FILE.write_text(json.dumps(current, indent=2), encoding="utf-8")
        _cache = current
        return dict(current)


def migrate_legacy_webhook() -> None:
    """One-shot migration: fold the legacy single webhook_secret + every currently
    enabled trade account into a "Default" webhook, so existing TradingView alerts
    keep working unchanged after upgrading to per-strategy webhooks.

    Runs once (guarded by ``webhooks_migrated``) on startup. Safe on a fresh
    install too — it just creates an empty "Default" webhook to edit.
    """
    with _lock:
        s = load_settings(force=True)
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
        default_webhook = {
            "id": f"wh_{secrets.token_hex(4)}",
            "name": "Default",
            "token": s.get("webhook_secret") or secrets.token_urlsafe(16),
            "enabled": True,
            "strategy": "bracket",
            "default_qty": s.get("default_qty", 3),
            "tp_qty": s.get("tp_qty", 1),
            "accounts": accounts,
        }
        webhooks = list(s.get("webhooks") or [])
        webhooks.append(default_webhook)
        save_settings({"webhooks": webhooks, "webhooks_migrated": True})


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


# Fields that must never be returned to the browser in plain text.
SECRET_FIELDS = {
    "webhook_passphrase", "dashboard_password",
    "alert_discord_webhook_url", "alert_smtp_password",
    "discord_user_token",
    "google_client_secret", "session_secret",
}

# Per-entry secret fields inside the token_accounts list.
_TOKEN_SECRETS = ("access_token", "md_token")


def public_settings() -> dict[str, Any]:
    """Settings safe to send to the dashboard (secrets masked)."""
    s = load_settings()
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


def update_token_account(idx: int, **fields: Any) -> None:
    """Persist fields (e.g. a renewed token) into token_accounts[idx]. Best-effort,
    thread-safe read-modify-write so concurrent session renewals don't clobber."""
    with _lock:
        current = load_settings(force=True)
        accounts = list(current.get("token_accounts") or [])
        if 0 <= idx < len(accounts):
            accounts[idx] = {**accounts[idx], **fields}
            current["token_accounts"] = accounts
            _ensure_data_dir()
            SETTINGS_FILE.write_text(json.dumps(current, indent=2), encoding="utf-8")
            global _cache
            _cache = current


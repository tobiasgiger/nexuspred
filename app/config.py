"""Settings management.

Settings are persisted to ``data/settings.json`` so they survive restarts and are
editable from the dashboard. Sensitive credentials never leave the local machine.
"""
from __future__ import annotations

import json
import logging
import os
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

    # --- Webhook security -----------------------------------------------------
    "webhook_secret": "change-me",   # required in the webhook URL path
    "webhook_passphrase": "",        # optional passphrase checked in JSON body
    # Protect the dashboard + API with HTTP Basic auth when hosted publicly.
    # The DASHBOARD_PASSWORD env var overrides this (use it for the first deploy).
    # The /webhook/<secret> endpoint is never behind this (TradingView can't auth).
    "dashboard_password": "",

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


# Fields that must never be returned to the browser in plain text.
SECRET_FIELDS = {"webhook_passphrase", "dashboard_password"}

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


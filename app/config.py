"""Settings management.

Settings are persisted to ``data/settings.json`` so they survive restarts and are
editable from the dashboard. Sensitive credentials never leave the local machine.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

# Repository root (one level up from the ``app`` package).
ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT_DIR / "data"
SETTINGS_FILE = DATA_DIR / "settings.json"
VERSION_FILE = ROOT_DIR / "VERSION"

# GitHub repo used by the auto-updater.
GITHUB_OWNER = "tobiasgiger"
GITHUB_REPO = "nexuspred"
GITHUB_BRANCH = os.environ.get("NEXUSPRED_BRANCH", "main")

DEFAULT_SETTINGS: dict[str, Any] = {
    # --- Tradovate connection -------------------------------------------------
    "environment": "demo",          # "demo" or "live"
    "username": "",
    "password": "",
    "app_id": "",
    "app_version": "1.0",
    "cid": "",                       # API key id
    "sec": "",                       # API secret
    "device_id": "",                 # optional device id
    "account_spec": "",              # account name, e.g. "DEMO12345"
    "account_id": 0,                 # numeric account id (auto-filled on connect)

    # --- Trading behaviour ----------------------------------------------------
    "trading_enabled": False,        # master kill switch (safety: off by default)
    "default_qty": 3,                # contracts for the initial market entry
    "tp_qty": 1,                     # contracts per take-profit limit order
    "entry_order_type": "Market",    # initial buy/sell are market orders
    "tp_order_type": "Limit",        # take-profits are resting limit orders
    "sl_order_type": "Stop",         # stop-loss as a protective stop order

    # Symbol mapping: TradingView root -> Tradovate front-month contract symbol.
    # Tradovate needs the dated contract (e.g. "MNQU5"); leave blank to let the
    # bridge auto-resolve the front month via the Tradovate contract API.
    "symbol_map": {
        "MNQ1!": "MNQ",
        "MES1!": "MES",
    },
    "allowed_symbols": ["MNQ", "MES"],

    # --- Webhook security -----------------------------------------------------
    "webhook_secret": "change-me",   # required in the webhook URL path
    "webhook_passphrase": "",        # optional passphrase checked in JSON body

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
    DATA_DIR.mkdir(parents=True, exist_ok=True)


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
SECRET_FIELDS = {"password", "sec", "webhook_passphrase"}


def public_settings() -> dict[str, Any]:
    """Settings safe to send to the dashboard (secrets masked)."""
    s = load_settings()
    out = dict(s)
    for field in SECRET_FIELDS:
        if out.get(field):
            out[field] = "********"
        else:
            out[field] = ""
    return out

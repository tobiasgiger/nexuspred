"""One declarative description of every workspace setting: type, bounds,
choices, and whether it is a secret, protected (written only by its own
validating endpoint) or portable (travels in a settings export).

``coerce`` turns an untrusted update into typed values or raises
``ValueError`` naming the key — the settings form, the settings import and
the automations all go through it, so a rule lives here once. Defaults stay in
``config.DEFAULT_SETTINGS``; ``tests/test_settings_schema.py`` keeps the two
in step."""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Optional

ORDER_TYPES = ("Market", "Limit", "Stop", "StopLimit")


@dataclass(frozen=True)
class Field:
    type: str                                   # bool | int | float | str | list | dict | json
    min: Optional[float] = None
    max: Optional[float] = None
    choices: Optional[tuple] = None
    fmt: str = ""                               # "time" (HH:MM) | "tz" (IANA) | "url" | ""
    secret: bool = False                        # masked in the API, never exported
    protected: bool = False                     # only its own endpoint writes it (never the generic form / import)
    portable: bool = True                       # travels in the settings export
    max_len: int = 0                            # str: characters, list: items
    clamp: bool = False                         # numbers: clamp into [min, max] instead of refusing


SCHEMA: dict[str, Field] = {
    # --- broker logins and routing (own endpoints)
    "token_accounts": Field("list", protected=True, portable=False),
    "trading_enabled": Field("bool", portable=False),
    "default_qty": Field("int", min=1, max=1000),
    "tp_qty": Field("int", min=1, max=1000),
    "entry_order_type": Field("str", choices=("Market", "Limit")),
    "tp_order_type": Field("str", choices=("Limit",)),
    "sl_order_type": Field("str", choices=("Stop", "StopLimit")),
    "breakeven_to_entry": Field("bool"),
    "symbol_map": Field("dict", max_len=500),
    "allowed_symbols": Field("list", max_len=500),
    "webhooks": Field("list", protected=True),
    "webhooks_migrated": Field("bool", protected=True, portable=False),
    "webhook_secret": Field("str", secret=True, protected=True, portable=False),
    "webhook_passphrase": Field("str", secret=True, portable=False, max_len=200),
    # --- alerts
    "alert_discord_enabled": Field("bool"),
    "alert_discord_webhook_url": Field("str", fmt="url", secret=True, portable=False, max_len=500),
    "alert_discord_mention_everyone": Field("bool"),
    "alert_email_enabled": Field("bool"),
    "alert_email_to": Field("str", max_len=200),
    "alert_smtp_host": Field("str", max_len=200),
    "alert_smtp_port": Field("int", min=1, max=65535),
    "alert_smtp_username": Field("str", max_len=200),
    "alert_smtp_password": Field("str", secret=True, portable=False),
    "alert_push_enabled": Field("bool"),
    "alert_on_connection_lost": Field("bool"),
    "alert_on_connection_restored": Field("bool"),
    "alert_on_trade_executed": Field("bool"),
    "alert_on_trade_opened": Field("bool"),
    "alert_accounts": Field("list", max_len=500),
    "alert_on_trade_closed": Field("bool"),
    "alert_on_agent_lost": Field("bool"),
    "alert_on_agent_restored": Field("bool"),
    "alert_on_copy": Field("bool"),
    "alert_on_risk": Field("bool"),
    "alert_daily_summary": Field("bool"),
    "daily_summary_time": Field("str", fmt="time"),
    "alert_on_discord_lost": Field("bool"),
    "alert_on_discord_restored": Field("bool"),
    "alert_on_webhook_failed": Field("bool"),
    "alert_on_rollover": Field("bool"),
    "rollover_warn_days": Field("int", min=0, max=60),
    "rollover_notified": Field("dict", protected=True, portable=False),
    "discord_health_grace": Field("int", min=15, max=3600),
    # --- runtime state (internal)
    "dd_state": Field("dict", protected=True, portable=False),
    "risk_state": Field("dict", protected=True, portable=False),
    "news_lock": Field("dict", protected=True),
    "copy_groups": Field("list", protected=True, portable=False),
    # --- Discord listener (own endpoint)
    "discord_enabled": Field("bool", protected=True, portable=False),
    "discord_user_token": Field("str", secret=True, protected=True, portable=False),
    "discord_dry_run": Field("bool", protected=True, portable=False),
    "discord_channels": Field("list", protected=True, portable=False),
    # --- polling, journal, updates, display, watchdog
    "pnl_poll_seconds": Field("int", min=0, max=3600),
    "journal_auto_import": Field("bool"),
    "journal_import_time": Field("str", fmt="time"),
    "journal_timezone": Field("str", fmt="tz"),
    "journal_last_import": Field("str", protected=True, portable=False),
    "journal_history_days": Field("int", min=1, max=3650),
    "journal_fee_per_side": Field("float", min=0, max=1000),
    "journal_report_cursor": Field("dict", protected=True, portable=False),
    "journal_report_window": Field("int", min=1, max=365, protected=True, portable=False),
    "auto_check_updates": Field("bool"),
    "ui_language": Field("str", choices=("auto", "de", "en")),
    "ui_language_seen": Field("str", choices=("", "de", "en"), portable=False),
    "heartbeat_url": Field("str", fmt="url", portable=False, max_len=500),
    "heartbeat_interval": Field("int", min=30, max=3600, clamp=True),
    "health_check_interval": Field("int", min=10, max=3600),
}

SECRET_KEYS = frozenset(k for k, f in SCHEMA.items() if f.secret)
PROTECTED_KEYS = frozenset(k for k, f in SCHEMA.items() if f.protected)
PORTABLE_KEYS = tuple(k for k, f in SCHEMA.items() if f.portable and not f.secret)


def _hhmm(raw: Any, key: str) -> str:
    text = str(raw or "").strip()
    parts = text.split(":")
    if len(parts) != 2 or not all(p.isdigit() for p in parts) or not (0 <= int(parts[0]) < 24 and 0 <= int(parts[1]) < 60):
        raise ValueError(f"{key} must be HH:MM")
    return f"{int(parts[0]):02d}:{int(parts[1]):02d}"


def coerce_one(key: str, value: Any) -> Any:
    """``value`` typed and bounded per the schema, or ValueError."""
    f = SCHEMA[key]
    t = f.type
    if t == "bool":
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
        return bool(value)
    if t in ("int", "float"):
        try:
            n = float(value)
        except (TypeError, ValueError):
            raise ValueError(f"{key} must be a number")
        if not math.isfinite(n):
            raise ValueError(f"{key} must be a finite number")
        if f.clamp:
            n = max(f.min if f.min is not None else n, min(f.max if f.max is not None else n, n))
        if f.min is not None and n < f.min:
            raise ValueError(f"{key} must be at least {f.min:g}")
        if f.max is not None and n > f.max:
            raise ValueError(f"{key} must be at most {f.max:g}")
        return int(n) if t == "int" else n
    if t == "str":
        if not isinstance(value, str):
            raise ValueError(f"{key} must be text")
        s = value
        if f.choices is not None and s not in f.choices:
            raise ValueError(f"{key} must be one of {', '.join(repr(c) for c in f.choices)}")
        if f.max_len and len(s) > f.max_len:
            raise ValueError(f"{key} is too long (at most {f.max_len} characters)")
        if f.fmt == "time":
            return _hhmm(s, key)
        if f.fmt == "tz":
            from zoneinfo import ZoneInfo
            name = s.strip() or "Europe/Zurich"
            try:
                ZoneInfo(name)
            except Exception as exc:  # noqa: BLE001
                raise ValueError(f"Unknown timezone '{name}' (use an IANA name like Europe/Zurich)") from exc
            return name
        return s
    if t == "list":
        if value is None:
            return []
        if not isinstance(value, list):
            raise ValueError(f"{key} must be a list")
        if f.max_len and len(value) > f.max_len:
            raise ValueError(f"{key} has too many entries (at most {f.max_len})")
        return value
    if t == "dict":
        if not isinstance(value, dict):
            raise ValueError(f"{key} must be an object")
        if f.max_len and len(value) > f.max_len:
            raise ValueError(f"{key} has too many entries (at most {f.max_len})")
        return value
    return value


def coerce(updates: dict[str, Any], *, allow_protected: bool = False) -> dict[str, Any]:
    """Every known key of ``updates`` typed and bounded; unknown keys raise;
    protected keys raise unless ``allow_protected``. Returns a new dict."""
    out: dict[str, Any] = {}
    for key, value in updates.items():
        f = SCHEMA.get(key)
        if f is None:
            raise ValueError(f"unknown setting '{key}'")
        if f.protected and not allow_protected:
            raise ValueError(f"'{key}' can only be changed through its own endpoint")
        out[key] = coerce_one(key, value)
    return out

"""Per-webhook trading window: entries (buy / sell, TS-Hunter signals) are only
executed inside the configured local time range on the configured weekdays.
Exits, stop moves and management signals always run — a window closes the
door for *new* risk, it never traps an open position.

Stored on the webhook as ``trade_window``::

    {"enabled": bool, "from": "08:00", "to": "17:00", "tz": "Europe/Zurich" | "",
     "days": ["mon", "tue", "wed", "thu", "fri"]}

``from`` > ``to`` means the window spans midnight (22:00 → 06:00: the day check
applies to the evening the window opens on). An empty ``tz`` follows the
workspace's journal timezone."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Optional
from zoneinfo import ZoneInfo

DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
DEFAULT: dict[str, Any] = {"enabled": False, "from": "08:00", "to": "17:00", "tz": "", "days": ["mon", "tue", "wed", "thu", "fri"]}


def _hhmm(raw: Any, default: str) -> str:
    text = str(raw if raw not in (None, "") else default).strip()
    parts = text.split(":")
    if len(parts) != 2 or not all(p.isdigit() for p in parts) or not (0 <= int(parts[0]) < 24 and 0 <= int(parts[1]) < 60):
        raise ValueError(f"time must be HH:MM, got '{text}'")
    return f"{int(parts[0]):02d}:{int(parts[1]):02d}"


def normalize(raw: Any) -> dict[str, Any]:
    """A validated window dict (raises ValueError). ``None`` / ``{}`` → disabled default."""
    if raw in (None, "", False):
        return dict(DEFAULT, days=list(DEFAULT["days"]))
    if not isinstance(raw, dict):
        raise ValueError("trade_window must be an object")
    out = {"enabled": bool(raw.get("enabled")), "from": _hhmm(raw.get("from"), DEFAULT["from"]), "to": _hhmm(raw.get("to"), DEFAULT["to"])}
    tz = str(raw.get("tz") or "").strip()
    if tz:
        try:
            ZoneInfo(tz)
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"unknown timezone '{tz}' (use an IANA name like Europe/Zurich)") from exc
    out["tz"] = tz[:64]
    days_raw = raw.get("days")
    if days_raw is None:
        days = list(DEFAULT["days"])
    elif isinstance(days_raw, (list, tuple)):
        days = [str(d).lower()[:3] for d in days_raw]
        bad = [d for d in days if d not in DAYS]
        if bad:
            raise ValueError(f"unknown weekday '{bad[0]}' (mon … sun)")
        days = [d for d in DAYS if d in days]
    else:
        raise ValueError("days must be a list of weekdays")
    if out["enabled"] and not days:
        raise ValueError("an enabled trading window needs at least one weekday")
    if out["enabled"] and out["from"] == out["to"]:
        raise ValueError("the window's start and end must differ")
    out["days"] = days
    return out


def _zone(name: str, fallback: str) -> ZoneInfo:
    for cand in (name, fallback, "Europe/Zurich"):
        if cand:
            try:
                return ZoneInfo(cand)
            except Exception:  # noqa: BLE001
                continue
    return ZoneInfo("UTC")


def is_open(window: Any, *, now: Optional[datetime] = None, default_tz: str = "") -> tuple[bool, str]:
    """``(True, "")`` when entries may run now, else ``(False, reason)``. A
    missing or disabled window is always open; a malformed one is treated as
    open too (a broken setting must not silently stop a strategy) — the API
    never stores a malformed one."""
    if not isinstance(window, dict) or not window.get("enabled"):
        return True, ""
    try:
        w = normalize(window)
    except ValueError:
        return True, ""
    zone = _zone(w["tz"], default_tz)
    local = (now or datetime.now(zone)).astimezone(zone)
    hm = local.strftime("%H:%M")
    day = DAYS[local.weekday()]
    frm, to, days = w["from"], w["to"], w["days"]
    if frm < to:
        opened = day in days and frm <= hm < to
    else:                                              # spans midnight: 22:00 → 06:00
        prev = DAYS[(local - timedelta(days=1)).weekday()]
        opened = (day in days and hm >= frm) or (prev in days and hm < to)
    if opened:
        return True, ""
    label = f"{frm}–{to} {zone.key}" if hasattr(zone, "key") else f"{frm}–{to}"
    return False, f"outside the trading window {label} ({', '.join(days)}); now {hm} {day}"

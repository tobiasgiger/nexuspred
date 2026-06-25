"""In-memory runtime state: rolling logs of signals, orders and events.

Kept intentionally simple (bounded deques) so the bridge stays dependency-light and
restart-cheap. The dashboard polls these via the API.
"""
from __future__ import annotations

import threading
from collections import deque
from datetime import datetime, timezone
from typing import Any, Deque

_MAX = 200

_lock = threading.Lock()
_signals: Deque[dict[str, Any]] = deque(maxlen=_MAX)
_orders: Deque[dict[str, Any]] = deque(maxlen=_MAX)
_events: Deque[dict[str, Any]] = deque(maxlen=_MAX)

# Per-session connection status, keyed by account name.
sessions: dict[str, dict[str, Any]] = {}


def set_session_status(name: str, **fields: Any) -> None:
    with _lock:
        s = sessions.setdefault(name, {"name": name, "connected": False})
        s.update(fields)
        s["name"] = name


def session_status(name: str) -> dict[str, Any]:
    with _lock:
        return dict(sessions.get(name, {"name": name, "connected": False}))


def session_statuses() -> list[dict[str, Any]]:
    with _lock:
        return [dict(v) for v in sessions.values()]


def aggregate_connection() -> dict[str, Any]:
    with _lock:
        vals = list(sessions.values())
    total = len(vals)
    connected = sum(1 for v in vals if v.get("connected"))
    return {"connected": connected > 0, "accounts_total": total,
            "accounts_connected": connected}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def log_signal(payload: dict[str, Any], result: str = "received") -> dict[str, Any]:
    entry = {"ts": _now(), "payload": payload, "result": result}
    with _lock:
        _signals.appendleft(entry)
    return entry


def log_order(order: dict[str, Any]) -> None:
    entry = {"ts": _now(), **order}
    with _lock:
        _orders.appendleft(entry)


def log_event(level: str, message: str, **extra: Any) -> None:
    entry = {"ts": _now(), "level": level, "message": message, **extra}
    with _lock:
        _events.appendleft(entry)


def snapshot() -> dict[str, Any]:
    with _lock:
        return {
            "signals": list(_signals),
            "orders": list(_orders),
            "events": list(_events),
            "sessions": [dict(v) for v in sessions.values()],
        }


def recent_signals() -> list[dict[str, Any]]:
    with _lock:
        return list(_signals)


def recent_orders() -> list[dict[str, Any]]:
    with _lock:
        return list(_orders)


def recent_events() -> list[dict[str, Any]]:
    with _lock:
        return list(_events)

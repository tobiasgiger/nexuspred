"""In-memory runtime state: rolling logs of signals, orders and events, plus
per-account connection status — **isolated per area**.

Each area (user workspace) gets its own bounded deques and session map, selected
by the current-area context (:mod:`app.context`). Kept intentionally simple so
the bridge stays dependency-light and restart-cheap. The dashboard polls these
via the API (which runs in the logged-in user's area context).
"""
from __future__ import annotations

import threading
from collections import deque
from datetime import datetime, timezone
from typing import Any, Deque

from . import context

_MAX = 200
_lock = threading.Lock()


class _AreaState:
    __slots__ = ("signals", "orders", "events", "sessions")

    def __init__(self) -> None:
        self.signals: Deque[dict[str, Any]] = deque(maxlen=_MAX)
        self.orders: Deque[dict[str, Any]] = deque(maxlen=_MAX)
        self.events: Deque[dict[str, Any]] = deque(maxlen=_MAX)
        self.sessions: dict[str, dict[str, Any]] = {}


_areas: dict[int, _AreaState] = {}


def _st() -> _AreaState:
    aid = context.get_area()
    with _lock:
        st = _areas.get(aid)
        if st is None:
            st = _areas[aid] = _AreaState()
        return st


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ------------------------------------------------------------- session status
def set_session_status(name: str, **fields: Any) -> None:
    st = _st()
    with _lock:
        s = st.sessions.setdefault(name, {"name": name, "connected": False})
        s.update(fields)
        s["name"] = name


def session_status(name: str) -> dict[str, Any]:
    st = _st()
    with _lock:
        return dict(st.sessions.get(name, {"name": name, "connected": False}))


def session_statuses() -> list[dict[str, Any]]:
    st = _st()
    with _lock:
        return [dict(v) for v in st.sessions.values()]


def aggregate_connection() -> dict[str, Any]:
    st = _st()
    with _lock:
        vals = list(st.sessions.values())
    total = len(vals)
    connected = sum(1 for v in vals if v.get("connected"))
    return {"connected": connected > 0, "accounts_total": total,
            "accounts_connected": connected}


# ------------------------------------------------------------- rolling logs
def log_signal(payload: dict[str, Any], result: str = "received") -> dict[str, Any]:
    entry = {"ts": _now(), "payload": payload, "result": result}
    st = _st()
    with _lock:
        st.signals.appendleft(entry)
    return entry


def log_order(order: dict[str, Any]) -> None:
    entry = {"ts": _now(), **order}
    st = _st()
    with _lock:
        st.orders.appendleft(entry)


def log_event(level: str, message: str, **extra: Any) -> None:
    entry = {"ts": _now(), "level": level, "message": message, **extra}
    st = _st()
    with _lock:
        st.events.appendleft(entry)


def snapshot() -> dict[str, Any]:
    st = _st()
    with _lock:
        return {
            "signals": list(st.signals),
            "orders": list(st.orders),
            "events": list(st.events),
            "sessions": [dict(v) for v in st.sessions.values()],
        }


def recent_signals() -> list[dict[str, Any]]:
    st = _st()
    with _lock:
        return list(st.signals)


def recent_orders() -> list[dict[str, Any]]:
    st = _st()
    with _lock:
        return list(st.orders)


def recent_events() -> list[dict[str, Any]]:
    st = _st()
    with _lock:
        return list(st.events)

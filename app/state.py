"""In-memory runtime state: rolling logs of signals, orders and events, plus
per-account connection status — **isolated per area**.

Each area (user workspace) gets its own bounded deques and session map, selected
by the current-area context (:mod:`app.context`). Kept intentionally simple so
the bridge stays dependency-light and restart-cheap. The dashboard polls these
via the API (which runs in the logged-in user's area context).
"""
from __future__ import annotations

import asyncio
import threading
from collections import deque
from datetime import datetime, timezone
from typing import Any, Deque

from . import context

_MAX = 200
_lock = threading.Lock()


class _Sub:
    """A live subscriber (SSE connection): a queue plus the loop it belongs to,
    so events logged from any thread can be delivered thread-safely."""
    __slots__ = ("queue", "loop")

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=200)
        self.loop = loop


class _AreaState:
    __slots__ = ("signals", "orders", "events", "sessions", "subscribers")

    def __init__(self) -> None:
        self.signals: Deque[dict[str, Any]] = deque(maxlen=_MAX)
        self.orders: Deque[dict[str, Any]] = deque(maxlen=_MAX)
        self.events: Deque[dict[str, Any]] = deque(maxlen=_MAX)
        self.sessions: dict[str, dict[str, Any]] = {}
        self.subscribers: set[_Sub] = set()


_areas: dict[int, _AreaState] = {}


def _st_for(aid: int) -> _AreaState:
    with _lock:
        st = _areas.get(aid)
        if st is None:
            st = _areas[aid] = _AreaState()
        return st


def _st() -> _AreaState:
    return _st_for(context.get_area())


def _safe_put(q: asyncio.Queue, message: dict[str, Any]) -> None:
    try:
        q.put_nowait(message)
    except asyncio.QueueFull:
        pass


def _broadcast(st: _AreaState, message: dict[str, Any]) -> None:
    """Push a message to this area's live subscribers (thread-safe)."""
    for sub in list(st.subscribers):
        try:
            sub.loop.call_soon_threadsafe(_safe_put, sub.queue, message)
        except RuntimeError:  # loop already closed
            pass


# --------------------------------------------------------------- live stream
def subscribe(area_id: int | None = None) -> _Sub:
    """Register a live subscriber for an area. Call from within a running loop."""
    aid = area_id if area_id is not None else context.get_area()
    sub = _Sub(asyncio.get_running_loop())
    st = _st_for(aid)
    with _lock:
        st.subscribers.add(sub)
    return sub


def unsubscribe(sub: _Sub, area_id: int | None = None) -> None:
    aid = area_id if area_id is not None else context.get_area()
    st = _st_for(aid)
    with _lock:
        st.subscribers.discard(sub)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ------------------------------------------------------------- session status
def set_session_status(name: str, **fields: Any) -> None:
    st = _st()
    with _lock:
        s = st.sessions.setdefault(name, {"name": name, "connected": False})
        s.update(fields)
        s["name"] = name


def has_session(name: str) -> bool:
    """Whether this area has ever recorded a status for ``name`` (used to detect
    the first observation vs. a connection transition)."""
    st = _st()
    with _lock:
        return name in st.sessions


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
    _broadcast(st, {"kind": "signal", "data": entry})
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
    _broadcast(st, {"kind": "event", "data": entry})


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

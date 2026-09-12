"""In-memory runtime state: rolling logs of signals, orders and events, plus
per-account connection status — **isolated per area**.

Each area (user workspace) gets its own bounded deques and session map, selected
by the current-area context (:mod:`app.context`). Kept intentionally simple so
the bridge stays dependency-light and restart-cheap. The dashboard polls these
via the API (which runs in the logged-in user's area context).
"""
from __future__ import annotations

import asyncio
import json
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
    __slots__ = ("queue", "loop", "dropped")

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=200)
        self.loop = loop
        self.dropped = False          # a message was lost on a full queue: the stream tells the client to resync


class _AreaState:
    __slots__ = ("signals", "orders", "events", "sessions", "subscribers", "rollover", "pnl")

    def __init__(self) -> None:
        self.signals: Deque[dict[str, Any]] = deque(maxlen=_MAX)
        self.orders: Deque[dict[str, Any]] = deque(maxlen=_MAX)
        self.events: Deque[dict[str, Any]] = deque(maxlen=_MAX)
        self.sessions: dict[str, dict[str, Any]] = {}
        self.subscribers: set[_Sub] = set()
        self.rollover: list[dict[str, Any]] = []  # contract-rollover warnings (app.rollover)
        self.pnl: dict[str, Any] = {}  # latest live account P&L (app.pnl)


_areas: dict[int, _AreaState] = {}


def _st_for(aid: int) -> _AreaState:
    with _lock:
        st = _areas.get(aid)
        if st is None:
            st = _areas[aid] = _AreaState()
        return st


def _st() -> _AreaState:
    return _st_for(context.get_area())


def _safe_put(sub: "_Sub", frame: str) -> None:
    try:
        sub.queue.put_nowait(frame)
    except asyncio.QueueFull:
        sub.dropped = True            # a slow tab: it gets a resync marker once it catches up


def frame_message(frame: str) -> dict[str, Any]:
    """The message inside an SSE frame built by :func:`_broadcast` (tests)."""
    return json.loads(frame[len("data: "):].strip())


def _broadcast(st: _AreaState, message: dict[str, Any]) -> None:
    """Push a message to this area's live subscribers (thread-safe). The SSE
    frame is built once here, not once per subscriber in the stream handler."""
    if not st.subscribers:
        return
    frame = f"data: {json.dumps(message, default=str)}\n\n"
    for sub in list(st.subscribers):
        try:
            sub.loop.call_soon_threadsafe(_safe_put, sub, frame)
        except RuntimeError:  # loop already closed
            pass


def publish(kind: str, data: dict[str, Any], area_id: int | None = None) -> None:
    """Push a ``{"kind", "data"}`` message onto an area's live stream. Kinds on
    ``/api/stream``: ``event`` | ``signal`` | ``order`` | ``session`` | ``discord``."""
    st = _st_for(area_id) if area_id is not None else _st()
    _broadcast(st, {"kind": kind, "data": data})


# --------------------------------------------------------------- live stream
def subscribe(area_id: int | None = None) -> _Sub:
    """Register a live subscriber for an area. Call from within a running loop."""
    aid = area_id if area_id is not None else context.get_area()
    sub = _Sub(asyncio.get_running_loop())
    st = _st_for(aid)
    with _lock:
        st.subscribers.add(sub)
    return sub


def subscriber_count(area_id: int | None = None) -> int:
    """How many live-stream connections an area has (0 = nobody is watching)."""
    aid = area_id if area_id is not None else context.get_area()
    with _lock:
        st = _areas.get(aid)
        return len(st.subscribers) if st else 0


def set_pnl(summary: dict[str, Any], area_id: int | None = None) -> bool:
    """Store the latest live P&L; True when the figures changed."""
    st = _st_for(area_id) if area_id is not None else _st()
    keys = ("realized", "open", "week", "cash", "error")
    with _lock:
        prev = st.pnl
        changed = (not prev or any(prev.get(k) != summary.get(k) for k in keys)
                   or [(a["account_id"], a["realized"], a["open"]) for a in prev.get("accounts", [])]
                   != [(a["account_id"], a["realized"], a["open"]) for a in summary.get("accounts", [])])
        st.pnl = dict(summary)
    return changed


def pnl(area_id: int | None = None) -> dict[str, Any]:
    st = _st_for(area_id) if area_id is not None else _st()
    with _lock:
        return dict(st.pnl)


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
        snapshot = dict(s)
    _broadcast(st, {"kind": "session", "data": snapshot})


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


# ------------------------------------------------------- rollover warnings
def set_rollover_warnings(warnings: list[dict[str, Any]], area_id: int | None = None) -> None:
    st = _st_for(area_id) if area_id is not None else _st()
    with _lock:
        st.rollover = [dict(w) for w in warnings]


def rollover_warnings(area_id: int | None = None) -> list[dict[str, Any]]:
    st = _st_for(area_id) if area_id is not None else _st()
    with _lock:
        return [dict(w) for w in st.rollover]


# ------------------------------------------------------------- rolling logs
_SECRET_PAYLOAD_KEYS = ("passphrase", "secret", "password", "token")


def redact_payload(payload: Any) -> Any:
    """A copy of a signal payload with credential-like fields masked. Signal logs
    are persisted, streamed to every browser of the area and forwarded to
    marketplace subscribers — none of those may learn the webhook passphrase."""
    if not isinstance(payload, dict):
        return payload
    out = {}
    for k, v in payload.items():
        if isinstance(k, str) and any(s in k.lower() for s in _SECRET_PAYLOAD_KEYS) and v not in (None, ""):
            out[k] = "********"
        else:
            out[k] = redact_payload(v) if isinstance(v, dict) else v
    return out


def log_signal(payload: dict[str, Any], result: str = "received", webhook: str = "", webhook_id: str = "") -> dict[str, Any]:
    from . import history
    entry = {"ts": _now(), "payload": redact_payload(payload), "result": result, "webhook": webhook or "", "webhook_id": webhook_id or ""}
    aid = context.get_area()
    st = _st_for(aid)
    with _lock:
        st.signals.appendleft(entry)
    _broadcast(st, {"kind": "signal", "data": entry})
    history.record_signal(aid, entry)
    return entry


def log_order(order: dict[str, Any]) -> None:
    from . import history
    entry = {"ts": _now(), **order}
    aid = context.get_area()
    st = _st_for(aid)
    with _lock:
        st.orders.appendleft(entry)
    _broadcast(st, {"kind": "order", "data": entry})
    history.record_order(aid, entry)


def hydrate(area_id: int, *, signals: list[dict[str, Any]], orders: list[dict[str, Any]]) -> None:
    """Seed an area's ring buffers from persisted history (newest first)."""
    st = _st_for(area_id)
    with _lock:
        st.signals.clear()
        st.signals.extend(signals[:_MAX])
        st.orders.clear()
        st.orders.extend(orders[:_MAX])


def log_event(level: str, message: str, **extra: Any) -> None:
    if "payload" in extra:
        extra["payload"] = redact_payload(extra["payload"])
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

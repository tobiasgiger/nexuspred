"""In-memory event hub for the Discord signal module.

Holds a bounded ring buffer of recent signal events (so the dashboard can show
history on load) and broadcasts every new event to any number of live
subscribers via :mod:`asyncio` queues — the plumbing behind the Server-Sent
Events endpoint, so the dashboard updates in real time without polling.

Kept dependency-light and restart-cheap, mirroring :mod:`app.state`.
"""
from __future__ import annotations

import asyncio
import threading
from collections import deque
from datetime import datetime, timezone
from typing import Any, Deque

_MAX = 200

_lock = threading.Lock()
_events: Deque[dict[str, Any]] = deque(maxlen=_MAX)

# Live SSE subscribers. Each is an asyncio.Queue; a slow/dead consumer only
# affects its own queue (bounded), never the producer or other subscribers.
_subscribers: set[asyncio.Queue] = set()
_subscribers_lock = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def record(event: dict[str, Any]) -> dict[str, Any]:
    """Store an event in the ring buffer and broadcast it to live subscribers."""
    entry = {"ts": event.get("ts") or _now(), **event}
    with _lock:
        _events.appendleft(entry)
    _broadcast(entry)
    return entry


def recent() -> list[dict[str, Any]]:
    """Snapshot of recent events, newest first (for the dashboard's initial load)."""
    with _lock:
        return list(_events)


def _broadcast(entry: dict[str, Any]) -> None:
    with _subscribers_lock:
        subs = list(_subscribers)
    for q in subs:
        try:
            q.put_nowait(entry)
        except asyncio.QueueFull:
            # Drop for this one slow consumer; never block the producer.
            pass


def subscribe() -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue(maxsize=100)
    with _subscribers_lock:
        _subscribers.add(q)
    return q


def unsubscribe(q: asyncio.Queue) -> None:
    with _subscribers_lock:
        _subscribers.discard(q)


def subscriber_count() -> int:
    with _subscribers_lock:
        return len(_subscribers)

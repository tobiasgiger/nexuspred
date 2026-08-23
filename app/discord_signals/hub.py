"""In-memory event hub for the Discord signal module — **isolated per area**.

Holds a bounded ring buffer of recent signal events per area (so each user's
dashboard shows only their own history) and broadcasts new events to that area's
live Server-Sent-Events subscribers. Selected by the current-area context
(:mod:`app.context`): the SSE route runs in the logged-in user's request context,
and the listener/webhook processing runs in that area's task context.
"""
from __future__ import annotations

import asyncio
import threading
from collections import deque
from datetime import datetime, timezone
from typing import Any, Deque

from .. import context

_MAX = 200
_lock = threading.Lock()


class _AreaHub:
    __slots__ = ("events", "subscribers")

    def __init__(self) -> None:
        self.events: Deque[dict[str, Any]] = deque(maxlen=_MAX)
        self.subscribers: set[asyncio.Queue] = set()


_areas: dict[int, _AreaHub] = {}


def _hub(area_id: int | None = None) -> _AreaHub:
    aid = area_id if area_id is not None else context.get_area()
    with _lock:
        h = _areas.get(aid)
        if h is None:
            h = _areas[aid] = _AreaHub()
        return h


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def record(event: dict[str, Any]) -> dict[str, Any]:
    """Store an event in this area's ring buffer and broadcast to its subscribers."""
    entry = {"ts": event.get("ts") or _now(), **event}
    h = _hub()
    with _lock:
        h.events.appendleft(entry)
        subs = list(h.subscribers)
    for q in subs:
        try:
            q.put_nowait(entry)
        except asyncio.QueueFull:
            pass
    return entry


def recent(area_id: int | None = None) -> list[dict[str, Any]]:
    h = _hub(area_id)
    with _lock:
        return list(h.events)


def subscribe(area_id: int | None = None) -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue(maxsize=100)
    h = _hub(area_id)
    with _lock:
        h.subscribers.add(q)
    return q


def unsubscribe(q: asyncio.Queue, area_id: int | None = None) -> None:
    h = _hub(area_id)
    with _lock:
        h.subscribers.discard(q)

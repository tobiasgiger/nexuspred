"""One REST snapshot per login, shared by every copy group that reads it.

Several copy groups can lead from accounts of the same broker login (two
strategies on one Tradovate login, each mirrored to its own followers). Each
group runner polls the leader's positions and orders on its own cadence; without
sharing, three groups on one login cost three ``/position/list`` calls a
second against one rate budget. Here a runner asking within
``TTL_S`` of the last fetch gets the rows that fetch returned, and runners
asking at the same moment wait for the in-flight request instead of starting
their own (single-flight). Rows are copied on the way out; a runner never sees
another runner's mutations."""
from __future__ import annotations

import asyncio
import time
from typing import Any

TTL_S = 0.8
KINDS = ("positions", "orders")


class _Feed:
    __slots__ = ("at", "rows", "locks", "hits", "fetches")

    def __init__(self) -> None:
        self.at: dict[str, float] = {}
        self.rows: dict[str, list[dict[str, Any]]] = {}
        self.locks: dict[str, asyncio.Lock] = {}
        self.hits = 0
        self.fetches = 0


_feeds: dict[str, _Feed] = {}


def key_of(area_id: int, session: Any) -> str:
    return f"{area_id}:{getattr(session, 'lid', '') or getattr(session, 'name', '') or id(session)}"


def reset() -> None:
    _feeds.clear()


def stats(area_id: int, session: Any) -> dict[str, int]:
    f = _feeds.get(key_of(area_id, session))
    return {"hits": f.hits, "fetches": f.fetches} if f else {"hits": 0, "fetches": 0}


async def snapshot(area_id: int, session: Any, kind: str, *, fresh: bool = False) -> tuple[list[dict[str, Any]], bool]:
    """``(rows, shared)`` — the login's positions or orders; ``shared`` is True
    when the rows came from another runner's fetch within ``TTL_S``. ``fresh``
    bypasses the reuse (rows must postdate an event the caller already saw).
    Errors of the fetch propagate to the caller that made it (the next asker
    fetches again). ``fetched_at(area_id, session, kind)`` gives the monotonic
    time of the rows returned last."""
    if kind not in KINDS:
        raise ValueError(f"unknown feed kind {kind!r}")
    feed = _feeds.setdefault(key_of(area_id, session), _Feed())
    lock = feed.locks.get(kind)
    if lock is None:
        lock = feed.locks[kind] = asyncio.Lock()
    async with lock:
        if not fresh and time.monotonic() - feed.at.get(kind, -1e9) < TTL_S:
            feed.hits += 1
            return [dict(r) for r in feed.rows.get(kind, [])], True
        raw = await (session.positions_snapshot() if kind == "positions" else session.orders_snapshot())
        rows = [r for r in raw if isinstance(r, dict)] if isinstance(raw, list) else []
        feed.at[kind] = time.monotonic()
        feed.rows[kind] = rows
        feed.fetches += 1
        return [dict(r) for r in rows], False


def fetched_at(area_id: int, session: Any, kind: str) -> float:
    """Monotonic time of the login's last fetch of ``kind`` (0 when none)."""
    f = _feeds.get(key_of(area_id, session))
    return f.at.get(kind, 0.0) if f else 0.0

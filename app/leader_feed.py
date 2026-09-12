"""One REST snapshot per login, shared by every copy group that reads it.

Several copy groups can lead from accounts of the same broker login. A runner
asking within ``TTL_S`` of the last fetch gets the same snapshot and concurrent
askers share the one in-flight request. Rows are copied on return.
"""
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


def drop(area_id: int, session: Any) -> None:
    """Forget snapshots for a login whose broker session is being replaced."""
    _feeds.pop(key_of(area_id, session), None)


def stats(area_id: int, session: Any) -> dict[str, int]:
    f = _feeds.get(key_of(area_id, session))
    return {"hits": f.hits, "fetches": f.fetches} if f else {"hits": 0, "fetches": 0}


async def snapshot(area_id: int, session: Any, kind: str) -> tuple[list[dict[str, Any]], bool]:
    if kind not in KINDS:
        raise ValueError(f"unknown feed kind {kind!r}")
    feed = _feeds.setdefault(key_of(area_id, session), _Feed())
    lock = feed.locks.get(kind)
    if lock is None:
        lock = feed.locks[kind] = asyncio.Lock()
    async with lock:
        if time.monotonic() - feed.at.get(kind, -1e9) < TTL_S:
            feed.hits += 1
            return [dict(r) for r in feed.rows.get(kind, [])], True
        raw = await (session.positions_snapshot() if kind == "positions" else session.orders_snapshot())
        rows = [r for r in raw if isinstance(r, dict)] if isinstance(raw, list) else []
        feed.at[kind] = time.monotonic()
        feed.rows[kind] = rows
        feed.fetches += 1
        return [dict(r) for r in rows], False

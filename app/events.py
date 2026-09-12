"""The bridge's event bus: producers (engines, brokers, the position watch,
the risk guard, the copy engine, the news lock …) announce what happened;
consumers (alerts today, automations and metrics next) subscribe to the kinds
they care about. Producers no longer know who listens.

An event is a kind (dotted name) plus keyword data. ``emit`` runs the sync
handlers now and schedules the coroutines they return in the background (the
area context is inherited) — the fire-and-forget the alert paths always used.
``emit_async`` awaits them, for the paths that must not return before the
alert went out. A failing handler never affects another one or the producer.

Kinds in use: connection.lost / connection.restored, trade.executed,
position.opened / position.added / position.closed, agent.lost /
agent.restored, risk.triggered, execution.problem, news.lock, copy.alert,
daily.summary, discord.lost / discord.restored, signal.failed, rollover.due,
signal.done."""
from __future__ import annotations

import asyncio
import inspect
import logging
import time
from collections import defaultdict
from typing import Any, Awaitable, Callable, Optional

log = logging.getLogger("nexuspred.events")

Handler = Callable[[dict[str, Any]], Any]
_handlers: dict[str, list[Handler]] = defaultdict(list)
_recent: list[dict[str, Any]] = []
RECENT_MAX = 200
_bg: set[asyncio.Task] = set()


def subscribe(kind: str, handler: Handler) -> Callable[[], None]:
    """Register ``handler(data)`` for ``kind`` (``"*"`` = every event).
    Returns the unsubscribe function."""
    _handlers[kind].append(handler)

    def off() -> None:
        try:
            _handlers[kind].remove(handler)
        except ValueError:
            pass
    return off


def _targets(kind: str) -> list[Handler]:
    return list(_handlers.get(kind, ())) + list(_handlers.get("*", ()))


def _remember(kind: str, data: dict[str, Any]) -> None:
    _recent.append({"kind": kind, "at": time.time(), **{k: v for k, v in data.items() if k != "settings"}})
    if len(_recent) > RECENT_MAX:
        del _recent[:-RECENT_MAX]


def _schedule(coro: Awaitable[Any], kind: str) -> None:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        coro.close() if hasattr(coro, "close") else None       # no loop (a sync test): nothing to run it on
        return
    task = loop.create_task(coro)  # type: ignore[arg-type]
    _bg.add(task)

    def done(t: asyncio.Task) -> None:
        _bg.discard(t)
        if not t.cancelled() and t.exception() is not None:
            log.warning("event handler for %s failed: %r", kind, t.exception())
    task.add_done_callback(done)


def emit(kind: str, /, **data: Any) -> int:
    """Announce ``kind``; coroutines returned by handlers run in the background.
    Returns how many handlers were invoked."""
    _remember(kind, data)
    n = 0
    for h in _targets(kind):
        n += 1
        try:
            r = h(data)
        except Exception as exc:  # noqa: BLE001 - one listener never breaks the producer
            log.warning("event handler for %s failed: %r", kind, exc)
            continue
        if inspect.isawaitable(r):
            _schedule(r, kind)
    return n


async def emit_async(kind: str, /, **data: Any) -> int:
    """Announce ``kind`` and wait for every handler (the producer needs the
    alert out before it returns)."""
    _remember(kind, data)
    coros = []
    for h in _targets(kind):
        try:
            r = h(data)
        except Exception as exc:  # noqa: BLE001
            log.warning("event handler for %s failed: %r", kind, exc)
            continue
        if inspect.isawaitable(r):
            coros.append(r)
    results = await asyncio.gather(*coros, return_exceptions=True)
    for r in results:
        if isinstance(r, asyncio.CancelledError):
            raise r
        if isinstance(r, Exception):
            log.warning("event handler for %s failed: %r", kind, r)
    return len(coros)


def recent(limit: int = 50, kind: Optional[str] = None) -> list[dict[str, Any]]:
    rows = [e for e in _recent if kind is None or e["kind"] == kind]
    return rows[-limit:]


def reset() -> None:
    """Tests: drop the recent list (subscribers stay — alerts registers once at import)."""
    _recent.clear()

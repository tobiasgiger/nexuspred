"""Per-request / per-task "current area" context.

Fluxbridge is multi-tenant: every user has their own isolated **area** holding
their settings, accounts, webhooks, Discord listener, logs, etc. Rather than
thread an ``area_id`` through every call site, the current area is kept in a
:class:`contextvars.ContextVar`. It's set:

* by the auth middleware for each dashboard/API request (the logged-in user's area),
* by the webhook endpoint (resolved from the webhook token) before processing,
* by the background loops, once per area, each in its own task/context.

``contextvars`` propagate into ``asyncio`` tasks created within the context, so a
webhook's background processing task and per-area loop tasks each see the right
area automatically.
"""
from __future__ import annotations

import contextlib
from contextvars import ContextVar
from typing import Iterator, Optional

# The area used when nothing else is set — keeps single-tenant/legacy code paths
# and tests working. The first migrated area gets this id.
DEFAULT_AREA_ID = 1

_area: ContextVar[Optional[int]] = ContextVar("current_area_id", default=None)


def get_area() -> int:
    """Current area id, falling back to the default area."""
    val = _area.get()
    return val if val is not None else DEFAULT_AREA_ID


def get_area_optional() -> Optional[int]:
    return _area.get()


def set_area(area_id: Optional[int]):
    """Set the current area; returns the token for :func:`reset_area`."""
    return _area.set(area_id)


def reset_area(token) -> None:
    _area.reset(token)


@contextlib.contextmanager
def use_area(area_id: int) -> Iterator[None]:
    token = _area.set(area_id)
    try:
        yield
    finally:
        _area.reset(token)

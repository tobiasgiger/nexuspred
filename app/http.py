"""Shared outbound HTTP clients.

v4 opened a fresh ``httpx.AsyncClient`` for every Tradovate call, Discord alert
and GitHub check — a TCP + TLS handshake per order. v5 keeps one pooled client
per purpose, created lazily on the running event loop and closed on shutdown,
so keep-alive connections are reused across requests.

Clients are keyed by *name* and by the event loop they were created on: a
client whose loop is gone (tests spin up a loop per test) is transparently
replaced instead of raising "attached to a different loop".
"""
from __future__ import annotations

import asyncio
import importlib.util
from typing import Any

import httpx

# Optional HTTP/2 (only if the ``h2`` package is installed; httpx negotiates via
# ALPN and silently falls back to HTTP/1.1 keep-alive otherwise).
_HTTP2 = importlib.util.find_spec("h2") is not None

_LIMITS = httpx.Limits(max_connections=64, max_keepalive_connections=32, keepalive_expiry=60.0)

# Per-purpose defaults. ``timeout`` can still be overridden per request.
PROFILES: dict[str, dict[str, Any]] = {
    "tradovate": {"timeout": 20.0, "limits": _LIMITS, "http2": _HTTP2},
    "outbound": {"timeout": 10.0, "limits": _LIMITS},
}

_clients: dict[str, httpx.AsyncClient] = {}
_loops: dict[str, asyncio.AbstractEventLoop] = {}


def client(name: str = "outbound") -> httpx.AsyncClient:
    """The shared client for ``name`` on the current event loop (created on first use)."""
    loop = asyncio.get_running_loop()
    c = _clients.get(name)
    if c is None or c.is_closed or _loops.get(name) is not loop:
        c = httpx.AsyncClient(**PROFILES.get(name, PROFILES["outbound"]))
        _clients[name] = c
        _loops[name] = loop
    return c


async def aclose_all() -> None:
    """Close every pooled client (app shutdown)."""
    clients = list(_clients.values())
    _clients.clear()
    _loops.clear()
    for c in clients:
        try:
            await c.aclose()
        except Exception:  # noqa: BLE001 - shutdown must never fail on a socket
            pass


def reset() -> None:
    """Forget every client without closing it (test isolation between loops)."""
    _clients.clear()
    _loops.clear()

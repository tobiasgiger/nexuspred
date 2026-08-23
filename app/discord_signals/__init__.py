"""Discord signal-listener module for the nexuspred bridge.

A self-contained module that watches one or more Discord channels (via the
Gateway, using a personal user token — a "self-bot") in which a signal provider
posts trade updates as Discord embeds, parses those embeds into structured
signals, and fans them out to configurable webhook targets (typically the
bridge itself, but also arbitrary external endpoints).

The module runs *inside* the existing FastAPI process:

* ``parser``     — embed -> :class:`~app.discord_signals.parser.Signal`
* ``dispatcher`` — parallel fan-out to webhook targets (own HTTP client)
* ``hub``        — in-memory event ring buffer + Server-Sent-Events broadcast
* ``listener``   — Discord Gateway client (self-bot) + supervisor
* ``routes``     — APIRouter mounted onto the bridge's app (settings + SSE + test)

Configuration lives in the bridge's existing settings layer
(``app.config`` -> ``data/settings.json``), not in a parallel file, and is read
live on every incoming Discord event so changes take effect without a restart.
"""
from __future__ import annotations

__all__ = ["parser", "dispatcher", "hub", "listener", "routes"]

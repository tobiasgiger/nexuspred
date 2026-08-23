"""Discord Gateway listener (self-bot) and its supervisor.

Connects to the Discord **Gateway** (WebSocket push — never polling) using a
personal user token via ``discord.py-self``, watches the configured channels,
and pushes every embed through :mod:`app.discord_signals.pipeline`.

Robustness is the priority: the whole thing runs as an isolated asyncio task and
is wrapped so a Discord connection error, a library bug, or a missing dependency
can **never** crash the rest of the bridge (order execution, health loop, …).
``discord.py-self`` is imported lazily so the bridge boots fine even when it
isn't installed — the dashboard then shows the listener as "library missing".

Runtime control (spec: config changes take effect without a process restart):

* Channel list / targets / dry-run are read live per event in the pipeline — no
  restart needed.
* Enabling/disabling the listener or changing the user token calls
  :meth:`ListenerManager.apply_config`, which starts/stops/reconnects the client.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Optional

from .. import config, context, state
from . import pipeline
from .parser import embed_from_discord


def _import_discord() -> tuple[Any, Optional[str]]:
    """Import ``discord`` (from discord.py-self) lazily. Returns (module, error)."""
    try:
        import discord  # type: ignore

        return discord, None
    except Exception as exc:  # noqa: BLE001 - ImportError or any load-time failure
        return None, str(exc)


class ListenerManager:
    """Owns the Discord client lifecycle and reconciles it with live config."""

    def __init__(self, area_id: int | None = None) -> None:
        self.area_id = area_id
        self._supervisor_task: Optional[asyncio.Task] = None
        self._client: Any = None
        self._current_token: Optional[str] = None
        self._shutdown = False
        self._status: dict[str, Any] = {
            "state": "stopped",     # stopped|disabled|connecting|connected|error|library_missing
            "connected": False,
            "error": "",
            "user": "",
            "last_event_ts": "",
            "last_event_channel": "",
        }
        self._discord, self._lib_error = _import_discord()

    # ------------------------------------------------------------------ status
    def library_available(self) -> bool:
        return self._discord is not None

    def _area(self) -> int:
        return self.area_id if self.area_id is not None else context.get_area()

    def status(self) -> dict[str, Any]:
        s = config.load_settings(area_id=self.area_id)
        with context.use_area(self._area()):
            watched = sorted(pipeline.watched_channel_ids())
        return {
            **self._status,
            "enabled": bool(s.get("discord_enabled")),
            "dry_run": bool(s.get("discord_dry_run")),
            "has_token": bool(s.get("discord_user_token")),
            "library_available": self.library_available(),
            "library_error": self._lib_error or "",
            "watched_channels": watched,
        }

    def _set_status(self, **fields: Any) -> None:
        self._status.update(fields)

    # ------------------------------------------------------------ client build
    def _build_client(self) -> Any:
        discord = self._discord
        manager = self

        class _SignalClient(discord.Client):  # type: ignore[misc]
            async def on_ready(self) -> None:
                user = str(getattr(self, "user", "") or "")
                manager._set_status(state="connected", connected=True, error="", user=user)
                state.log_event("info", f"[discord] listener connected as {user}")

            async def on_disconnect(self) -> None:
                # The library auto-reconnects; just reflect the transient drop.
                if manager._status.get("state") == "connected":
                    manager._set_status(connected=False, state="connecting")

            async def on_message(self, message: Any) -> None:
                await manager._handle_message(message, source="message")

            async def on_message_edit(self, before: Any, after: Any) -> None:
                # Some signal bots edit an existing message instead of posting anew.
                await manager._handle_message(after, source="edit")

        return _SignalClient()

    async def _handle_message(self, message: Any, *, source: str) -> None:
        received = time.monotonic()  # capture ASAP for the latency measurement
        with context.use_area(self._area()):  # this listener's area
            try:
                channel_id = str(getattr(getattr(message, "channel", None), "id", "") or "")
                if not channel_id or channel_id not in pipeline.watched_channel_ids():
                    return
                embeds = list(getattr(message, "embeds", None) or [])
                if not embeds:
                    return
                for raw in embeds:
                    embed = embed_from_discord(raw)
                    event = await pipeline.process_embed(
                        embed, channel_id, source=source, received_monotonic=received
                    )
                    if event:
                        self._set_status(
                            last_event_ts=event.get("ts", ""),
                            last_event_channel=event.get("channel_label", ""),
                        )
            except Exception as exc:  # noqa: BLE001 - a handler error must not kill the client
                state.log_event("warn", f"[discord] message handler error: {exc}")

    # -------------------------------------------------------------- lifecycle
    async def _run_client(self, token: str) -> None:
        """Run one client connection until it disconnects or is closed."""
        self._current_token = token
        self._client = self._build_client()
        self._set_status(state="connecting", connected=False, error="")
        try:
            await self._client.start(token)
        finally:
            try:
                if not self._client.is_closed():
                    await self._client.close()
            except Exception:  # noqa: BLE001
                pass
            self._client = None
            self._current_token = None

    async def _close_client(self) -> None:
        client = self._client
        if client is not None:
            try:
                await client.close()
            except Exception:  # noqa: BLE001
                pass

    async def _supervisor(self) -> None:
        """Reconcile desired (config) vs actual client state, forever, safely."""
        context.set_area(self._area())  # this task (and its child tasks) run in the area
        while not self._shutdown:
            try:
                s = config.load_settings(area_id=self.area_id)
                enabled = bool(s.get("discord_enabled"))
                token = s.get("discord_user_token") or ""

                if not self.library_available():
                    self._set_status(
                        state="library_missing", connected=False, error=self._lib_error or "",
                    )
                    await asyncio.sleep(15)
                    continue

                if not enabled or not token:
                    self._set_status(state="disabled", connected=False, error="")
                    await asyncio.sleep(3)
                    continue

                # Enabled + have a token + library present -> run a client.
                await self._run_client(token)

                # Client returned (disconnected / closed). If still wanted, retry.
                if not self._shutdown:
                    self._set_status(connected=False)
                    await asyncio.sleep(5)  # backoff before reconnect attempt
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - never let the supervisor die
                self._set_status(state="error", connected=False, error=str(exc))
                state.log_event("warn", f"[discord] supervisor error: {exc}")
                await asyncio.sleep(5)

    # --------------------------------------------------------------- controls
    def start(self) -> None:
        """Launch the supervisor task (idempotent). Called at app startup."""
        if self._supervisor_task and not self._supervisor_task.done():
            return
        self._shutdown = False
        self._supervisor_task = asyncio.create_task(self._supervisor())

    async def apply_config(self) -> None:
        """React to a settings change: (re)connect or disconnect as needed.

        The supervisor loop does the actual (re)connect; here we just nudge it by
        closing the current client when the token changed or the listener was
        disabled, so it re-evaluates immediately instead of on its next poll.
        """
        s = config.load_settings(area_id=self.area_id)
        enabled = bool(s.get("discord_enabled"))
        token = s.get("discord_user_token") or ""
        if not enabled or not token:
            await self._close_client()
            return
        if self._current_token is not None and token != self._current_token:
            await self._close_client()  # supervisor reconnects with the new token
        # If nothing is running yet, make sure the supervisor exists.
        self.start()

    async def shutdown(self) -> None:
        """Stop the listener cleanly (app shutdown)."""
        self._shutdown = True
        await self._close_client()
        if self._supervisor_task:
            self._supervisor_task.cancel()
            try:
                await self._supervisor_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass


# One ListenerManager per area (user workspace).
import threading as _threading

_managers: dict[int, ListenerManager] = {}
_managers_lock = _threading.Lock()


def manager_for(area_id: int) -> ListenerManager:
    with _managers_lock:
        m = _managers.get(area_id)
        if m is None:
            m = _managers[area_id] = ListenerManager(area_id)
        return m


def all_managers() -> list[ListenerManager]:
    with _managers_lock:
        return list(_managers.values())

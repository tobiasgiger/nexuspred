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
import random
import time
from typing import Any, Optional

from .. import alerts, config, context, db, state
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
        self._reconnect_attempts = 0  # for exponential backoff between full reconnects
        self._recent: dict[str, float] = {}  # (msg id+content) -> ts, for edit de-dup
        # Health tracking: last time we were connected, and whether we've already
        # fired an "offline" alert (so the outage/restore pair alerts once each).
        self._last_connected_mono: float = time.monotonic()
        self._health_down = False
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
            "health": self.health(),
        }

    def _set_status(self, **fields: Any) -> None:
        self._status.update(fields)

    def _desired(self, s: dict[str, Any]) -> bool:
        """Is the listener supposed to be connected right now?"""
        try:
            entitled = bool(db.get_area_features(self._area()).get("discord_signals"))
        except Exception:  # noqa: BLE001
            entitled = True
        return bool(entitled and s.get("discord_enabled") and s.get("discord_user_token")
                    and self.library_available())

    async def health_tick(self) -> None:
        """Evaluate connection health and fire lost/restored alerts once each.

        Called periodically by the health loop (in this area's context). A grace
        period keeps the library's normal transient reconnects from alerting;
        only a sustained outage of a *wanted* connection counts.
        """
        s = config.load_settings(area_id=self.area_id)
        now = time.monotonic()
        connected = bool(self._status.get("connected"))
        if connected:
            self._last_connected_mono = now
            if self._health_down:
                self._health_down = False
                with context.use_area(self._area()):
                    await alerts.discord_listener_restored(self._status.get("user", ""))
            return
        if not self._desired(s):
            # Not meant to be connected (disabled / no token / not entitled): a
            # gap here is expected, so reset the clock and clear any outage flag.
            self._health_down = False
            self._last_connected_mono = now
            return
        grace = float(s.get("discord_health_grace", 90) or 90)
        if (now - self._last_connected_mono) >= grace and not self._health_down:
            self._health_down = True
            with context.use_area(self._area()):
                await alerts.discord_listener_lost(self._status.get("error", ""))

    def health(self) -> str:
        """Coarse health label for the dashboard: ok | connecting | down | idle."""
        s = config.load_settings(area_id=self.area_id)
        if self._status.get("connected"):
            return "ok"
        if not self._desired(s):
            return "idle"
        return "down" if self._health_down else "connecting"

    # ------------------------------------------------------------ client build
    def _build_client(self) -> Any:
        discord = self._discord
        manager = self

        class _SignalClient(discord.Client):  # type: ignore[misc]
            async def on_ready(self) -> None:
                user = str(getattr(self, "user", "") or "")
                manager._mark_connected(user=user, how="connected")

            async def on_resumed(self) -> None:
                # A transient drop was recovered by RESUMING the session. The
                # library fires this (NOT on_ready), so we must mark ourselves
                # connected again here — otherwise the status would stay stuck on
                # "connecting" after the first blip even though the gateway is up.
                manager._mark_connected(how="resumed")

            async def on_connect(self) -> None:
                # Socket connected (before READY). Reflect progress, not "down".
                if not manager._status.get("connected"):
                    manager._set_status(state="connecting", error="")

            async def on_disconnect(self) -> None:
                # A socket drop. The library auto-reconnects/resumes; reflect the
                # transient state but don't treat it as an outage — the health
                # loop's grace period + a missing resume is what flags a real one.
                if manager._status.get("connected"):
                    manager._set_status(connected=False, state="connecting")

            async def on_message(self, message: Any) -> None:
                manager._mark_connected(how="message")  # receiving = definitely up
                await manager._handle_message(message, source="message")

            async def on_message_edit(self, before: Any, after: Any) -> None:
                # Some signal bots edit an existing message instead of posting anew.
                await manager._handle_message(after, source="edit")

        return _SignalClient()

    def _mark_connected(self, *, user: str | None = None, how: str = "connected") -> None:
        """Record a healthy connection (ready / resumed / traffic) and reset backoff."""
        was_connected = bool(self._status.get("connected"))
        fields: dict[str, Any] = {"state": "connected", "connected": True, "error": ""}
        if user is not None:
            fields["user"] = user
        self._set_status(**fields)
        self._reconnect_attempts = 0
        self._last_connected_mono = time.monotonic()
        if not was_connected and how in ("connected", "resumed"):
            who = user or self._status.get("user", "")
            verb = "connected" if how == "connected" else "resumed"
            state.log_event("info", f"[discord] listener {verb}"
                            + (f" as {who}" if who else ""))

    def _is_duplicate(self, message: Any, embed: Any) -> bool:
        """True if this exact (message id + embed content) was already handled.

        Providers post a message and then EDIT it (e.g. to attach a GIF), which
        fires on_message *and* on_message_edit for the same signal — without this
        guard the trade would be placed twice. Keyed by message id + a hash of the
        embed's title/fields, so a genuinely changed edit still gets through while
        an identical re-render is ignored. Entries expire after 1 hour."""
        mid = str(getattr(message, "id", "") or "")
        if not mid:
            return False
        fp = f"{embed.title}|" + "|".join(f"{f.name}={f.value}" for f in embed.fields)
        key = f"{mid}:{hash(fp)}"
        now = time.monotonic()
        # prune old entries so the dict can't grow unbounded
        if len(self._recent) > 512:
            for k, ts in list(self._recent.items()):
                if now - ts > 3600:
                    self._recent.pop(k, None)
        if key in self._recent and now - self._recent[key] < 3600:
            return True
        self._recent[key] = now
        return False

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
                    if self._is_duplicate(message, embed):
                        continue  # message + its edit fire twice for one signal
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
    def _is_auth_error(self, exc: BaseException) -> bool:
        """Whether an exception means Discord rejected the token (don't hammer)."""
        d = self._discord
        login_failure = getattr(d, "LoginFailure", None)
        if login_failure and isinstance(exc, login_failure):
            return True
        http = getattr(d, "HTTPException", None)
        if http and isinstance(exc, http) and getattr(exc, "status", None) in (401, 403):
            return True
        return False

    async def _run_client(self, token: str) -> None:
        """Run one client connection until it disconnects or is closed.

        ``reconnect=True`` keeps discord.py's own resume/reconnect loop running,
        so this only returns on a hard failure — where the supervisor backs off."""
        self._current_token = token
        self._client = self._build_client()
        self._set_status(state="connecting", connected=False, error="")
        try:
            await self._client.start(token, reconnect=True)
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
                # Admin-controlled entitlement: if this area isn't granted the
                # Discord Signals feature, stay idle no matter its own settings.
                try:
                    entitled = bool(db.get_area_features(self._area()).get("discord_signals"))
                except Exception:  # noqa: BLE001 - never let a db hiccup crash the loop
                    entitled = True
                if not entitled:
                    self._set_status(state="not_entitled", connected=False, error="")
                    await asyncio.sleep(10)
                    continue

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

                # Enabled + have a token + library present -> run a client. Its
                # own reconnect loop handles transient drops; this returns/raises
                # only on a hard failure, which we back off from below.
                auth_failed = False
                try:
                    await self._run_client(token)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    auth_failed = self._is_auth_error(exc)
                    if auth_failed:
                        self._set_status(state="token_invalid", connected=False,
                                         error="Discord rejected the token — re-extract it "
                                               "(Settings → Discord Listener).")
                        state.log_event("warn", "[discord] token rejected by Discord — "
                                        "listener paused until the token is updated")
                    else:
                        self._set_status(state="error", connected=False, error=str(exc))
                        state.log_event("warn", f"[discord] connection error: {exc}")

                if self._shutdown:
                    break

                # Back off before reconnecting: jittered exponential (3→60s). This
                # is essential for a self-bot — reconnecting in a tight loop makes
                # Discord rate-limit the token, which itself causes more drops. A
                # rejected token backs off hard so we never hammer identify.
                self._reconnect_attempts += 1
                step = min(self._reconnect_attempts - 1, 5)
                base = 60.0 if auth_failed else min(60.0, 3.0 * (2 ** step))
                delay = base * (0.5 + random.random())  # 0.5x–1.5x jitter
                self._set_status(connected=False)
                await asyncio.sleep(min(delay, 90.0))
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

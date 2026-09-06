"""Background loops: proactive Tradovate token renewal / connection checks and
Discord listener health, for every area concurrently."""
from __future__ import annotations

import asyncio

from . import config, context, db, rollover, state, tradovate
from .discord_signals import listener as discord_listener


async def _refresh_session(sess) -> float:
    """Proactively renew one session's token and verify it; return next-check delay."""
    interval = int(config.load_settings().get("health_check_interval", 60) or 60)
    try:
        if sess.has_token():
            await sess.proactive_refresh()    # renew well before expiry (never lapse)
        await sess.health_check()
        ok = bool(state.session_status(sess.name).get("connected"))
    except Exception as exc:  # noqa: BLE001 - never let the loop die
        state.log_event("warn", f"[{sess.name}] refresh error: {exc}")
        ok = False
    return sess.seconds_until_refresh(fallback=interval) if ok else 60.0


async def _health_area(area_id: int) -> list[float]:
    """One health cycle for one area: keep its Discord supervisor alive and
    refresh every session concurrently. Returns the sessions' next-check delays."""
    with context.use_area(area_id):
        # Make sure every area has a Discord supervisor (idempotent).
        try:
            discord_listener.manager_for(area_id).start()
        except Exception:  # noqa: BLE001
            pass
        interval = int(config.load_settings(area_id=area_id).get("health_check_interval", 60) or 0)
        if interval <= 0:
            return []
        mgr = tradovate.manager_for(area_id)
        mgr.reload()  # diff-based: unchanged logins keep their session
        sessions = mgr.all()
        if not sessions:
            return []
        delays = await asyncio.gather(*(_refresh_session(s) for s in sessions),
                                      return_exceptions=True)
        return [d for d in delays if isinstance(d, (int, float))]


async def health_loop() -> None:
    """Keep every area's account tokens alive proactively — all areas in parallel
    (v4 walked them one after another, so one slow login delayed everyone)."""
    while True:
        try:
            area_ids = db.all_area_ids()
        except Exception:  # noqa: BLE001
            area_ids = []
        results = await asyncio.gather(*(_health_area(a) for a in area_ids), return_exceptions=True)
        next_delays = [d for r in results if isinstance(r, list) for d in r]
        # Once a day per area: are the mapped contracts about to roll? (Runs after
        # the sessions were refreshed so the broker's exact expiry can be used.)
        try:
            await rollover.check_all()
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(min(next_delays) if next_delays else 30.0)


async def _discord_tick(area_id: int) -> None:
    try:
        with context.use_area(area_id):
            await discord_listener.manager_for(area_id).health_tick()
    except Exception:  # noqa: BLE001 - a health tick must never crash the loop
        pass


async def discord_health_loop() -> None:
    """Evaluate every area's Discord listener health on a steady cadence and fire
    lost/restored alerts. Kept separate from the token health loop (which paces
    itself to token expiry, sometimes minutes apart) so outages surface quickly."""
    while True:
        try:
            area_ids = db.all_area_ids()
        except Exception:  # noqa: BLE001
            area_ids = []
        await asyncio.gather(*(_discord_tick(a) for a in area_ids))
        await asyncio.sleep(30.0)


def start_discord_listeners() -> None:
    """Start a Discord listener supervisor per existing area (isolated tasks; a
    Discord failure can never crash order execution). New areas are picked up
    by the health loop."""
    try:
        area_ids = db.all_area_ids()
        for area_id in area_ids:
            discord_listener.manager_for(area_id).start()
        if area_ids and not discord_listener.manager_for(area_ids[0]).library_available():
            state.log_event(
                "warn",
                "[discord] listener library not installed (discord.py-self) — "
                "module idle. Install it to enable the Discord signal listener.",
            )
    except Exception as exc:  # noqa: BLE001 - never let module startup break the app
        state.log_event("warn", f"[discord] listener startup failed: {exc}")


async def stop_discord_listeners() -> None:
    for m in discord_listener.all_managers():
        try:
            await m.shutdown()
        except Exception as exc:  # noqa: BLE001
            state.log_event("warn", f"[discord] shutdown error: {exc}")

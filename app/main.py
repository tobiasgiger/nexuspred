"""FastAPI application: auth middleware, lifespan (background loops, HTTP
pool), and the routers under :mod:`app.routers` + the Discord module.

Run with a single uvicorn worker: runtime state (sessions, active trades, live
streams) is in-process by design."""
from __future__ import annotations

import asyncio
import contextlib
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import auth, config, context, db, health, http, state
from .discord_signals.routes import router as discord_router
from .routers import ROUTERS
from .web import AUTH_EXEMPT, BASE_DIR, wants_html

_loop_tasks: list[asyncio.Task] = []


async def _startup() -> None:
    db.init()
    # Default each area's alert "Notify email" to its owner's address where unset.
    try:
        if db.backfill_alert_emails():
            for aid in db.all_area_ids():
                config.invalidate(aid)
    except Exception as exc:  # noqa: BLE001 - never let a migration block startup
        state.log_event("warn", f"alert-email backfill failed: {exc}")
    state.log_event("info", f"Bridge started (v{config.get_version()})")
    _loop_tasks[:] = [asyncio.create_task(health.health_loop(), name="health-loop"),
                      asyncio.create_task(health.discord_health_loop(), name="discord-health-loop")]
    health.start_discord_listeners()


async def _shutdown() -> None:
    """Stop the background loops and Discord listeners; close the HTTP pool."""
    for t in _loop_tasks:
        t.cancel()
    for t in _loop_tasks:
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await t
    _loop_tasks.clear()
    await health.stop_discord_listeners()
    await http.aclose_all()


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    await _startup()
    try:
        yield
    finally:
        await _shutdown()


app = FastAPI(title="Fluxbridge", version=config.get_version(), lifespan=_lifespan)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
for _router in ROUTERS:
    app.include_router(_router)
app.include_router(discord_router)  # Discord signal module (same server + auth)


@app.middleware("http")
async def _auth_middleware(request: Request, call_next):
    """Require a login session; set the request's area context to the user's area."""
    path = request.url.path
    if path.startswith(AUTH_EXEMPT):
        return await call_next(request)

    if db.user_count() == 0:  # first run: force admin setup
        if wants_html(request):
            return RedirectResponse("/setup", status_code=302)
        return JSONResponse({"detail": "Setup required"}, status_code=503)

    user = auth.current_user(request)
    if not user:
        if wants_html(request):
            return RedirectResponse("/login", status_code=302)
        return JSONResponse({"detail": "Authentication required"}, status_code=401)

    area = db.user_primary_area(user["id"]) or context.DEFAULT_AREA_ID
    request.state.user = user
    request.state.area_id = area
    tok = context.set_area(area)
    try:
        return await call_next(request)
    finally:
        context.reset_area(tok)

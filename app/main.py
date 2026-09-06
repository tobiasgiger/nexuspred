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

from . import auth, config, context, crypto, db, health, http, security, state
from .discord_signals.routes import router as discord_router
from .routers import ROUTERS
from .web import BASE_DIR, is_auth_exempt, wants_html

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
    # Encrypt secrets written by earlier versions (idempotent, one pass).
    try:
        if db.encrypt_existing_settings():
            config.invalidate()
        if crypto.key_source() == "db":
            state.log_event("warn", "Secrets are encrypted with the auto-generated key stored in the "
                                    "database. Set NEXUSPRED_ENCRYPTION_KEY (or SESSION_SECRET) in the "
                                    "environment so the key lives outside the DB file.")
    except Exception as exc:  # noqa: BLE001 - never block startup on the migration
        state.log_event("warn", f"secret encryption pass failed: {exc}")
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


class _Static(StaticFiles):
    """Static files whose scripts/styles always revalidate (ETag → 304), so a
    deploy never leaves a browser with a stale ES module next to a fresh one."""

    async def get_response(self, path: str, scope):  # type: ignore[override]
        resp = await super().get_response(path, scope)
        if path.endswith((".js", ".css")):
            resp.headers["Cache-Control"] = "no-cache"
        return resp


app = FastAPI(title="Fluxbridge", version=config.get_version(), lifespan=_lifespan)
app.mount("/static", _Static(directory=str(BASE_DIR / "static")), name="static")
for _router in ROUTERS:
    app.include_router(_router)
app.include_router(discord_router)  # Discord signal module (same server + auth)


@app.middleware("http")
async def _auth_middleware(request: Request, call_next):
    """Require a login session; set the request's area context to the user's area."""
    path = request.url.path
    if is_auth_exempt(path):
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


# Outermost first: body cap → CSRF/rate-limit/headers → auth. (Starlette runs
# ``@app.middleware`` decorators innermost-last, so this one wraps the auth one.)
app.middleware("http")(security.security_middleware)
app.add_middleware(security.BodyLimitMiddleware)

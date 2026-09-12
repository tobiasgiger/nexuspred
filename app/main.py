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

from . import (auth, config, context, copy, copy_bindings, crypto, db, drawdown, health,
               history, http, journal, journal_other, marketplace_safety, news, pnl,
               push, security, state, watchdog)
from .discord_signals.routes import router as discord_router
from .routers import ROUTERS
from .web import BASE_DIR, is_auth_exempt, mfa_setup_allowed, wants_html

_loop_tasks: list[asyncio.Task] = []


async def _startup() -> None:
    db.init()
    journal_other.install()
    marketplace_safety.install()

    # Repair persisted copy bindings and stale marketplace ACL state before any
    # live copy runner starts. Positions are never touched during startup repair.
    try:
        for aid in db.all_area_ids():
            copy_bindings.repair(aid)
        await marketplace_safety.reconcile_all(sync=False)
    except Exception as exc:  # noqa: BLE001
        state.log_event("warn", f"copy/marketplace safety repair failed: {exc}")

    try:
        if db.backfill_alert_emails():
            for aid in db.all_area_ids():
                config.invalidate(aid)
    except Exception as exc:  # noqa: BLE001
        state.log_event("warn", f"alert-email backfill failed: {exc}")
    try:
        if not db.meta_get("journal_dedupe_v1"):
            for aid in db.all_area_ids():
                n = db.dedupe_journal_trades(aid)
                if n:
                    with context.use_area(aid):
                        state.log_event("info", f"Journal: removed {n} duplicate trade(s) stored by earlier imports")
            db.meta_set("journal_dedupe_v1", "done")
    except Exception as exc:  # noqa: BLE001
        state.log_event("warn", f"journal dedupe failed: {exc}")
    try:
        try:
            push.available() and push.public_key()
        except Exception:  # noqa: BLE001
            pass
        if db.encrypt_existing_settings():
            config.invalidate()
        if crypto.key_source() == "db":
            state.log_event("warn", "Secrets are encrypted with the auto-generated key stored in the "
                                    "database. Set NEXUSPRED_ENCRYPTION_KEY (or SESSION_SECRET) in the "
                                    "environment so the key lives outside the DB file.")
    except Exception as exc:  # noqa: BLE001
        state.log_event("warn", f"secret encryption pass failed: {exc}")
    try:
        db.prune_copy_events()
        pruned = history.prune()
        loaded = history.hydrate(db.all_area_ids())
        if loaded or pruned:
            state.log_event("info", f"History: {loaded} signal/order rows restored"
                                    + (f", {pruned} expired rows pruned" if pruned else ""))
    except Exception as exc:  # noqa: BLE001
        state.log_event("warn", f"history restore failed: {exc}")
    history.start()
    state.log_event("info", f"Bridge started (v{config.get_version()})")
    _loop_tasks[:] = [asyncio.create_task(health.health_loop(), name="health-loop"),
                      asyncio.create_task(health.discord_health_loop(), name="discord-health-loop"),
                      asyncio.create_task(_history_prune_loop(), name="history-prune-loop"),
                      asyncio.create_task(journal.scheduler_loop(), name="journal-import-loop"),
                      asyncio.create_task(pnl.pnl_loop(), name="pnl-loop"),
                      asyncio.create_task(copy.copy_loop(), name="copy-loop"),
                      asyncio.create_task(news.news_loop(), name="news-loop"),
                      asyncio.create_task(watchdog.heartbeat_loop(), name="heartbeat-loop")]
    health.start_discord_listeners()


async def _history_prune_loop() -> None:
    while True:
        await asyncio.sleep(24 * 3600)
        try:
            await asyncio.to_thread(history.prune)
        except Exception as exc:  # noqa: BLE001
            state.log_event("warn", f"history prune failed: {exc}")


async def _shutdown() -> None:
    for t in _loop_tasks:
        t.cancel()
    for t in _loop_tasks:
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await t
    _loop_tasks.clear()
    await copy.stop_all()
    with contextlib.suppress(Exception):
        await asyncio.to_thread(drawdown.flush)
    await health.stop_discord_listeners()
    await http.aclose_all()
    await asyncio.to_thread(history.stop)


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    await _startup()
    try:
        yield
    finally:
        await _shutdown()


class _Static(StaticFiles):
    async def get_response(self, path: str, scope):  # type: ignore[override]
        resp = await super().get_response(path, scope)
        if path.endswith(".js"):
            resp.headers["Cache-Control"] = "no-store"
        elif path.endswith(".css"):
            resp.headers["Cache-Control"] = "no-cache"
        return resp


app = FastAPI(title="Fluxbridge", version=config.get_version(), lifespan=_lifespan)
app.mount("/static", _Static(directory=str(BASE_DIR / "static")), name="static")
for _router in ROUTERS:
    app.include_router(_router)
app.include_router(discord_router)


@app.middleware("http")
async def _auth_middleware(request: Request, call_next):
    path = request.url.path
    if is_auth_exempt(path):
        return await call_next(request)

    if db.user_count() == 0:
        if wants_html(request):
            return RedirectResponse("/setup", status_code=302)
        return JSONResponse({"detail": "Setup required"}, status_code=503)

    user = auth.current_user(request)
    if not user:
        if wants_html(request):
            return RedirectResponse("/login", status_code=302)
        return JSONResponse({"detail": "Authentication required"}, status_code=401)

    if user.get("totp_required") and not user.get("totp_enabled") and not mfa_setup_allowed(path):
        if wants_html(request):
            return RedirectResponse("/2fa/setup", status_code=302)
        return JSONResponse({"detail": "Two-factor setup required"}, status_code=403)
    area = db.user_primary_area(user["id"]) or context.DEFAULT_AREA_ID
    request.state.user = user
    request.state.area_id = area
    tok = context.set_area(area)
    try:
        response = await call_next(request)
        if response.status_code < 400:
            # Connect & Verify can rediscover broker account ids. Repair every
            # stored/runtime copy binding before returning control to the user.
            if request.method == "POST" and path == "/api/connect":
                await copy_bindings.refresh(area)
            # A selected-user ACL change must remove revoked copy followers now,
            # not on the next process restart or only in the marketplace UI.
            if request.method == "PUT" and path.startswith("/api/copy/groups/") and path.endswith("/sharing"):
                group_id = path[len("/api/copy/groups/"):-len("/sharing")].strip("/")
                if group_id:
                    await marketplace_safety.reconcile_copy_group(area, group_id)
        return response
    finally:
        context.reset_area(tok)


app.middleware("http")(security.security_middleware)
app.add_middleware(security.BodyLimitMiddleware)


@app.exception_handler(config.SettingsUnavailable)
async def _settings_unavailable(_request: Request, exc: config.SettingsUnavailable) -> JSONResponse:
    return JSONResponse({"detail": f"{exc} — check the database and try again"}, status_code=503)

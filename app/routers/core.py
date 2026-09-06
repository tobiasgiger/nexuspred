"""Dashboard shell, liveness, status/settings, logs, the live event stream,
positions and connection checks."""
from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, HTMLResponse, Response, StreamingResponse

from .. import alerts, config, context, db, signals, state, tradovate
from ..tradovate import TradovateError
from ..web import BASE_DIR, templates
from .accounts import trade_accounts_overview

router = APIRouter(tags=["core"])


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        "dashboard.html",
        {"request": request, "version": config.get_version()},
    )


@router.get("/favicon.ico")
async def favicon() -> Response:
    """Serve a tiny inline SVG favicon (matches the ◈ brand mark)."""
    svg = (
        "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'>"
        "<rect width='32' height='32' rx='6' fill='#0b0e14'/>"
        "<path d='M16 5l11 11-11 11L5 16z' fill='#4f8cff'/></svg>"
    )
    return Response(content=svg, media_type="image/svg+xml")


@router.get("/healthz")
async def healthz() -> dict[str, Any]:
    """Unauthenticated liveness probe (for Render/uptime checks)."""
    return {"ok": True, "version": config.get_version()}


@router.get("/guide", response_class=HTMLResponse)
async def guide() -> FileResponse:
    """Standalone, self-contained setup guide page."""
    return FileResponse(str(BASE_DIR / "docs" / "setup-guide.html"))


# ------------------------------------------------------------ status / settings
@router.get("/api/status")
async def api_status() -> dict[str, Any]:
    return {
        "version": config.get_version(),
        "connection": state.aggregate_connection(),
        "sessions": state.session_statuses(),
        "trade_accounts": trade_accounts_overview(),
        "active_trades": signals.active_trades(),
        "trading_enabled": config.load_settings().get("trading_enabled", False),
    }


@router.get("/api/settings")
async def api_get_settings(request: Request) -> dict[str, Any]:
    s = config.public_settings()
    # Default the alert notify-email to the signed-in user's own address when it
    # hasn't been set, so the field is pre-filled per-user (they can override it).
    if not s.get("alert_email_to"):
        user = getattr(request.state, "user", None)
        if user:
            s["alert_email_to"] = user.get("email", "")
    return s


@router.post("/api/settings")
async def api_save_settings(request: Request) -> dict[str, Any]:
    updates = await request.json()
    # Drop masked secret fields so we don't overwrite stored secrets with "********".
    for field in config.SECRET_FIELDS:
        if updates.get(field) == "********":
            updates.pop(field, None)
    updates.pop("token_accounts", None)  # managed via /api/token-accounts
    config.save_settings(updates)
    state.log_event("info", "Settings updated")
    return config.public_settings()


@router.post("/api/flatten-all")
async def api_flatten_all(request: Request) -> dict[str, Any]:
    """Emergency kill-switch: flatten every position and cancel every working order
    on all trade accounts in the caller's area. Runs even if trading is paused."""
    user = getattr(request.state, "user", None)
    result = await signals.flatten_all()
    if user:
        db.log_action(user["id"], user["email"], "flatten_all", "",
                      f"{result.get('flattened', 0)} flattened, "
                      f"{result.get('cancelled', 0)} cancelled, "
                      f"{result.get('accounts', 0)} account(s)")
    return result


@router.post("/api/alerts/test")
async def api_test_alert() -> dict[str, Any]:
    """Send a test notification on every enabled channel (Discord / email)."""
    channels = await alerts.test_alert()
    if not any(channels.values()):
        return {"status": "none", "channels": channels,
                "detail": "No alert channel is enabled and fully configured."}
    return {"status": "sent", "channels": channels}


# ------------------------------------------------------------------- logs
@router.get("/api/signals")
async def api_signals() -> list[dict[str, Any]]:
    return state.recent_signals()


@router.get("/api/orders")
async def api_orders() -> list[dict[str, Any]]:
    return state.recent_orders()


@router.get("/api/events")
async def api_events() -> list[dict[str, Any]]:
    return state.recent_events()


@router.get("/api/stream")
async def api_stream(request: Request) -> StreamingResponse:
    """Server-Sent Events: the area's live feed (no polling).

    Each message is ``{"kind": "event"|"signal"|"order"|"session"|"discord",
    "data": {...}}``. The connection is scoped to the logged-in user's area,
    captured before the generator starts (it runs after the request's area
    context has been reset)."""
    area = context.get_area()
    sub = state.subscribe(area)

    async def gen():
        try:
            yield ": connected\n\n"  # prime so proxies flush headers
            yield "event: ping\ndata: {}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    msg = await asyncio.wait_for(sub.queue.get(), timeout=10.0)
                    yield f"data: {json.dumps(msg)}\n\n"
                except asyncio.TimeoutError:
                    yield "event: ping\ndata: {}\n\n"  # named heartbeat; keeps proxies open
        finally:
            state.unsubscribe(sub, area)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive",
                 "X-Accel-Buffering": "no"},
    )


# ------------------------------------------------------------ broker checks
@router.get("/api/positions")
async def api_positions() -> Any:
    """Open positions across every enabled account, fetched concurrently."""
    async def one(ex) -> list[dict[str, Any]]:
        try:
            return await ex.positions()
        except TradovateError:
            return []

    results = await asyncio.gather(*(one(ex) for ex in tradovate.manager().enabled()))
    return [p for chunk in results for p in chunk]


@router.post("/api/connect")
async def api_connect() -> dict[str, Any]:
    """Connect & verify all configured accounts (in parallel)."""
    mgr = tradovate.manager()
    mgr.reload()
    sessions = mgr.all()
    await asyncio.gather(*(s.connect() for s in sessions), return_exceptions=True)
    return {"sessions": state.session_statuses()}


@router.get("/api/health")
async def api_health() -> dict[str, Any]:
    """On-demand health check of every configured account (current area)."""
    await asyncio.gather(*(s.health_check() for s in tradovate.manager().all()),
                         return_exceptions=True)
    return {"sessions": state.session_statuses()}

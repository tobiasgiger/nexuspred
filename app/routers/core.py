"""Dashboard shell, liveness, status/settings, logs, the live event stream,
positions and connection checks."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse

from .. import alerts, config, context, db, news, pnl, rollover, security, signals, state, tradovate
from ..tradovate import TradovateError
from ..web import BASE_DIR, render
from .accounts import trade_accounts_overview

router = APIRouter(tags=["core"])


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request) -> HTMLResponse:
    return render(request, "shell.html", {"version": config.get_version()})


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
        "public_url": config.PUBLIC_URL,
        "rollover": state.rollover_warnings(),
        "pnl": state.pnl(),
        "news_lock": news.status(),
    }


@router.get("/api/rollover")
async def api_rollover(refresh: bool = False) -> dict[str, Any]:
    """Current rollover warnings with the proposed next contracts (``?refresh=1``
    re-runs the check first)."""
    if refresh:
        return {"rollover": await rollover.check_area(context.get_area(), force=True)}
    return {"rollover": state.rollover_warnings()}


@router.post("/api/rollover/apply")
async def api_rollover_apply(request: Request) -> dict[str, Any]:
    """Confirm a rollover: switch the given TradingView symbols to their new
    contracts, then re-check."""
    body = await request.json()
    items = body.get("items") if isinstance(body, dict) else None
    if not isinstance(items, list) or not items:
        raise HTTPException(status_code=400, detail="items required")
    try:
        result = rollover.apply(context.get_area(), items)
    except (TypeError, ValueError, AttributeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    result["rollover"] = await rollover.check_area(context.get_area(), force=True)
    return result


@router.post("/api/rollover/check")
async def api_rollover_check() -> dict[str, Any]:
    """Re-run the contract-rollover check for the caller's area right now
    (e.g. after editing the symbol map)."""
    warnings = await rollover.check_area(context.get_area(), force=True)
    return {"rollover": warnings}


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
    if not isinstance(updates, dict):
        raise HTTPException(status_code=400, detail="Settings must be a JSON object")
    # Drop masked secret fields so we don't overwrite stored secrets with "********".
    for field in config.SECRET_FIELDS:
        if updates.get(field) == "********":
            updates.pop(field, None)
    # Webhooks, token accounts and the Discord listener have their own validating
    # endpoints; the generic form must not be able to write them.
    for field in config.SETTINGS_PROTECTED_KEYS:
        updates.pop(field, None)
    if "ui_language" in updates:
        if str(updates.get("ui_language") or "auto") not in ("auto", "de", "en"):
            raise HTTPException(status_code=400, detail="ui_language must be auto, de or en")
        updates["ui_language"] = str(updates.get("ui_language") or "auto")
    if "journal_import_time" in updates:
        raw = str(updates.get("journal_import_time") or "23:30").strip()
        parts = raw.split(":")
        if len(parts) != 2 or not all(x.isdigit() for x in parts) or not (0 <= int(parts[0]) < 24 and 0 <= int(parts[1]) < 60):
            raise HTTPException(status_code=400, detail="Journal import time must be HH:MM")
        updates["journal_import_time"] = f"{int(parts[0]):02d}:{int(parts[1]):02d}"
    if "alert_accounts" in updates:
        raw = updates.get("alert_accounts")
        if raw is None:
            raw = []
        if not isinstance(raw, list) or len(raw) > 500 or not all(isinstance(x, str) and len(x) <= 64 for x in raw):
            raise HTTPException(status_code=400, detail="alert_accounts must be a list of account names")
        updates["alert_accounts"] = sorted({x.strip() for x in raw if x.strip()})
    if "daily_summary_time" in updates:
        raw = str(updates.get("daily_summary_time") or "22:05").strip()
        parts = raw.split(":")
        if len(parts) != 2 or not all(x.isdigit() for x in parts) or not (0 <= int(parts[0]) < 24 and 0 <= int(parts[1]) < 60):
            raise HTTPException(status_code=400, detail="Daily summary time must be HH:MM")
        updates["daily_summary_time"] = f"{int(parts[0]):02d}:{int(parts[1]):02d}"
    if "journal_timezone" in updates:
        from zoneinfo import ZoneInfo
        name = str(updates.get("journal_timezone") or "Europe/Zurich").strip()
        try:
            ZoneInfo(name)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=f"Unknown timezone '{name}' (use an IANA name like Europe/Zurich)") from exc
        updates["journal_timezone"] = name
    url = str(updates.get("alert_discord_webhook_url") or "").strip()
    if url:
        problem = await asyncio.to_thread(security.check_outbound_url, url)
        if problem:
            raise HTTPException(status_code=400, detail=f"Discord webhook URL: {problem}")
        updates["alert_discord_webhook_url"] = url
    if "alert_smtp_host" in updates or "alert_smtp_port" in updates:
        current = config.load_settings()
        host = str(updates.get("alert_smtp_host") or current.get("alert_smtp_host") or "").strip()
        port = updates.get("alert_smtp_port", current.get("alert_smtp_port") or 587)
        try:
            port = int(port)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="SMTP port must be a number")
        if not (1 <= port <= 65535):
            raise HTTPException(status_code=400, detail="SMTP port must be between 1 and 65535")
        if host:
            problem = await asyncio.to_thread(security.check_outbound_url, f"https://{host}:{port}/")
            if problem:
                raise HTTPException(status_code=400, detail=f"SMTP host: {problem}")
        updates["alert_smtp_host"] = host
        updates["alert_smtp_port"] = port
    config.save_settings(updates)
    state.log_event("info", "Settings updated")
    return config.public_settings()


@router.post("/api/pnl/drawdown")
async def api_pnl_drawdown(request: Request) -> dict[str, Any]:
    """Pin the trailing-drawdown threshold the prop firm shows for one account
    (``{"account_id", "level"}``), or clear the tracker (``level`` null). The
    next poll re-derives peak / room from it; the response is that poll."""
    from .. import drawdown
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="JSON object expected")
    try:
        account_id = int(body.get("account_id") or 0)
    except (TypeError, ValueError):
        account_id = 0
    if account_id <= 0:
        raise HTTPException(status_code=400, detail="account_id is required")
    level = body.get("level")
    if level not in (None, ""):
        try:
            level = float(level)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="level must be a number")
        if not (0 < level < 1e9):
            raise HTTPException(status_code=400, detail="level is out of range")
    else:
        level = None
    known = {int(a["account_id"]) for a in (state.pnl().get("accounts") or [])}
    if known and account_id not in known:
        raise HTTPException(status_code=404, detail="Unknown trade account")
    drawdown.set_seed(context.get_area(), account_id, level)
    state.log_event("info", f"Drawdown threshold {'pinned at ' + format(level, '.2f') if level is not None else 'tracking reset'} for account #{account_id}")
    return await pnl.refresh_area(context.get_area())


@router.get("/api/pnl")
async def api_pnl(refresh: bool = False) -> dict[str, Any]:
    """Live account P&L (today's realised, open, week, cash) — the last poll,
    or a fresh one with ``?refresh=1``."""
    if refresh:
        return await pnl.refresh_area(context.get_area())
    return state.pnl()


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
# The ring buffers hold plain JSON dicts (they are json.dumps'd for the live
# stream already): answer with them directly, skipping FastAPI's encoder walk.
@router.get("/api/signals")
async def api_signals() -> JSONResponse:
    return JSONResponse(state.recent_signals())


@router.get("/api/orders")
async def api_orders() -> JSONResponse:
    return JSONResponse(state.recent_orders())


@router.get("/api/events")
async def api_events() -> JSONResponse:
    return JSONResponse(state.recent_events())


@router.get("/api/history/signals")
async def api_history_signals(limit: int = 100, before: int | None = None,
                              result: str = "", q: str = "") -> dict[str, Any]:
    """Persisted signals, newest first, cursor-paginated (``next_before``)."""
    return db.list_signals(context.get_area(), limit=limit, before=before,
                           result=result[:50], q=q[:100])


@router.get("/api/history/orders")
async def api_history_orders(limit: int = 100, before: int | None = None,
                             symbol: str = "", account: str = "") -> dict[str, Any]:
    return db.list_orders(context.get_area(), limit=limit, before=before,
                          symbol=symbol[:40], account=account[:120])


@router.get("/api/history/stats")
async def api_history_stats(days: int = 7) -> dict[str, Any]:
    """Signal outcomes + order counts per day for the last ``days`` days."""
    days = max(1, min(int(days), 365))
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    return {"days_window": days, **db.history_stats(context.get_area(), since)}


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
                    frame = await asyncio.wait_for(sub.queue.get(), timeout=10.0)
                    yield frame                       # pre-serialised by state._broadcast
                    if sub.dropped:
                        sub.dropped = False
                        yield "event: resync\ndata: {}\n\n"   # messages were lost on a full queue: re-pull
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
    """Open positions across every enabled account: one ``/position/list`` per
    login (not per account), logins fetched concurrently."""
    async def one(ex) -> list[dict[str, Any]]:
        try:
            return await ex.positions()
        except TradovateError:
            return []

    async def per_login(sess, exs) -> list[dict[str, Any]]:
        try:
            raw = await sess.positions_snapshot()
        except Exception:  # noqa: BLE001 - one login down must not hide the others
            return []
        rows = [r for ex in exs for r in sess.positions_from(raw or [], account_id=ex.id, account_name=ex.name)]
        return await sess.positions_named(rows)

    groups: dict[int, tuple[Any, list[Any]]] = {}
    singles: list[Any] = []
    for ex in tradovate.manager().enabled():
        sess = getattr(ex, "session", None)
        if sess is not None and getattr(sess, "kind", "tradovate") == "tradovate" and hasattr(sess, "positions_from") and getattr(ex, "id", 0):
            groups.setdefault(id(sess), (sess, []))[1].append(ex)
        else:
            singles.append(ex)
    results = await asyncio.gather(*[per_login(s, exs) for s, exs in groups.values()], *(one(ex) for ex in singles))
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

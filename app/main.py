"""FastAPI application: webhook endpoint + dashboard + management API."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import config, signals, state, updater
from .simulator import SCENARIOS, sim_client
from .tradovate import TradovateError, client

BASE_DIR = Path(__file__).resolve().parent.parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

app = FastAPI(title="Tradovate Webhook Bridge", version=config.get_version())
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


# ============================================================== Dashboard view
@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        "dashboard.html",
        {"request": request, "version": config.get_version()},
    )


# ===================================================================== Webhook
@app.post("/webhook/{secret}")
async def webhook(secret: str, request: Request) -> JSONResponse:
    """Receive a TradingView alert and route it to Tradovate.

    The ``secret`` path segment must match ``webhook_secret`` in settings.
    """
    s = config.load_settings()
    if secret != s.get("webhook_secret"):
        raise HTTPException(status_code=403, detail="Invalid webhook secret")

    payload = await _parse_payload(request)
    state.log_signal(payload, result="received")

    try:
        result = await signals.process(payload)
        state.log_signal(payload, result=result.get("status", "ok"))
        return JSONResponse(result)
    except (signals.SignalError, TradovateError) as exc:
        state.log_event("error", f"Signal error: {exc}", payload=payload)
        state.log_signal(payload, result=f"error: {exc}")
        raise HTTPException(status_code=400, detail=str(exc)) from exc


async def _parse_payload(request: Request) -> dict[str, Any]:
    """Accept JSON bodies; tolerate text/plain alerts that contain JSON."""
    raw = await request.body()
    if not raw:
        raise HTTPException(status_code=400, detail="Empty body")
    try:
        import json

        return json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {exc}") from exc


# ========================================================================= API
@app.get("/api/status")
async def api_status() -> dict[str, Any]:
    return {
        "version": config.get_version(),
        "connection": state.connection,
        "active_trades": signals.active_trades(),
        "trading_enabled": config.load_settings().get("trading_enabled", False),
    }


@app.get("/api/settings")
async def api_get_settings() -> dict[str, Any]:
    return config.public_settings()


@app.post("/api/settings")
async def api_save_settings(request: Request) -> dict[str, Any]:
    updates = await request.json()
    # Drop masked secret fields so we don't overwrite stored secrets with "********".
    for field in config.SECRET_FIELDS:
        if updates.get(field) == "********":
            updates.pop(field, None)
    config.save_settings(updates)
    # If anything auth-related changed, drop cached tokens so the new values apply.
    auth_keys = {
        "environment", "username", "password", "cid", "sec", "app_id",
        "app_version", "device_id", "use_web_trader_fallback",
        "access_token", "md_token",
    }
    if auth_keys & set(updates):
        client.invalidate()
    state.log_event("info", "Settings updated")
    return config.public_settings()


@app.get("/api/signals")
async def api_signals() -> list[dict[str, Any]]:
    return state.recent_signals()


@app.get("/api/orders")
async def api_orders() -> list[dict[str, Any]]:
    return state.recent_orders()


@app.get("/api/events")
async def api_events() -> list[dict[str, Any]]:
    return state.recent_events()


@app.get("/api/positions")
async def api_positions() -> Any:
    try:
        return await client.positions()
    except TradovateError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/api/connect")
async def api_connect() -> dict[str, Any]:
    try:
        return await client.connect()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# =================================================================== Accounts
@app.get("/api/accounts")
async def api_accounts() -> list[dict[str, Any]]:
    return config.load_settings().get("accounts", [])


@app.post("/api/accounts")
async def api_save_accounts(request: Request) -> list[dict[str, Any]]:
    """Persist the enable flags / quantity multipliers chosen in the dashboard."""
    accounts = await request.json()
    cleaned = [
        {
            "id": a.get("id"),
            "name": a.get("name", ""),
            "enabled": bool(a.get("enabled")),
            "qty_multiplier": float(a.get("qty_multiplier", 1) or 1),
        }
        for a in accounts
        if a.get("id") is not None
    ]
    config.save_settings({"accounts": cleaned})
    enabled = [a for a in cleaned if a["enabled"]]
    state.connection["accounts_total"] = len(cleaned)
    state.connection["accounts_enabled"] = len(enabled)
    state.log_event("info", f"Accounts updated — {len(enabled)}/{len(cleaned)} enabled")
    return cleaned


@app.post("/api/accounts/refresh")
async def api_refresh_accounts() -> list[dict[str, Any]]:
    """Fetch the account list from Tradovate, preserving existing enable flags."""
    s = config.load_settings()
    try:
        fetched = await client.list_accounts()
    except TradovateError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    merged = client._merge_accounts(fetched, s)
    config.save_settings({"accounts": merged})
    return merged


@app.post("/api/webhook-test")
async def api_webhook_test(request: Request) -> dict[str, Any]:
    """Run a payload through the signal pipeline without an external POST."""
    payload = await request.json()
    state.log_signal(payload, result="test")
    try:
        return await signals.process(payload)
    except (signals.SignalError, TradovateError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# ================================================================== Simulator
@app.get("/api/scenarios")
async def api_scenarios() -> list[dict[str, Any]]:
    return SCENARIOS


@app.post("/api/simulate")
async def api_simulate(request: Request) -> dict[str, Any]:
    """Run a single signal through the pipeline in simulation mode (no broker)."""
    payload = await request.json()
    state.log_signal(payload, result="simulated")
    try:
        return await signals.process(payload, simulate=True)
    except (signals.SignalError, TradovateError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/simulate/state")
async def api_simulate_state() -> dict[str, Any]:
    return {
        "positions": await sim_client.positions(),
        "working_orders": await sim_client.working_orders(),
        "active_trades": signals.active_trades(simulate=True),
    }


@app.post("/api/simulate/reset")
async def api_simulate_reset() -> dict[str, Any]:
    signals.reset_simulation()
    return {"status": "reset"}


# ==================================================================== Updater
@app.get("/api/update/check")
async def api_update_check() -> dict[str, Any]:
    return await updater.check_for_update()


@app.post("/api/update/apply")
async def api_update_apply() -> dict[str, Any]:
    result = await updater.apply_update()
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("message"))
    return result


@app.get("/api/health")
async def api_health() -> dict[str, Any]:
    """On-demand connection health check (also runs periodically in the background)."""
    return await client.health_check()


async def _health_loop() -> None:
    """Periodically verify the Tradovate session and renew the token before expiry."""
    import asyncio
    while True:
        interval = int(config.load_settings().get("health_check_interval", 60) or 0)
        if interval <= 0:
            await asyncio.sleep(30)
            continue
        try:
            await client.health_check()
        except Exception as exc:  # noqa: BLE001 - never let the loop die
            state.log_event("warn", f"Health check error: {exc}")
        await asyncio.sleep(interval)


@app.on_event("startup")
async def _startup() -> None:
    import asyncio
    s = config.load_settings()
    state.connection["environment"] = s.get("environment", "demo")
    state.log_event("info", f"Bridge started (v{config.get_version()})")
    asyncio.create_task(_health_loop())

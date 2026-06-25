"""FastAPI application: webhook endpoint + dashboard + management API."""
from __future__ import annotations

import base64
import os
import secrets
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import config, signals, state, updater
from .simulator import SCENARIOS, sim_client
from .tradovate import TradovateError, manager

BASE_DIR = Path(__file__).resolve().parent.parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

app = FastAPI(title="Tradovate Webhook Bridge", version=config.get_version())
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

# Paths that must stay reachable without the dashboard password: the webhook
# (TradingView can't send auth), static assets, and the health check.
_AUTH_EXEMPT = ("/webhook/", "/static/", "/healthz", "/guide")


def _dashboard_password() -> str:
    return os.environ.get("DASHBOARD_PASSWORD") or config.load_settings().get(
        "dashboard_password", ""
    )


@app.middleware("http")
async def _basic_auth(request: Request, call_next):
    """Require HTTP Basic auth for the dashboard/API when a password is configured."""
    pw = _dashboard_password()
    path = request.url.path
    if pw and not path.startswith(_AUTH_EXEMPT):
        header = request.headers.get("Authorization", "")
        ok = False
        if header.startswith("Basic "):
            try:
                _, _, supplied = base64.b64decode(header[6:]).decode().partition(":")
                ok = secrets.compare_digest(supplied, pw)
            except (ValueError, UnicodeDecodeError):
                ok = False
        if not ok:
            return Response(
                status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="nexuspred"'},
            )
    return await call_next(request)


@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    """Unauthenticated liveness probe (for Render/uptime checks)."""
    return {"ok": True, "version": config.get_version()}


@app.get("/guide", response_class=HTMLResponse)
async def guide() -> FileResponse:
    """Standalone, self-contained setup guide page."""
    return FileResponse(str(BASE_DIR / "docs" / "setup-guide.html"))


# ============================================================== Dashboard view
@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        "dashboard.html",
        {"request": request, "version": config.get_version()},
    )


# ===================================================================== Webhook
async def _process_signal_bg(payload: dict[str, Any]) -> None:
    """Run the signal pipeline in the background so the webhook returns instantly."""
    try:
        result = await signals.process(payload)
        state.log_signal(payload, result=result.get("status", "ok"))
    except (signals.SignalError, TradovateError) as exc:
        state.log_event("error", f"Signal error: {exc}", payload=payload)
        state.log_signal(payload, result=f"error: {exc}")
    except Exception as exc:  # noqa: BLE001 - never let a background task die silently
        state.log_event("error", f"Signal failed: {exc}", payload=payload)
        state.log_signal(payload, result=f"error: {exc}")


@app.post("/webhook/{secret}")
async def webhook(secret: str, request: Request) -> JSONResponse:
    """Receive a TradingView alert and route it to Tradovate.

    The ``secret`` path segment must match ``webhook_secret`` in settings. The
    alert is acknowledged immediately (HTTP 202) and processed in the background,
    so bursts of alerts can't make TradingView time out ("request took too long").
    """
    import asyncio

    s = config.load_settings()
    if secret != s.get("webhook_secret"):
        raise HTTPException(status_code=403, detail="Invalid webhook secret")

    payload = await _parse_payload(request)
    state.log_signal(payload, result="received")
    asyncio.create_task(_process_signal_bg(payload))
    return JSONResponse({"status": "accepted"}, status_code=202)


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
def _trade_accounts_overview() -> list[dict[str, Any]]:
    """Flat list of every trade account across all logins, with execution toggle
    and live connection status — powers the Trade Accounts overview."""
    out: list[dict[str, Any]] = []
    for idx, t in enumerate(config.load_settings().get("token_accounts") or []):
        tname = t.get("name") or f"account {idx + 1}"
        env = t.get("environment") or "demo"
        tconn = bool(state.session_status(tname).get("connected"))
        accts = t.get("accounts") or []
        if not accts and (t.get("account_spec") or t.get("account_id")):
            accts = [{"spec": t.get("account_spec", ""), "id": t.get("account_id", 0),
                      "enabled": True, "qty_multiplier": t.get("qty_multiplier", 1)}]
        for a in accts:
            out.append({
                "token_idx": idx, "token_name": tname, "environment": env,
                "token_enabled": bool(t.get("enabled")), "connected": tconn,
                "spec": a.get("spec") or a.get("account_spec") or "",
                "id": a.get("id") or a.get("account_id") or 0,
                "enabled": bool(a.get("enabled", True)),
                "qty_multiplier": float(a.get("qty_multiplier", t.get("qty_multiplier", 1)) or 1),
            })
    return out


@app.get("/api/status")
async def api_status() -> dict[str, Any]:
    return {
        "version": config.get_version(),
        "connection": state.aggregate_connection(),
        "sessions": state.session_statuses(),
        "trade_accounts": _trade_accounts_overview(),
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
    updates.pop("token_accounts", None)  # managed via /api/token-accounts
    config.save_settings(updates)
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
    out: list[dict[str, Any]] = []
    for sess in manager.enabled():
        try:
            out.extend(await sess.positions())
        except TradovateError:
            pass
    return out


@app.post("/api/connect")
async def api_connect() -> dict[str, Any]:
    """Connect & verify all configured accounts (in parallel)."""
    manager.reload()
    import asyncio
    sessions = manager.all()
    await asyncio.gather(*(s.connect() for s in sessions), return_exceptions=True)
    return {"sessions": state.session_statuses()}


# =============================================================== Token accounts
@app.get("/api/token-accounts")
async def api_token_accounts() -> list[dict[str, Any]]:
    return config.public_settings().get("token_accounts", [])


@app.post("/api/token-accounts")
async def api_save_token_accounts(request: Request) -> list[dict[str, Any]]:
    """Save the per-account token list. Masked tokens ('********') keep the stored
    value, so editing other fields doesn't wipe the tokens."""
    incoming = await request.json()
    existing = config.load_settings().get("token_accounts") or []
    cleaned: list[dict[str, Any]] = []
    for i, a in enumerate(incoming):
        prev = existing[i] if i < len(existing) else {}
        access = a.get("access_token", "")
        md = a.get("md_token", "")
        cleaned.append({
            "name": (a.get("name") or f"account {i + 1}").strip(),
            "environment": "live" if a.get("environment") == "live" else "demo",
            "access_token": prev.get("access_token", "") if access == "********" else access.strip(),
            "md_token": prev.get("md_token", "") if md == "********" else md.strip(),
            "enabled": bool(a.get("enabled")),
            "qty_multiplier": float(a.get("qty_multiplier", 1) or 1),
            "account_spec": a.get("account_spec") or prev.get("account_spec", ""),
            "account_id": a.get("account_id") or prev.get("account_id", 0),
            "token_expires": prev.get("token_expires", ""),
        })
    config.save_settings({"token_accounts": cleaned})
    manager.reload()
    enabled = sum(1 for a in cleaned if a["enabled"])
    state.log_event("info", f"Token accounts updated — {enabled}/{len(cleaned)} enabled")
    return config.public_settings().get("token_accounts", [])


# =============================================================== Trade accounts
@app.get("/api/trade-accounts")
async def api_trade_accounts() -> list[dict[str, Any]]:
    """Overview of every trade account under every login, with on/off toggles."""
    return _trade_accounts_overview()


@app.post("/api/trade-accounts")
async def api_save_trade_accounts(request: Request) -> list[dict[str, Any]]:
    """Save per-account execution toggles & qty multipliers (keyed by login + spec)."""
    incoming = await request.json()
    tokens = list(config.load_settings().get("token_accounts") or [])
    by_token: dict[int, dict[str, Any]] = {}
    for item in incoming:
        try:
            idx = int(item.get("token_idx"))
        except (TypeError, ValueError):
            continue
        by_token.setdefault(idx, {})[item.get("spec", "")] = item

    for idx, updates in by_token.items():
        if not (0 <= idx < len(tokens)):
            continue
        t = dict(tokens[idx])
        existing = {(a.get("spec") or a.get("account_spec") or ""): dict(a)
                    for a in (t.get("accounts") or [])}
        for spec, u in updates.items():
            a = existing.get(spec, {"spec": spec, "id": u.get("id", 0)})
            a["spec"] = spec
            a["enabled"] = bool(u.get("enabled"))
            a["qty_multiplier"] = float(u.get("qty_multiplier", 1) or 1)
            if u.get("id"):
                a["id"] = u["id"]
            existing[spec] = a
        t["accounts"] = list(existing.values())
        tokens[idx] = t

    config.save_settings({"token_accounts": tokens})
    manager.reload()
    enabled = sum(1 for a in _trade_accounts_overview() if a["enabled"])
    state.log_event("info", f"Trade-account toggles updated — {enabled} enabled for execution")
    return _trade_accounts_overview()


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
    """On-demand health check of every configured account."""
    import asyncio
    await asyncio.gather(*(s.health_check() for s in manager.all()), return_exceptions=True)
    return {"sessions": state.session_statuses()}


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


async def _health_loop() -> None:
    """Keep every account's token alive proactively (Bridge-Bot-TV style), in parallel."""
    import asyncio
    while True:
        interval = int(config.load_settings().get("health_check_interval", 60) or 0)
        if interval <= 0:                       # background refresh disabled
            await asyncio.sleep(30)
            continue
        sessions = manager.all()
        if not sessions:
            await asyncio.sleep(max(30, interval))
            continue
        delays = await asyncio.gather(*(_refresh_session(s) for s in sessions),
                                      return_exceptions=True)
        ok_delays = [d for d in delays if isinstance(d, (int, float))]
        await asyncio.sleep(min(ok_delays) if ok_delays else 60.0)


@app.on_event("startup")
async def _startup() -> None:
    import asyncio
    manager.reload()
    state.log_event("info", f"Bridge started (v{config.get_version()})")
    asyncio.create_task(_health_loop())

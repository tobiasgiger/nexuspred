"""Manual trading from the dashboard (order ticket, close a position), the
exposure view, per-workspace automations and the Prometheus endpoint."""
from __future__ import annotations

import math
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse

from .. import automations, config, context, db, exposure, metrics, security, signals, state, tradovate
from ..engine.common import _cancel_working
from ..tradovate import OrderOutcomeUnknown, TradovateError

router = APIRouter()

MAX_MANUAL_QTY = 100
ORDER_TYPES = ("Market", "Limit", "Stop", "StopLimit")
_TICKET_LIMIT = security.RateLimiter(30, 60)          # manual orders per user and minute


def _user(request: Request) -> dict[str, Any]:
    return getattr(request.state, "user", None) or {}


def _executor(body: dict[str, Any]) -> Any:
    spec = str(body.get("spec") or "").strip()
    if not spec:
        raise HTTPException(status_code=400, detail="spec (trade account) is required")
    lid = str(body.get("lid") or "").strip() or None
    try:
        idx = int(body["token_idx"]) if body.get("token_idx") is not None and body.get("token_idx") != "" else None
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="token_idx must be a number")
    if lid is None and idx is None:
        raise HTTPException(status_code=400, detail="lid or token_idx (the login) is required")
    ex = tradovate.manager().executor_for(idx if idx is not None else -1, spec, 1, lid=lid)
    if ex is None:
        raise HTTPException(status_code=404, detail=f"Account '{spec}' not found or its login is disabled")
    return ex


def _price(v: Any, key: str, *, required: bool) -> float | None:
    if v in (None, ""):
        if required:
            raise HTTPException(status_code=400, detail=f"{key} is required for this order type")
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail=f"{key} must be a number")
    if not math.isfinite(f) or f <= 0:
        raise HTTPException(status_code=400, detail=f"{key} must be a positive number")
    return f


@router.post("/api/orders/manual")
async def api_manual_order(request: Request) -> dict[str, Any]:
    """Place one order from the dashboard's order ticket. Respects the Trading
    switch and the account's risk lock like every signal; no symbol allow-list
    (the symbol map is applied when it knows the symbol)."""
    user = _user(request)
    if not _TICKET_LIMIT.hit(f"ticket:{user.get('id', 0)}"):
        raise HTTPException(status_code=429, detail="Too many manual orders — wait a minute")
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Body must be a JSON object")
    action = str(body.get("action") or "").strip().lower()
    if action not in ("buy", "sell"):
        raise HTTPException(status_code=400, detail="action must be buy or sell")
    try:
        qty = int(float(body.get("qty") or 0))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="qty must be a whole number")
    if not 1 <= qty <= MAX_MANUAL_QTY:
        raise HTTPException(status_code=400, detail=f"qty must be between 1 and {MAX_MANUAL_QTY}")
    order_type = str(body.get("order_type") or "Market").strip()
    if order_type not in ORDER_TYPES:
        raise HTTPException(status_code=400, detail=f"order_type must be one of {', '.join(ORDER_TYPES)}")
    price = _price(body.get("price"), "price", required=order_type in ("Limit", "StopLimit"))
    stop_price = _price(body.get("stop_price"), "stop_price", required=order_type in ("Stop", "StopLimit"))
    symbol = str(body.get("symbol") or "").strip()
    if not symbol or len(symbol) > 20:
        raise HTTPException(status_code=400, detail="symbol is required")
    s = config.load_settings()
    if not s.get("trading_enabled"):
        raise HTTPException(status_code=409, detail="Trading is disabled — switch it on in the top bar first")
    target = (s.get("symbol_map") or {}).get(symbol) or (s.get("symbol_map") or {}).get(symbol.upper()) or symbol.upper()
    ex = _executor(body)
    try:
        contract = await ex.resolve_contract(target)
        order = await ex.place_order(symbol=contract, action=action.capitalize(), qty=qty, order_type=order_type,
                                     price=price, stop_price=stop_price)
    except OrderOutcomeUnknown as exc:
        state.log_event("error", f"Manual order {action} {qty} {target} on {ex.name}: outcome unknown — {exc}")
        raise HTTPException(status_code=502, detail=f"Order outcome unknown — check the broker: {exc}") from exc
    except TradovateError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    state.log_event("info", f"Manual order: {action} {qty} {contract} {order_type} on {ex.name} by {user.get('email', '?')}")
    if user:
        db.log_action(user["id"], user["email"], "manual_order", ex.name, f"{action} {qty} {contract} {order_type}"
                      + (f" @ {price}" if price is not None else "") + (f" stop {stop_price}" if stop_price is not None else ""))
    return {"status": order.get("status", "submitted"), "order_id": order.get("order_id"), "contract": contract,
            "account": ex.name, "action": action, "qty": qty, "order_type": order_type}


@router.post("/api/positions/close")
async def api_close_position(request: Request) -> dict[str, Any]:
    """Close one open position at market: the contract's working orders are
    cancelled first (a stop or target must not re-fill into a new position),
    then the position is liquidated. Works with the Trading switch off."""
    user = _user(request)
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Body must be a JSON object")
    contract = str(body.get("symbol") or "").strip()
    if not contract or len(contract) > 20:
        raise HTTPException(status_code=400, detail="symbol is required")
    ex = _executor(body)
    errors: list[str] = []
    cancelled = await _cancel_working(ex, "", errors, contract=contract)
    try:
        await ex.liquidate_position(contract)
    except TradovateError as exc:
        raise HTTPException(status_code=502, detail=f"{ex.name}: close {contract} failed — {exc}" + (f" (cancel errors: {'; '.join(errors)})" if errors else "")) from exc
    # the bridge stops managing that trade on this account (other accounts stay tracked)
    live = signals._map_for(False)
    with signals._lock:
        for key in list(live):
            accounts = live[key].get("accounts") or {}
            acc = accounts.get(ex.name)
            if acc and str(acc.get("contract") or live[key].get("contract") or "") == contract:
                accounts.pop(ex.name, None)
                if not accounts:
                    live.pop(key, None)
    state.log_event("info", f"Position closed from the dashboard: {contract} on {ex.name} ({cancelled} order(s) cancelled) by {user.get('email', '?')}")
    if user:
        db.log_action(user["id"], user["email"], "close_position", ex.name, f"{contract}, {cancelled} order(s) cancelled")
    return {"status": "ok", "contract": contract, "account": ex.name, "cancelled": cancelled, "errors": errors}


@router.get("/api/exposure")
async def api_exposure() -> dict[str, Any]:
    rows = await exposure.collect_positions()
    return {"positions": rows, **exposure.summarize(rows)}


# ------------------------------------------------------------ automations
def _automations_view(area_id: int) -> dict[str, Any]:
    return {"rules": automations.rules_for(area_id), "log": automations.recent(area_id),
            "events": [{"id": k, "filters": sorted(v)} for k, v in automations.KINDS.items()], "actions": list(automations.ACTIONS)}


@router.get("/api/automations")
async def api_automations() -> dict[str, Any]:
    return _automations_view(context.get_area())


@router.put("/api/automations")
async def api_save_automations(request: Request) -> dict[str, Any]:
    user = _user(request)
    body = await request.json()
    raw = body.get("rules") if isinstance(body, dict) else body
    try:
        rules = automations.normalize_rules(raw)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    config.save_settings({"automations": rules})
    state.log_event("info", f"Automations saved ({len(rules)} rule(s))")
    if user:
        db.log_action(user["id"], user["email"], "automations_saved", "", f"{len(rules)} rule(s)")
    return _automations_view(context.get_area())


# ---------------------------------------------------------------- metrics
@router.get("/metrics")
async def api_metrics(request: Request) -> PlainTextResponse:
    if not metrics.enabled():
        raise HTTPException(status_code=404, detail="Not found")
    if not metrics.authorized(request.headers.get("authorization")):
        return PlainTextResponse("unauthorized\n", status_code=401, headers={"WWW-Authenticate": "Bearer"})
    return PlainTextResponse(metrics.render(), media_type="text/plain; version=0.0.4; charset=utf-8",
                             headers={"Cache-Control": "no-store"})

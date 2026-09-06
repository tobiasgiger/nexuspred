"""The TradingView ingress (``/webhook/{token}``) and per-strategy webhook CRUD."""
from __future__ import annotations

import json
import secrets
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from .. import config, context, signals, state
from ..tradovate import TradovateError

router = APIRouter(tags=["webhooks"])


# ===================================================================== Ingress
def _resolve_webhook(token: str) -> tuple[int | None, dict[str, Any] | None]:
    """Find which area owns a webhook token (webhooks are per area). Returns
    (area_id, webhook) or (None, None). Served from config's in-memory index."""
    return config.find_webhook(token)


async def _parse_payload(request: Request) -> dict[str, Any]:
    """Accept JSON bodies; tolerate text/plain alerts that contain JSON."""
    raw = await request.body()
    if not raw:
        raise HTTPException(status_code=400, detail="Empty body")
    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {exc}") from exc


@router.post("/webhook/{token}")
async def webhook(token: str, request: Request) -> JSONResponse:
    """Receive a TradingView alert and route it to Tradovate.

    The ``token`` path segment must match a configured webhook's ``token``; each
    webhook carries its own strategy + routed trade accounts (see
    ``/api/webhooks``). The alert is acknowledged immediately (HTTP 202) and
    processed in the background, so bursts of alerts can't make TradingView time
    out ("request took too long").
    """
    area_id, wh = _resolve_webhook(token)
    if not wh or not wh.get("enabled") or area_id is None:
        raise HTTPException(status_code=403, detail="Invalid webhook token")

    # Process in the owning user's area context (the background task inherits it).
    tok = context.set_area(area_id)
    try:
        payload = await _parse_payload(request)
        signals.accept(payload, wh)
    finally:
        context.reset_area(tok)
    return JSONResponse({"status": "accepted"}, status_code=202)


# ======================================================================== CRUD
def _webhook_or_404(webhook_id: str) -> tuple[list[dict[str, Any]], int]:
    """Return (all webhooks, index of webhook_id) or raise 404."""
    webhooks = config.load_settings().get("webhooks", [])
    for i, wh in enumerate(webhooks):
        if wh.get("id") == webhook_id:
            return webhooks, i
    raise HTTPException(status_code=404, detail="Webhook not found")


@router.get("/api/webhooks")
async def api_list_webhooks() -> list[dict[str, Any]]:
    return config.load_settings().get("webhooks", [])


@router.post("/api/webhooks")
async def api_create_webhook(request: Request) -> dict[str, Any]:
    body = await request.json()
    try:
        wh = config.new_webhook(
            name=body.get("name") or "New Webhook",
            strategy=body.get("strategy", "simple"),
            default_qty=body.get("default_qty", 1),
            tp_qty=body.get("tp_qty", 1),
        )
    except (TypeError, ValueError, AttributeError) as exc:
        raise HTTPException(status_code=400, detail=f"Invalid webhook payload: {exc}") from exc
    config.update(lambda s: s.__setitem__("webhooks", [*(s.get("webhooks") or []), wh]))
    state.log_event("info", f"Webhook '{wh['name']}' created ({wh['strategy']})")
    return wh


@router.put("/api/webhooks/{webhook_id}")
async def api_update_webhook(webhook_id: str, request: Request) -> dict[str, Any]:
    body = await request.json()
    webhooks, i = _webhook_or_404(webhook_id)
    wh = webhooks[i]
    try:
        if "name" in body:
            wh["name"] = str(body["name"]) or wh["name"]
        if "enabled" in body:
            wh["enabled"] = bool(body["enabled"])
        if "strategy" in body and body["strategy"] in config.STRATEGIES:
            wh["strategy"] = body["strategy"]
        if "default_qty" in body:
            wh["default_qty"] = max(1, int(body["default_qty"] or 1))
        if "tp_qty" in body:
            wh["tp_qty"] = max(1, int(body["tp_qty"] or 1))
        if "accounts" in body:
            wh["accounts"] = [
                {
                    "token_idx": int(a["token_idx"]),
                    "spec": a.get("spec", ""),
                    "enabled": bool(a.get("enabled")),
                    "qty_multiplier": float(a.get("qty_multiplier", 1) or 1),
                }
                for a in body["accounts"]
                if a.get("spec") and a.get("token_idx") is not None
            ]
    except (TypeError, ValueError, KeyError, AttributeError) as exc:
        raise HTTPException(status_code=400, detail=f"Invalid webhook payload: {exc}") from exc
    webhooks[i] = wh
    config.save_settings({"webhooks": webhooks})
    state.log_event("info", f"Webhook '{wh['name']}' updated")
    return wh


@router.delete("/api/webhooks/{webhook_id}")
async def api_delete_webhook(webhook_id: str) -> dict[str, Any]:
    webhooks, i = _webhook_or_404(webhook_id)
    removed = webhooks.pop(i)
    config.save_settings({"webhooks": webhooks})
    state.log_event("info", f"Webhook '{removed.get('name')}' deleted")
    return {"status": "deleted", "id": webhook_id}


@router.post("/api/webhooks/{webhook_id}/regenerate-token")
async def api_regenerate_webhook_token(webhook_id: str) -> dict[str, Any]:
    webhooks, i = _webhook_or_404(webhook_id)
    webhooks[i]["token"] = secrets.token_urlsafe(16)
    config.save_settings({"webhooks": webhooks})
    state.log_event("info", f"Webhook '{webhooks[i]['name']}' token regenerated")
    return webhooks[i]


@router.post("/api/webhooks/{webhook_id}/test")
async def api_test_webhook(webhook_id: str, request: Request) -> dict[str, Any]:
    """Run a payload through the signal pipeline for a specific webhook (real
    execution — respects the trading_enabled switch, same as a live POST)."""
    webhooks, i = _webhook_or_404(webhook_id)
    wh = webhooks[i]
    payload = await request.json()
    state.log_signal(payload, result="test")
    try:
        return await signals.process(payload, wh)
    except (signals.SignalError, TradovateError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

"""The TradingView ingress (``/webhook/{token}``) and per-strategy webhook CRUD."""
from __future__ import annotations

import json
import secrets
from typing import Any, Callable

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from .. import config, context, db, marketplace, signals, sizing, state
from ..tradovate import TradovateError
from ..web import require_admin

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
def _routed_account(a: dict[str, Any]) -> dict[str, Any]:
    """One routed (login, account) entry with its sizing rule; raises ValueError."""
    sz = sizing.normalize(a)
    s = config.load_settings()
    idx = int(a["token_idx"])
    lid = str(a.get("lid") or "")
    if lid and config.login_index(s, lid) is not None:
        idx = config.login_index(s, lid)
    elif not lid:
        tokens = s.get("token_accounts") or []
        lid = str(tokens[idx].get("lid") or "") if 0 <= idx < len(tokens) else ""
    return {"token_idx": idx, "lid": lid, "spec": str(a.get("spec", "")), "enabled": bool(a.get("enabled")),
            "qty_multiplier": sizing.effective_multiplier(sz), "sizing": sz}


def _webhook_or_404(webhook_id: str) -> tuple[list[dict[str, Any]], int]:
    """Return (all webhooks, index of webhook_id) or raise 404."""
    webhooks = config.load_settings().get("webhooks", [])
    for i, wh in enumerate(webhooks):
        if wh.get("id") == webhook_id:
            return webhooks, i
    raise HTTPException(status_code=404, detail="Webhook not found")


def _edit_webhook(webhook_id: str, fn: Callable[[list[dict[str, Any]], int], Any]) -> Any:
    """Atomic read-modify-write of one webhook: ``fn(webhooks, index)`` runs on
    a fresh copy under the settings lock, so two concurrent edits (a rename and
    a token regenerate, say) can never overwrite each other. Returns fn's result."""
    out: dict[str, Any] = {}

    def mutate(s: dict[str, Any]) -> None:
        webhooks = list(s.get("webhooks") or [])
        for i, wh in enumerate(webhooks):
            if wh.get("id") == webhook_id:
                out["result"] = fn(webhooks, i)
                s["webhooks"] = webhooks
                return
        raise HTTPException(status_code=404, detail="Webhook not found")
    config.update(mutate)
    return out.get("result")


@router.get("/api/webhooks")
async def api_list_webhooks() -> list[dict[str, Any]]:
    """The area's webhooks, each with its marketplace subscriber count."""
    counts = db.subscriber_counts(context.get_area())
    return [{**wh, "subscriber_count": counts.get(wh.get("id"), 0)}
            for wh in config.load_settings().get("webhooks", [])]


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
    wh = _edit_webhook(webhook_id, lambda webhooks, i: _apply_webhook_edit(webhooks, i, body))
    state.log_event("info", f"Webhook '{wh['name']}' updated")
    return wh


def _apply_webhook_edit(webhooks: list[dict[str, Any]], i: int, body: dict[str, Any]) -> dict[str, Any]:
    wh = dict(webhooks[i])
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
            wh["accounts"] = [_routed_account(a) for a in body["accounts"] if a.get("spec") and a.get("token_idx") is not None]
    except (TypeError, ValueError, KeyError, AttributeError) as exc:
        raise HTTPException(status_code=400, detail=f"Invalid webhook payload: {exc}") from exc
    webhooks[i] = wh
    return wh


@router.delete("/api/webhooks/{webhook_id}")
async def api_delete_webhook(webhook_id: str) -> dict[str, Any]:
    removed = _edit_webhook(webhook_id, lambda webhooks, i: webhooks.pop(i))
    dropped = db.delete_subscriptions_for_webhook(context.get_area(), webhook_id)
    state.log_event("info", f"Webhook '{removed.get('name')}' deleted"
                    + (f" ({dropped} subscription(s) removed)" if dropped else ""))
    return {"status": "deleted", "id": webhook_id, "subscriptions_removed": dropped}


# ================================================================= Marketplace
@router.put("/api/webhooks/{webhook_id}/sharing")
async def api_update_sharing(webhook_id: str, request: Request) -> dict[str, Any]:
    """Publish / unpublish a webhook on the marketplace (admins only). Existing
    subscriptions are kept but paused while it's unpublished."""
    user = require_admin(request)
    body = await request.json()
    edited: dict[str, Any] = {}

    def apply(webhooks: list[dict[str, Any]], i: int) -> dict[str, Any]:
        wh = dict(webhooks[i])
        edited["before"] = marketplace.sharing_of(wh)
        wh["sharing"] = marketplace.normalize_sharing(body, wh.get("sharing"))
        webhooks[i] = wh
        return wh
    wh = _edit_webhook(webhook_id, apply)
    before, after = edited["before"], wh["sharing"]
    if before["enabled"] != after["enabled"]:
        db.log_action(user["id"], user["email"], "webhook_share", after["title"] or wh.get("name", ""),
                      "published" if after["enabled"] else "unpublished")
        state.log_event("info", f"Webhook '{wh.get('name')}' {'published on' if after['enabled'] else 'removed from'} the marketplace")
    return {**wh, "subscriber_count": db.subscriber_counts(context.get_area()).get(webhook_id, 0)}


@router.get("/api/webhooks/{webhook_id}/subscribers")
async def api_list_subscribers(webhook_id: str, request: Request) -> list[dict[str, Any]]:
    require_admin(request)
    _webhook_or_404(webhook_id)
    return db.list_subscribers(context.get_area(), webhook_id)


@router.delete("/api/webhooks/{webhook_id}/subscribers/{sub_id}")
async def api_remove_subscriber(webhook_id: str, sub_id: int, request: Request) -> dict[str, Any]:
    """Publisher removes a subscriber ("kick")."""
    user = require_admin(request)
    _webhook_or_404(webhook_id)
    removed = db.delete_subscription(sub_id, publisher_area_id=context.get_area())
    if not removed or removed["webhook_id"] != webhook_id:
        raise HTTPException(status_code=404, detail="Subscriber not found")
    email = db.area_owner_email(removed["area_id"]) or str(removed["area_id"])
    db.log_action(user["id"], user["email"], "subscriber_remove", email, f"webhook {webhook_id}")
    state.log_event("info", f"Subscriber {email} removed from webhook {webhook_id}")
    return {"status": "deleted", "id": sub_id}


@router.post("/api/webhooks/{webhook_id}/regenerate-token")
async def api_regenerate_webhook_token(webhook_id: str) -> dict[str, Any]:
    def apply(webhooks: list[dict[str, Any]], i: int) -> dict[str, Any]:
        webhooks[i] = {**webhooks[i], "token": secrets.token_urlsafe(16)}
        return webhooks[i]
    wh = _edit_webhook(webhook_id, apply)
    state.log_event("info", f"Webhook '{wh['name']}' token regenerated")
    return wh


@router.post("/api/webhooks/{webhook_id}/test")
async def api_test_webhook(webhook_id: str, request: Request, subscribers: bool = False) -> dict[str, Any]:
    """Run a payload through the signal pipeline for a specific webhook (real
    execution — respects the trading_enabled switch, same as a live POST).
    With ``?subscribers=true`` a published webhook's test signal is also
    forwarded to its marketplace subscribers (off by default)."""
    webhooks, i = _webhook_or_404(webhook_id)
    wh = webhooks[i]
    payload = await request.json()
    state.log_signal(payload, result="test", webhook=wh.get("name", ""))
    try:
        result = await signals.process(payload, wh)
    except (signals.SignalError, TradovateError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if subscribers:
        result = {**result, "forwarded": signals.forward_to_subscribers(payload, wh)}
    return result

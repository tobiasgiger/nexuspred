"""Marketplace: browse published webhooks and manage your own subscriptions.
(Publishing and the subscriber list are on the webhook routes.)"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from .. import context, db, marketplace, state

router = APIRouter(prefix="/api", tags=["marketplace"])


def _enrich(sub: dict[str, Any]) -> dict[str, Any]:
    """A subscription plus the public view of the webhook it follows (or a
    'missing' marker when the publisher unpublished/deleted it)."""
    wh, sh = marketplace.find_published(sub["publisher_area_id"], sub["webhook_id"])
    if wh is None:
        return {**sub, "webhook": None, "active": False}
    view = marketplace.public_view(wh, sub["publisher_area_id"])
    return {**sub, "webhook": view, "active": bool(sub["enabled"] and view["webhook_enabled"])}


@router.get("/marketplace")
async def api_marketplace(request: Request) -> list[dict[str, Any]]:
    """Published webhooks visible to the signed-in user (never their own area),
    each with the user's subscription (if any) and the subscriber count."""
    user = request.state.user
    area = context.get_area()
    mine = {(s["publisher_area_id"], s["webhook_id"]): s for s in db.list_subscriptions(area)}
    items = marketplace.published_webhooks(user_id=user["id"], exclude_area=area)
    counts: dict[int, dict[str, int]] = {}
    for it in items:
        pa = it["publisher_area_id"]
        if pa not in counts:
            counts[pa] = db.subscriber_counts(pa)
        it["subscriber_count"] = counts[pa].get(it["webhook_id"], 0)
        it["subscription"] = mine.get((pa, it["webhook_id"]))
    return items


@router.post("/marketplace/{publisher_area_id}/{webhook_id}/subscribe")
async def api_subscribe(request: Request, publisher_area_id: int, webhook_id: str) -> dict[str, Any]:
    user = request.state.user
    area = context.get_area()
    if publisher_area_id == area:
        raise HTTPException(status_code=400, detail="You can't subscribe to your own webhook")
    wh, sh = marketplace.find_published(publisher_area_id, webhook_id)
    if wh is None:
        raise HTTPException(status_code=404, detail="That signal isn't published")
    if not marketplace.visible_to(sh, user["id"]):
        raise HTTPException(status_code=403, detail="That signal isn't available to your account")
    body = await request.json()
    accounts = marketplace.clean_accounts(body.get("accounts"))
    enabled = bool(body.get("enabled", True))
    sub = db.upsert_subscription(area, publisher_area_id, webhook_id, accounts, enabled)
    title = sh.get("title") or wh.get("name", "")
    db.log_action(user["id"], user["email"], "subscribe", title,
                  f"{len([a for a in accounts if a.get('enabled')])} account(s), {'on' if enabled else 'off'}")
    state.log_event("info", f"Subscribed to '{title}' on {len(accounts)} account(s)")
    return _enrich(sub)


@router.get("/subscriptions")
async def api_subscriptions() -> list[dict[str, Any]]:
    return [_enrich(s) for s in db.list_subscriptions(context.get_area())]


@router.put("/subscriptions/{sub_id}")
async def api_update_subscription(request: Request, sub_id: int) -> dict[str, Any]:
    body = await request.json()
    kwargs: dict[str, Any] = {}
    if "enabled" in body:
        kwargs["enabled"] = bool(body["enabled"])
    if "accounts" in body:
        kwargs["accounts"] = marketplace.clean_accounts(body["accounts"])
    sub = db.update_subscription(sub_id, context.get_area(), **kwargs)
    if not sub:
        raise HTTPException(status_code=404, detail="Subscription not found")
    view = _enrich(sub)
    state.log_event("info", f"Subscription '{(view.get('webhook') or {}).get('title', sub['webhook_id'])}' "
                    f"{'enabled' if sub['enabled'] else 'disabled'}")
    return view


@router.delete("/subscriptions/{sub_id}")
async def api_unsubscribe(request: Request, sub_id: int) -> dict[str, Any]:
    user = request.state.user
    sub = db.delete_subscription(sub_id, area_id=context.get_area())
    if not sub:
        raise HTTPException(status_code=404, detail="Subscription not found")
    wh, sh = marketplace.find_published(sub["publisher_area_id"], sub["webhook_id"])
    title = (sh.get("title") or (wh or {}).get("name") or sub["webhook_id"]) if wh else sub["webhook_id"]
    db.log_action(user["id"], user["email"], "unsubscribe", title)
    state.log_event("info", f"Unsubscribed from '{title}'")
    return {"status": "deleted", "id": sub_id}

"""Marketplace: browse published webhooks and manage your own subscriptions.
(Publishing and the subscriber list are on the webhook routes.)"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from .. import context, copy, db, marketplace, marketplace_safety, state

router = APIRouter(prefix="/api", tags=["marketplace"])


def _is_copy(sub: dict[str, Any]) -> bool:
    return str(sub.get("webhook_id") or "").startswith("copy:")


def _enrich(sub: dict[str, Any]) -> dict[str, Any]:
    if _is_copy(sub):
        g, sh = copy.find_published(sub["publisher_area_id"], sub["webhook_id"][5:])
        if g is None:
            return {**sub, "kind": "copy", "webhook": None, "copy": None, "active": False}
        view = copy.public_view(g, sub["publisher_area_id"])
        return {**sub, "kind": "copy", "webhook": None, "copy": view, "active": bool(sub["enabled"] and view["enabled"])}
    wh, sh = marketplace.find_published(sub["publisher_area_id"], sub["webhook_id"])
    if wh is None:
        return {**sub, "webhook": None, "active": False}
    view = marketplace.public_view(wh, sub["publisher_area_id"])
    return {**sub, "webhook": view, "active": bool(sub["enabled"] and view["webhook_enabled"])}


@router.get("/marketplace")
async def api_marketplace(request: Request) -> list[dict[str, Any]]:
    user = request.state.user
    area = context.get_area()
    mine = {(s["publisher_area_id"], s["webhook_id"]): s for s in db.list_subscriptions(area)}
    items = [{**it, "kind": "webhook"} for it in marketplace.published_webhooks(user_id=user["id"], exclude_area=area)]
    items += copy.published_groups(user_id=user["id"], exclude_area=area)
    counts: dict[int, dict[str, int]] = {}
    for it in items:
        pa = it["publisher_area_id"]
        if pa not in counts:
            counts[pa] = db.subscriber_counts(pa)
        key = it["webhook_id"] if it["kind"] == "webhook" else f"copy:{it['group_id']}"
        it["subscriber_count"] = counts[pa].get(key, 0)
        it["subscription"] = mine.get((pa, key))
    return items


@router.post("/marketplace/{publisher_area_id}/copy/{group_id}/subscribe")
async def api_subscribe_copy(request: Request, publisher_area_id: int, group_id: str) -> dict[str, Any]:
    user = request.state.user
    area = context.get_area()
    if publisher_area_id == area:
        raise HTTPException(status_code=400, detail="You can't subscribe to your own copy group")
    g, sh = copy.find_published(publisher_area_id, group_id)
    if g is None:
        raise HTTPException(status_code=404, detail="That copy group isn't published")
    if not marketplace.visible_to(sh, user["id"]):
        raise HTTPException(status_code=403, detail="That copy group isn't available to your account")
    body = await request.json()
    try:
        accounts = copy.clean_subscriber_accounts(body.get("accounts"), area, broker_kind=copy.leader_broker(publisher_area_id, group_id))
        marketplace_safety.validate_copy_accounts(publisher_area_id, group_id, accounts)
    except (TypeError, ValueError, KeyError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    enabled = bool(body.get("enabled", True))
    sub = db.upsert_subscription(area, publisher_area_id, f"copy:{group_id}", accounts, enabled)
    title = sh.get("title") or g.get("name", "")
    db.log_action(user["id"], user["email"], "subscribe", title, f"copy · {len([a for a in accounts if a.get('enabled')])} account(s), {'on' if enabled else 'off'}")
    state.log_event("info", f"Following copy group '{title}' on {len(accounts)} account(s)")
    await copy.sync_area(publisher_area_id)
    return _enrich(sub)


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
    current = db.get_subscription(sub_id, context.get_area())
    if not current:
        raise HTTPException(status_code=404, detail="Subscription not found")
    kwargs: dict[str, Any] = {}
    if "enabled" in body:
        kwargs["enabled"] = bool(body["enabled"])
    if "accounts" in body:
        if _is_copy(current):
            try:
                accounts = copy.clean_subscriber_accounts(
                    body["accounts"], context.get_area(), exclude_sub_id=sub_id,
                    broker_kind=copy.leader_broker(current["publisher_area_id"], current["webhook_id"][5:]))
                marketplace_safety.validate_copy_accounts(
                    current["publisher_area_id"], current["webhook_id"][5:], accounts, exclude_sub_id=sub_id)
                kwargs["accounts"] = accounts
            except (TypeError, ValueError, KeyError) as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        else:
            kwargs["accounts"] = marketplace.clean_accounts(body["accounts"])
    sub = db.update_subscription(sub_id, context.get_area(), **kwargs)
    if not sub:
        raise HTTPException(status_code=404, detail="Subscription not found")
    if _is_copy(sub):
        before = copy._enabled_specs(current.get("accounts")) if current.get("enabled", True) else set()
        after = copy._enabled_specs(sub.get("accounts")) if sub.get("enabled", True) else set()
        await copy.release_followers(sub["publisher_area_id"], sub["webhook_id"][5:], before - after)
    view = _enrich(sub)
    state.log_event("info", f"Subscription '{((view.get('webhook') or view.get('copy')) or {}).get('title', sub['webhook_id'])}' "
                    f"{'enabled' if sub['enabled'] else 'disabled'}")
    if _is_copy(sub):
        await copy.sync_area(sub["publisher_area_id"])
    return view


@router.delete("/subscriptions/{sub_id}")
async def api_unsubscribe(request: Request, sub_id: int) -> dict[str, Any]:
    user = request.state.user
    sub = db.delete_subscription(sub_id, area_id=context.get_area())
    if not sub:
        raise HTTPException(status_code=404, detail="Subscription not found")
    if _is_copy(sub):
        g, sh = copy.find_published(sub["publisher_area_id"], sub["webhook_id"][5:])
        title = (sh.get("title") or (g or {}).get("name") or sub["webhook_id"]) if g else sub["webhook_id"]
        db.log_action(user["id"], user["email"], "unsubscribe", title)
        cancelled = await copy.release_followers(sub["publisher_area_id"], sub["webhook_id"][5:], copy._enabled_specs(sub.get("accounts")))
        state.log_event("info", f"Stopped following copy group '{title}' — your positions are not touched"
                        + (f" ({cancelled} mirrored working order(s) cancelled)" if cancelled else ""))
        await copy.sync_area(sub["publisher_area_id"])
        return {"status": "deleted", "id": sub_id}
    wh, sh = marketplace.find_published(sub["publisher_area_id"], sub["webhook_id"])
    title = (sh.get("title") or (wh or {}).get("name") or sub["webhook_id"]) if wh else sub["webhook_id"]
    db.log_action(user["id"], user["email"], "unsubscribe", title)
    state.log_event("info", f"Unsubscribed from '{title}'")
    return {"status": "deleted", "id": sub_id}

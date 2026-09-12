"""Copy-trading groups (leader account → follower accounts) and their live status."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from .. import config, context, copy, db, history, marketplace, state
from ..web import require_admin
from .accounts import trade_accounts_overview

router = APIRouter(prefix="/api/copy", tags=["copy"])


def _group_or_404(group_id: str) -> tuple[list[dict[str, Any]], int]:
    groups = copy.load_groups()
    for i, g in enumerate(groups):
        if g.get("id") == group_id:
            return groups, i
    raise HTTPException(status_code=404, detail="Copy group not found")


def _apply(g: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
    """Merge an API body into a group (validating each field)."""
    if "name" in body:
        g["name"] = str(body["name"]).strip()[:60] or g["name"]
    if "enabled" in body:
        g["enabled"] = bool(body["enabled"])
    if "leader" in body:
        lead = body["leader"] or {}
        g["leader"] = {"token_idx": int(lead["token_idx"]), "lid": str(lead.get("lid") or ""), "spec": str(lead["spec"]),
                       "account_id": int(lead.get("account_id") or 0)}
    if "symbols" in body:
        raw = body["symbols"]
        if isinstance(raw, str):
            raw = raw.replace(";", ",").split(",")
        g["symbols"] = sorted({str(x).strip().upper() for x in raw if str(x).strip()})[:50]
    if "followers" in body:
        g["followers"] = [copy.normalize_follower(f) for f in body["followers"] if f.get("spec") and f.get("token_idx") is not None]
    if "feed" in body:
        g["feed"] = body["feed"] if body["feed"] in ("auto", "websocket", "poll") else "auto"
    if "feed_loss_flatten_s" in body:
        g["feed_loss_flatten_s"] = int(body["feed_loss_flatten_s"])
    if "copy_adds" in body:
        g["copy_adds"] = bool(body["copy_adds"])
    if "copy_orders" in body:
        g["copy_orders"] = bool(body["copy_orders"])
    if "on_feed_loss" in body:
        g["on_feed_loss"] = "pause" if body["on_feed_loss"] == "pause" else "flatten"
    return g


def _with_status(g: dict[str, Any], st: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return {**g, "status": st.get(g["id"])}


@router.get("/groups")
async def api_list_groups() -> list[dict[str, Any]]:
    st = copy.statuses(context.get_area())
    counts = db.subscriber_counts(context.get_area())
    return [{**_with_status(g, st), "subscriber_count": counts.get(f"copy:{g['id']}", 0)} for g in copy.load_groups()]


@router.get("/following")
async def api_following() -> list[dict[str, Any]]:
    """Copy groups this workspace follows through the marketplace, with the live
    picture of its own accounts."""
    return copy.following_status(context.get_area())


@router.put("/groups/{group_id}/sharing")
async def api_group_sharing(group_id: str, request: Request) -> dict[str, Any]:
    """Publish / unpublish a copy group on the marketplace (admins only)."""
    user = require_admin(request)
    body = await request.json()
    groups, i = _group_or_404(group_id)
    g = dict(groups[i])
    before = marketplace.sharing_of(g)
    g["sharing"] = marketplace.normalize_sharing(body, g.get("sharing"))
    groups[i] = g
    copy.save_groups(groups)
    after = g["sharing"]
    if before["enabled"] != after["enabled"]:
        db.log_action(user["id"], user["email"], "copy_share", after["title"] or g.get("name", ""), "published" if after["enabled"] else "unpublished")
        state.log_event("info", f"Copy group '{g.get('name')}' {'published on' if after['enabled'] else 'removed from'} the marketplace")
    if before["enabled"] and not after["enabled"]:
        r = copy._runners.get((context.get_area(), group_id))
        await copy.release_followers(context.get_area(), group_id, [f["spec"] for f in (r.external if r else [])])
    await copy.sync_area(context.get_area())          # unpublished → subscribers' accounts leave the mirror
    return {**_with_status(g, copy.statuses(context.get_area())), "subscriber_count": db.subscriber_counts(context.get_area()).get(f"copy:{group_id}", 0)}


@router.get("/groups/{group_id}/subscribers")
async def api_group_subscribers(group_id: str, request: Request) -> list[dict[str, Any]]:
    require_admin(request)
    _group_or_404(group_id)
    return [{"id": s["id"], "email": s["email"], "enabled": s["enabled"], "created_at": s["created_at"],
             "accounts": len([a for a in s.get("accounts") or [] if isinstance(a, dict) and a.get("enabled", True)])}
            for s in db.list_subscribers(context.get_area(), f"copy:{group_id}")]


@router.delete("/groups/{group_id}/subscribers/{sub_id}")
async def api_group_remove_subscriber(group_id: str, sub_id: int, request: Request) -> dict[str, Any]:
    """Publisher removes a follower ("kick"); the follower keeps their positions."""
    user = require_admin(request)
    _group_or_404(group_id)
    removed = db.delete_subscription(sub_id, publisher_area_id=context.get_area())
    if not removed or removed["webhook_id"] != f"copy:{group_id}":
        raise HTTPException(status_code=404, detail="Subscriber not found")
    email = db.area_owner_email(removed["area_id"]) or str(removed["area_id"])
    db.log_action(user["id"], user["email"], "subscriber_remove", email, f"copy group {group_id}")
    state.log_event("info", f"Subscriber {email} removed from copy group {group_id}")
    await copy.release_followers(context.get_area(), group_id, copy._enabled_specs(removed.get("accounts")))
    await copy.sync_area(context.get_area())
    return {"status": "deleted", "id": sub_id}


@router.post("/groups")
async def api_create_group(request: Request) -> dict[str, Any]:
    body = await request.json()
    groups = copy.load_groups()
    if len(groups) >= 50:
        raise HTTPException(status_code=400, detail="At most 50 copy groups per area")
    try:
        g = _apply(copy.new_group(str(body.get("name") or "Copy group")), body)
        g["enabled"] = False if "leader" not in body else bool(body.get("enabled", False))
        if "leader" in body:
            copy.validate_group(g, groups, trade_accounts_overview())
    except (TypeError, ValueError, KeyError, AttributeError) as exc:
        raise HTTPException(status_code=400, detail=f"Invalid copy group: {exc}") from exc
    copy.save_groups([*groups, g])
    state.log_event("info", f"Copy group '{g['name']}' created")
    await copy.sync_area(context.get_area())
    return _with_status(g, copy.statuses(context.get_area()))


@router.put("/groups/{group_id}")
async def api_update_group(group_id: str, request: Request) -> dict[str, Any]:
    body = await request.json()
    groups, i = _group_or_404(group_id)
    try:
        g = _apply(dict(groups[i]), body)
        if g.get("enabled") or any(k in body for k in ("leader", "followers", "feed_loss_flatten_s")):
            copy.validate_group(g, groups, trade_accounts_overview())
    except (TypeError, ValueError, KeyError, AttributeError) as exc:
        raise HTTPException(status_code=400, detail=f"Invalid copy group: {exc}") from exc
    groups[i] = g
    copy.save_groups(groups)
    state.log_event("info", f"Copy group '{g['name']}' updated")
    await copy.sync_area(context.get_area())
    return _with_status(g, copy.statuses(context.get_area()))


@router.delete("/groups/{group_id}")
async def api_delete_group(group_id: str) -> dict[str, Any]:
    groups, i = _group_or_404(group_id)
    removed = groups.pop(i)
    r = copy.runner(context.get_area(), group_id)
    if r is not None:
        await r.orders.cancel_all(reason="group deleted")      # never leave twins resting at the broker
    copy.save_groups(groups)
    await copy.sync_area(context.get_area())
    history.defer(db.delete_copy_state, context.get_area(), group_id)   # behind the runner's queued state writes
    db.delete_copy_twins(context.get_area(), group_id)
    dropped = db.delete_subscriptions_for_webhook(context.get_area(), f"copy:{group_id}")
    state.log_event("info", f"Copy group '{removed.get('name')}' deleted" + (f" ({dropped} subscription(s) removed)" if dropped else ""))
    return {"status": "deleted", "id": group_id}


async def _set_enabled(group_id: str, enabled: bool) -> dict[str, Any]:
    groups, i = _group_or_404(group_id)
    g = dict(groups[i])
    g["enabled"] = enabled
    if enabled:
        try:
            copy.validate_group(g, groups, trade_accounts_overview())
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    groups[i] = g
    if not enabled:
        r = copy.runner(context.get_area(), group_id)
        if r is not None:
            await r.orders.cancel_all(reason="group disabled")
    copy.save_groups(groups)
    await copy.sync_area(context.get_area())
    if not enabled:
        history.defer(db.delete_copy_state, context.get_area(), group_id)     # a re-enable starts from a clean baseline (ordered behind queued writes)
    state.log_event("info", f"Copy group '{g['name']}' {'enabled' if enabled else 'disabled'}")
    return _with_status(g, copy.statuses(context.get_area()))


@router.post("/groups/{group_id}/enable")
async def api_enable_group(group_id: str) -> dict[str, Any]:
    return await _set_enabled(group_id, True)


@router.post("/groups/{group_id}/disable")
async def api_disable_group(group_id: str) -> dict[str, Any]:
    return await _set_enabled(group_id, False)


def _runner_or_409(group_id: str) -> copy.GroupRunner:
    _group_or_404(group_id)
    r = copy.runner(context.get_area(), group_id)
    if r is None:
        raise HTTPException(status_code=409, detail="Copy group is not running (enable it first)")
    return r


@router.post("/groups/{group_id}/resume")
async def api_resume_group(group_id: str) -> dict[str, Any]:
    """Clear a feed-loss pause; mirroring resumes on the leader's next change."""
    r = _runner_or_409(group_id)
    r.paused, r.pause_reason = False, ""
    r._record("resumed", detail="resumed by user")
    state.log_event("info", f"Copy group '{r.group['name']}' resumed")
    return r.status()


@router.post("/groups/{group_id}/sync")
async def api_sync_group(group_id: str) -> dict[str, Any]:
    """Copy the leader's current positions now (drops the baseline)."""
    r = _runner_or_409(group_id)
    if not config.load_settings().get("trading_enabled"):
        raise HTTPException(status_code=409, detail="Trading switch is off")
    n = await r.sync_now()
    state.log_event("info", f"Copy group '{r.group['name']}' synced ({n} contract(s))")
    return {**r.status(), "synced": n}


@router.post("/groups/{group_id}/flatten")
async def api_flatten_group(group_id: str) -> dict[str, Any]:
    """Close every mirrored position on the followers and pause the group."""
    r = _runner_or_409(group_id)
    n = await r.flatten_followers(reason="flattened by user")
    r.paused, r.pause_reason = True, "flattened by user — resume to mirror again"
    r._record("paused", detail=r.pause_reason)
    state.log_event("warn", f"Copy group '{r.group['name']}' flattened by user ({n} order(s))")
    return {**r.status(), "flattened": n}


@router.get("/status")
async def api_status() -> dict[str, Any]:
    return copy.statuses(context.get_area())


@router.get("/events")
async def api_events(group_id: str = "", limit: int = 100) -> list[dict[str, Any]]:
    return db.list_copy_events(context.get_area(), group_id=group_id, limit=limit)

"""Web Push: the service worker, VAPID public key, per-device subscriptions and a test push."""
from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from .. import config, context, db, push, security
from ..web import BASE_DIR

MAX_DEVICES_PER_AREA = 25

router = APIRouter(tags=["push"])


@router.get("/sw.js")
async def service_worker() -> Response:
    """The service worker must live at the site root to control ``/``."""
    path = BASE_DIR / "static" / "js" / "sw.js"
    return Response(content=path.read_bytes(), media_type="application/javascript",
                    headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"})


@router.get("/api/push/public-key")
async def api_push_key() -> dict[str, Any]:
    if not push.available():
        raise HTTPException(status_code=503, detail="Push is not available on this server (pywebpush missing)")
    return {"public_key": push.public_key(), "enabled": bool(config.load_settings().get("alert_push_enabled", True))}


@router.get("/api/push/subscriptions")
async def api_push_list(request: Request) -> list[dict[str, Any]]:
    return db.list_push_subscriptions(context.get_area(), public=True)


@router.post("/api/push/subscribe")
async def api_push_subscribe(request: Request) -> dict[str, Any]:
    """Register this device's push subscription (from ``PushManager.subscribe``)."""
    body = await request.json()
    sub = (body or {}).get("subscription") or {}
    endpoint = str(sub.get("endpoint") or "")
    keys = sub.get("keys") or {}
    if not endpoint.startswith("https://") or not keys.get("p256dh") or not keys.get("auth"):
        raise HTTPException(status_code=400, detail="A push subscription with endpoint and keys is required")
    if len(endpoint) > 2048 or len(str(keys["p256dh"])) > 200 or len(str(keys["auth"])) > 100:
        raise HTTPException(status_code=400, detail="Push subscription is malformed")
    # The bridge will POST to this URL later — never let it be an internal address.
    problem = await asyncio.to_thread(security.check_outbound_url, endpoint)
    if problem:
        raise HTTPException(status_code=400, detail=f"Push endpoint rejected: {problem}")
    existing = db.list_push_subscriptions(context.get_area())
    if len(existing) >= MAX_DEVICES_PER_AREA and not any(s["endpoint"] == endpoint for s in existing):
        raise HTTPException(status_code=400, detail=f"At most {MAX_DEVICES_PER_AREA} push devices per workspace — remove one first")
    user = request.state.user
    rec = db.upsert_push_subscription(context.get_area(), user["id"], endpoint, str(keys["p256dh"]), str(keys["auth"]),
                                      device=str((body or {}).get("device") or "")[:120])
    return {"id": rec["id"], "device": rec["device"], "devices": len(db.list_push_subscriptions(context.get_area()))}


@router.post("/api/push/known")
async def api_push_known(request: Request) -> dict[str, Any]:
    """Is this browser's subscription registered for the current area? (Endpoints never leave the server.)"""
    body = await request.json()
    endpoint = str((body or {}).get("endpoint") or "")
    subs = db.list_push_subscriptions(context.get_area())
    match = next((s for s in subs if s["endpoint"] == endpoint), None)
    return {"known": bool(match), "id": match["id"] if match else None}


@router.delete("/api/push/subscribe")
async def api_push_unsubscribe(request: Request) -> dict[str, Any]:
    body = await request.json()
    endpoint = str((body or {}).get("endpoint") or "")
    sub_id = (body or {}).get("id")
    removed = db.delete_push_subscription(context.get_area(), int(sub_id)) if sub_id else db.delete_push_subscription_by_endpoint(context.get_area(), endpoint)
    return {"removed": bool(removed)}


@router.post("/api/push/diag")
async def api_push_diag(request: Request) -> dict[str, Any]:
    """Admin: server push identity + a live, non-pruning send to every device."""
    from ..web import require_admin
    require_admin(request)
    return push.diagnose(context.get_area())


@router.post("/api/push/test")
async def api_push_test(request: Request) -> dict[str, Any]:
    """Send a test push — to one device (``{"id": …}``) or to all of the area's devices."""
    body = await request.json() if request.headers.get("content-length", "0") not in ("", "0") else {}
    only = [int(body["id"])] if isinstance(body, dict) and body.get("id") else None
    result = await push.send(context.get_area(), "Fluxbridge test", "Push notifications work on this device.",
                             url="/#/settings/alerts", tag="test", only_ids=only)
    if result.get("error"):
        raise HTTPException(status_code=503, detail=result["error"])
    return result

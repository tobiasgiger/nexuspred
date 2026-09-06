"""FastAPI router for the Discord signal module.

Mounted onto the bridge's existing ``app`` (see ``app.main``), so it lives on the
same server, port and HTTP Basic auth as the rest of the dashboard/API — no
second server, no open endpoint exposing webhook secrets.

Endpoints (all under ``/api/discord``):

* ``GET  /config``  — current config, secrets masked
* ``POST /config``  — save config live (masked secrets are preserved), then
  reconcile the listener without a restart
* ``GET  /status``  — listener/connection status
* ``GET  /signals`` — recent events (ring-buffer snapshot, for initial load)
* ``GET  /stream``  — Server-Sent Events: live signal feed (no polling)
* ``POST /test``    — push a synthetic embed through the pipeline (acceptance
  testing: fan-out, disabled targets, secret header, dry-run)
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from .. import config, context, security, state
from ..web import require_feature
from . import hub, listener, pipeline
from .parser import embed_from_dict

FEATURE = "discord_signals"

router = APIRouter(prefix="/api/discord", tags=["discord"])

_MASK = "********"


# --------------------------------------------------------------------- config
@router.get("/config")
async def get_config() -> dict[str, Any]:
    s = config.public_settings()
    return {
        "discord_enabled": s.get("discord_enabled", False),
        "discord_dry_run": s.get("discord_dry_run", False),
        "discord_user_token": s.get("discord_user_token", ""),  # masked by public_settings
        "discord_channels": s.get("discord_channels", []),      # target secrets masked
    }


def _merge_channels(incoming: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Clean incoming channels; restore masked target secrets from stored config.

    Masked secrets ('********') keep the previously stored value (matched by
    channel id + target url), so editing a target's label doesn't wipe its
    secret — mirroring the token_accounts pattern.
    """
    existing = config.load_settings().get("discord_channels") or []
    prev_secret: dict[tuple[str, str], str] = {}
    for c in existing:
        cid = str(c.get("id", ""))
        for t in c.get("targets") or []:
            prev_secret[(cid, t.get("url", ""))] = t.get("secret", "")

    cleaned: list[dict[str, Any]] = []
    for c in incoming or []:
        cid = str(c.get("id", "")).strip()
        if not cid:
            continue
        targets: list[dict[str, Any]] = []
        for t in c.get("targets") or []:
            webhook_id = str(t.get("webhook_id", "")).strip()
            url = str(t.get("url", "")).strip()
            # A target is either a bridge-webhook reference or a custom URL.
            if not webhook_id and not url:
                continue
            secret = t.get("secret", "")
            if secret == _MASK:
                secret = prev_secret.get((cid, url), "")
            targets.append({
                "label": str(t.get("label", "")).strip(),
                "webhook_id": webhook_id,
                "url": "" if webhook_id else url,
                "secret": "" if webhook_id else secret,
                "enabled": bool(t.get("enabled", True)),
            })
        cleaned.append({
            "id": cid,
            "label": str(c.get("label", "")).strip() or f"channel {cid}",
            "enabled": bool(c.get("enabled", True)),
            "targets": targets,
        })
    return cleaned


async def _validate_target_urls(channels: list[dict[str, Any]]) -> None:
    """Custom target URLs are POSTed by the bridge itself — refuse anything that
    would make it call into loopback / private networks (SSRF)."""
    seen: set[str] = set()
    for c in channels:
        for t in c.get("targets") or []:
            url = t.get("url") or ""
            if not url or url in seen:
                continue
            seen.add(url)
            problem = await asyncio.to_thread(security.check_outbound_url, url)
            if problem:
                raise HTTPException(status_code=400, detail=f"Target '{t.get('label') or url}': {problem}")


@router.post("/config")
async def save_config(request: Request) -> dict[str, Any]:
    require_feature(request, FEATURE)
    body = await request.json()
    updates: dict[str, Any] = {}
    if "discord_enabled" in body:
        updates["discord_enabled"] = bool(body["discord_enabled"])
    if "discord_dry_run" in body:
        updates["discord_dry_run"] = bool(body["discord_dry_run"])
    if "discord_user_token" in body:
        tok = body["discord_user_token"]
        # A masked token means "keep the stored one".
        if tok != _MASK:
            updates["discord_user_token"] = str(tok or "").strip()
    if "discord_channels" in body:
        updates["discord_channels"] = _merge_channels(body["discord_channels"])
        await _validate_target_urls(updates["discord_channels"])

    config.save_settings(updates)
    state.log_event("info", "[discord] configuration updated")

    # Reconcile the listener with the new config (no process restart).
    try:
        await listener.manager_for(context.get_area()).apply_config()
    except Exception as exc:  # noqa: BLE001
        state.log_event("warn", f"[discord] apply_config failed: {exc}")

    return await get_config()


# --------------------------------------------------------------------- status
@router.get("/status")
async def get_status() -> dict[str, Any]:
    return listener.manager_for(context.get_area()).status()


# -------------------------------------------------------------------- signals
@router.get("/signals")
async def get_signals() -> list[dict[str, Any]]:
    return hub.recent()


# ---------------------------------------------------------------------- stream
@router.get("/stream")
async def stream(request: Request) -> StreamingResponse:
    """Server-Sent Events feed of live signal events (no polling on the client)."""
    area = context.get_area()  # capture now; the generator runs outside request ctx
    queue = hub.subscribe(area)

    async def event_gen():
        try:
            # Prime the stream so proxies flush headers immediately.
            yield ": connected\n\n"
            yield "event: ping\ndata: {}\n\n"  # immediate liveness ping
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=10.0)
                    yield f"data: {json.dumps(event)}\n\n"
                except asyncio.TimeoutError:
                    # Named heartbeat (not a bare comment): reaches the client as a
                    # `ping` event so it can affirm liveness, and its bytes keep
                    # proxies from closing the connection as idle.
                    yield "event: ping\ndata: {}\n\n"
        finally:
            hub.unsubscribe(queue, area)

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # disable proxy buffering (nginx)
        },
    )


# ------------------------------------------------------------------------ test
@router.post("/test")
async def test_signal(request: Request) -> dict[str, Any]:
    """Push a synthetic embed through the full pipeline for a given channel.

    Body: ``{"channel_id": "...", "embed": {"title": "...", "fields": [...]}}``.
    ``force`` bypasses the channel enabled-check so you can test without a live
    Discord connection. Respects the dry-run switch, exactly like a real event.
    """
    require_feature(request, FEATURE)
    body = await request.json()
    channel_id = str(body.get("channel_id", "")).strip()
    if not channel_id:
        return {"error": "channel_id is required"}
    embed = embed_from_dict(body.get("embed") or {})
    event = await pipeline.process_embed(
        embed, channel_id, source="test", force=bool(body.get("force", True))
    )
    return {"event": event}

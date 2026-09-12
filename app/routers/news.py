"""Economic calendar + news lock: events, status, settings, manual refresh."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from .. import config, context, news, state
from ..web import require_admin

router = APIRouter(prefix="/api/news", tags=["news"])


@router.get("")
async def api_news(hours: float = 72.0) -> dict[str, Any]:
    """Upcoming events that count for this workspace, with their lock windows."""
    if not news._events:
        await news.refresh()
    return {"status": news.status(), "settings": news.settings_for(context.get_area()),
            "events": news.windows(context.get_area(), hours=max(1.0, min(hours, 24 * 14)))}


@router.put("/settings")
async def api_news_settings(request: Request) -> dict[str, Any]:
    require_admin(request)
    body = await request.json()
    try:
        block = news.normalize(body)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"Invalid news-lock settings: {exc}") from exc
    config.save_settings({"news_lock": block})
    state.log_event("info", f"News lock {'enabled' if block['enabled'] else 'disabled'}: {', '.join(block['currencies'])} "
                            f"{'/'.join(block['impacts'])}, −{block['before']}/+{block['after']} min, {block['action']}")
    return {"settings": block, "status": news.status()}


@router.post("/refresh")
async def api_news_refresh(request: Request) -> dict[str, Any]:
    require_admin(request)
    r = await news.refresh(force=True)
    return {**r, "status": news.status()}

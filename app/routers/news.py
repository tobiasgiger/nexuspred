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


@router.get("/calendar")
async def api_news_calendar(days: float = 7.0, start: str = "", end: str = "", currencies: str = "", impacts: str = "",
                            q: str = "", relevant: bool = False) -> dict[str, Any]:
    """The full calendar (feed + manual) for a range — default the next seven days —
    with optional currency / impact / text filters. Every row says whether the
    news lock counts it and, if so, its lock window."""
    from datetime import datetime, timedelta, timezone
    if not news._events:
        await news.refresh()
    now = datetime.now(timezone.utc)
    lo = news._parse_ts(start) if start else now - timedelta(hours=6)
    hi = news._parse_ts(end) if end else now + timedelta(days=max(0.1, min(days, 60.0)))
    if lo is None or hi is None:
        raise HTTPException(status_code=400, detail="start / end must be ISO 8601 timestamps")
    cur = {c.strip().upper() for c in currencies.split(",") if c.strip()} or None
    imp = {i.strip().title() for i in impacts.split(",") if i.strip()} or None
    rows = news.calendar(context.get_area(), start=lo, end=hi, currencies=cur, impacts=imp, query=q, relevant_only=relevant, now=now)
    return {"status": news.status(), "range": {"start": lo.isoformat(), "end": hi.isoformat()},
            "currencies": news.feed_currencies(), "impacts": list(news.IMPACTS), "events": rows}


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

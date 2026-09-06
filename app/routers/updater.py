"""GitHub-backed self-updater endpoints."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from .. import updater
from ..web import require_admin

router = APIRouter(prefix="/api/update", tags=["updater"])


@router.get("/check")
async def api_update_check() -> dict[str, Any]:
    return await updater.check_for_update()


@router.post("/apply")
async def api_update_apply(request: Request) -> dict[str, Any]:
    """Pull + restart the whole process (every tenant): admins only."""
    require_admin(request)
    result = await updater.apply_update()
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("message"))
    return result

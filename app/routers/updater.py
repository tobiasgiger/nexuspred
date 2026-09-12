"""GitHub-backed self-updater endpoints."""
from __future__ import annotations

import asyncio
import os
import sqlite3
import tempfile
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from .. import db, updater
from ..web import require_admin

router = APIRouter(prefix="/api/update", tags=["updater"])


@router.get("/check")
async def api_update_check() -> dict[str, Any]:
    return await updater.check_for_update()


def _backup_to(path: str) -> None:
    """A consistent copy of the live database via SQLite's online backup API."""
    src = sqlite3.connect(str(db.DB_FILE))
    dst = sqlite3.connect(path)
    try:
        with dst:
            src.backup(dst)
    finally:
        dst.close()
        src.close()


@router.get("/backup")
async def api_download_backup(request: Request) -> FileResponse:
    """Admin: download the whole database (every tenant) as one SQLite file —
    the way to move an installation, e.g. from Render to your own server
    (``fluxbridge restore FILE``). Consistent even while the bridge is trading."""
    require_admin(request)
    db.init()
    fd, path = tempfile.mkstemp(prefix="fluxbridge-backup-", suffix=".db")
    os.close(fd)
    try:
        await asyncio.to_thread(_backup_to, path)
    except Exception as exc:  # noqa: BLE001
        os.unlink(path)
        raise HTTPException(status_code=500, detail=f"backup failed: {exc}") from exc
    name = f"fluxbridge-backup-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.db"
    return FileResponse(path, media_type="application/vnd.sqlite3", filename=name,
                        background=BackgroundTask(os.unlink, path))


@router.post("/apply")
async def api_update_apply(request: Request) -> dict[str, Any]:
    """Pull + restart the whole process (every tenant): admins only."""
    require_admin(request)
    result = await updater.apply_update()
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("message"))
    return result

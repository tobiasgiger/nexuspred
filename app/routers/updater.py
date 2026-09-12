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

from .. import crypto, db, updater
from ..security import client_ip
from ..web import require_admin

router = APIRouter(prefix="/api/update", tags=["updater"])


@router.get("/check")
async def api_update_check() -> dict[str, Any]:
    return await updater.check_for_update()


# meta rows that never leave the server inside a backup: the cookie-signing /
# encryption secret (when it is DB-stored) and the Web-Push private key. A
# backup holding them would let whoever downloads it forge any user's session
# cookie and decrypt every tenant's broker tokens offline.
BACKUP_EXCLUDED_META = ("session_secret", "vapid_private_pem")


def _backup_to(path: str) -> None:
    """A consistent copy of the live database via SQLite's online backup API,
    minus :data:`BACKUP_EXCLUDED_META`."""
    src = sqlite3.connect(str(db.DB_FILE))
    dst = sqlite3.connect(path)
    try:
        with dst:
            src.backup(dst)
        with dst:
            dst.executemany("DELETE FROM meta WHERE key=?", [(k,) for k in BACKUP_EXCLUDED_META])
            # unused one-time capabilities never travel: a reset link, an invite or a
            # pairing code lifted from a backup must not open an account or an agent slot
            dst.execute("DELETE FROM password_resets WHERE used_at IS NULL")
            dst.execute("DELETE FROM invites WHERE used_by IS NULL")
            dst.execute("DELETE FROM agent_pairings")
        dst.execute("VACUUM")               # the deleted rows must not survive in free pages
    finally:
        dst.close()
        src.close()


@router.get("/backup")
async def api_download_backup(request: Request) -> FileResponse:
    """Admin: download the whole database (every tenant) as one SQLite file —
    the way to move an installation, e.g. from Render to your own server
    (``fluxbridge restore FILE``). Consistent even while the bridge is trading."""
    admin = require_admin(request)
    db.init()
    if crypto.key_source() == "db":
        raise HTTPException(status_code=409, detail=(
            "The encryption key still lives inside the database, so a backup would carry it. "
            "Set SESSION_SECRET (or NEXUSPRED_ENCRYPTION_KEY) in the environment and restart — "
            "the stored secrets are re-encrypted under it at startup — then download the backup."))
    db.log_action(admin["id"], admin["email"], "backup_download", client_ip(request))
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

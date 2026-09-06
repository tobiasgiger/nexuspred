"""The browser token-extractor extension, served as a downloadable zip."""
from __future__ import annotations

import asyncio
import io
import zipfile

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

from ..web import BASE_DIR

router = APIRouter(prefix="/api/extension", tags=["extension"])

_EXT_ZIP: bytes | None = None


def _build_extension_zip() -> bytes:
    ext_dir = BASE_DIR / "browser-extension" / "token-extractor"
    if not ext_dir.is_dir():
        raise HTTPException(status_code=404, detail="Extension not found")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for path in sorted(ext_dir.rglob("*")):
            if path.is_file():
                # Keep the top-level folder name so unzip yields token-extractor/.
                z.write(path, path.relative_to(ext_dir.parent))
    return buf.getvalue()


@router.get("/token-extractor.zip")
async def extension_zip() -> Response:
    """Serve the browser token-extractor extension as a downloadable .zip so it
    can be installed via chrome://extensions -> Load unpacked (Tools tab).
    Built once per process (the files ship with the code)."""
    global _EXT_ZIP
    if _EXT_ZIP is None:
        _EXT_ZIP = await asyncio.to_thread(_build_extension_zip)
    return Response(
        content=_EXT_ZIP,
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="token-extractor.zip"'},
    )

"""Execution agents: pairing, the relay endpoints the agent talks to, and the
admin endpoints the dashboard uses (list / pairing codes / revoke / download)."""
from __future__ import annotations

import io
import json
import re
import time
import zipfile
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from .. import config, context, db, http, relay
from ..security import client_ip
from ..web import BASE_DIR, base_url, require_admin

# Windows build of the agent, produced by .github/workflows/agent-exe.yml and
# published to a rolling GitHub release; bundled into the preconfigured download.
AGENT_EXE_URL = (f"https://github.com/{config.GITHUB_OWNER}/{config.GITHUB_REPO}"
                 "/releases/download/agent-latest/fluxbridge-agent.exe")
_exe_cache: tuple[float, bytes] | None = None
EXE_CACHE_S = 3600.0


async def fetch_agent_exe() -> bytes | None:
    """The latest agent .exe (cached for an hour); None when unavailable."""
    global _exe_cache
    if _exe_cache and time.monotonic() - _exe_cache[0] < EXE_CACHE_S:
        return _exe_cache[1]
    try:
        resp = await http.client("outbound").get(AGENT_EXE_URL, follow_redirects=True, timeout=60.0)
        if resp.status_code == 200 and resp.content[:2] == b"MZ":
            _exe_cache = (time.monotonic(), resp.content)
            return resp.content
    except Exception:  # noqa: BLE001 - the Python files still work without the exe
        pass
    return None

router = APIRouter(tags=["agents"])

AGENT_DIR = BASE_DIR / "agent"


# ------------------------------------------------------------ agent-facing
def _agent_from_request(request: Request) -> dict[str, Any]:
    auth = request.headers.get("authorization", "")
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    agent = relay.authenticate(token)
    if not agent:
        raise HTTPException(status_code=401, detail="Invalid or revoked agent token")
    return agent


@router.post("/api/agent/pair")
async def api_agent_pair(request: Request) -> dict[str, Any]:
    """Exchange a one-time pairing code for an agent token (unauthenticated,
    rate-limited). The code was created by an admin in the dashboard."""
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="JSON object expected")
    code = str(body.get("code") or "").strip().upper().replace("-", "")
    if not code:
        raise HTTPException(status_code=400, detail="code is required")
    pairing = db.consume_agent_pairing(code)
    if not pairing:
        db.log_action(None, "", "agent_pair_failed", client_ip(request), "invalid or expired pairing code")
        raise HTTPException(status_code=400, detail="Pairing code is invalid, expired or already used")
    name = str(body.get("name") or pairing["name"] or "agent").strip()[:60]
    token, agent = db.create_agent(pairing["area_id"], name, version=str(body.get("version") or "")[:40],
                                   ip=client_ip(request))
    with context.use_area(pairing["area_id"]):
        from .. import state
        state.log_event("info", f"Execution agent '{name}' paired from {client_ip(request)}")
    db.log_action(None, "", "agent_paired", name, f"from {client_ip(request)}")
    return {"token": token, "agent_id": agent["id"], "name": agent["name"], "poll_path": "/api/agent/jobs"}


@router.get("/api/agent/jobs")
async def api_agent_jobs(request: Request, wait: float = 25.0) -> dict[str, Any]:
    """Long-poll for relay jobs (the agent's heartbeat as well)."""
    agent = _agent_from_request(request)
    db.touch_agent(agent["id"], ip=client_ip(request), version=request.headers.get("x-agent-version", ""))
    jobs = await relay.next_jobs(agent["id"], min(max(float(wait), 0.0), 30.0))
    return {"jobs": jobs, "agent": agent["name"]}


@router.post("/api/agent/jobs/{job_id}/result")
async def api_agent_result(job_id: str, request: Request) -> dict[str, Any]:
    agent = _agent_from_request(request)
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="JSON object expected")
    ok = relay.deliver(agent["id"], job_id, int(body.get("status_code") or 0),
                       str(body.get("text") or ""), str(body.get("error") or ""))
    return {"accepted": ok}


# ------------------------------------------------------------- dashboard
@router.get("/api/agents")
async def api_agents(request: Request) -> list[dict[str, Any]]:
    online = relay.online_ids()
    return [{**a, "online": a["id"] in online, "pending_jobs": relay.pending(a["id"])}
            for a in db.list_agents(context.get_area())]


@router.post("/api/agents/pairing-code")
async def api_agent_pairing_code(request: Request) -> dict[str, Any]:
    """Admin: a one-time code (valid 15 min) to pair a new agent."""
    user = require_admin(request)
    body = await request.json()
    name = str((body or {}).get("name") or "").strip()[:60] or "agent"
    code = db.create_agent_pairing(context.get_area(), name)
    db.log_action(user["id"], user["email"], "agent_pairing_code", name)
    return {"code": f"{code[:4]}-{code[4:]}", "expires_in": db.AGENT_PAIRING_TTL_S, "name": name}


@router.delete("/api/agents/{agent_id}")
async def api_agent_revoke(agent_id: int, request: Request) -> dict[str, Any]:
    user = require_admin(request)
    agent = db.get_agent(context.get_area(), agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    db.delete_agent(context.get_area(), agent_id)
    db.log_action(user["id"], user["email"], "agent_revoke", agent["name"])
    from .. import state, tradovate
    state.log_event("warn", f"Execution agent '{agent['name']}' revoked — logins assigned to it now fail until re-assigned")
    tradovate.manager_for(context.get_area()).reload()
    return {"status": "deleted", "id": agent_id}


@router.put("/api/agents/{agent_id}")
async def api_agent_rename(agent_id: int, request: Request) -> dict[str, Any]:
    require_admin(request)
    body = await request.json()
    name = str((body or {}).get("name") or "").strip()[:60]
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    if not db.rename_agent(context.get_area(), agent_id, name):
        raise HTTPException(status_code=404, detail="Agent not found")
    return db.get_agent(context.get_area(), agent_id) or {}


def _agent_files() -> list[tuple[str, bytes]]:
    if not AGENT_DIR.is_dir():
        raise HTTPException(status_code=404, detail="Agent files not found")
    out = []
    for path in sorted(AGENT_DIR.rglob("*")):
        if path.is_file() and path.suffix in (".py", ".bat", ".sh", ".md", ".txt"):
            out.append((str(path.relative_to(AGENT_DIR)), path.read_bytes()))
    return out


def _zip(files: list[tuple[str, bytes]], folder: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in files:
            info = zipfile.ZipInfo(f"{folder}/{name}")
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (0o755 if name.endswith((".sh", ".exe")) else 0o644) << 16
            z.writestr(info, data)
    return buf.getvalue()


@router.post("/api/agents/bundle")
async def api_agent_bundle(request: Request) -> Response:
    """Admin: a **preconfigured** agent — the zip already holds ``agent.json``
    with this bridge's URL and a freshly issued token, plus the Windows .exe
    when the release build is reachable. Unzip, start, done — nothing to type."""
    user = require_admin(request)
    body = await request.json()
    name = str((body or {}).get("name") or "").strip()[:60] or "agent"
    token, agent = db.create_agent(context.get_area(), name, version="", ip="")
    db.log_action(user["id"], user["email"], "agent_bundle", name, "preconfigured download")
    cfg = {"bridge": base_url(request), "token": token, "name": agent["name"], "agent_id": agent["id"]}
    files = _agent_files() + [("agent.json", json.dumps(cfg, indent=2).encode("utf-8"))]
    exe = await fetch_agent_exe()
    if exe:
        files.append(("fluxbridge-agent.exe", exe))
    safe = re.sub(r"[^A-Za-z0-9_-]+", "-", name).strip("-") or "agent"
    return Response(content=_zip(files, f"fluxbridge-agent-{safe}"), media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="fluxbridge-agent-{safe}.zip"',
                             "X-Agent-Id": str(agent["id"]), "X-Agent-Exe": "1" if exe else "0"})


_ZIP: Optional[bytes] = None


@router.get("/api/agents/download.zip")
async def api_agent_download(request: Request) -> Response:
    """The agent script + start files as a zip (the files ship with the code)."""
    require_admin(request)
    global _ZIP
    if _ZIP is None:
        _ZIP = _zip(_agent_files(), "fluxbridge-agent")
    return Response(content=_ZIP, media_type="application/zip",
                    headers={"Content-Disposition": 'attachment; filename="fluxbridge-agent.zip"'})

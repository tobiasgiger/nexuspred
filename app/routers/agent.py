"""Execution agents: pairing, the relay endpoints the agent talks to, and the
admin endpoints the dashboard uses (list / pairing codes / revoke / download)."""
from __future__ import annotations

import io
import zipfile
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from .. import context, db, relay
from ..security import client_ip
from ..web import BASE_DIR, require_admin

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


_ZIP: Optional[bytes] = None


@router.get("/api/agents/download.zip")
async def api_agent_download(request: Request) -> Response:
    """The agent script + start files as a zip (the files ship with the code)."""
    require_admin(request)
    global _ZIP
    if _ZIP is None:
        if not AGENT_DIR.is_dir():
            raise HTTPException(status_code=404, detail="Agent files not found")
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for path in sorted(AGENT_DIR.rglob("*")):
                if path.is_file() and path.suffix in (".py", ".bat", ".sh", ".md", ".txt"):
                    z.write(path, f"fluxbridge-agent/{path.relative_to(AGENT_DIR)}")
        _ZIP = buf.getvalue()
    return Response(content=_ZIP, media_type="application/zip",
                    headers={"Content-Disposition": 'attachment; filename="fluxbridge-agent.zip"'})

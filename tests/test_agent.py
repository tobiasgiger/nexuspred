"""Execution agents: pairing, relay long-poll + results, Tradovate routing,
offline handling, admin endpoints and the agent script's job runner."""
from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx
import pytest

from app import config, context, db, relay, tradovate
from app.main import app


async def _pair(anon_client, client, name="VPS 1"):
    code = (await client.post("/api/agents/pairing-code", json={"name": name})).json()["code"]
    r = await anon_client.post("/api/agent/pair", json={"code": code, "version": "1.0.0"})
    assert r.status_code == 200, r.text
    return r.json()


def _agent_client(token):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver",
                             headers={"Authorization": f"Bearer {token}"})


async def test_pairing_flow_and_admin_endpoints(client, anon_client):
    r = await client.post("/api/agents/pairing-code", json={"name": "VPS 1"})
    code = r.json()["code"]
    assert len(code) == 9 and code[4] == "-" and r.json()["expires_in"] == 900
    # wrong code → 400 and an audit row; right code → token; second use → 400
    assert (await anon_client.post("/api/agent/pair", json={"code": "NOPE-0000"})).status_code == 400
    paired = (await anon_client.post("/api/agent/pair", json={"code": code.lower(), "version": "1.0.0"})).json()
    assert paired["token"].startswith("fba_") and paired["name"] == "VPS 1"
    assert (await anon_client.post("/api/agent/pair", json={"code": code})).status_code == 400
    agents = (await client.get("/api/agents")).json()
    assert len(agents) == 1 and agents[0]["name"] == "VPS 1" and agents[0]["online"] is False
    assert "token" not in agents[0] and "token_hash" not in agents[0]
    # agent endpoints need the agent token, never the dashboard cookie
    assert (await client.get("/api/agent/jobs?wait=0")).status_code == 401
    assert (await anon_client.get("/api/agent/jobs?wait=0", headers={"Authorization": "Bearer fba_wrong"})).status_code == 401
    async with _agent_client(paired["token"]) as ac:
        r = await ac.get("/api/agent/jobs?wait=0")
        assert r.status_code == 200 and r.json() == {"jobs": [], "agent": "VPS 1"}
    assert (await client.get("/api/agents")).json()[0]["online"] is True
    # rename / revoke (admin only)
    assert (await client.put(f"/api/agents/{agents[0]['id']}", json={"name": "VPS Frankfurt"})).json()["name"] == "VPS Frankfurt"
    assert (await client.delete(f"/api/agents/{agents[0]['id']}")).json()["status"] == "deleted"
    async with _agent_client(paired["token"]) as ac:
        assert (await ac.get("/api/agent/jobs?wait=0")).status_code == 401   # revoked immediately
    actions = [a["action"] for a in db.list_audit(20)]
    assert {"agent_pairing_code", "agent_paired", "agent_pair_failed", "agent_revoke"} <= set(actions)


async def test_non_admin_cannot_manage_agents(admin, anon_client):
    from app import auth
    user = db.create_user("u@example.com", "password123", is_admin=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as c:
        c.cookies.set(auth.COOKIE, auth.make_session(user["id"]))
        assert (await c.post("/api/agents/pairing-code", json={})).status_code == 403
        assert (await c.get("/api/agents/download.zip")).status_code == 403


async def test_relay_round_trip_through_tradovate_session(client, anon_client):
    """A login assigned to the agent has its Tradovate call executed by the agent."""
    paired = await _pair(anon_client, client)
    with context.use_area(1):
        config.save_settings({"token_accounts": [{"name": "L1", "environment": "demo", "enabled": True,
                                                  "access_token": "tok", "agent_id": paired["agent_id"],
                                                  "accounts": [{"spec": "DEMO11", "id": 11, "enabled": True}]}]})
        mgr = tradovate.manager_for(1)
        mgr.reload()
        sess = mgr.all()[0]
    assert sess.agent_id == paired["agent_id"]
    sess._token_expires = None
    sess._token = "tok"
    # pretend the token is valid so no renewal happens
    from datetime import datetime, timedelta, timezone
    sess._token_expires = datetime.now(timezone.utc) + timedelta(hours=1)

    async def fake_agent():
        async with _agent_client(paired["token"]) as ac:
            await ac.get("/api/agent/jobs?wait=0")            # heartbeat → online
            for _ in range(50):
                r = await ac.get("/api/agent/jobs?wait=0.2")
                jobs = r.json()["jobs"]
                if jobs:
                    job = jobs[0]
                    assert job["method"] == "GET" and job["url"].endswith("/account/list")
                    assert job["headers"]["Authorization"] == "Bearer tok"
                    await ac.post(f"/api/agent/jobs/{job['id']}/result", json={"status_code": 200, "text": json.dumps([{"id": 11, "name": "DEMO11"}])})
                    return job
                await asyncio.sleep(0.05)
        return None

    agent_task = asyncio.create_task(fake_agent())
    await asyncio.sleep(0.1)
    accounts = await sess.list_accounts()
    assert accounts == [{"id": 11, "name": "DEMO11"}]
    assert await agent_task is not None
    # an error answer from the broker propagates as TradovateError with the status
    async def failing_agent():
        async with _agent_client(paired["token"]) as ac:
            for _ in range(50):
                jobs = (await ac.get("/api/agent/jobs?wait=0.2")).json()["jobs"]
                if jobs:
                    await ac.post(f"/api/agent/jobs/{jobs[0]['id']}/result", json={"status_code": 403, "text": "nope"})
                    return
                await asyncio.sleep(0.05)
    t = asyncio.create_task(failing_agent())
    with pytest.raises(tradovate.TradovateError, match="403 /account/list: nope"):
        await sess.list_accounts()
    await t


async def test_offline_agent_fails_loudly(client, anon_client):
    paired = await _pair(anon_client, client)
    with context.use_area(1):
        config.save_settings({"token_accounts": [{"name": "L1", "environment": "demo", "enabled": True,
                                                  "access_token": "tok", "agent_id": paired["agent_id"]}]})
        mgr = tradovate.manager_for(1)
        mgr.reload()
        sess = mgr.all()[0]
    from datetime import datetime, timedelta, timezone
    sess._token_expires = datetime.now(timezone.utc) + timedelta(hours=1)
    with pytest.raises(tradovate.TradovateError, match="offline"):
        await sess.list_accounts()
    # online but silent → times out with a clear message (short timeout for the test)
    relay.touch(paired["agent_id"])
    orig = relay.RESULT_TIMEOUT_EXTRA_S
    relay.RESULT_TIMEOUT_EXTRA_S = 0.1
    try:
        with pytest.raises(tradovate.TradovateError, match="did not answer"):
            await sess._request("GET", "/x", timeout=0.1)
    finally:
        relay.RESULT_TIMEOUT_EXTRA_S = orig


async def test_token_accounts_save_keeps_agent_and_accounts(client, anon_client):
    paired = await _pair(anon_client, client)
    with context.use_area(1):
        config.save_settings({"token_accounts": [{"name": "L1", "environment": "demo", "enabled": True, "access_token": "tok",
                                                  "accounts": [{"spec": "DEMO11", "id": 11, "enabled": True}]}]})
    r = await client.post("/api/token-accounts", json=[{"name": "L1", "environment": "demo", "enabled": True,
                                                        "access_token": "********", "agent_id": paired["agent_id"], "qty_multiplier": 1}])
    assert r.status_code == 200 and r.json()[0]["agent_id"] == paired["agent_id"]
    with context.use_area(1):
        s = config.load_settings()
    assert s["token_accounts"][0]["access_token"] == "tok" and s["token_accounts"][0]["accounts"][0]["spec"] == "DEMO11"
    st = (await client.get("/api/status")).json()
    assert st["trade_accounts"][0]["agent_id"] == paired["agent_id"]


async def test_agent_download_zip(client):
    r = await client.get("/api/agents/download.zip")
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    import io, zipfile
    names = zipfile.ZipFile(io.BytesIO(r.content)).namelist()
    assert "fluxbridge-agent/fluxbridge_agent.py" in names and "fluxbridge-agent/start-agent.bat" in names


# ------------------------------------------------------- the agent script
class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({"path": self.path, "auth": self.headers.get("Authorization")}).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers(); self.wfile.write(body)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        data = json.loads(self.rfile.read(n) or b"{}")
        if self.path.endswith("/fail"):
            self.send_response(422); self.end_headers(); self.wfile.write(b"bad order"); return
        self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers()
        self.wfile.write(json.dumps({"echo": data}).encode())

    def log_message(self, *a):  # silence
        pass


def test_agent_script_runs_jobs():
    import importlib.util, pathlib
    spec = importlib.util.spec_from_file_location("fluxbridge_agent", pathlib.Path("agent/fluxbridge_agent.py"))
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    srv = HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_port}"
    try:
        r = mod.run_job({"method": "GET", "url": base + "/v1/account/list", "headers": {"Authorization": "Bearer t"}, "params": {"ids": "1,2"}, "timeout": 5})
        assert r["status_code"] == 200 and json.loads(r["text"]) == {"path": "/v1/account/list?ids=1%2C2", "auth": "Bearer t"}
        r = mod.run_job({"method": "POST", "url": base + "/v1/order/placeorder", "headers": {}, "json": {"qty": 2}, "timeout": 5})
        assert json.loads(r["text"]) == {"echo": {"qty": 2}}
        r = mod.run_job({"method": "POST", "url": base + "/fail", "headers": {}, "json": {}, "timeout": 5})
        assert r["status_code"] == 422 and r["text"] == "bad order"          # broker errors are relayed, not raised
        r = mod.run_job({"method": "GET", "url": "http://127.0.0.1:1/x", "headers": {}, "timeout": 1})
        assert r["status_code"] == 0 and "error" in r
    finally:
        srv.shutdown()

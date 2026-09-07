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
    mod.allowed_url = lambda url: True   # the allowlist is covered by its own test; here a local stub plays Tradovate
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


async def test_preconfigured_bundle(client, monkeypatch):
    """The zip carries agent.json (bridge URL + fresh token) and the exe when the release is reachable."""
    from app.routers import agent as agent_router
    import io, zipfile

    async def fake_exe():
        return b"MZ fake exe"
    monkeypatch.setattr(agent_router, "fetch_agent_exe", fake_exe)
    monkeypatch.setattr(config, "PUBLIC_URL", "https://bridge.example.com")
    r = await client.post("/api/agents/bundle", json={"name": "VPS Frankfurt"})
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    assert r.headers["X-Agent-Exe"] == "1" and 'filename="fluxbridge-agent-VPS-Frankfurt.zip"' in r.headers["content-disposition"]
    z = zipfile.ZipFile(io.BytesIO(r.content))
    names = z.namelist()
    assert "fluxbridge-agent-VPS-Frankfurt/agent.json" in names and "fluxbridge-agent-VPS-Frankfurt/fluxbridge-agent.exe" in names
    cfg = json.loads(z.read("fluxbridge-agent-VPS-Frankfurt/agent.json"))
    assert cfg["bridge"] == "https://bridge.example.com" and cfg["token"].startswith("fba_") and cfg["name"] == "VPS Frankfurt"
    agents = (await client.get("/api/agents")).json()
    assert [a["name"] for a in agents] == ["VPS Frankfurt"] and cfg["agent_id"] == agents[0]["id"]
    # the embedded token works for the relay endpoints straight away
    async with _agent_client(cfg["token"]) as ac:
        assert (await ac.get("/api/agent/jobs?wait=0")).status_code == 200
    # without the exe the Python files still ship
    async def no_exe():
        return None
    monkeypatch.setattr(agent_router, "fetch_agent_exe", no_exe)
    r = await client.post("/api/agents/bundle", json={"name": "plain"})
    assert r.headers["X-Agent-Exe"] == "0" and "fluxbridge-agent-plain/fluxbridge_agent.py" in zipfile.ZipFile(io.BytesIO(r.content)).namelist()
    assert any(a["action"] == "agent_bundle" for a in db.list_audit(10))


# ------------------------------------------------------------ hardening
async def test_relay_refuses_non_tradovate_urls(client, anon_client):
    paired = await _pair(anon_client, client)
    relay.touch(paired["agent_id"])
    for bad in ("https://169.254.169.254/latest/meta-data", "http://demo.tradovateapi.com/v1/x",
                "https://demo.tradovateapi.com.evil.io/v1/x", "https://tradovateapi.com/v1/x"):
        with pytest.raises(ValueError):
            await relay.request(paired["agent_id"], method="GET", url=bad, headers={})
    assert relay.allowed_url("https://live.tradovateapi.com/v1/order/placeorder")
    assert relay.allowed_url("https://rpt-demo.tradovateapi.com/v1/reports/requestreport")


def test_agent_script_refuses_non_tradovate_urls():
    import importlib.util
    spec = importlib.util.spec_from_file_location("fluxbridge_agent", "agent/fluxbridge_agent.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for bad in ("https://169.254.169.254/latest/meta-data", "http://demo.tradovateapi.com/v1/x",
                "https://demo.tradovateapi.com.evil.io/v1/x", "ftp://demo.tradovateapi.com/", ""):
        r = mod.run_job({"url": bad, "method": "GET"})
        assert r["status_code"] == 0 and r["error"].startswith("refused"), bad
    assert mod.allowed_url("https://demo.tradovateapi.com/v1/auth/renewaccesstoken")


async def test_unknown_agent_tokens_are_not_cached(anon_client, admin):
    before = len(db._agents_by_hash)
    for i in range(50):
        r = await anon_client.get("/api/agent/jobs?wait=0", headers={"Authorization": f"Bearer fba_{'x' * 20}{i}"})
        assert r.status_code == 401
    assert len(db._agents_by_hash) == before


async def test_bundle_rejects_exe_with_wrong_checksum(client, monkeypatch):
    from app.routers import agent as agent_router
    import hashlib

    class Resp:
        def __init__(self, status, content=b"", text=""):
            self.status_code, self.content, self.text = status, content, text

    exe = b"MZ real exe"
    answers = {agent_router.AGENT_EXE_URL: Resp(200, exe),
               agent_router.AGENT_EXE_URL + ".sha256": Resp(200, text="deadbeef  fluxbridge-agent.exe")}

    class FakeClient:
        async def get(self, url, **kw):
            return answers[url]

    monkeypatch.setattr(agent_router.http, "client", lambda name="outbound": FakeClient())
    agent_router._exe_cache = None
    assert await agent_router.fetch_agent_exe() is None                 # checksum mismatch → not bundled
    answers[agent_router.AGENT_EXE_URL + ".sha256"] = Resp(200, text=hashlib.sha256(exe).hexdigest() + "  fluxbridge-agent.exe\n")
    agent_router._exe_cache = None
    assert await agent_router.fetch_agent_exe() == exe                 # matching checksum → bundled
    answers[agent_router.AGENT_EXE_URL + ".sha256"] = Resp(404)
    agent_router._exe_cache = None
    assert await agent_router.fetch_agent_exe() == exe                 # no checksum published (older release) → accepted
    agent_router._exe_cache = None


async def test_agent_id_must_belong_to_the_callers_area(client, anon_client):
    """A user of another workspace must not be able to route their logins through
    someone else's execution agent (their Tradovate tokens would land on that VPS)."""
    from app import auth
    paired = await _pair(anon_client, client)                       # admin's agent
    victim_agent = paired["agent_id"]
    u2 = db.create_user("other@example.com", "password123")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver",
                                 cookies={auth.COOKIE: auth.make_session(u2["id"])}) as c2:
        r = await c2.post("/api/token-accounts", json=[{"name": "L1", "environment": "demo", "enabled": True,
                                                        "access_token": "tok", "agent_id": victim_agent}])
        assert r.status_code == 400 and "not paired with this workspace" in r.json()["detail"]
        # 0 / missing is always fine
        r = await c2.post("/api/token-accounts", json=[{"name": "L1", "environment": "demo", "enabled": True, "access_token": "tok"}])
        assert r.status_code == 200 and r.json()[0]["agent_id"] == 0
    # the owner may assign it
    r = await client.post("/api/token-accounts", json=[{"name": "L1", "environment": "demo", "enabled": True,
                                                        "access_token": "tok", "agent_id": victim_agent}])
    assert r.status_code == 200 and r.json()[0]["agent_id"] == victim_agent
    # defence in depth: the relay itself refuses a foreign area
    relay.touch(victim_agent)
    with pytest.raises(relay.AgentOffline, match="not paired with this workspace"):
        await relay.request(victim_agent, method="GET", url="https://demo.tradovateapi.com/v1/x", headers={},
                            area_id=db.user_primary_area(u2["id"]))


async def test_deleting_a_user_revokes_their_agents(client, anon_client, admin):
    from app import auth
    u2 = db.create_user("owner2@example.com", "password123", is_admin=True)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver",
                                 cookies={auth.COOKIE: auth.make_session(u2["id"])}) as c2:
        paired = await _pair(anon_client, c2, name="VPS-2")
    async with _agent_client(paired["token"]) as ac:
        assert (await ac.get("/api/agent/jobs?wait=0")).status_code == 200
    db.delete_user(u2["id"])
    async with _agent_client(paired["token"]) as ac:
        assert (await ac.get("/api/agent/jobs?wait=0")).status_code == 401

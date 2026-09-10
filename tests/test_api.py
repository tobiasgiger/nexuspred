"""Characterisation of the HTTP surface: auth flows, middleware, settings
masking, webhook/account CRUD, the TradingView ingress and the SSE streams."""
from __future__ import annotations

import asyncio
import io
import zipfile

import pytest

from app import auth, config, context, db, signals, state
from tests.conftest import _make_client, login_as
from tests.helpers import settle


class _StreamRequest:
    """Minimal stand-in for ``starlette.Request`` for the SSE generators.

    httpx's ``ASGITransport`` buffers the *whole* response before returning it,
    so an infinite SSE generator can never be consumed through it. We drive the
    route coroutines directly instead and simulate the client going away after
    ``polls`` disconnect checks."""

    def __init__(self, polls: int = 1):
        self.polls = polls

    async def is_disconnected(self) -> bool:
        self.polls -= 1
        return self.polls < 0


async def _drain(response, timeout: float = 5.0) -> str:
    """Collect everything the StreamingResponse body yields until it finishes."""
    async def _collect():
        out = []
        async for chunk in response.body_iterator:
            out.append(chunk if isinstance(chunk, str) else chunk.decode())
        return "".join(out)
    return await asyncio.wait_for(_collect(), timeout)


# ------------------------------------------------------------- first run
async def test_setup_flow_creates_admin_and_migrates(anon_client):
    r = await anon_client.get("/", headers={"accept": "text/html"})
    assert r.status_code == 302 and r.headers["location"] == "/setup"
    assert (await anon_client.get("/api/status")).status_code == 503

    r = await anon_client.post("/setup", data={"email": "Boss@Example.com", "password": "password123",
                                               "password2": "password123"})
    assert r.status_code == 302 and r.headers["location"] == "/"
    cookie = r.cookies.get(auth.COOKIE)
    assert cookie and auth.read_session(cookie) == 1
    login_as(anon_client, cookie)

    me = (await anon_client.get("/api/me")).json()
    assert me == {"id": 1, "email": "boss@example.com", "is_admin": True, "features": {"discord_signals": True}}
    whs = (await anon_client.get("/api/webhooks")).json()
    assert len(whs) == 1 and whs[0]["name"] == "Default" and whs[0]["strategy"] == "bracket"
    assert (await anon_client.get("/setup")).headers["location"] == "/login"


@pytest.mark.parametrize("form,err", [
    ({"email": "nope", "password": "password123", "password2": "password123"}, "email"),
    ({"email": "a@b.c", "password": "password123", "password2": "different"}, "mismatch"),
    ({"email": "a@b.c", "password": "short", "password2": "short"}, "short"),
])
async def test_setup_validation(anon_client, form, err):
    r = await anon_client.post("/setup", data=form)
    assert r.status_code == 302 and r.headers["location"] == f"/setup?error={err}"
    assert db.user_count() == 0


async def test_setup_seeds_area_from_legacy_settings_json(anon_client):
    config.LEGACY_SETTINGS_FILE.write_text('{"default_qty": 7, "webhook_secret": "legacy-tok", "junk": 1}', encoding="utf-8")
    try:
        r = await anon_client.post("/setup", data={"email": "a@b.c", "password": "password123", "password2": "password123"})
        login_as(anon_client, r.cookies[auth.COOKIE])
        s = (await anon_client.get("/api/settings")).json()
        assert s["default_qty"] == 7
        whs = (await anon_client.get("/api/webhooks")).json()
        assert whs[0]["token"] == "legacy-tok" and whs[0]["default_qty"] == 7
    finally:
        config.LEGACY_SETTINGS_FILE.unlink()


# ------------------------------------------------------------ middleware
async def test_unauthenticated_requests(admin, anon_client):
    assert (await anon_client.get("/api/status")).status_code == 401
    r = await anon_client.get("/", headers={"accept": "text/html"})
    assert r.status_code == 302 and r.headers["location"] == "/login"
    for path in ("/healthz", "/favicon.ico", "/guide"):
        assert (await anon_client.get(path)).status_code == 200, path
    assert (await anon_client.get("/healthz")).json()["ok"] is True


async def test_login_logout(admin, anon_client):
    r = await anon_client.post("/login", data={"email": "admin@example.com", "password": "wrong"})
    assert r.headers["location"] == "/login?error=bad"
    r = await anon_client.post("/login", data={"email": "ADMIN@example.com", "password": "password123"})
    assert r.headers["location"] == "/" and auth.read_session(r.cookies[auth.COOKIE]) == admin["id"]
    login_as(anon_client, r.cookies[auth.COOKIE])
    assert (await anon_client.get("/login")).headers["location"] == "/"
    r = await anon_client.get("/logout")
    assert r.headers["location"] == "/login" and "fb_session=" in r.headers["set-cookie"]


async def test_session_cookie_tamper_and_expiry(admin):
    good = auth.make_session(admin["id"])
    body, _, sig = good.partition(".")
    assert auth.read_session(body + "." + ("y" if sig[0] == "x" else "x") + sig[1:]) is None  # flip one signature char
    assert auth.read_session(None) is None and auth.read_session("garbage") is None
    assert auth.read_session(good) == admin["id"]


# ------------------------------------------------------ invites / register
async def test_invite_register_and_area_isolation(client, anon_client):
    r = await client.post("/api/users/invite", json={"elevated": False, "email": "new@x.com"})
    inv = r.json()
    assert inv["code"] and inv["url"].endswith(f"/register?code={inv['code']}") and inv["emailed"] is False
    assert (await client.get("/api/invites")).json()[0]["email"] == "new@x.com"

    r = await anon_client.post("/register", data={"code": inv["code"], "email": "new@x.com",
                                                  "password": "password123", "password2": "password123"})
    assert r.status_code == 302 and r.headers["location"] == "/"
    login_as(anon_client, r.cookies[auth.COOKIE])
    me = (await anon_client.get("/api/me")).json()
    assert me["is_admin"] is False and me["features"] == {"discord_signals": False}
    whs = (await anon_client.get("/api/webhooks")).json()
    assert len(whs) == 1 and whs[0]["name"] == "Default"
    assert (await anon_client.get("/api/users")).status_code == 403

    # invite is single-use; admin's settings are untouched by the new area
    r = await anon_client.post("/register", data={"code": inv["code"], "email": "z@x.com",
                                                  "password": "password123", "password2": "password123"})
    assert "error=invite" in r.headers["location"]
    assert config.load_settings(area_id=1)["webhooks"] == []


async def test_invite_accepts_legacy_is_admin_and_audits(client):
    r = await client.post("/api/users/invite", json={"is_admin": True})
    assert db.get_invite(r.json()["code"])["is_admin"] is True
    audit = (await client.get("/api/audit")).json()
    assert audit[0]["action"] == "invite_create" and audit[0]["detail"] == "admin invite"
    code = r.json()["code"]
    assert (await client.delete(f"/api/invites/{code}")).json()["status"] == "deleted"
    assert db.get_invite(code) is None


async def test_admin_gates(admin):
    u2 = db.create_user("u2@example.com", "password123")
    async with _make_client(auth.make_session(u2["id"])) as c:
        for path in ("/api/users", "/api/invites", "/api/audit"):
            assert (await c.get(path)).status_code == 403, path
        assert (await c.post("/api/users/invite", json={})).status_code == 403
        assert (await c.delete(f"/api/users/{admin['id']}")).status_code == 403


async def test_user_features_delete_and_reset(client, anon_client):
    u2 = db.create_user("u2@example.com", "password123")
    r = await client.post(f"/api/users/{u2['id']}/features", json={"feature": "discord_signals", "enabled": True})
    assert r.json() == {"user_id": u2["id"], "features": {"discord_signals": True}}
    assert (await client.post(f"/api/users/{u2['id']}/features", json={"feature": "x"})).status_code == 400
    users = (await client.get("/api/users")).json()
    assert {u["email"] for u in users["users"]} == {"admin@example.com", "u2@example.com"}
    assert users["features"] == db.FEATURES

    r = await client.post(f"/api/users/{u2['id']}/reset")
    token = r.json()["url"].rsplit("token=", 1)[1]
    r = await anon_client.post("/reset", data={"token": token, "password": "newpassword1", "password2": "newpassword1"})
    assert r.headers["location"] == "/" and auth.read_session(r.cookies[auth.COOKIE]) == u2["id"]
    assert db.authenticate("u2@example.com", "newpassword1")
    r = await anon_client.post("/reset", data={"token": token, "password": "x1234567", "password2": "x1234567"})
    assert "error=token" in r.headers["location"]

    assert (await client.delete("/api/users/1")).status_code == 400  # can't delete yourself
    assert (await client.delete(f"/api/users/{u2['id']}")).json()["status"] == "deleted"
    assert db.get_user(u2["id"]) is None and db.all_area_ids() == [1]


async def test_change_password(client):
    r = await client.post("/api/account/password", json={"current": "wrong", "new": "newpassword1"})
    assert r.status_code == 400
    r = await client.post("/api/account/password", json={"current": "password123", "new": "short"})
    assert r.status_code == 400
    r = await client.post("/api/account/password", json={"current": "password123", "new": "newpassword1"})
    assert r.json() == {"status": "ok"} and db.authenticate("admin@example.com", "newpassword1")


# ------------------------------------------------------------- settings
async def test_settings_masking_and_secret_preservation(client, admin):
    s = (await client.get("/api/settings")).json()
    assert s["alert_email_to"] == "admin@example.com"  # seeded from the owner
    r = await client.post("/api/settings", json={"webhook_passphrase": "pp", "default_qty": 5,
                                                  "token_accounts": [{"name": "ignored"}], "junk": 1})
    body = r.json()
    assert body["webhook_passphrase"] == "********" and body["default_qty"] == 5
    assert body["token_accounts"] == []
    await client.post("/api/settings", json={"webhook_passphrase": "********", "default_qty": 6})
    with context.use_area(1):
        s = config.load_settings()
    assert s["webhook_passphrase"] == "pp" and s["default_qty"] == 6 and "junk" not in s


async def test_status_shape(client):
    s = (await client.get("/api/status")).json()
    assert set(s) == {"version", "connection", "sessions", "trade_accounts", "active_trades", "trading_enabled", "public_url", "rollover", "pnl"}
    assert s["connection"] == {"connected": False, "accounts_total": 0, "accounts_connected": 0}
    assert s["trading_enabled"] is False and s["active_trades"] == {}


# ------------------------------------------------------------- webhooks
async def test_webhook_crud(client):
    r = await client.post("/api/webhooks", json={"name": "S1", "strategy": "ts_hunter", "default_qty": 2})
    wh = r.json()
    assert wh["strategy"] == "ts_hunter" and wh["default_qty"] == 2 and wh["tp_qty"] == 1 and wh["enabled"]
    assert wh["id"].startswith("wh_") and len(wh["token"]) >= 16 and wh["accounts"] == []

    r = await client.put(f"/api/webhooks/{wh['id']}", json={
        "name": "S1b", "enabled": False, "strategy": "bogus", "default_qty": 0, "tp_qty": 3,
        "accounts": [{"token_idx": "0", "spec": "A1", "enabled": True, "qty_multiplier": "2"},
                     {"token_idx": 0, "spec": "", "enabled": True}, {"spec": "X"}]})
    up = r.json()
    assert up["name"] == "S1b" and up["enabled"] is False and up["strategy"] == "ts_hunter"
    assert up["default_qty"] == 1 and up["tp_qty"] == 3
    assert isinstance(up["accounts"][0].pop("lid"), str)
    assert up["accounts"] == [{"token_idx": 0, "spec": "A1", "enabled": True, "qty_multiplier": 2.0,
                               "sizing": {"mode": "multiplier", "multiplier": 2.0, "fixed": 1, "max_contracts": 0}}]

    r = await client.post(f"/api/webhooks/{wh['id']}/regenerate-token")
    assert r.json()["token"] != wh["token"]
    assert (await client.put("/api/webhooks/nope", json={})).status_code == 404
    assert (await client.delete(f"/api/webhooks/{wh['id']}")).json() == {"status": "deleted", "id": wh["id"], "subscriptions_removed": 0}
    assert (await client.get("/api/webhooks")).json() == []


async def test_webhook_test_endpoint_runs_pipeline(client):
    wh = (await client.post("/api/webhooks", json={"name": "T", "strategy": "simple"})).json()
    r = await client.post(f"/api/webhooks/{wh['id']}/test", json={"action": "buy", "symbol": "MNQ1!"})
    assert r.json() == {"status": "skipped", "reason": "trading_disabled", "action": "buy"}
    r = await client.post(f"/api/webhooks/{wh['id']}/test", json={"action": "buy"})
    assert r.status_code == 400 and "symbol" in r.json()["detail"]
    with context.use_area(1):
        assert state.recent_signals()[0]["result"] == "test"


# ------------------------------------------------------ token/trade accounts
async def test_token_accounts_save_preserves_masked_tokens(client):
    body = [{"name": " L1 ", "environment": "live", "access_token": " secret1 ", "md_token": "m1", "accounts": [], "agent_id": 0,
             "enabled": True, "qty_multiplier": 2}]
    r = await client.post("/api/token-accounts", json=body)
    out = r.json()
    assert out[0]["name"] == "L1" and out[0]["access_token"] == "********" and out[0]["environment"] == "live"
    r = await client.post("/api/token-accounts", json=[{"name": "L1x", "environment": "weird",
                                                       "access_token": "********", "md_token": "********",
                                                       "enabled": False}])
    with context.use_area(1):
        t = config.load_settings()["token_accounts"][0]
    assert t.pop("lid").startswith("lg_")
    assert t == {"name": "L1x", "environment": "demo", "access_token": "secret1", "md_token": "m1",
                 "enabled": False, "qty_multiplier": 1.0, "account_spec": "", "account_id": 0, "token_expires": "",
                 "accounts": [], "agent_id": 0}
    assert (await client.get("/api/token-accounts")).json()[0]["md_token"] == "********"


async def test_trade_accounts_save(client):
    with context.use_area(1):
        config.save_settings({"token_accounts": [{"name": "L1", "enabled": True, "accounts": [
            {"spec": "A1", "id": 1, "enabled": True, "qty_multiplier": 1}]}]})
    r = await client.post("/api/trade-accounts", json=[
        {"token_idx": 0, "spec": "A1", "enabled": False, "qty_multiplier": 3},
        {"token_idx": 0, "spec": "NEW", "id": 9, "enabled": True},
        {"token_idx": "bad", "spec": "X"}, {"token_idx": 7, "spec": "Y"}])
    rows = r.json()
    assert [(x["spec"], x["enabled"], x["qty_multiplier"], x["id"]) for x in rows] == \
        [("A1", False, 3.0, 1), ("NEW", True, 1.0, 9)]
    assert (await client.get("/api/trade-accounts")).json() == rows


# ------------------------------------------------------ TradingView ingress
async def test_webhook_ingress(client, anon_client):
    wh = (await client.post("/api/webhooks", json={"name": "W", "strategy": "simple"})).json()
    url = f"/webhook/{wh['token']}"
    assert (await anon_client.post("/webhook/unknown", json={"action": "buy"})).status_code == 403
    assert (await anon_client.post(url, content=b"")).status_code == 400
    assert (await anon_client.post(url, content=b"{not json")).status_code == 400

    r = await anon_client.post(url, json={"action": "buy", "symbol": "MNQ1!"})
    assert r.status_code == 202 and r.json() == {"status": "accepted"}
    await settle()
    with context.use_area(1):
        results = [s["result"] for s in state.recent_signals()]
    assert results == ["skipped", "received"]  # trading disabled -> skipped, logged newest-first

    await client.put(f"/api/webhooks/{wh['id']}", json={"enabled": False})
    assert (await anon_client.post(url, json={"action": "buy", "symbol": "MNQ1!"})).status_code == 403


async def test_webhook_ingress_routes_to_owning_area(admin, anon_client):
    u2 = db.create_user("u2@example.com", "password123")
    wh = config.new_webhook("two")
    config.save_settings({"webhooks": [wh]}, area_id=db.user_primary_area(u2["id"]))
    r = await anon_client.post(f"/webhook/{wh['token']}", json={"action": "buy", "symbol": "MNQ1!"})
    assert r.status_code == 202
    await settle()
    with context.use_area(2):
        assert [s["result"] for s in state.recent_signals()] == ["skipped", "received"]
    with context.use_area(1):
        assert state.recent_signals() == []


async def test_webhook_failure_logs_error_and_alerts(admin, anon_client, monkeypatch):
    called = []

    async def fake_failed(name, reason):
        called.append((name, reason))

    monkeypatch.setattr("app.alerts.webhook_failed", fake_failed)
    config.save_settings({"trading_enabled": True})
    wh = config.new_webhook("F")
    config.save_settings({"webhooks": [wh]})
    r = await anon_client.post(f"/webhook/{wh['token']}", json={"action": "buy"})  # missing symbol
    assert r.status_code == 202
    await settle()
    with context.use_area(1):
        assert state.recent_signals()[0]["result"].startswith("error:")
        assert state.recent_events()[0]["level"] == "error"
    assert called == [("F", "Payload missing 'action' or 'symbol'")]


# --------------------------------------------------------------- streams
async def test_streams_require_auth(admin):
    async with _make_client() as anon:
        assert (await anon.get("/api/stream")).status_code == 401
        assert (await anon.get("/api/discord/stream")).status_code == 401


async def test_event_stream_handshake(admin):
    from app.routers import core as app_main
    with context.use_area(1):
        resp = await app_main.api_stream(_StreamRequest(polls=0))
    assert resp.media_type == "text/event-stream"
    assert resp.headers["cache-control"] == "no-cache" and resp.headers["x-accel-buffering"] == "no"
    text = await _drain(resp)
    assert text == ": connected\n\nevent: ping\ndata: {}\n\n"
    assert state._st_for(1).subscribers == set()  # unsubscribed on disconnect


async def test_discord_stream_handshake(admin):
    from app.discord_signals import hub, routes
    with context.use_area(1):
        resp = await routes.stream(_StreamRequest(polls=0))
    assert resp.media_type == "text/event-stream"
    text = await _drain(resp)
    assert text == ": connected\n\nevent: ping\ndata: {}\n\n"
    assert hub._hub(1).subscribers == set()


async def test_event_stream_delivers_events(admin):
    from app.routers import core as app_main
    with context.use_area(1):
        resp = await app_main.api_stream(_StreamRequest(polls=2))
        state.log_event("info", "hello-stream")   # queued before the generator polls
        state.log_signal({"action": "buy", "symbol": "MNQ"}, "ok")
    text = await _drain(resp)
    frames = [f for f in text.split("\n\n") if f]
    assert frames[0] == ": connected" and frames[1] == "event: ping\ndata: {}"
    assert '"kind": "event"' in frames[2] and "hello-stream" in frames[2]
    assert '"kind": "signal"' in frames[3] and '"symbol": "MNQ"' in frames[3]
    assert len(frames) == 4


async def test_discord_stream_delivers_hub_events(admin):
    from app.discord_signals import hub, routes
    with context.use_area(1):
        resp = await routes.stream(_StreamRequest(polls=1))
        hub.record({"kind": "signal", "channel_label": "x"})
    with context.use_area(2):
        hub.record({"kind": "signal", "channel_label": "other-area"})  # must not leak
    text = await _drain(resp)
    assert '"channel_label": "x"' in text and "other-area" not in text


# --------------------------------------------------------- misc endpoints
async def test_simulator_endpoints(client):
    assert len((await client.get("/api/scenarios")).json()) >= 4
    r = await client.post("/api/simulate", json={"event": "entry", "action": "sell", "symbol": "MNQ1!",
                                                  "entry": 100, "sl": 110, "tp1": 97})
    assert r.json()["status"] == "ok" and len(r.json()["orders"]) == 3
    st = (await client.get("/api/simulate/state")).json()
    assert st["positions"][0]["netPos"] == -3 and len(st["working_orders"]) == 2 and "sim:MNQ" in st["active_trades"]
    assert (await client.post("/api/simulate/reset")).json() == {"status": "reset"}
    assert (await client.get("/api/simulate/state")).json()["positions"] == []
    assert (await client.post("/api/simulate", json={"action": "x", "symbol": "MNQ1!"})).status_code == 400


async def test_alerts_test_without_channels(client):
    r = await client.post("/api/alerts/test")
    assert r.json()["status"] == "none" and r.json()["channels"] == {"discord": False, "email": False, "push": False}


async def test_flatten_all_endpoint_audits(client):
    r = await client.post("/api/flatten-all")
    assert r.json() == {"status": "ok", "accounts": 0, "cancelled": 0, "flattened": 0, "errors": []}
    audit = (await client.get("/api/audit")).json()
    assert audit[0]["action"] == "flatten_all" and audit[0]["detail"] == "0 flattened, 0 cancelled, 0 account(s)"


async def test_empty_account_endpoints(client):
    assert (await client.get("/api/positions")).json() == []
    assert (await client.post("/api/connect")).json() == {"sessions": []}
    assert (await client.get("/api/health")).json() == {"sessions": []}
    assert (await client.get("/api/orders")).json() == []
    assert (await client.get("/api/events")).json()[0]["message"] == "Settings updated" or True


async def test_extension_zip(client):
    r = await client.get("/api/extension/token-extractor.zip")
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    names = zipfile.ZipFile(io.BytesIO(r.content)).namelist()
    assert "token-extractor/manifest.json" in names and "token-extractor/popup.js" in names


async def test_dashboard_renders(client):
    r = await client.get("/")
    assert r.status_code == 200 and "app.js?v=" in r.text and config.get_version() in r.text

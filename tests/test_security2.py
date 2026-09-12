"""Security review batch (alpha.61): backups without key material, masked
subscriber routing, login brake exemptions, reset links, ingress hardening,
bounded relay results, push endpoint ownership, revocable sessions."""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile

import httpx
import pytest

from app import alerts, auth, config, context, crypto, db, relay, security
from app.main import app
from tests.conftest import _make_client, login_as


# ------------------------------------------------------------------ backup
async def test_backup_never_contains_the_key_material(client, admin, monkeypatch):
    db.meta_set("vapid_private_pem", "PRIVATE")
    db.meta_set("session_secret", "db-secret")
    monkeypatch.setattr(crypto, "key_source", lambda: "env:session")
    r = await client.get("/api/update/backup")
    assert r.status_code == 200
    fd, path = tempfile.mkstemp(suffix=".db"); os.write(fd, r.content); os.close(fd)
    try:
        c = sqlite3.connect(path)
        keys = {k for (k,) in c.execute("SELECT key FROM meta").fetchall()}
        assert "session_secret" not in keys and "vapid_private_pem" not in keys
        assert c.execute("SELECT email FROM users").fetchone()[0] == "admin@example.com"
        assert b"db-secret" not in r.content and b"PRIVATE" not in r.content     # vacuumed, not just unlinked
    finally:
        os.unlink(path)
    assert any(a["action"] == "backup_download" for a in db.list_audit(20))


async def test_backup_refused_while_the_key_lives_in_the_database(client, admin, monkeypatch):
    monkeypatch.setattr(crypto, "key_source", lambda: "db")
    r = await client.get("/api/update/backup")
    assert r.status_code == 409 and "SESSION_SECRET" in r.json()["detail"]


# ------------------------------------------------------------- subscribers
async def test_publisher_sees_subscriber_counts_not_their_routing(client, admin, webhook_factory):
    wh = webhook_factory(name="pub")
    sub = db.create_user("sub@example.com", "password123")
    db.upsert_subscription(db.user_primary_area(sub["id"]), 1, wh["id"],
                           [{"spec": "SECRET-ACCT", "lid": "l1", "token_idx": 0, "enabled": True, "qty_multiplier": 3}])
    r = await client.get(f"/api/webhooks/{wh['id']}/subscribers")
    assert r.status_code == 200
    (row,) = r.json()
    assert row["email"] == "sub@example.com" and row["accounts"] == 1
    assert "SECRET-ACCT" not in r.text and "qty_multiplier" not in r.text


# -------------------------------------------------------------- login brake
async def test_login_brake_spares_the_accounts_usual_address(admin, anon_client):
    db.record_login(admin["id"], ip="203.0.113.7")
    db.reset_caches()
    for _ in range(25):
        security.login_failed("admin@example.com")
    assert not security.login_allowed("admin@example.com")
    r = await anon_client.post("/login", data={"email": "admin@example.com", "password": "password123"})
    assert r.headers["location"] == "/login?error=rate"                        # a stranger's address stays blocked
    r = await anon_client.post("/login", data={"email": "admin@example.com", "password": "password123"},
                               headers={"x-forwarded-for": "203.0.113.7"})
    assert r.headers["location"] == "/"                                        # the owner's own address gets in
    r = await anon_client.post("/login", data={"email": "admin@example.com", "password": "wrong"},
                               headers={"x-forwarded-for": "203.0.113.7"})
    assert r.headers["location"] == "/login?error=bad"                         # …but still needs the password


# --------------------------------------------------------------- reset links
async def test_reset_link_is_not_returned_when_it_was_emailed(client, admin, monkeypatch):
    u = db.create_user("user@example.com", "password123")

    async def mailed(*a, **k):
        return True
    monkeypatch.setattr(alerts, "send_email_to", mailed)
    r = await client.post(f"/api/users/{u['id']}/reset")
    assert r.status_code == 200 and r.json()["emailed"] is True and r.json()["url"] == ""

    async def not_mailed(*a, **k):
        return False
    monkeypatch.setattr(alerts, "send_email_to", not_mailed)
    r = await client.post(f"/api/users/{u['id']}/reset")
    assert r.json()["emailed"] is False and "/reset?token=" in r.json()["url"]


async def test_only_the_bootstrap_admin_resets_another_admin(admin):
    other = db.create_user("admin2@example.com", "password123", is_admin=True)
    third = db.create_user("admin3@example.com", "password123", is_admin=True)
    async with _make_client(auth.make_session(other["id"])) as c:
        assert (await c.post(f"/api/users/{third['id']}/reset")).status_code == 403
        assert (await c.post(f"/api/users/{third['id']}/sessions/revoke")).status_code == 403
        assert (await c.post(f"/api/users/{other['id']}/reset")).status_code == 200   # their own account is fine
    async with _make_client(auth.make_session(admin["id"])) as c:
        assert (await c.post(f"/api/users/{third['id']}/reset")).status_code == 200


# ------------------------------------------------------------- ingress
async def test_webhook_ingress_rejects_pathological_bodies(anon_client, admin, webhook_factory):
    wh = webhook_factory(name="in")
    deep = "[" * 20_000 + "]" * 20_000                                          # 40 KB: under the ingress body cap
    r = await anon_client.post(f"/webhook/{wh['token']}", content=deep, headers={"content-type": "application/json"})
    assert r.status_code == 400 and "deep" in r.json()["detail"]
    r = await anon_client.post(f"/webhook/{wh['token']}", content="[1,2]", headers={"content-type": "application/json"})
    assert r.status_code == 400 and "object" in r.json()["detail"]


# ----------------------------------------------------------- relay results
async def test_relay_results_are_validated_and_bounded(client, anon_client):
    from tests.test_agent import _pair, _agent_client
    paired = await _pair(anon_client, client)
    async with _agent_client(paired["token"]) as ac:
        r = await ac.post("/api/agent/jobs/nope/result", json={"status_code": "abc"})
        assert r.status_code == 400
        r = await ac.post("/api/agent/jobs/nope/result", json={"status_code": 999})
        assert r.status_code == 400
        seen = {}
        def fake_deliver(agent_id, job_id, status, text, error=""):
            seen.update(status=status, text=text, error=error)
            return True
        real = relay.deliver
        relay.deliver = fake_deliver
        try:
            r = await ac.post("/api/agent/jobs/x/result", json={"status_code": 200, "text": "t" * 3_000_000, "error": "e" * 2000})
        finally:
            relay.deliver = real
        assert r.status_code == 200 and len(seen["text"]) == relay.MAX_RESULT_TEXT and len(seen["error"]) == relay.MAX_RESULT_ERROR


# ------------------------------------------------------------ push endpoints
async def test_push_endpoint_cannot_be_rehomed_to_another_workspace(client, admin):
    from tests.test_push import SUB
    assert (await client.post("/api/push/subscribe", json={"subscription": SUB, "device": "a"})).status_code == 200
    other = db.create_user("other@example.com", "password123")
    async with _make_client(auth.make_session(other["id"])) as c:
        r = await c.post("/api/push/subscribe", json={"subscription": SUB, "device": "b"})
        assert r.status_code == 409
    assert db.list_push_subscriptions(1)[0]["device"] == "a"


# ------------------------------------------------------------- sessions
async def test_sessions_can_be_revoked(client, admin):
    other_device = auth.make_session(admin["id"])
    assert auth.read_session(other_device) == admin["id"]
    r = await client.post("/api/account/sessions/revoke")
    assert r.status_code == 200 and "fb_session=" in r.headers.get("set-cookie", "")
    assert auth.read_session(other_device) is None                             # the old cookie is dead
    fresh = r.headers["set-cookie"].split("fb_session=", 1)[1].split(";", 1)[0]
    assert auth.read_session(fresh) == admin["id"]                              # this device continues
    login_as(client, fresh)
    u = db.create_user("user@example.com", "password123")
    cookie = auth.make_session(u["id"])
    assert (await client.post(f"/api/users/{u['id']}/sessions/revoke")).status_code == 200
    assert auth.read_session(cookie) is None
    async with _make_client(auth.make_session(u["id"])) as c:
        assert (await c.get("/api/me")).status_code == 200                      # signing in again works

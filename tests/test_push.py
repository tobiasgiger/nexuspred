"""Web Push: VAPID key, per-device subscriptions, delivery + pruning, the
service worker route and the alerts channel (no network)."""
from __future__ import annotations

import base64
import json

import pytest

from app import alerts, config, context, db, push


SUB = {"endpoint": "https://web.push.apple.com/QAbc123", "keys": {"p256dh": "BPubKey", "auth": "authsecret"}}
SUB2 = {"endpoint": "https://fcm.googleapis.com/fcm/send/xyz", "keys": {"p256dh": "BOther", "auth": "auth2"}}


@pytest.fixture
def sent(monkeypatch):
    """Capture ``webpush`` calls; ``responses`` maps endpoint → HTTP status (default 201)."""
    calls: list[dict] = []
    responses: dict[str, int] = {}

    class Resp:
        def __init__(self, status):
            self.status_code = status
            self.text = "gone" if status >= 400 else ""

    def fake_send_one(sub, payload):
        calls.append({"endpoint": sub["endpoint"], "payload": payload})
        status = responses.get(sub["endpoint"], 201)
        if status < 300:
            return True, status, ""
        return False, status, f"HTTP {status}"

    monkeypatch.setattr(push, "_send_one", fake_send_one)
    monkeypatch.setattr(push, "available", lambda: True)
    return calls, responses


def test_public_key_is_urlsafe_uncompressed_point_and_persists(admin):
    key = push.public_key()
    raw = base64.urlsafe_b64decode(key + "=" * (-len(key) % 4))
    assert len(raw) == 65 and raw[0] == 0x04          # X9.62 uncompressed P-256 point
    assert "=" not in key and "+" not in key and "/" not in key
    stored = db.meta_get("vapid_private_pem")
    assert stored and "PRIVATE KEY" not in stored     # encrypted at rest
    push.reset()
    assert push.public_key() == key                   # same pair after a restart


async def test_service_worker_is_public_and_served_from_root(anon_client):
    r = await anon_client.get("/sw.js")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/javascript")
    assert r.headers["service-worker-allowed"] == "/"
    assert "no-cache" in r.headers["cache-control"]
    assert 'addEventListener("push"' in r.text and 'addEventListener("fetch"' not in r.text


async def test_subscribe_list_known_delete(client):
    r = await client.get("/api/push/public-key")
    assert r.status_code == 200 and r.json()["enabled"] is True and len(r.json()["public_key"]) > 80
    r = await client.post("/api/push/subscribe", json={"subscription": SUB, "device": "iPhone · Safari (app)"})
    assert r.status_code == 200 and r.json()["devices"] == 1
    sub_id = r.json()["id"]
    # re-subscribing the same endpoint updates instead of duplicating
    r = await client.post("/api/push/subscribe", json={"subscription": {**SUB, "keys": {"p256dh": "BNew", "auth": "a2"}}, "device": ""})
    assert r.json()["id"] == sub_id and r.json()["devices"] == 1
    assert db.list_push_subscriptions(context.get_area())[0]["p256dh"] == "BNew"
    listed = (await client.get("/api/push/subscriptions")).json()
    assert len(listed) == 1 and listed[0]["device"] == "iPhone · Safari (app)"
    assert "endpoint" not in listed[0] and listed[0]["endpoint_host"] == "web.push.apple.com"
    assert "p256dh" not in listed[0] and "auth" not in listed[0]
    assert (await client.post("/api/push/known", json={"endpoint": SUB["endpoint"]})).json() == {"known": True, "id": sub_id}
    assert (await client.post("/api/push/known", json={"endpoint": "https://nope"})).json()["known"] is False
    # invalid subscriptions are rejected
    assert (await client.post("/api/push/subscribe", json={"subscription": {"endpoint": "http://plain", "keys": SUB["keys"]}})).status_code == 400
    assert (await client.post("/api/push/subscribe", json={"subscription": {"endpoint": SUB2["endpoint"]}})).status_code == 400
    # delete by endpoint (browser side) and by id (device list)
    await client.post("/api/push/subscribe", json={"subscription": SUB2, "device": "Windows · Chrome"})
    r = await client.request("DELETE", "/api/push/subscribe", json={"endpoint": SUB["endpoint"]})
    assert r.json()["removed"] is True
    other = (await client.get("/api/push/subscriptions")).json()
    assert len(other) == 1 and other[0]["device"] == "Windows · Chrome"
    r = await client.request("DELETE", "/api/push/subscribe", json={"id": other[0]["id"]})
    assert r.json()["removed"] is True and (await client.get("/api/push/subscriptions")).json() == []


async def test_push_requires_login(anon_client, admin):
    for path in ("/api/push/public-key", "/api/push/subscriptions"):
        assert (await anon_client.get(path)).status_code == 401
    assert (await anon_client.post("/api/push/subscribe", json={"subscription": SUB})).status_code == 401


async def test_test_push_delivers_and_prunes_dead_devices(client, sent):
    calls, responses = sent
    a = (await client.post("/api/push/subscribe", json={"subscription": SUB, "device": "iPhone"})).json()["id"]
    b = (await client.post("/api/push/subscribe", json={"subscription": SUB2, "device": "PC"})).json()["id"]
    r = await client.post("/api/push/test", json={"id": a})
    assert r.status_code == 200 and r.json() == {"sent": 1, "gone": 0, "failed": 0, "devices": 1}
    assert calls[-1]["endpoint"] == SUB["endpoint"]
    assert calls[-1]["payload"]["title"] == "Fluxbridge test" and calls[-1]["payload"]["url"] == "/#/settings/alerts"
    assert db.list_push_subscriptions(context.get_area())[0]["last_used_at"]
    # 410 Gone → the device is removed; a 5xx is counted, kept and remembered
    responses[SUB["endpoint"]] = 410
    responses[SUB2["endpoint"]] = 502
    r = await client.post("/api/push/test")
    assert r.json() == {"sent": 0, "gone": 1, "failed": 1, "devices": 2}
    left = (await client.get("/api/push/subscriptions")).json()
    assert [d["id"] for d in left] == [b] and left[0]["failures"] == 1 and "502" in left[0]["last_error"]
    # a later success clears the failure counter
    del responses[SUB2["endpoint"]]
    await client.post("/api/push/test")
    assert (await client.get("/api/push/subscriptions")).json()[0]["failures"] == 0


async def test_push_is_scoped_to_the_area(client, sent):
    calls, _ = sent
    await client.post("/api/push/subscribe", json={"subscription": SUB, "device": "iPhone"})
    other_area = context.get_area() + 1000
    result = await push.send(other_area, "t", "b")
    assert result["devices"] == 0 and calls == []
    assert db.list_push_subscriptions(other_area) == []
    assert db.delete_push_subscription(other_area, db.list_push_subscriptions(context.get_area())[0]["id"]) is False


async def test_alert_triggers_reach_push_devices(client, sent, monkeypatch):
    calls, _ = sent

    async def quiet(*a, **k):
        return None
    monkeypatch.setattr(alerts, "_send_discord", quiet)
    monkeypatch.setattr(alerts, "_send_email", quiet)
    await client.post("/api/push/subscribe", json={"subscription": SUB, "device": "iPhone"})
    await alerts.trade_executed("Breakout", "buy", "MNQZ6", ["DEMO1"])
    assert calls[-1]["payload"]["title"] == "Trade executed: BUY MNQZ6"
    assert calls[-1]["payload"]["body"] == "⚡ Trade executed — strategy Breakout: BUY MNQZ6 on DEMO1"
    assert calls[-1]["payload"]["url"] == "/#/orders"
    await alerts.connection_lost("DEMO1", "demo", "timeout")
    assert calls[-1]["payload"]["title"] == "Connection lost: DEMO1" and "**" not in calls[-1]["payload"]["body"]
    await alerts.contract_rollover("**MNQZ6** rolls in 3 days")
    assert calls[-1]["payload"]["title"] == "Contract rollover due"
    # the test-alert endpoint reports the push channel
    r = await client.post("/api/alerts/test")
    assert r.json()["channels"]["push"] is True and calls[-1]["payload"]["title"] == "Test alert"
    # master switch off → nothing is pushed, channel reported as off
    n = len(calls)
    config.save_settings({**config.load_settings(), "alert_push_enabled": False})
    await alerts.trade_executed("Breakout", "sell", "MNQZ6", ["DEMO1"])
    assert len(calls) == n
    assert (await client.post("/api/alerts/test")).json()["channels"]["push"] is False
    # per-trigger switch off → nothing either
    config.save_settings({**config.load_settings(), "alert_push_enabled": True, "alert_on_trade_executed": False})
    await alerts.trade_executed("Breakout", "sell", "MNQZ6", ["DEMO1"])
    assert len(calls) == n


async def test_send_without_pywebpush_is_a_soft_failure(client, monkeypatch):
    monkeypatch.setattr(push, "available", lambda: False)
    assert (await client.get("/api/push/public-key")).status_code == 503
    r = await client.post("/api/push/test")
    assert r.status_code == 503
    # alerts must not raise
    await alerts.contract_rollover("x")


def test_real_webpush_call_shape(admin, monkeypatch):
    """``_send_one`` hands pywebpush a Vapid instance + claims and maps the response."""
    import pywebpush
    seen = {}

    class Resp:
        status_code = 201
        text = ""

    def fake_webpush(**kw):
        seen.update(kw)
        return Resp()
    monkeypatch.setattr(pywebpush, "webpush", fake_webpush)
    sub = {"endpoint": SUB["endpoint"], "p256dh": "k", "auth": "a"}
    ok, status, err = push._send_one(sub, {"title": "t", "body": "b"})
    assert (ok, status, err) == (True, 201, "")
    assert seen["subscription_info"] == {"endpoint": SUB["endpoint"], "keys": {"p256dh": "k", "auth": "a"}}
    assert json.loads(seen["data"])["title"] == "t"
    assert seen["vapid_claims"]["sub"].startswith("mailto:") and seen["ttl"] == 600
    from py_vapid import Vapid
    assert isinstance(seen["vapid_private_key"], Vapid)

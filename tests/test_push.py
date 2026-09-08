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
        body = "403: {\"reason\":\"BadJwtToken\"}" if status == 403 else f"HTTP {status}"
        return False, status, body

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
    import time
    assert 11 * 3600 < seen["vapid_claims"]["exp"] - time.time() <= 12 * 3600   # Apple caps VAPID JWTs at 24 h
    from py_vapid import Vapid
    assert isinstance(seen["vapid_private_key"], Vapid)


async def test_subscribe_rejects_internal_endpoints_and_caps_devices(client, monkeypatch):
    from app import security
    from app.routers import push as push_router
    monkeypatch.setattr(security, "check_outbound_url", lambda url: "URL points at an internal address" if "10.0.0" in url else None)
    r = await client.post("/api/push/subscribe", json={"subscription": {"endpoint": "https://10.0.0.5/push", "keys": SUB["keys"]}})
    assert r.status_code == 400 and "internal" in r.json()["detail"]
    monkeypatch.setattr(push_router, "MAX_DEVICES_PER_AREA", 2)
    for i in range(2):
        assert (await client.post("/api/push/subscribe", json={"subscription": {"endpoint": f"https://push.example/{i}", "keys": SUB["keys"]}})).status_code == 200
    r = await client.post("/api/push/subscribe", json={"subscription": {"endpoint": "https://push.example/3", "keys": SUB["keys"]}})
    assert r.status_code == 400 and "At most 2" in r.json()["detail"]
    # re-registering an existing endpoint is still fine at the cap
    assert (await client.post("/api/push/subscribe", json={"subscription": {"endpoint": "https://push.example/1", "keys": SUB["keys"]}})).status_code == 200
    # oversized keys are rejected
    r = await client.post("/api/push/subscribe", json={"subscription": {"endpoint": "https://push.example/4", "keys": {"p256dh": "x" * 300, "auth": "a"}}})
    assert r.status_code == 400


async def test_apple_badjwt_prunes_the_subscription(client, sent):
    calls, responses = sent
    a = (await client.post("/api/push/subscribe", json={"subscription": SUB, "device": "iPhone"})).json()["id"]
    (await client.post("/api/push/subscribe", json={"subscription": SUB2, "device": "Chrome"}))
    # Apple rejects a subscription bound to a rotated key with 403 BadJwtToken → prune it
    responses[SUB["endpoint"]] = 403
    r = await client.post("/api/push/test")
    assert r.json()["gone"] == 1 and r.json()["sent"] == 1
    left = [d.get("endpoint_host") for d in (await client.get("/api/push/subscriptions")).json()]
    assert left == ["fcm.googleapis.com"]   # the FCM one survives, the stale Apple one is gone


def test_send_one_reports_the_push_service_reason(admin, monkeypatch):
    import pywebpush

    class Resp:
        status_code = 403
        text = '{"reason":"BadJwtToken"}'

    def boom(**kw):
        raise pywebpush.WebPushException("rejected", response=Resp())
    monkeypatch.setattr(pywebpush, "webpush", boom)
    ok, status, err = push._send_one({"endpoint": SUB["endpoint"], "p256dh": "k", "auth": "a"}, {"title": "t"})
    assert ok is False and status == 403 and "BadJwtToken" in err


def test_vapid_key_is_reencrypted_after_a_crypto_key_change(admin, monkeypatch):
    from app import crypto, db
    key1 = push.public_key()
    stored1 = db.meta_get("vapid_private_pem")
    assert crypto.is_current(stored1)
    # rotate the crypto key, keeping the old one as legacy (as NEXUSPRED_ENCRYPTION_KEY_PREVIOUS would)
    monkeypatch.setenv("NEXUSPRED_ENCRYPTION_KEY_PREVIOUS", "test-session-secret")
    monkeypatch.setenv("NEXUSPRED_ENCRYPTION_KEY", "a-brand-new-encryption-key-value")
    crypto.reset()
    push.reset()
    assert not crypto.is_current(stored1)          # the old ciphertext needs the legacy key now
    key2 = push.public_key()                        # _load decrypts via legacy and must NOT regenerate
    assert key2 == key1                              # same keypair survived the rotation
    assert crypto.is_current(db.meta_get("vapid_private_pem"))   # …and was re-encrypted with the new key


async def test_diagnose_reports_identity_and_does_not_prune(client, sent):
    calls, responses = sent
    await client.post("/api/push/subscribe", json={"subscription": SUB, "device": "iPhone"})
    responses[SUB["endpoint"]] = 410           # would normally be pruned by a test
    r = await client.post("/api/push/diag")
    d = r.json()
    assert d["available"] is True and len(d["public_key"]) > 80 and len(d["public_key_fp"]) == 12
    assert d["vapid_key_encrypted"] and d["vapid_key_decrypts"] and d["vapid_key_current"]
    assert d["claims_sub"].startswith("mailto:")
    assert d["devices"][0]["host"] == "web.push.apple.com" and d["devices"][0]["ok"] is False and "410" in d["devices"][0]["error"]
    # diagnose must NOT delete the device (unlike test)
    assert len(db.list_push_subscriptions(context.get_area())) == 1


async def test_diag_requires_admin(anon_client, admin):
    assert (await anon_client.post("/api/push/diag")).status_code == 401


def test_vapid_sub_is_never_localhost(admin, monkeypatch):
    from app import config, db
    monkeypatch.setattr(config, "PUBLIC_URL", "")
    db.meta_set("push_origin_host", "")
    push.reset()
    assert push._claims()["sub"] == "mailto:admin@fluxbridge.app"     # neutral fallback, never localhost
    db.meta_set("push_origin_host", "bridge.hurenzone.ch")
    push.reset()
    assert push._claims()["sub"] == "mailto:admin@bridge.hurenzone.ch"
    monkeypatch.setattr(config, "PUBLIC_URL", "https://my.example.com")
    push.reset()
    assert push._claims()["sub"] == "mailto:admin@my.example.com"     # PUBLIC_URL wins


async def test_subscribe_learns_the_dashboard_host(client, admin):
    from app import db
    db.meta_set("push_origin_host", "")
    r = await client.post("/api/push/subscribe", json={"subscription": SUB, "device": "iPhone"},
                          headers={"host": "bridge.hurenzone.ch"})
    assert r.status_code == 200
    assert db.meta_get("push_origin_host") == "bridge.hurenzone.ch"
    push.reset()
    assert "localhost" not in push._claims()["sub"] and push._claims()["sub"].endswith("bridge.hurenzone.ch")

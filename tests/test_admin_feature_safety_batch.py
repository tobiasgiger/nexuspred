"""Regression coverage for alpha.70/71 safety hardening."""
from __future__ import annotations

import httpx

from app import context, db, leader_feed, marketplace, signals, watchdog
from app.routers import settings_io


class _Session:
    def __init__(self, value: int):
        self.lid = "same-login"
        self.value = value
        self.calls = 0

    async def positions_snapshot(self):
        self.calls += 1
        return [{"accountId": 1, "contractId": 10, "netPos": self.value}]

    async def orders_snapshot(self):
        return []


async def test_replacement_session_does_not_reuse_old_shared_snapshot(monkeypatch):
    leader_feed.reset()
    monkeypatch.setattr(leader_feed, "TTL_S", 60.0)
    old = _Session(1)
    replacement = _Session(2)
    first, shared1 = await leader_feed.snapshot(1, old, "positions")
    second, shared2 = await leader_feed.snapshot(1, replacement, "positions")
    assert shared1 is False and first[0]["netPos"] == 1
    assert shared2 is False and second[0]["netPos"] == 2
    assert old.calls == 1 and replacement.calls == 1


def test_marketplace_subscription_rechecks_current_acl(monkeypatch):
    webhook = {"sharing": {"enabled": True, "visibility": "selected", "allowed_user_ids": [7]}}
    monkeypatch.setattr(marketplace.db, "area_owner", lambda area_id: 7 if area_id == 2 else 9)
    assert marketplace.subscription_allowed(webhook, {"area_id": 2}) is True
    assert marketplace.subscription_allowed(webhook, {"area_id": 3}) is False


def test_subscription_window_keeps_publisher_timezone(monkeypatch):
    monkeypatch.setattr(marketplace.config, "load_settings", lambda area_id=None: {"journal_timezone": "America/New_York"})
    webhook = {
        "id": "wh_x",
        "name": "x",
        "sharing": {"enabled": True},
        "trade_window": {"enabled": True, "from": "09:00", "to": "17:00", "tz": "", "days": ["mon"]},
    }
    view = marketplace.subscription_view(webhook, {"id": 1, "accounts": []}, 42)
    assert view["trade_window"]["tz"] == "America/New_York"


def test_settings_export_drops_installation_local_marketplace_acl():
    raw = {"id": "wh_x", "sharing": {
        "enabled": True, "title": "Alpha", "description": "desc",
        "visibility": "selected", "allowed_user_ids": [4, 9],
    }}
    out = settings_io._portable_webhook(raw)
    assert out["sharing"]["enabled"] is False
    assert out["sharing"]["allowed_user_ids"] == []
    assert out["sharing"]["title"] == "Alpha" and out["sharing"]["description"] == "desc"


def test_signal_fanout_skips_revoked_marketplace_subscriptions(monkeypatch):
    webhook = {"id": "wh_x", "name": "x", "sharing": {"enabled": True}}
    subs = [{"id": 1, "area_id": 2}, {"id": 2, "area_id": 3}]
    monkeypatch.setattr(db, "active_subscriptions", lambda area_id, webhook_id: subs)
    monkeypatch.setattr(marketplace, "subscription_allowed", lambda wh, sub: sub["id"] == 1)
    monkeypatch.setattr(marketplace, "subscription_view", lambda wh, sub, aid: {"id": "sub", "name": "sub", "accounts": []})
    monkeypatch.setattr(signals.state, "log_signal", lambda *a, **k: None)
    monkeypatch.setattr(signals.state, "log_event", lambda *a, **k: None)
    spawned = []

    def fake_spawn(coro):
        spawned.append(coro)
        coro.close()
        return None

    monkeypatch.setattr(signals, "_spawn", fake_spawn)
    with context.use_area(1):
        assert signals.forward_to_subscribers({"action": "buy"}, webhook) == 1
    assert len(spawned) == 1


class _Http:
    def __init__(self):
        self.calls = []

    async def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return httpx.Response(200, request=httpx.Request("GET", url))


async def test_heartbeat_revalidates_destination_at_request_time(monkeypatch):
    watchdog.reset()
    fake = _Http()
    monkeypatch.setattr(watchdog.http, "client", lambda name="outbound": fake)
    monkeypatch.setattr(watchdog.security, "check_outbound_url", lambda url: "private address")
    assert await watchdog.ping(1, "https://example.test/ping") is False
    assert fake.calls == []
    assert "destination rejected at request time" in watchdog.status(1)["error"]

    monkeypatch.setattr(watchdog.security, "check_outbound_url", lambda url: None)
    assert await watchdog.ping(1, "https://example.test/ping") is True
    assert fake.calls[-1][1]["follow_redirects"] is False

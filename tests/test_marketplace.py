"""Marketplace: publishing, visibility, subscriptions and the signal fan-out
into subscriber areas (own accounts, own trading switch, own logs)."""
from __future__ import annotations

import pytest

from app import auth, config, context, db, marketplace, signals, state
from tests.conftest import _make_client
from tests.helpers import FakeExecutor, settle

PAYLOAD = {"action": "buy", "symbol": "MNQ1!", "qty": 1}
S1 = {"token_idx": 0, "spec": "S1", "enabled": True, "qty_multiplier": 2}


@pytest.fixture
def two_areas(admin):
    """Area 1 (admin) publishes 'Alpha Scalper'; area 2 belongs to sub@example.com."""
    u2 = db.create_user("sub@example.com", "password123")
    a2 = db.user_primary_area(u2["id"])
    wh = config.new_webhook("Alpha", strategy="simple", default_qty=1)
    wh["accounts"] = [{"token_idx": 0, "spec": "P1", "enabled": True, "qty_multiplier": 1}]
    wh["sharing"] = {"enabled": True, "title": "Alpha Scalper", "description": "NQ scalps", "visibility": "all"}
    config.save_settings({"webhooks": [wh], "trading_enabled": True}, area_id=1)
    config.save_settings({"trading_enabled": True}, area_id=a2)
    return {"u2": u2, "a2": a2, "wh": wh}


@pytest.fixture
def execs(monkeypatch):
    """Executors are derived from the webhook (view) the engine was given, so the
    test can see which area executed with which accounts."""
    made: list[tuple[int, str, FakeExecutor]] = []

    def fake(wh):
        out = [FakeExecutor(a["spec"], qty_multiplier=a.get("qty_multiplier", 1))
               for a in wh.get("accounts") or [] if a.get("enabled")]
        made.extend((context.get_area(), wh["id"], e) for e in out)
        return out

    monkeypatch.setattr(signals, "_webhook_executors", fake)

    async def no_alert(*a, **k):
        pass
    monkeypatch.setattr(signals.alerts, "trade_executed", no_alert)
    return made


@pytest.fixture
async def sub_client(two_areas):
    async with _make_client(auth.make_session(two_areas["u2"]["id"])) as c:
        yield c


# ------------------------------------------------------------------ helpers
def test_sharing_normalisation_and_visibility():
    sh = marketplace.normalize_sharing({"enabled": 1, "title": " T ", "visibility": "selected",
                                        "allowed_user_ids": ["3", 4, "x", 3]})
    assert sh == {"enabled": True, "title": "T", "description": "", "visibility": "selected", "allowed_user_ids": [3, 4]}
    assert marketplace.visible_to(sh, 3) and not marketplace.visible_to(sh, 5)
    assert marketplace.sharing_of({})["enabled"] is False
    assert marketplace.visible_to(marketplace.sharing_of({"sharing": {"enabled": True, "visibility": "bogus"}}), 99)
    assert not marketplace.visible_to(marketplace.sharing_of({"sharing": {"enabled": False}}), 1)
    assert marketplace.clean_accounts([{"token_idx": "0", "spec": "A", "qty_multiplier": "1.5"}, {"spec": ""}, "junk"]) == [
        {"token_idx": 0, "spec": "A", "enabled": True, "qty_multiplier": 1.5}]


def test_public_view_hides_secrets_and_excludes_own_area(two_areas):
    items = marketplace.published_webhooks(user_id=two_areas["u2"]["id"], exclude_area=two_areas["a2"])
    assert len(items) == 1
    it = items[0]
    assert it["title"] == "Alpha Scalper" and it["publisher_email"] == "admin@example.com" and it["strategy"] == "simple"
    assert "token" not in it and "accounts" not in it
    assert marketplace.published_webhooks(exclude_area=1) == []
    wh, sh = marketplace.find_published(1, two_areas["wh"]["id"])
    assert wh["id"] == two_areas["wh"]["id"] and sh["title"] == "Alpha Scalper"
    assert marketplace.find_published(1, "wh_nope") == (None, {})


# ---------------------------------------------------------------- fan-out
async def test_signal_fans_out_to_subscriber_area(two_areas, execs):
    a2, wh = two_areas["a2"], two_areas["wh"]
    db.upsert_subscription(a2, 1, wh["id"], [S1])
    with context.use_area(1):
        signals.accept(PAYLOAD, wh)
    await settle(30)
    assert sorted((aid, wid) for aid, wid, _ in execs) == [(1, wh["id"]), (2, f"sub1_{wh['id']}")]
    sub_exec = next(e for aid, _, e in execs if aid == 2)
    assert sub_exec.of("place")[0]["qty"] == 2  # the subscriber's own multiplier
    with context.use_area(2):
        assert [s["result"] for s in state.recent_signals()[:2]] == ["ok", "received"]
        trades = signals.active_trades()
        assert list(trades) == [f"sub1_{wh['id']}:MNQ"]
        assert trades[f"sub1_{wh['id']}:MNQ"]["webhook_name"] == "Alpha Scalper"
        assert list(trades[f"sub1_{wh['id']}:MNQ"]["accounts"]) == ["S1"]
    with context.use_area(1):
        assert any("forwarded to 1 subscriber" in e["message"] for e in state.recent_events())
        assert set(signals.active_trades()) == {f"{wh['id']}:MNQ"}


async def test_fan_out_respects_subscription_and_publish_state(two_areas, execs):
    a2, wh = two_areas["a2"], two_areas["wh"]
    sub = db.upsert_subscription(a2, 1, wh["id"], [S1], enabled=False)
    with context.use_area(1):
        assert signals.forward_to_subscribers(PAYLOAD, wh) == 0
    db.update_subscription(sub["id"], a2, enabled=True)
    unpublished = {**wh, "sharing": {**wh["sharing"], "enabled": False}}
    with context.use_area(1):
        assert signals.forward_to_subscribers(PAYLOAD, unpublished) == 0
        assert signals.forward_to_subscribers(PAYLOAD, wh) == 1
    await settle(30)
    assert [aid for aid, _, _ in execs] == [2]


async def test_subscriber_trading_switch_and_passphrase(two_areas, execs):
    a2, wh = two_areas["a2"], two_areas["wh"]
    db.upsert_subscription(a2, 1, wh["id"], [S1])
    config.save_settings({"trading_enabled": False}, area_id=a2)
    with context.use_area(1):
        assert signals.forward_to_subscribers(PAYLOAD, wh) == 1
    await settle(30)
    with context.use_area(2):
        assert state.recent_signals()[0]["result"] == "skipped"
    assert execs == []
    # The subscriber's own passphrase must not block a signal the publisher authenticated.
    config.save_settings({"trading_enabled": True, "webhook_passphrase": "secret"}, area_id=a2)
    with context.use_area(1):
        signals.forward_to_subscribers(PAYLOAD, wh)
    await settle(30)
    with context.use_area(2):
        assert state.recent_signals()[0]["result"] == "ok"
    assert [aid for aid, _, _ in execs] == [2]


async def test_subscriber_failure_is_isolated(two_areas, execs, monkeypatch):
    a2, wh = two_areas["a2"], two_areas["wh"]
    db.upsert_subscription(a2, 1, wh["id"], [S1])
    failed = []

    async def fake_failed(name, reason):
        failed.append((context.get_area(), name, reason))
    monkeypatch.setattr(signals.alerts, "webhook_failed", fake_failed)
    with context.use_area(1):
        signals.accept({"action": "buy", "symbol": "XX1!"}, wh)  # not mapped → error in both areas
    await settle(30)
    assert sorted(a for a, _, _ in failed) == [1, 2]
    assert {n for _, n, _ in failed} == {"Alpha", "Alpha Scalper"}


# --------------------------------------------------------------- db layer
def test_subscription_store_and_cache(two_areas):
    a2, wid = two_areas["a2"], two_areas["wh"]["id"]
    assert db.active_subscriptions(1, wid) == []
    sub = db.upsert_subscription(a2, 1, wid, [S1])
    assert [s["id"] for s in db.active_subscriptions(1, wid)] == [sub["id"]]
    again = db.upsert_subscription(a2, 1, wid, [], enabled=False)  # upsert, same row
    assert again["id"] == sub["id"] and again["accounts"] == [] and db.active_subscriptions(1, wid) == []
    assert db.update_subscription(sub["id"], a2, enabled=True, accounts=[S1])["enabled"] is True
    assert db.active_subscriptions(1, wid)[0]["accounts"] == [S1]
    assert db.update_subscription(sub["id"], 1, enabled=False) is None  # wrong area
    assert db.list_subscribers(1, wid)[0]["email"] == "sub@example.com"
    assert db.subscriber_counts(1) == {wid: 1}
    assert db.delete_subscription(sub["id"], area_id=1) is None  # not the subscriber's area
    assert db.delete_subscription(sub["id"], publisher_area_id=1)["id"] == sub["id"]
    assert db.active_subscriptions(1, wid) == [] and db.list_subscriptions(a2) == []
    db.upsert_subscription(a2, 1, wid, [S1])
    assert db.delete_subscriptions_for_webhook(1, wid) == 1 and db.active_subscriptions(1, wid) == []


def test_delete_user_cascades_subscriptions(two_areas):
    a2, wid = two_areas["a2"], two_areas["wh"]["id"]
    db.upsert_subscription(a2, 1, wid, [S1])
    db.delete_user(two_areas["u2"]["id"])
    assert db.active_subscriptions(1, wid) == [] and db.list_subscribers(1, wid) == []


# --------------------------------------------------------------------- API
async def test_marketplace_api_flow(two_areas, client, sub_client):
    wh, u2 = two_areas["wh"], two_areas["u2"]
    items = (await sub_client.get("/api/marketplace")).json()
    assert [i["title"] for i in items] == ["Alpha Scalper"]
    assert items[0]["subscription"] is None and items[0]["subscriber_count"] == 0 and "token" not in items[0]
    assert (await client.get("/api/marketplace")).json() == []  # never your own

    r = await sub_client.post(f"/api/marketplace/1/{wh['id']}/subscribe",
                              json={"accounts": [{"token_idx": 0, "spec": "S1", "qty_multiplier": "1.5"}, {"spec": ""}], "enabled": True})
    assert r.status_code == 200
    sub = r.json()
    assert sub["accounts"] == [{"token_idx": 0, "spec": "S1", "enabled": True, "qty_multiplier": 1.5}]
    assert sub["active"] is True and sub["webhook"]["title"] == "Alpha Scalper"
    assert (await sub_client.get("/api/marketplace")).json()[0]["subscription"]["id"] == sub["id"]
    assert (await client.post(f"/api/marketplace/1/{wh['id']}/subscribe", json={})).status_code == 400
    assert (await sub_client.post("/api/marketplace/1/wh_nope/subscribe", json={})).status_code == 404

    subs = (await client.get(f"/api/webhooks/{wh['id']}/subscribers")).json()
    assert subs[0]["email"] == "sub@example.com" and subs[0]["id"] == sub["id"]
    assert (await client.get("/api/webhooks")).json()[0]["subscriber_count"] == 1
    assert (await sub_client.get(f"/api/webhooks/{wh['id']}/subscribers")).status_code == 403

    r = await sub_client.put(f"/api/subscriptions/{sub['id']}", json={"enabled": False})
    assert r.json()["enabled"] is False and r.json()["active"] is False
    assert (await client.put(f"/api/subscriptions/{sub['id']}", json={"enabled": True})).status_code == 404
    assert (await sub_client.get("/api/subscriptions")).json()[0]["webhook"]["publisher_email"] == "admin@example.com"

    # Publishing is admin-only; 'selected' visibility hides it until the user is allowed.
    assert (await sub_client.put(f"/api/webhooks/{wh['id']}/sharing", json={"enabled": False})).status_code == 403
    r = await client.put(f"/api/webhooks/{wh['id']}/sharing", json={"visibility": "selected", "allowed_user_ids": [999]})
    assert r.json()["sharing"]["visibility"] == "selected" and r.json()["subscriber_count"] == 1
    assert (await sub_client.get("/api/marketplace")).json() == []
    await client.put(f"/api/webhooks/{wh['id']}/sharing", json={"allowed_user_ids": [u2["id"]]})
    assert len((await sub_client.get("/api/marketplace")).json()) == 1

    r = await client.delete(f"/api/webhooks/{wh['id']}/subscribers/{sub['id']}")
    assert r.status_code == 200
    assert (await sub_client.get("/api/subscriptions")).json() == []
    assert (await client.delete(f"/api/webhooks/{wh['id']}/subscribers/{sub['id']}")).status_code == 404
    actions = [a["action"] for a in (await client.get("/api/audit")).json()]
    assert {"subscribe", "subscriber_remove"} <= set(actions)


async def test_unpublish_pauses_and_delete_cascades(two_areas, client, sub_client):
    wh = two_areas["wh"]
    sub = (await sub_client.post(f"/api/marketplace/1/{wh['id']}/subscribe", json={"accounts": [S1]})).json()
    await client.put(f"/api/webhooks/{wh['id']}/sharing", json={"enabled": False})
    mine = (await sub_client.get("/api/subscriptions")).json()
    assert mine[0]["webhook"] is None and mine[0]["active"] is False
    assert (await sub_client.get("/api/marketplace")).json() == []
    await client.put(f"/api/webhooks/{wh['id']}/sharing", json={"enabled": True})
    assert (await sub_client.get("/api/subscriptions")).json()[0]["active"] is True
    r = await client.delete(f"/api/webhooks/{wh['id']}")
    assert r.json()["subscriptions_removed"] == 1
    assert (await sub_client.get("/api/subscriptions")).json() == []
    assert db.get_subscription(sub["id"]) is None


async def test_test_endpoint_forwards_only_on_request(two_areas, client, execs):
    a2, wh = two_areas["a2"], two_areas["wh"]
    db.upsert_subscription(a2, 1, wh["id"], [S1])
    r = await client.post(f"/api/webhooks/{wh['id']}/test", json=PAYLOAD)
    assert r.status_code == 200 and "forwarded" not in r.json()
    await settle(30)
    assert [aid for aid, _, _ in execs] == [1]
    r = await client.post(f"/api/webhooks/{wh['id']}/test?subscribers=true", json=PAYLOAD)
    assert r.json()["forwarded"] == 1
    await settle(30)
    assert [aid for aid, _, _ in execs] == [1, 1, 2]

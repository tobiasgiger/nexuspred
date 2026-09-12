"""Copy groups as a marketplace product: publish, subscribe with own accounts,
mirror into the subscriber's workspace, privacy, lifecycle."""
from __future__ import annotations

import asyncio

import pytest

from app import alerts, auth, config, context, copy as cp, db, tradovate
from tests.conftest import _make_client
from tests.helpers import FakeExecutor
from tests.test_copy import Manager, Sess, _wait


@pytest.fixture
def worlds(monkeypatch, admin):
    """Area 1 (admin): leader login + own follower F1. Area 2 (sub@example.com):
    one login with account X1 — subscribes through the marketplace."""
    u2 = db.create_user("sub@example.com", "password123")
    a2 = db.user_primary_area(u2["id"])
    leader = Sess(0, "L", [{"id": 1, "spec": "LEAD"}])
    fol1 = Sess(1, "F", [{"id": 2, "spec": "F1"}])
    ex1 = FakeExecutor("F1"); ex1.session, ex1.id = fol1, 2
    sub_login = Sess(0, "S", [{"id": 9, "spec": "X1"}])
    exx = FakeExecutor("X1"); exx.session, exx.id = sub_login, 9
    mgrs = {1: Manager([leader, fol1], {"F1": ex1}), a2: Manager([sub_login], {"X1": exx})}
    monkeypatch.setattr(tradovate, "manager_for", lambda area_id: mgrs[area_id])
    monkeypatch.setattr(cp.group_runner, "POLL_INTERVAL_S", 0.01)
    monkeypatch.setattr(cp.group_runner, "POLL_WS_INTERVAL_S", 0.05)
    monkeypatch.setattr(cp.group_runner, "RECONCILE_INTERVAL_S", 3600)
    monkeypatch.setattr(cp.group_runner, "RECONNECT_BACKOFF_S", 0.02)
    monkeypatch.setattr(cp.group_runner, "POLL_ERROR_SLEEP_S", 0.02)
    monkeypatch.setattr(cp.group_runner, "ORDER_SETTLE_S", 0.0)

    async def no_ws(self, session, account_id):
        await self._stop.wait()
    monkeypatch.setattr(cp.GroupRunner, "_ws_accelerator", no_ws)
    sent = []

    async def rec(title, message, **kw):
        sent.append((context.get_area(), title, message))
    monkeypatch.setattr(alerts, "copy_alert", rec)
    with context.use_area(1):
        config.save_settings({"trading_enabled": True, "token_accounts": [
            {"name": "L", "environment": "demo", "enabled": True, "access_token": "t", "accounts": [{"spec": "LEAD", "id": 1, "enabled": True}]},
            {"name": "F", "environment": "demo", "enabled": True, "access_token": "t", "accounts": [{"spec": "F1", "id": 2, "enabled": True}]}]})
        g = cp.new_group("Alpha mirror")
        g.update({"enabled": True, "feed": "poll", "leader": {"token_idx": 0, "spec": "LEAD", "account_id": 1},
                  "followers": [cp.normalize_follower({"token_idx": 1, "spec": "F1", "account_id": 2})],
                  "sharing": {"enabled": True, "title": "Alpha mirror", "description": "MNQ scalps, 1:1", "visibility": "all"}})
        cp.save_groups([g], 1)
    with context.use_area(a2):
        config.save_settings({"trading_enabled": True, "token_accounts": [
            {"name": "S", "environment": "demo", "enabled": True, "access_token": "t", "accounts": [{"spec": "X1", "id": 9, "enabled": True}]}]})
    return {"a2": a2, "u2": u2, "g": g, "leader": leader, "sub_login": sub_login, "ex1": ex1, "exx": exx, "sent": sent}


@pytest.fixture
async def sub_client(worlds):
    async with _make_client(auth.make_session(worlds["u2"]["id"])) as c:
        yield c


async def test_publish_subscribe_and_mirror_into_the_subscriber_workspace(worlds, client, sub_client):
    a2, g, leader, ex1, exx = worlds["a2"], worlds["g"], worlds["leader"], worlds["ex1"], worlds["exx"]
    # the subscriber sees the group on the marketplace, never the publisher's accounts
    items = (await sub_client.get("/api/marketplace")).json()
    it = next(x for x in items if x["kind"] == "copy")
    assert it["title"] == "Alpha mirror" and it["group_id"] == g["id"] and it["publisher_email"] == "admin@example.com"
    assert "LEAD" not in str(it) and "followers" not in it and it["followers_count"] == 1
    # own group cannot be subscribed by the publisher
    assert (await client.post(f"/api/marketplace/1/copy/{g['id']}/subscribe", json={"accounts": []})).status_code == 400
    # subscribe with own account X1, fixed 2 contracts
    r = await sub_client.post(f"/api/marketplace/1/copy/{g['id']}/subscribe", json={"accounts": [{"spec": "X1", "mode": "fixed", "fixed": 2}]})
    assert r.status_code == 200, r.text
    sub = r.json()
    assert sub["kind"] == "copy" and sub["copy"]["title"] == "Alpha mirror" and sub["accounts"][0]["fixed"] == 2 and sub["accounts"][0]["token_idx"] == 0
    r_ = cp.runner(1, g["id"])
    assert r_ is not None and [f["spec"] for f in r_.followers] == ["F1", "X1"] and r_.followers[1]["area_id"] == a2
    try:
        assert await _wait(lambda: r_.feed_ok)
        leader.positions = [{"accountId": 1, "contractId": 901, "netPos": 3}]
        assert await _wait(lambda: len(exx.of("place")) == 1 and len(ex1.of("place")) == 1)
        assert exx.of("place")[0]["qty"] == 2 and exx.of("place")[0]["action"] == "Buy"      # fixed 2 on the subscriber's account
        assert ex1.of("place")[0]["qty"] == 3                                                 # own follower 1:1
        # events: the subscriber's workspace holds its own row (leader hidden), the publisher's row hides the account
        mine = db.list_copy_events(a2, group_id=g["id"])
        assert any(e["kind"] == "mirror" and e["follower"] == "X1" and e["leader"] == "leader" for e in mine)
        theirs = db.list_copy_events(1, group_id=g["id"])
        assert any(e["kind"] == "mirror" and e["follower"].startswith("subscriber #") for e in theirs) and not any(e["follower"] == "X1" for e in theirs)
        # publisher's status masks the subscriber's account; the subscriber's view shows it
        st = (await client.get("/api/copy/status")).json()[g["id"]]
        assert [f["spec"] for f in st["followers"]] == ["F1", f"subscriber #{sub['id']}"]
        fol = (await sub_client.get("/api/copy/following")).json()
        assert fol[0]["title"] == "Alpha mirror" and fol[0]["running"] and fol[0]["feed_ok"]
        assert fol[0]["followers"][0]["spec"] == "X1" and fol[0]["followers"][0]["positions"][0]["actual"] == 2
        assert fol[0]["leader_positions"][0]["net"] == 3 and "LEAD" not in str(fol[0])
        # the publisher sees the subscriber list (email, account count) and the count on the group
        subs = (await client.get(f"/api/copy/groups/{g['id']}/subscribers")).json()
        assert subs[0]["email"] == "sub@example.com" and subs[0]["accounts"] == 1
        assert (await client.get("/api/copy/groups")).json()[0]["subscriber_count"] == 1
        # unsubscribe → the same runner drops the external follower in place (no feed restart); X1 keeps its position
        assert (await sub_client.delete(f"/api/subscriptions/{sub['id']}")).status_code == 200
        r2 = cp.runner(1, g["id"])
        assert r2 is r_ and [f["spec"] for f in r2.followers] == ["F1"]
        assert len(exx.of("place")) == 1
    finally:
        await cp.stop_all()


async def test_subscription_validation_and_workspace_switches(worlds, client, sub_client):
    a2, g, leader, exx = worlds["a2"], worlds["g"], worlds["leader"], worlds["exx"]
    url = f"/api/marketplace/1/copy/{g['id']}/subscribe"
    assert (await sub_client.post(url, json={"accounts": [{"spec": "NOPE"}]})).status_code == 400          # not their account
    assert (await sub_client.post(url, json={"accounts": [{"spec": "X1"}, {"spec": "X1"}]})).status_code == 400
    # X1 already follows a leader in an own group of area 2 → refused
    with context.use_area(a2):
        og = cp.new_group("own"); og.update({"enabled": False, "leader": {"token_idx": 0, "spec": "OTHER", "account_id": 0},
                                              "followers": [cp.normalize_follower({"token_idx": 0, "spec": "X1", "account_id": 9})]})
        cp.save_groups([og], a2)
    r = await sub_client.post(url, json={"accounts": [{"spec": "X1"}]})
    assert r.status_code == 400 and "one leader only" in r.json()["detail"]
    with context.use_area(a2):
        cp.save_groups([], a2)
    # subscribed, but the subscriber's trading switch is off → skipped in their log, nothing placed
    r = await sub_client.post(url, json={"accounts": [{"spec": "X1"}]})
    assert r.status_code == 200
    with context.use_area(a2):
        config.save_settings({"trading_enabled": False})
    r_ = cp.runner(1, g["id"])
    try:
        assert await _wait(lambda: r_.feed_ok)
        leader.positions = [{"accountId": 1, "contractId": 901, "netPos": 1}]
        assert await _wait(lambda: any(e["kind"] == "skipped" for e in db.list_copy_events(a2, group_id=g["id"])))
        assert not exx.of("place")
        # unpublishing drops the subscriber's account from the mirror
        r = await client.put(f"/api/copy/groups/{g['id']}/sharing", json={"enabled": False})
        assert r.status_code == 200
        assert await _wait(lambda: cp.runner(1, g["id"]) is not None and [f["spec"] for f in cp.runner(1, g["id"]).followers] == ["F1"])
        assert (await sub_client.get("/api/marketplace")).json() == []
        fol = (await sub_client.get("/api/copy/following")).json()
        assert fol and not fol[0]["published"]
        # kicking a subscriber (publisher side) and deleting the group cleans the subscription up
        assert (await client.put(f"/api/copy/groups/{g['id']}/sharing", json={"enabled": True})).status_code == 200
        subs = (await client.get(f"/api/copy/groups/{g['id']}/subscribers")).json()
        assert (await client.delete(f"/api/copy/groups/{g['id']}/subscribers/{subs[0]['id']}")).status_code == 200
        assert (await sub_client.get("/api/subscriptions")).json() == []
    finally:
        await cp.stop_all()


async def test_selected_visibility_and_non_admin_publish(worlds, client, sub_client):
    g = worlds["g"]
    r = await client.put(f"/api/copy/groups/{g['id']}/sharing", json={"visibility": "selected", "allowed_user_ids": []})
    assert r.status_code == 200
    assert (await sub_client.get("/api/marketplace")).json() == []
    assert (await sub_client.post(f"/api/marketplace/1/copy/{g['id']}/subscribe", json={"accounts": [{"spec": "X1"}]})).status_code == 403
    r = await client.put(f"/api/copy/groups/{g['id']}/sharing", json={"allowed_user_ids": [worlds["u2"]["id"]]})
    assert [x["group_id"] for x in (await sub_client.get("/api/marketplace")).json()] == [g["id"]]
    assert (await sub_client.put(f"/api/copy/groups/{g['id']}/sharing", json={"enabled": False})).status_code in (403, 404)
    await cp.stop_all()

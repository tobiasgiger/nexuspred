"""Copy trading stage 2 (app/copy_orders.py): twins of the leader's working orders."""
from __future__ import annotations

import asyncio

import pytest

from app import alerts, config, context, copy as cp, db, tradovate
from tests.helpers import BrokerFeed, FakeExecutor


class Sess(BrokerFeed):
    """Leader login: positions, working orders and their versions under test control."""
    def __init__(self):
        self.idx, self.name, self.environment, self.enabled, self.agent_id = 0, "L", "demo", True, 0
        self.accounts = [{"id": 1, "spec": "LEAD"}]
        self.positions: list[dict] = []
        self.orders: list[dict] = []          # {id, accountId, ordStatus, contractId, action, ocoId?}
        self.versions: dict[int, dict] = {}   # orderId → {id, orderQty, orderType, price, stopPrice}
        self.fail = False

    def has_token(self):
        return True

    async def _get_token(self):
        return "tok"

    async def _request(self, method, path, **kw):
        if self.fail:
            raise RuntimeError("offline")
        if path == "/position/list":
            return [dict(p) for p in self.positions]
        if path == "/order/list":
            return [dict(o) for o in self.orders]
        if path == "/contract/item":
            return {"name": {901: "MNQZ6", 902: "ESZ6"}.get(kw["params"]["id"], "?")}
        if path == "/auth/me":
            return {"userId": 7}
        if path == "/account/list":
            return [{"id": 1, "name": "LEAD"}]
        raise AssertionError(path)

    async def order_versions(self, ids):
        return {i: dict(self.versions[i]) for i in ids if i in self.versions}

    # helpers
    def add_order(self, oid, action, qty, otype, *, price=None, stop=None, cid=901, oco=0, vid=None):
        self.orders.append({"id": oid, "accountId": 1, "ordStatus": "Working", "contractId": cid, "action": action, "ocoId": oco})
        self.versions[oid] = {"id": vid or oid * 10, "orderId": oid, "orderQty": qty, "orderType": otype, "price": price, "stopPrice": stop}

    def drop_order(self, oid):
        self.orders = [o for o in self.orders if o["id"] != oid]


class FollowerSess(BrokerFeed):
    """Follower login whose /position/list the test controls (broker truth)."""
    def __init__(self):
        self.idx, self.name, self.environment, self.enabled, self.agent_id = 1, "F", "demo", True, 0
        self.accounts = [{"id": 2, "spec": "F1"}]
        self.positions: list[dict] = []

    def has_token(self):
        return True

    async def _request(self, method, path, **kw):
        if path == "/position/list":
            return [dict(p) for p in self.positions]
        raise AssertionError(path)


class Manager:
    def __init__(self, sessions, execs):
        self.sessions, self.execs = sessions, execs

    def all(self):
        return list(self.sessions)

    def executor_for(self, idx, spec, mult=1):
        return self.execs.get(spec)


def _group(**over):
    g = cp.new_group("G")
    g.update({"enabled": True, "feed": "poll", "copy_orders": True, "leader": {"token_idx": 0, "spec": "LEAD", "account_id": 1},
              "followers": [cp.normalize_follower({"token_idx": 1, "spec": "F1", "account_id": 2, "multiplier": 2})]})
    g.update(over)
    return g


@pytest.fixture
def world(monkeypatch, admin):
    lead, fol = Sess(), FollowerSess()
    ex = FakeExecutor("F1", track_working=True)
    ex.session, ex.id = fol, 2
    mgr = Manager([lead, fol], {"F1": ex})
    monkeypatch.setattr(tradovate, "manager_for", lambda area_id: mgr)
    monkeypatch.setattr(cp, "ORDERS_EVERY_N", 1)     # every _poll_once reads the orders in these tests
    sent = []

    async def rec(*a, **k):
        sent.append(a)
    monkeypatch.setattr(alerts, "copy_alert", rec)
    with context.use_area(1):
        config.save_settings({"trading_enabled": True})
    return {"lead": lead, "fol": fol, "ex": ex, "sent": sent}


async def _runner(world, **over):
    r = cp.GroupRunner(1, _group(**over))
    await r._seed_followers()
    await r._seed_leader(world["lead"], 1)
    r.leader_account_id = 1
    r._mark_feed(True)
    return r


def _kinds(limit=50):
    return [e["kind"] for e in db.list_copy_events(1, limit=limit)][::-1]


async def test_limit_twin_created_modified_and_cancelled(world):
    lead, ex = world["lead"], world["ex"]
    r = await _runner(world)
    lead.add_order(10, "Buy", 2, "Limit", price=21000.0)
    await r._poll_once(lead, 1)
    place = ex.of("place")
    assert len(place) == 1 and place[0]["order_type"] == "Limit" and place[0]["price"] == 21000.0 and place[0]["qty"] == 4
    twin = r.orders.twins[("F1", 10)]
    assert twin["follower_order_id"] == place[0]["order_id"] and db.list_copy_twins(1, r.id)[0]["leader_order_id"] == 10
    # same look again: nothing new
    await r._poll_once(lead, 1)
    assert len(ex.of("place")) == 1
    # leader moves the price and changes the size → modify
    lead.versions[10] = {"id": 101, "orderId": 10, "orderQty": 3, "orderType": "Limit", "price": 20990.0, "stopPrice": None}
    await r._poll_once(lead, 1)
    mod = ex.of("modify")
    assert mod and mod[-1] == {"order_id": place[0]["order_id"], "qty": 6, "order_type": "Limit", "price": 20990.0, "stop_price": None}
    assert r.orders.twins[("F1", 10)]["price"] == 20990.0
    # leader cancels → twin cancelled and forgotten
    lead.drop_order(10)
    await r._poll_once(lead, 1)
    assert ex.of("cancel") == [{"order_id": place[0]["order_id"]}] and not r.orders.twins and db.list_copy_twins(1, r.id) == []
    assert _kinds() == ["feed_up", "order_mirror", "order_modify", "order_cancel"]
    st = r.status()
    assert st["orders_enabled"] and st["leader_orders"] == [] and st["followers"][0]["orders"] == []


async def test_leader_fill_cancels_twin_and_uses_broker_truth(world):
    """The leader's limit fills. The follower's twin filled too: no market order is
    doubled on top. If it had not filled, the difference is sent as a market order."""
    lead, fol, ex = world["lead"], world["fol"], world["ex"]
    r = await _runner(world)
    lead.add_order(10, "Buy", 1, "Limit", price=21000.0)
    await r._poll_once(lead, 1)
    fid = ex.of("place")[0]["order_id"]
    # fill at the broker: leader position +1, leader order gone, follower's twin filled (+2), gone from working
    lead.drop_order(10)
    lead.positions = [{"accountId": 1, "contractId": 901, "netPos": 1}]
    ex.working = []
    fol.positions = [{"accountId": 2, "contractId": 901, "netPos": 2}]
    await r._poll_once(lead, 1)
    assert [c["order_type"] for c in ex.of("place")] == ["Limit"]          # no extra market order
    assert r.follower_pos[("F1", 901)] == 2
    assert ex.of("cancel") == [{"order_id": fid}]                            # cancel attempted, harmless
    # second scenario: leader adds via limit, this time the follower twin did not fill
    lead.add_order(11, "Buy", 1, "Limit", price=20950.0)
    await r._poll_once(lead, 1)
    fid2 = ex.of("place")[-1]["order_id"]
    lead.drop_order(11)
    lead.positions[0]["netPos"] = 2
    await r._poll_once(lead, 1)                                              # follower still at +2 at the broker
    last = ex.of("place")[-1]
    assert ex.of("cancel")[-1] == {"order_id": fid2} and last["order_type"] == "Market" and last["action"] == "Buy" and last["qty"] == 2
    assert r.follower_pos[("F1", 901)] == 4


async def test_oco_pair_becomes_one_oco_on_the_follower(world):
    lead, fol, ex = world["lead"], world["fol"], world["ex"]
    lead.positions = [{"accountId": 1, "contractId": 901, "netPos": 0}]
    r = await _runner(world)
    lead.positions = [{"accountId": 1, "contractId": 901, "netPos": 2}]
    await r._poll_once(lead, 1)                                               # mirror the position first (+4)
    assert ex.of("place")[-1]["qty"] == 4
    fol.positions = [{"accountId": 2, "contractId": 901, "netPos": 4}]
    lead.add_order(20, "Sell", 2, "Limit", price=21100.0, oco=21)
    lead.add_order(21, "Sell", 2, "Stop", stop=20900.0, oco=20)
    await r._poll_once(lead, 1)
    oco = ex.of("place_oco")
    assert len(oco) == 1 and oco[0]["qty"] == 4 and oco[0]["order_type"] == "Limit" and oco[0]["price"] == 21100.0
    assert oco[0]["other"] == {"action": "Sell", "order_type": "Stop", "price": None, "stop_price": 20900.0}
    assert {t["oco_with"] for t in r.orders.twins.values()} == {20, 21} and len(r.orders.twins) == 2
    assert all(t["qty"] == 4 for t in r.orders.twins.values())
    st = r.status()
    assert len(st["followers"][0]["orders"]) == 2 and all(o["oco"] for o in st["followers"][0]["orders"])
    # the stop fills at the broker: leader flat, both leader orders gone, follower flat too
    lead.drop_order(20); lead.drop_order(21)
    lead.positions = []
    ex.working = []
    fol.positions = []
    await r._poll_once(lead, 1)
    assert not r.orders.twins and r.follower_pos[("F1", 901)] == 0
    assert not any(c["order_type"] == "Market" for c in ex.of("place")[1:])   # no double close


async def test_skips_baseline_unmirrored_types_filters_and_zero_sizing(world):
    lead, ex = world["lead"], world["ex"]
    lead.positions = [{"accountId": 1, "contractId": 902, "netPos": 1}]        # ES held before the group → baseline
    r = await _runner(world, symbols=["MNQ", "ES"], followers=[cp.normalize_follower({"token_idx": 1, "spec": "F1", "direction": "long"})])
    lead.add_order(30, "Sell", 1, "Stop", stop=5000.0, cid=902)              # stop on the baseline position
    lead.add_order(31, "Buy", 1, "TrailingStop", cid=901)                    # exotic
    lead.add_order(32, "Sell", 1, "Limit", price=21500.0, cid=901)           # short entry, follower long-only → 0
    await r._poll_once(lead, 1)
    assert not ex.of("place") and not ex.of("place_oco")
    details = [e["detail"] for e in db.list_copy_events(1) if e["kind"] == "order_skip"]
    assert any("baseline" in d for d in details) and any("not mirrored" in d for d in details) and any("sizing gives 0" in d for d in details)
    with context.use_area(1):
        config.save_settings({"trading_enabled": False})
    lead.add_order(33, "Buy", 1, "Limit", price=20000.0, cid=901)
    await r._poll_once(lead, 1)
    assert not ex.of("place") and any("trading switch" in e["detail"] for e in db.list_copy_events(1, limit=1))


async def test_reconcile_recreates_missing_drops_done_and_cancels_orphans(world):
    lead, ex = world["lead"], world["ex"]
    r = await _runner(world)
    lead.add_order(40, "Buy", 1, "Limit", price=21000.0)
    await r._poll_once(lead, 1)
    fid = ex.of("place")[0]["order_id"]
    # 1) the follower's twin vanished at the broker (cancelled by hand) → dropped, then re-created by reconcile
    ex.working = []
    n = await r.orders.reconcile(lead, 1)
    assert n >= 1 and ("F1", 40) not in r.orders.twins or r.orders.twins[("F1", 40)]["follower_order_id"] != fid
    assert any(e["kind"] == "order_done" for e in db.list_copy_events(1))
    # a twin that vanished at the broker while the leader order still works is
    # held back for a moment (it may have filled: the position mirror reads the broker)
    n = await r.orders.reconcile(lead, 1)
    assert ("F1", 40) not in r.orders.twins and len(ex.of("place")) == 1
    r.orders._done_at.clear()
    n = await r.orders.reconcile(lead, 1)
    assert ("F1", 40) in r.orders.twins and len(ex.of("place")) == 2
    # 2) orphan: the leader order disappears but a poll was missed → reconcile cancels the twin
    lead.drop_order(40)
    r.orders.leader_orders = {}
    await r.orders.reconcile(lead, 1)
    assert ex.of("cancel") and not r.orders.twins


async def test_twins_survive_a_restart_and_are_verified(world):
    lead, ex = world["lead"], world["ex"]
    r = await _runner(world)
    lead.add_order(50, "Buy", 1, "Limit", price=21000.0)
    lead.add_order(51, "Buy", 1, "Limit", price=20900.0)
    await r._poll_once(lead, 1)
    fids = [c["order_id"] for c in ex.of("place")]
    assert len(db.list_copy_twins(1, r.id)) == 2
    # a new runner (restart): one twin is still working at the broker, the other is gone
    ex.working = [o for o in ex.working if o["id"] == fids[0]]
    r2 = cp.GroupRunner(1, {**r.group})
    await r2._seed_followers()
    assert set(r2.orders.twins) == {("F1", 50)} and len(db.list_copy_twins(1, r.id)) == 1
    # the leader cancels 50 → the restored twin is cancelled with the right follower id
    lead.drop_order(50)
    await r2._seed_leader(lead, 1)
    r2._mark_feed(True)
    await r2._poll_once(lead, 1)
    assert ex.of("cancel")[-1] == {"order_id": fids[0]}


async def test_flatten_cancels_twins_first_and_disabled_groups_ignore_orders(world):
    lead, fol, ex = world["lead"], world["fol"], world["ex"]
    r = await _runner(world)
    lead.positions = [{"accountId": 1, "contractId": 901, "netPos": 1}]
    await r._poll_once(lead, 1)
    fol.positions = [{"accountId": 2, "contractId": 901, "netPos": 2}]
    lead.add_order(60, "Sell", 1, "Stop", stop=20900.0)
    await r._poll_once(lead, 1)
    assert r.orders.twins
    n = await r.flatten_followers(reason="test")
    assert n == 1 and not r.orders.twins and ex.of("cancel")
    assert [c["order_type"] for c in ex.of("place")][-1] == "Market"
    # a group without copy_orders never touches orders
    ex2 = FakeExecutor("F1", track_working=True); ex2.session, ex2.id = fol, 2
    world_mgr = tradovate.manager_for(1)
    world_mgr.execs["F1"] = ex2
    r3 = await _runner(world, copy_orders=False)
    lead.add_order(61, "Buy", 1, "Limit", price=20000.0)
    await r3._poll_once(lead, 1)
    assert not r3.orders.enabled and not ex2.of("place") and r3.status()["orders_enabled"] is False


async def test_api_accepts_copy_orders_flag(client, admin, world):
    with context.use_area(1):
        config.save_settings({"token_accounts": [
            {"name": "L", "environment": "demo", "enabled": True, "access_token": "t", "accounts": [{"spec": "LEAD", "id": 1, "enabled": True}]},
            {"name": "F", "environment": "demo", "enabled": True, "access_token": "t", "accounts": [{"spec": "F1", "id": 2, "enabled": True}]}]})
    r = await client.post("/api/copy/groups", json={"name": "x"})
    gid = r.json()["id"]
    assert r.json()["copy_orders"] is True
    r = await client.put(f"/api/copy/groups/{gid}", json={"copy_orders": False})
    assert r.status_code == 200 and r.json()["copy_orders"] is False


async def test_socket_entities_drive_the_order_mirror(world):
    """The user-sync snapshot and order / orderVersion props events maintain the
    leader's order picture without a REST poll."""
    lead, ex = world["lead"], world["ex"]
    r = await _runner(world)
    # sync snapshot: one working limit with its version
    await r._on_ws_message(lead, 1, {"i": 2, "s": 200, "d": {"positions": [], "accounts": [{"id": 1}],
        "orders": [{"id": 70, "accountId": 1, "contractId": 901, "action": "Buy", "ordStatus": "Working", "ocoId": None}],
        "orderVersions": [{"id": 700, "orderId": 70, "orderQty": 1, "orderType": "Limit", "price": 21000.0}]}})
    assert _kinds() and ("F1", 70) in r.orders.twins and ex.of("place")[0]["price"] == 21000.0
    # a modification arrives as a new orderVersion
    await r._on_ws_message(lead, 1, {"e": "props", "d": {"entityType": "orderVersion", "eventType": "Created",
        "entity": {"id": 701, "orderId": 70, "orderQty": 2, "orderType": "Limit", "price": 20990.0}}})
    await asyncio.sleep(0.4)
    assert ex.of("modify")[-1]["price"] == 20990.0 and r.orders.twins[("F1", 70)]["qty"] == 4
    # a brand-new order: the order event lands first, its version a moment later
    await r._on_ws_message(lead, 1, {"e": "props", "d": {"entityType": "order", "eventType": "Created",
        "entity": {"id": 71, "accountId": 1, "contractId": 901, "action": "Sell", "ordStatus": "Working"}}})
    await r._on_ws_message(lead, 1, {"e": "props", "d": {"entityType": "orderVersion", "eventType": "Created",
        "entity": {"id": 710, "orderId": 71, "orderQty": 1, "orderType": "Stop", "stopPrice": 20800.0}}})
    await asyncio.sleep(0.4)
    assert ("F1", 71) in r.orders.twins and ex.of("place")[-1]["stop_price"] == 20800.0
    # the leader's order fills: status update → twin cancelled
    await r._on_ws_message(lead, 1, {"e": "props", "d": {"entityType": "order", "eventType": "Updated",
        "entity": {"id": 70, "accountId": 1, "contractId": 901, "action": "Buy", "ordStatus": "Filled"}}})
    await asyncio.sleep(0.4)
    assert ("F1", 70) not in r.orders.twins and ex.of("cancel")
    # another account's order is ignored
    await r._on_ws_message(lead, 1, {"e": "props", "d": {"entityType": "order", "entity": {"id": 99, "accountId": 5, "contractId": 901, "action": "Buy", "ordStatus": "Working"}}})
    await asyncio.sleep(0.4)
    assert ("F1", 99) not in r.orders.twins


async def test_leader_order_in_transition_keeps_the_twin(world):
    """PendingReplace / Suspended is not gone: the twin stays, the leader order is
    carried over until it works again (a version change then becomes a modify)."""
    lead, ex = world["lead"], world["ex"]
    r = await _runner(world)
    lead.add_order(80, "Buy", 1, "Limit", price=21000.0)
    await r._poll_once(lead, 1)
    fid = ex.of("place")[0]["order_id"]
    lead.orders[0]["ordStatus"] = "PendingReplace"
    await r._poll_once(lead, 1)
    assert ("F1", 80) in r.orders.twins and not ex.of("cancel") and 80 in r.orders.leader_orders
    lead.orders[0]["ordStatus"] = "Working"
    lead.versions[80] = {"id": 801, "orderId": 80, "orderQty": 1, "orderType": "Limit", "price": 20950.0, "stopPrice": None}
    await r._poll_once(lead, 1)
    assert ex.of("modify")[-1] == {"order_id": fid, "qty": 2, "order_type": "Limit", "price": 20950.0, "stop_price": None}
    for final in ("Filled",):
        lead.orders[0]["ordStatus"] = final
        await r._poll_once(lead, 1)
    assert ("F1", 80) not in r.orders.twins and ex.of("cancel") == [{"order_id": fid}]
    # a leader order without a version yet is waited for, not mirrored with size 0
    lead.orders.append({"id": 81, "accountId": 1, "ordStatus": "Working", "contractId": 901, "action": "Buy", "ocoId": 0})
    await r._poll_once(lead, 1)
    assert 81 not in r.orders.leader_orders and len(ex.of("place")) == 1
    lead.versions[81] = {"id": 810, "orderId": 81, "orderQty": 1, "orderType": "Limit", "price": 20900.0, "stopPrice": None}
    await r._poll_once(lead, 1)
    assert 81 in r.orders.leader_orders and len(ex.of("place")) == 2
    # a known order whose version is missing from one snapshot keeps its last picture
    lead.versions.pop(81)
    await r._poll_once(lead, 1)
    assert r.orders.leader_orders[81]["qty"] == 1 and not ex.of("modify")[1:]


async def test_oco_legs_of_different_size_become_two_orders(world):
    """The follower's OCO must share one quantity: legs that size differently
    (a 3-lot target and a 1-lot partial stop) are mirrored as independent orders."""
    lead, fol, ex = world["lead"], world["fol"], world["ex"]
    lead.positions = [{"accountId": 1, "contractId": 901, "netPos": 0}]
    r = await _runner(world)
    lead.positions = [{"accountId": 1, "contractId": 901, "netPos": 3}]
    await r._poll_once(lead, 1)
    fol.positions = [{"accountId": 2, "contractId": 901, "netPos": 6}]
    lead.add_order(20, "Sell", 3, "Limit", price=21100.0, oco=21)
    lead.add_order(21, "Sell", 1, "Stop", stop=20900.0, oco=20)
    await r._poll_once(lead, 1)
    assert not ex.of("place_oco")
    legs = [c for c in ex.of("place") if c["order_type"] != "Market"]
    assert [(c["order_type"], c["qty"]) for c in legs] == [("Limit", 6), ("Stop", 2)]
    assert len(r.orders.twins) == 2 and all(t["oco_with"] == 0 for t in r.orders.twins.values())
    assert any("two independent orders" in e["detail"] for e in db.list_copy_events(1))


async def test_cancel_failure_keeps_a_twin_that_still_works(world):
    lead, ex = world["lead"], world["ex"]
    r = await _runner(world)
    lead.add_order(90, "Buy", 1, "Limit", price=21000.0)
    await r._poll_once(lead, 1)
    fid = ex.of("place")[0]["order_id"]

    async def boom(order_id):
        raise tradovate.TradovateError("cancel: broker timeout")
    ex.cancel_order = boom
    lead.drop_order(90)
    await r._poll_once(lead, 1)
    assert ("F1", 90) in r.orders.twins and r.follower_err["F1"].startswith("cancel failed")
    assert any(e["kind"] == "order_reject" and "still working" in e["detail"] for e in db.list_copy_events(1))
    # the reconcile retries the orphan; once the broker no longer has it, it is dropped
    ex.working = []
    await r.orders.reconcile(lead, 1)
    assert ("F1", 90) not in r.orders.twins


async def test_concurrent_applies_place_one_twin(world):
    """A poll and a socket pass that both see a new order create one twin, not two."""
    lead, ex = world["lead"], world["ex"]
    r = await _runner(world)
    ex.place_delay = 0.05
    lead.add_order(95, "Buy", 1, "Limit", price=21000.0)
    now, statuses = await r.orders.snapshot(lead, 1)
    await asyncio.gather(r.orders.apply(lead, dict(now), dict(statuses)), r.orders.apply(lead, dict(now), dict(statuses)))
    assert len(ex.of("place")) == 1 and r.orders.twins[("F1", 95)]["follower_order_id"] == ex.of("place")[0]["order_id"]


async def test_socket_events_during_an_apply_are_not_lost(world):
    lead, ex = world["lead"], world["ex"]
    r = await _runner(world)
    ex.place_delay = 0.1
    await r._on_ws_message(lead, 1, {"e": "props", "d": {"entityType": "order", "eventType": "Created",
        "entity": {"id": 96, "accountId": 1, "contractId": 901, "action": "Buy", "ordStatus": "Working"}}})
    await r._on_ws_message(lead, 1, {"e": "props", "d": {"entityType": "orderVersion", "eventType": "Created",
        "entity": {"id": 960, "orderId": 96, "orderQty": 1, "orderType": "Limit", "price": 21000.0}}})
    await asyncio.sleep(0.3)                                    # the apply task is placing (slow broker)
    await r._on_ws_message(lead, 1, {"e": "props", "d": {"entityType": "order", "eventType": "Created",
        "entity": {"id": 97, "accountId": 1, "contractId": 901, "action": "Sell", "ordStatus": "Working"}}})
    await r._on_ws_message(lead, 1, {"e": "props", "d": {"entityType": "orderVersion", "eventType": "Created",
        "entity": {"id": 970, "orderId": 97, "orderQty": 1, "orderType": "Stop", "stopPrice": 20800.0}}})
    await asyncio.sleep(0.8)
    assert {k[1] for k in r.orders.twins} == {96, 97}
    # a final status prunes the socket entities once the twin is gone
    await r._on_ws_message(lead, 1, {"e": "props", "d": {"entityType": "order", "eventType": "Updated",
        "entity": {"id": 96, "accountId": 1, "contractId": 901, "action": "Buy", "ordStatus": "Filled"}}})
    await asyncio.sleep(0.5)
    assert ("F1", 96) not in r.orders.twins and 96 not in r.orders.ent_orders and 96 not in r.orders.ent_versions


async def test_first_follower_seed_runs_on_a_freshly_booted_host(world, monkeypatch):
    """monotonic() counts from boot: below the reseed interval it must not read as
    'seeded a moment ago' (CI runners boot seconds before the tests start)."""
    lead, ex = world["lead"], world["ex"]
    r = await _runner(world)
    lead.add_order(55, "Buy", 1, "Limit", price=21000.0)
    await r._poll_once(lead, 1)
    assert db.list_copy_twins(1, r.id)
    monkeypatch.setattr(cp.time, "monotonic", lambda: 5.0)
    r2 = cp.GroupRunner(1, {**r.group})
    await r2._seed_followers()
    assert set(r2.orders.twins) == {("F1", 55)}

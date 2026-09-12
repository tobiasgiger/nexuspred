"""Copy trading (app/copy.py + /api/copy): sizing, framing, the position mirror,
reconcile, feed-loss watchdog, validation and the CRUD API."""
from __future__ import annotations

import asyncio
import time

import pytest

from app import alerts, config, context, copy as cp, db, tradovate
from tests.helpers import BrokerFeed, FakeExecutor, settle


# ------------------------------------------------------------------ fakes
class Sess(BrokerFeed):
    def __init__(self, idx, name, accounts, *, positions=None, agent_id=0, enabled=True, environment="demo"):
        self.idx, self.name, self.environment, self.enabled = idx, name, environment, enabled
        self.accounts = accounts
        self.positions = list(positions or [])
        self.agent_id = agent_id
        self.fail = False
        self.requests: list[str] = []

    def has_token(self):
        return True

    async def _get_token(self):
        return "tok"

    async def _request(self, method, path, **kw):
        self.requests.append(path)
        if self.fail:
            raise RuntimeError("offline")
        if path == "/position/list":
            return [dict(p) for p in self.positions]
        if path == "/order/list":
            return []
        if path == "/contract/item":
            return {"name": {901: "MNQZ6", 902: "ESZ6"}.get(kw["params"]["id"], "?")}
        if path == "/user/list":
            return [{"id": 77}]
        if path == "/auth/me":
            return {"userId": 77, "name": "leader"}
        if path == "/account/list":
            return getattr(self, "account_rows", None) or [{"id": a.get("id"), "name": a["spec"]} for a in self.accounts]
        raise AssertionError(path)


class Manager:
    def __init__(self, sessions, executors):
        self.sessions, self.executors = sessions, executors

    def all(self):
        return list(self.sessions)

    def executor_for(self, idx, spec, mult=1):
        s = self.sessions[idx] if 0 <= idx < len(self.sessions) else None
        if s is None or not s.enabled:
            return None
        return self.executors.get(spec)


def _group(**over):
    g = cp.new_group("G1")
    g.update({"enabled": True, "feed": "poll", "leader": {"token_idx": 0, "spec": "LEAD", "account_id": 1},
              "followers": [cp.normalize_follower({"token_idx": 1, "spec": "F1", "account_id": 2})]})
    g.update(over)
    return g


_REAL_WS_ACCELERATOR = cp.GroupRunner._ws_accelerator


@pytest.fixture
def world(monkeypatch, admin):
    """A leader login (poll feed), one follower login with two accounts."""
    leader = Sess(0, "L", [{"id": 1, "spec": "LEAD"}])
    follower = Sess(1, "F", [{"id": 2, "spec": "F1"}, {"id": 3, "spec": "F2"}])
    execs = {"F1": FakeExecutor("F1"), "F2": FakeExecutor("F2")}
    execs["F1"].session, execs["F1"].id = follower, 2     # broker truth (position list) reachable per executor
    execs["F2"].session, execs["F2"].id = follower, 3
    mgr = Manager([leader, follower], execs)
    monkeypatch.setattr(tradovate, "manager_for", lambda area_id: mgr)
    monkeypatch.setattr(cp, "POLL_INTERVAL_S", 0.01)
    monkeypatch.setattr(cp, "POLL_WS_INTERVAL_S", 0.05)
    monkeypatch.setattr(cp, "RECONCILE_INTERVAL_S", 3600)
    monkeypatch.setattr(cp, "RECONNECT_BACKOFF_S", 0.02)
    monkeypatch.setattr(cp, "POLL_ERROR_SLEEP_S", 0.02)
    monkeypatch.setattr(cp, "ORDER_SETTLE_S", 0.0)          # reconcile right after an order (a dedicated test covers the hold-off)

    async def no_ws(self, session, account_id):             # never open a real socket from the tests
        await self._stop.wait()
    monkeypatch.setattr(cp.GroupRunner, "_ws_accelerator", no_ws)
    sent = []

    async def rec(title, message, **kw):
        sent.append((title, message, kw))
    monkeypatch.setattr(alerts, "copy_alert", rec)
    with context.use_area(1):
        config.save_settings({"trading_enabled": True})
    return {"leader": leader, "follower": follower, "execs": execs, "mgr": mgr, "sent": sent}


async def _wait(cond, timeout=2.0):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if cond():
            return True
        await asyncio.sleep(0.01)
    return cond()


def _placed(ex):
    return [(c["action"], c["qty"], c["symbol"]) for c in ex.of("place")]


# ------------------------------------------------------------------ sizing
def test_target_qty_multiplier_fixed_cap_and_direction():
    f = cp.normalize_follower({"token_idx": 1, "spec": "F1", "multiplier": 2})
    assert cp.target_qty(f, 3, 3) == 6 and cp.target_qty(f, -3, 3) == -6 and cp.target_qty(f, 0, 1) == 0
    half = cp.normalize_follower({"token_idx": 1, "spec": "F1", "multiplier": 0.5})
    assert cp.target_qty(half, 1, 1) == 1        # rounds half up: never zero for a real entry
    assert cp.target_qty(half, 3, 3) == 2
    fixed = cp.normalize_follower({"token_idx": 1, "spec": "F1", "mode": "fixed", "fixed": 2})
    assert cp.target_qty(fixed, 3, 3) == 2       # initial entry of 3 → 2 contracts
    assert cp.target_qty(fixed, 6, 3) == 4       # leader doubled → follower doubles
    assert cp.target_qty(fixed, 6, 3, copy_adds=False) == 2
    assert cp.target_qty(fixed, -1, 3) == -1     # proportional but never 0 while the leader holds
    capped = cp.normalize_follower({"token_idx": 1, "spec": "F1", "multiplier": 5, "max_contracts": 4})
    assert cp.target_qty(capped, 2, 2) == 4 and cp.target_qty(capped, -2, 2) == -4
    long_only = cp.normalize_follower({"token_idx": 1, "spec": "F1", "direction": "long"})
    assert cp.target_qty(long_only, 2, 2) == 2 and cp.target_qty(long_only, -2, 2) == 0


def test_normalize_follower_rejects_bad_values():
    with pytest.raises(ValueError):
        cp.normalize_follower({"token_idx": 1, "spec": "F1", "multiplier": 0})
    with pytest.raises(ValueError):
        cp.normalize_follower({"token_idx": 1, "spec": "F1", "mode": "fixed", "fixed": 0})
    with pytest.raises(ValueError):
        cp.normalize_follower({"token_idx": 1, "spec": "F1", "max_contracts": -1})
    assert cp.normalize_follower({"token_idx": 1, "spec": "F1", "direction": "sideways"})["direction"] == "both"


def test_parse_frames():
    assert cp.parse_frames("o") == [{"e": "open"}]
    assert cp.parse_frames("h") == [{"e": "heartbeat"}]
    assert cp.parse_frames('a[{"e":"props","d":{"entityType":"position"}},1]') == [{"e": "props", "d": {"entityType": "position"}}]
    assert cp.parse_frames("c[1000]") == [{"e": "close"}]
    assert cp.parse_frames("a[not json") == [] and cp.parse_frames("") == [] and cp.parse_frames("?") == []


# -------------------------------------------------------------- validation
def _accounts():
    return [{"token_idx": 0, "spec": "LEAD"}, {"token_idx": 1, "spec": "F1"}, {"token_idx": 1, "spec": "F2"}]


def test_validate_group_rules():
    g = _group()
    cp.validate_group(g, [], _accounts())
    with pytest.raises(ValueError, match="Leader"):
        cp.validate_group(_group(leader={"token_idx": 5, "spec": "X"}), [], _accounts())
    with pytest.raises(ValueError, match="at least one"):
        cp.validate_group(_group(followers=[]), [], _accounts())
    with pytest.raises(ValueError, match="own follower"):
        cp.validate_group(_group(followers=[cp.normalize_follower({"token_idx": 0, "spec": "LEAD"})]), [], _accounts())
    with pytest.raises(ValueError, match="twice"):
        cp.validate_group(_group(followers=[cp.normalize_follower({"token_idx": 1, "spec": "F1"})] * 2), [], _accounts())
    with pytest.raises(ValueError, match="flatten"):
        cp.validate_group(_group(feed_loss_flatten_s=2), [], _accounts())
    # LEAD → F1 exists; a group F1 → LEAD would loop
    other = _group(id="cg_other", leader={"token_idx": 1, "spec": "F1"},
                   followers=[cp.normalize_follower({"token_idx": 0, "spec": "LEAD"})])
    with pytest.raises(ValueError, match="loop"):
        cp.validate_group(other, [g], _accounts())
    # a chain LEAD → F1 → F2 is fine
    chain = _group(id="cg_chain", leader={"token_idx": 1, "spec": "F1"},
                   followers=[cp.normalize_follower({"token_idx": 1, "spec": "F2"})])
    cp.validate_group(chain, [g], _accounts())


# ------------------------------------------------------------------ mirror
async def test_poll_feed_mirrors_open_add_reduce_close_and_reverse(world):
    r = cp.GroupRunner(1, _group())
    r.start()
    try:
        assert await _wait(lambda: r.feed_ok)
        assert r.feed_kind == "poll"
        lead = world["leader"]
        ex = world["execs"]["F1"]
        lead.positions = [{"accountId": 1, "contractId": 901, "netPos": 2}]
        assert await _wait(lambda: len(ex.of("place")) == 1)
        assert _placed(ex) == [("Buy", 2, "MNQZ6")]
        lead.positions[0]["netPos"] = 3                                   # add
        assert await _wait(lambda: len(ex.of("place")) == 2)
        lead.positions[0]["netPos"] = 1                                   # reduce
        assert await _wait(lambda: len(ex.of("place")) == 3)
        lead.positions[0]["netPos"] = -2                                  # reverse
        assert await _wait(lambda: len(ex.of("place")) == 4)
        lead.positions = []                                               # close
        assert await _wait(lambda: len(ex.of("place")) == 5)
        assert _placed(ex) == [("Buy", 2, "MNQZ6"), ("Buy", 1, "MNQZ6"), ("Sell", 2, "MNQZ6"),
                               ("Sell", 3, "MNQZ6"), ("Buy", 2, "MNQZ6")]
        assert r.follower_pos[("F1", 901)] == 0
        assert r.last_latency_ms is not None
        st = r.status()
        assert st["running"] and st["feed"] == "poll" and st["followers"][0]["spec"] == "F1"
        kinds = [e["kind"] for e in db.list_copy_events(1)]
        assert kinds.count("mirror") == 5 and "feed_up" in kinds
        ev = db.list_copy_events(1, group_id=r.id, limit=1)[0]
        assert ev["follower"] == "F1" and ev["symbol"] == "MNQZ6" and ev["leader"] == "LEAD"
    finally:
        await r.stop()
    assert not r.status()["running"]


async def test_symbols_filter_direction_and_two_followers(world):
    g = _group(symbols=["ES"], followers=[
        cp.normalize_follower({"token_idx": 1, "spec": "F1", "multiplier": 2}),
        cp.normalize_follower({"token_idx": 1, "spec": "F2", "mode": "fixed", "fixed": 1, "direction": "long"}),
    ])
    r = cp.GroupRunner(1, g)
    r.start()
    try:
        assert await _wait(lambda: r.feed_ok)
        lead = world["leader"]
        lead.positions = [{"accountId": 1, "contractId": 901, "netPos": 2}]      # MNQ: filtered out
        await asyncio.sleep(0.05)
        assert not world["execs"]["F1"].of("place")
        lead.positions.append({"accountId": 1, "contractId": 902, "netPos": -3})  # ES short
        assert await _wait(lambda: len(world["execs"]["F1"].of("place")) == 1)
        assert _placed(world["execs"]["F1"]) == [("Sell", 6, "ESZ6")]
        assert not world["execs"]["F2"].of("place")                                # long-only follower stays out
        lead.positions[1]["netPos"] = 4                                            # reverse to long
        assert await _wait(lambda: len(world["execs"]["F2"].of("place")) == 1)
        assert _placed(world["execs"]["F2"]) == [("Buy", 1, "ESZ6")]
        assert await _wait(lambda: len(world["execs"]["F1"].of("place")) == 2)
        assert _placed(world["execs"]["F1"])[-1] == ("Buy", 14, "ESZ6")
    finally:
        await r.stop()


async def test_existing_leader_position_is_baseline_until_flat_or_synced(world):
    world["leader"].positions = [{"accountId": 1, "contractId": 901, "netPos": 2}]
    r = cp.GroupRunner(1, _group())
    r.start()
    try:
        assert await _wait(lambda: r.feed_ok and 901 in r.baseline)
        ex = world["execs"]["F1"]
        world["leader"].positions[0]["netPos"] = 3
        await asyncio.sleep(0.05)
        assert not ex.of("place")
        assert [e["kind"] for e in db.list_copy_events(1, limit=1)] == ["ignored"]
        assert r.status()["leader_positions"][0]["baseline"] is True
        world["leader"].positions = []                    # flat → mirrored from now on
        assert await _wait(lambda: 901 not in r.baseline)
        world["leader"].positions = [{"accountId": 1, "contractId": 901, "netPos": 1}]
        assert await _wait(lambda: len(ex.of("place")) == 1)
        # sync now copies whatever is open right away
        world["leader"].positions[0]["netPos"] = 4
        assert await _wait(lambda: len(ex.of("place")) == 2)
        r.baseline.add(901)
        r.follower_pos[("F1", 901)] = 0
        n = await r.sync_now()
        assert n == 1 and _placed(ex)[-1] == ("Buy", 4, "MNQZ6")
    finally:
        await r.stop()


async def test_reject_and_trading_switch_off(world):
    ex = world["execs"]["F1"]
    ex.fail_place = True
    r = cp.GroupRunner(1, _group())
    r.start()
    try:
        assert await _wait(lambda: r.feed_ok)
        world["leader"].positions = [{"accountId": 1, "contractId": 901, "netPos": 1}]
        assert await _wait(lambda: r.follower_err.get("F1"))
        assert "placeorder failed" in r.follower_err["F1"]
        assert world["sent"] and world["sent"][0][0] == "Copy reject: F1"
        assert r.status()["followers"][0]["error"]
        with context.use_area(1):
            config.save_settings({"trading_enabled": False})
        ex.fail_place = False
        world["leader"].positions[0]["netPos"] = 2
        assert await _wait(lambda: any(e["kind"] == "skipped" for e in db.list_copy_events(1)))
        assert not ex.of("place")
    finally:
        await r.stop()


async def test_reconcile_corrects_follower_drift(world):
    r = cp.GroupRunner(1, _group())
    r.start()
    try:
        assert await _wait(lambda: r.feed_ok)
        world["leader"].positions = [{"accountId": 1, "contractId": 901, "netPos": 2}]
        ex = world["execs"]["F1"]
        assert await _wait(lambda: len(ex.of("place")) == 1)
        # the follower's stop got hit at the broker: it is flat while the leader still holds 2
        world["follower"].positions = [{"accountId": 2, "contractId": 901, "netPos": 0}]
        assert await r.reconcile() == 1
        assert _placed(ex)[-1] == ("Buy", 2, "MNQZ6")
        assert any(e["kind"] == "drift" for e in db.list_copy_events(1))
        world["follower"].positions = [{"accountId": 2, "contractId": 901, "netPos": 2}]
        assert await r.reconcile() == 0
    finally:
        await r.stop()


async def test_feed_loss_flattens_followers_and_pauses(world, monkeypatch):
    monkeypatch.setattr(cp, "FEED_STALE_S", 0.05)
    r = cp.GroupRunner(1, _group(feed_loss_flatten_s=5))
    r.start()
    try:
        assert await _wait(lambda: r.feed_ok)
        world["leader"].positions = [{"accountId": 1, "contractId": 901, "netPos": 2}]
        ex = world["execs"]["F1"]
        assert await _wait(lambda: len(ex.of("place")) == 1)
        world["follower"].positions = [{"accountId": 2, "contractId": 901, "netPos": 2}]   # the fill at the broker
        world["leader"].fail = True                         # the poll feed dies
        await asyncio.sleep(0.1)
        await r.watchdog()
        assert not r.feed_ok and not r.paused                # lost, but not for long enough yet
        r.last_frame = time.monotonic() - 6
        await r.watchdog()
        assert r.paused and "feed lost" in r.pause_reason
        assert _placed(ex)[-1] == ("Sell", 2, "MNQZ6") and r.follower_pos[("F1", 901)] == 0
        world["follower"].positions = []
        assert await _wait(lambda: bool(world["sent"]))                       # the alert is fire-and-forget
        assert world["sent"][-1][0].startswith("Copy group paused") and world["sent"][-1][2] == {"email": True}
        # the flattened contract became baseline: leader changes are not mirrored
        # while the leader keeps the position (and the group is paused anyway)
        assert 901 in r.baseline
        world["leader"].fail = False
        assert await _wait(lambda: r.feed_ok)
        world["leader"].positions[0]["netPos"] = 3
        assert await _wait(lambda: any(e["kind"] == "ignored" and "existing position" in e["detail"] for e in db.list_copy_events(1)))
        assert len(ex.of("place")) == 2
        # resume + sync brings the follower back in line
        await r.sync_now()
        assert not r.paused and _placed(ex)[-1] == ("Buy", 3, "MNQZ6")
    finally:
        await r.stop()


async def test_websocket_messages_drive_the_mirror(world):
    r = cp.GroupRunner(1, _group(feed="websocket"))
    lead = world["leader"]
    await r._seed_followers()
    await r._seed_leader(lead, 1)
    r._mark_feed(True)
    ex = world["execs"]["F1"]
    # sync snapshot: a position the REST seed did not know was opened after the
    # seed → an entry, mirrored (the REST seed alone decides the baseline)
    await r._on_ws_message(lead, 1, {"i": 2, "s": 200, "d": {"positions": [{"accountId": 1, "contractId": 902, "netPos": 1}]}})
    assert 902 not in r.baseline and _placed(ex) == [("Buy", 1, "ESZ6")]
    # live property change on another account: ignored
    await r._on_ws_message(lead, 1, {"e": "props", "d": {"entityType": "position", "entity": {"accountId": 9, "contractId": 901, "netPos": 5}}})
    assert len(ex.of("place")) == 1
    await r._on_ws_message(lead, 1, {"e": "props", "d": {"entityType": "position", "entity": {"accountId": 1, "contractId": 901, "netPos": 2}}})
    assert _placed(ex) == [("Buy", 1, "ESZ6"), ("Buy", 2, "MNQZ6")]
    with pytest.raises(tradovate.TradovateError, match="authorize"):
        await r._on_ws_message(lead, 1, {"i": 1, "s": 401, "d": "bad token"})
    with pytest.raises(tradovate.TradovateError):
        await r._on_ws_message(lead, 1, {"e": "close"})


async def test_sync_area_starts_stops_and_restarts_runners(world):
    with context.use_area(1):
        g = _group()
        cp.save_groups([g], 1)
    await cp.sync_area(1)
    r = cp.runner(1, g["id"])
    assert r is not None and r.tasks
    await cp.sync_area(1)
    assert cp.runner(1, g["id"]) is r                       # unchanged config: same runner
    with context.use_area(1):
        cp.save_groups([{**g, "name": "renamed"}], 1)
    await cp.sync_area(1)
    assert cp.runner(1, g["id"]) is not r and not r.tasks   # changed: restarted
    with context.use_area(1):
        cp.save_groups([{**g, "enabled": False}], 1)
    await cp.sync_area(1)
    assert cp.runner(1, g["id"]) is None and cp.statuses(1) == {}
    await cp.stop_all()


async def test_reconcile_waits_for_a_fresh_order_to_settle(world, monkeypatch):
    """A market order that just went out is not yet in the broker's position list:
    the reconcile must not read the stale snapshot as drift and double the order."""
    monkeypatch.setattr(cp, "ORDER_SETTLE_S", 5.0)
    r = cp.GroupRunner(1, _group())
    lead = world["leader"]
    await r._seed_followers()
    await r._seed_leader(lead, 1)
    r._mark_feed(True)
    ex = world["execs"]["F1"]
    lead.positions = [{"accountId": 1, "contractId": 901, "netPos": 2}]
    await r._poll_once(lead, 1)
    assert _placed(ex) == [("Buy", 2, "MNQZ6")]
    # the broker still reports the follower flat (fill not yet in /position/list)
    world["follower"].positions = [{"accountId": 2, "contractId": 901, "netPos": 0}]
    assert await r.reconcile() == 0 and len(ex.of("place")) == 1
    # once the settle window is over the broker's word counts again
    r.last_order_at["F1"] = time.monotonic() - 10
    assert await r.reconcile() == 1 and _placed(ex)[-1] == ("Buy", 2, "MNQZ6")


async def test_poll_change_needs_two_looks_while_the_socket_is_synced(world):
    """With the socket synced a REST snapshot may predate an already applied
    socket event; a difference is acted on only when two consecutive polls agree."""
    r = cp.GroupRunner(1, _group(feed="websocket"))
    lead = world["leader"]
    await r._seed_followers()
    await r._seed_leader(lead, 1)
    r._mark_feed(True)
    ex = world["execs"]["F1"]
    r.ws_ok = True
    lead.positions = [{"accountId": 1, "contractId": 901, "netPos": 2}]
    assert await r._poll_once(lead, 1) == 0 and not ex.of("place")       # first look: pending
    lead.positions = [{"accountId": 1, "contractId": 901, "netPos": 3}]
    assert await r._poll_once(lead, 1) == 0 and not ex.of("place")       # a different value: pending again
    assert await r._poll_once(lead, 1) == 1 and _placed(ex) == [("Buy", 3, "MNQZ6")]
    # without the socket the poll is the feed and acts at once
    r.ws_ok = False
    lead.positions = []
    assert await r._poll_once(lead, 1) == 1 and _placed(ex)[-1] == ("Sell", 3, "MNQZ6")


async def test_flatten_uses_broker_truth_only_mirrored_contracts_and_enabled_followers(world):
    r = cp.GroupRunner(1, _group(followers=[cp.normalize_follower({"token_idx": 1, "spec": "F1", "account_id": 2}),
                                          cp.normalize_follower({"token_idx": 1, "spec": "F2", "account_id": 3, "enabled": False})]))
    lead = world["leader"]
    lead.positions = [{"accountId": 1, "contractId": 902, "netPos": 1}]      # ES held before the group: baseline
    await r._seed_followers()
    await r._seed_leader(lead, 1)
    r._mark_feed(True)
    ex, ex2 = world["execs"]["F1"], world["execs"]["F2"]
    lead.positions.append({"accountId": 1, "contractId": 901, "netPos": 2})
    await r._poll_once(lead, 1)
    assert _placed(ex) == [("Buy", 2, "MNQZ6")] and not ex2.of("place")
    # the broker says the follower holds 3 (a manual add) and 1 ES of its own
    world["follower"].positions = [{"accountId": 2, "contractId": 901, "netPos": 3}, {"accountId": 2, "contractId": 902, "netPos": 1},
                                   {"accountId": 3, "contractId": 901, "netPos": 5}]
    n = await r.flatten_followers(reason="test")
    assert n == 1 and _placed(ex)[-1] == ("Sell", 3, "MNQZ6")               # broker truth, MNQ only
    assert not ex2.of("place")                                             # disabled follower untouched
    assert 901 in r.baseline and r.follower_pos[("F1", 901)] == 0
    # the reconcile does not re-enter what was flattened
    world["follower"].positions = [{"accountId": 2, "contractId": 901, "netPos": 0}]
    assert await r.reconcile() == 0 and len(ex.of("place")) == 2


async def test_watchdog_treats_a_throttle_as_no_feed_loss(world, monkeypatch):
    monkeypatch.setattr(cp, "FEED_STALE_S", 0.05)
    r = cp.GroupRunner(1, _group(feed_loss_flatten_s=1))
    r.start()
    try:
        assert await _wait(lambda: r.feed_ok)
        world["leader"].positions = [{"accountId": 1, "contractId": 901, "netPos": 2}]
        ex = world["execs"]["F1"]
        assert await _wait(lambda: len(ex.of("place")) == 1)
        r.last_frame = time.monotonic() - 60
        r.throttled_until = time.monotonic() + 30                      # a 429 penalty is running
        await r.watchdog()
        assert r.feed_ok and not r.paused and len(ex.of("place")) == 1
    finally:
        await r.stop()


# --------------------------------------------------------------------- API
@pytest.fixture
def accounts_cfg(world):
    with context.use_area(1):
        config.save_settings({"token_accounts": [
            {"name": "L", "environment": "demo", "enabled": True, "access_token": "t", "accounts": [{"spec": "LEAD", "id": 1, "enabled": True}]},
            {"name": "F", "environment": "demo", "enabled": True, "access_token": "t", "accounts": [{"spec": "F1", "id": 2, "enabled": True}, {"spec": "F2", "id": 3, "enabled": True}]},
        ]})


async def test_api_crud_and_actions(client, accounts_cfg, world):
    r = await client.post("/api/copy/groups", json={"name": "Mirror MNQ"})
    assert r.status_code == 200 and r.json()["enabled"] is False
    gid = r.json()["id"]
    body = {"leader": {"token_idx": 0, "spec": "LEAD"}, "symbols": "mnq, es",
            "followers": [{"token_idx": 1, "spec": "F1", "mode": "fixed", "fixed": 2}], "feed_loss_flatten_s": 20, "enabled": True}
    r = await client.put(f"/api/copy/groups/{gid}", json=body)
    assert r.status_code == 200, r.text
    g = r.json()
    assert g["symbols"] == ["ES", "MNQ"] and g["followers"][0]["fixed"] == 2 and g["status"]["running"]
    # validation errors are 400
    r = await client.put(f"/api/copy/groups/{gid}", json={"followers": [{"token_idx": 0, "spec": "LEAD"}]})
    assert r.status_code == 400 and "own follower" in r.text
    r = await client.put(f"/api/copy/groups/{gid}", json={"feed_loss_flatten_s": 1})
    assert r.status_code == 400
    # the settings API must not overwrite groups (protected key)
    r = await client.get("/api/copy/groups")
    assert [x["id"] for x in r.json()] == [gid]
    # status + events
    assert await _wait(lambda: cp.runner(1, gid) is not None and cp.runner(1, gid).feed_ok)
    r = await client.get("/api/copy/status")
    assert r.json()[gid]["feed_ok"] is True
    r = await client.get("/api/copy/events", params={"group_id": gid})
    assert any(e["kind"] == "feed_up" for e in r.json())
    # flatten → paused; resume clears; sync copies
    world["leader"].positions = [{"accountId": 1, "contractId": 901, "netPos": 2}]
    ex = world["execs"]["F1"]
    assert await _wait(lambda: len(ex.of("place")) == 1)
    world["follower"].positions = [{"accountId": 2, "contractId": 901, "netPos": 2}]   # the fill at the broker
    r = await client.post(f"/api/copy/groups/{gid}/flatten")
    assert r.status_code == 200 and r.json()["paused"] and r.json()["flattened"] == 1
    r = await client.post(f"/api/copy/groups/{gid}/resume")
    assert r.status_code == 200 and not r.json()["paused"]
    r = await client.post(f"/api/copy/groups/{gid}/sync")
    assert r.status_code == 200 and r.json()["synced"] == 1
    r = await client.post(f"/api/copy/groups/{gid}/disable")
    assert r.status_code == 200 and r.json()["status"] is None
    r = await client.post(f"/api/copy/groups/{gid}/sync")
    assert r.status_code == 409
    r = await client.post(f"/api/copy/groups/{gid}/enable")
    assert r.status_code == 200 and r.json()["status"]["running"]
    r = await client.delete(f"/api/copy/groups/{gid}")
    assert r.status_code == 200 and cp.runner(1, gid) is None
    assert (await client.get("/api/copy/groups/nope/resume")).status_code in (404, 405)
    assert (await client.delete("/api/copy/groups/nope")).status_code == 404
    await settle()


async def test_api_enable_requires_a_valid_group(client, accounts_cfg):
    r = await client.post("/api/copy/groups", json={"name": "x"})
    gid = r.json()["id"]
    r = await client.post(f"/api/copy/groups/{gid}/enable")
    assert r.status_code == 400 and "Leader" in r.text


async def test_settings_api_cannot_touch_copy_groups(client, accounts_cfg):
    with context.use_area(1):
        cp.save_groups([_group(enabled=False)], 1)
    r = await client.post("/api/settings", json={"copy_groups": []})
    assert r.status_code == 200
    with context.use_area(1):
        assert len(cp.load_groups(1)) == 1


async def test_reconnect_mirrors_changes_made_during_the_outage(world):
    r = cp.GroupRunner(1, _group())
    r.start()
    try:
        assert await _wait(lambda: r.feed_ok)
        lead, ex = world["leader"], world["execs"]["F1"]
        lead.positions = [{"accountId": 1, "contractId": 901, "netPos": 1}]
        assert await _wait(lambda: len(ex.of("place")) == 1)
        lead.fail = True
        assert await _wait(lambda: not r.feed_ok)
        lead.positions = [{"accountId": 1, "contractId": 902, "netPos": -2}]   # closed MNQ, opened ES while away
        lead.fail = False
        assert await _wait(lambda: len(ex.of("place")) == 3)
        assert sorted(_placed(ex)[1:]) == [("Sell", 1, "MNQZ6"), ("Sell", 2, "ESZ6")]
        assert 902 not in r.baseline
    finally:
        await r.stop()


async def test_backstop_poll_mirrors_what_the_socket_missed(world):
    r = cp.GroupRunner(1, _group(feed="websocket"))
    lead, ex = world["leader"], world["execs"]["F1"]
    await r._seed_followers()
    await r._seed_leader(lead, 1)
    r._mark_feed(True)
    lead.positions = [{"accountId": 1, "contractId": 901, "netPos": 2}]      # no props event arrives
    assert await r._poll_once(lead, 1, source="backstop") == 1
    assert _placed(ex) == [("Buy", 2, "MNQZ6")]
    kinds = [e["kind"] for e in db.list_copy_events(1)]
    assert "ws_miss" in kinds and "mirror" in kinds
    assert await r._poll_once(lead, 1, source="backstop") == 0                # nothing new


async def test_reconcile_fills_a_follower_that_was_never_mirrored(world):
    r = cp.GroupRunner(1, _group(feed="websocket"))
    lead, ex = world["leader"], world["execs"]["F1"]
    await r._seed_followers()
    await r._seed_leader(lead, 1)
    r._mark_feed(True)
    r.leader_net[901], r.unit[901] = 2, 2          # leader change known, follower order never happened
    r.contract_names[901] = "MNQZ6"
    assert await r.reconcile() == 1
    assert _placed(ex) == [("Buy", 2, "MNQZ6")]
    # a follower that just rejected is left alone for a while
    ex.fail_place = True
    r.leader_net[901] = 3
    assert await r.reconcile() == 1 and r.follower_err["F1"]
    assert await r.reconcile() == 0                # hold-off after the reject


async def test_leader_account_id_falls_back_to_the_account_list(world):
    lead = world["leader"]
    lead.accounts = [{"spec": "LEAD"}]             # discovered without an id
    lead.account_rows = [{"id": 1, "name": "LEAD"}]
    r = cp.GroupRunner(1, _group(leader={"token_idx": 0, "spec": "LEAD", "account_id": 0}))
    assert await r._leader_account_id(lead) == 1
    lead.accounts, lead.account_rows = [], []
    assert await r._leader_account_id(lead) == 0


async def test_symbol_filter_is_reported_and_ws_diag_is_collected(world):
    r = cp.GroupRunner(1, _group(feed="websocket", symbols=["ES"]))
    lead = world["leader"]
    await r._seed_followers()
    await r._seed_leader(lead, 1)
    await r._on_ws_message(lead, 1, {"i": 2, "s": 200, "d": {"positions": [], "accounts": [{"id": 1}], "orders": []}})
    await r._on_ws_message(lead, 1, {"e": "props", "d": {"entityType": "Position", "eventType": "Created", "entity": {"accountId": 1, "contractId": 901, "netPos": 1}}})
    await r._on_ws_message(lead, 1, {"e": "props", "d": {"entityType": "order", "entity": {"accountId": 1}}})
    ev = db.list_copy_events(1, limit=1)[0]
    assert ev["kind"] == "filtered" and "MNQ" in ev["detail"]
    d = r.status()["diag"]
    assert d["sync"]["accounts"] == [1] and d["props"] == {"position": 1, "order": 1}
    assert d["last_position_event"]["contractId"] == 901 and d["leader_account_id"] == 0
    with pytest.raises(tradovate.TradovateError, match="sync failed"):
        await r._on_ws_message(lead, 1, {"i": 2, "s": 400, "d": "bad request"})


class _FakeWs:
    """A scripted Tradovate socket: frames to deliver, sends recorded."""
    def __init__(self, frames):
        self.frames = list(frames)
        self.sent: list[str] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def send(self, text):
        self.sent.append(text)
        if text.startswith("authorize"):
            self.frames.insert(0, 'a[{"s":200,"i":1}]')
        if text.startswith("user/syncrequest"):
            self.frames.insert(0, 'a[{"s":200,"i":2,"d":{"positions":[],"accounts":[{"id":1}]}}]')

    async def recv(self):
        if not self.frames:
            await asyncio.sleep(0.05)
            raise RuntimeError("socket closed by the fake")
        f = self.frames.pop(0)
        if f == "<wait>":
            await asyncio.sleep(0.2)
            return "h"
        return f


async def test_socket_accelerates_but_never_counts_as_the_feed(world, monkeypatch):
    import sys, types
    ws = _FakeWs(["o", "<wait>", 'a[{"e":"props","d":{"entityType":"position","eventType":"Updated","entity":{"accountId":1,"contractId":901,"netPos":2}}}]', "<wait>"])
    fake_mod = types.SimpleNamespace(connect=lambda *a, **k: ws)
    monkeypatch.setitem(sys.modules, "websockets", fake_mod)
    monkeypatch.setattr(cp.GroupRunner, "_ws_accelerator", _REAL_WS_ACCELERATOR)   # this test drives the real socket loop
    monkeypatch.setattr(cp, "RECONNECT_BACKOFF_S", 5.0)          # no reconnect during the test
    r = cp.GroupRunner(1, _group(feed="websocket"))
    r.start()
    try:
        assert await _wait(lambda: r.feed_ok and r.ws_ok)
        # the sync request went out only after the authorize answer
        assert [s.split("\n")[0] for s in ws.sent if not s.startswith("[")][:2] == ["authorize", "user/syncrequest"]
        assert r.diag["sync"]["accounts"] == [1]
        ex = world["execs"]["F1"]
        assert await _wait(lambda: len(ex.of("place")) == 1)       # the socket event was mirrored
        assert _placed(ex) == [("Buy", 2, "MNQZ6")]
        # the socket dies: the feed stays up (the poll carries it), the event is logged once
        assert await _wait(lambda: not r.ws_ok)
        assert r.feed_ok and not r.paused
        assert [e["kind"] for e in db.list_copy_events(1)].count("ws_lost") == 1
        await r.watchdog()
        assert not r.paused
        st = r.status()
        assert st["feed"] == "websocket" and st["feed_ok"] and not st["ws_ok"] and "socket closed" in st["ws_error"]
        # the poll keeps mirroring while the socket is down
        world["leader"].positions = [{"accountId": 1, "contractId": 901, "netPos": 3}]
        assert await _wait(lambda: len(ex.of("place")) == 2)
    finally:
        await r.stop()


async def test_rate_limit_is_a_throttle_not_a_lost_feed(world, monkeypatch):
    lead = world["leader"]
    r = cp.GroupRunner(1, _group())
    r.start()
    try:
        assert await _wait(lambda: r.feed_ok)
        base = cp.POLL_INTERVAL_S
        orig = lead._request

        async def limited(method, path, **kw):
            if path == "/position/list" and lead.limit:
                lead.limit -= 1
                raise tradovate.RateLimited(path, '{"p-ticket":"x","p-time":0.05}', 0.05)
            return await orig(method, path, **kw)
        lead.limit = 3
        lead._request = limited
        assert await _wait(lambda: lead.limit == 0)
        await r.watchdog()
        assert r.feed_ok and not r.paused and r.poll_interval > base and r.diag["rate_limits"] == 3
        assert "rate limited" in r.error
        assert not any(e["kind"] in ("feed_lost", "paused") for e in db.list_copy_events(1))
        # still mirroring once the penalty is over
        lead.positions = [{"accountId": 1, "contractId": 901, "netPos": 1}]
        assert await _wait(lambda: len(world["execs"]["F1"].of("place")) == 1)
        assert r.status()["throttled"] is False or r.status()["poll_interval"] > base
    finally:
        await r.stop()


def test_429_becomes_rate_limited_with_the_penalty(monkeypatch):
    assert tradovate._penalty_seconds('{"p-ticket":"t","p-time":12}') == 12.0
    assert tradovate._penalty_seconds("") == 5.0 and tradovate._penalty_seconds("garbage") == 5.0
    err = tradovate.RateLimited("/position/list", '{"p-time":3}', 3.0)
    assert isinstance(err, tradovate.TradovateError) and err.retry_after == 3.0 and "429" in str(err)


async def test_session_request_maps_429(admin):
    sess = tradovate.TradovateSession(0, {"name": "L", "environment": "demo", "enabled": True, "access_token": "t"}, area_id=1)

    class Resp:
        status_code, text = 429, '{"p-ticket":"abc","p-time":7}'

    class Client:
        async def request(self, *a, **k):
            return Resp()
    from app import http
    orig = http.client
    http.client = lambda name: Client()
    try:
        sess._get_token = lambda: _tok()
        with pytest.raises(tradovate.RateLimited) as ei:
            await sess._request("GET", "/position/list")
        assert ei.value.retry_after == 7.0
        # the login remembers the penalty: every loop sharing it waits
        import time
        assert sess.penalty_until > time.monotonic() + 5 and sess.rate_limits == 1
        sess.penalty_until = time.monotonic() + 0.1
        t0 = time.monotonic()
        with pytest.raises(tradovate.RateLimited):
            await sess._request("GET", "/order/list")
        assert time.monotonic() - t0 >= 0.09
    finally:
        http.client = orig


async def _tok():
    return "t"


async def test_mirrored_state_survives_a_restart(world):
    lead, ex = world["leader"], world["execs"]["F1"]
    g = _group()
    r = cp.GroupRunner(1, g)
    r.start()
    try:
        assert await _wait(lambda: r.feed_ok)
        lead.positions = [{"accountId": 1, "contractId": 901, "netPos": 2}]
        assert await _wait(lambda: len(ex.of("place")) == 1)
        assert db.list_copy_state(1, g["id"])[0]["leader_net"] == 2
    finally:
        await r.stop()
    # the bridge restarts while the leader adds one contract (the follower holds its 2 at the broker)
    lead.positions[0]["netPos"] = 3
    world["follower"].positions = [{"accountId": 2, "contractId": 901, "netPos": 2}]
    r2 = cp.GroupRunner(1, g)
    r2.start()
    try:
        assert await _wait(lambda: len(ex.of("place")) == 2)        # not baseline: the add is mirrored
        assert _placed(ex)[-1] == ("Buy", 1, "MNQZ6") and 901 not in r2.baseline
        assert any(e["kind"] == "resumed" and "restart" in e["detail"] for e in db.list_copy_events(1))
    finally:
        await r2.stop()
    # …and a leader that went flat during the downtime is closed on the followers
    lead.positions = []
    world["follower"].positions = [{"accountId": 2, "contractId": 901, "netPos": 3}]
    r3 = cp.GroupRunner(1, g)
    r3.start()
    try:
        assert await _wait(lambda: len(ex.of("place")) == 3)
        assert _placed(ex)[-1] == ("Sell", 3, "MNQZ6") and db.list_copy_state(1, g["id"]) == []
    finally:
        await r3.stop()


async def test_baseline_flat_is_not_mirrored(world):
    lead, ex = world["leader"], world["execs"]["F1"]
    lead.positions = [{"accountId": 1, "contractId": 901, "netPos": 2}]
    world["follower"].positions = [{"accountId": 2, "contractId": 901, "netPos": 5}]   # the follower's own trade
    r = cp.GroupRunner(1, _group())
    r.start()
    try:
        assert await _wait(lambda: r.feed_ok and 901 in r.baseline)
        lead.positions = []                                          # leader closes its pre-existing position
        assert await _wait(lambda: 901 not in r.baseline)
        await asyncio.sleep(0.05)
        assert not ex.of("place")                                    # the follower's 5 are left alone
        assert any(e["kind"] == "ignored" and "closed" in e["detail"] for e in db.list_copy_events(1))
        lead.positions = [{"accountId": 1, "contractId": 901, "netPos": 1}]   # next entry: mirrored (broker truth → 5 → target 1)
        assert await _wait(lambda: len(ex.of("place")) == 1)
    finally:
        await r.stop()


def test_a_follower_belongs_to_one_group_only():
    g1 = _group(id="cg_1")
    g2 = _group(id="cg_2", leader={"token_idx": 1, "spec": "F2", "account_id": 3},
                followers=[cp.normalize_follower({"token_idx": 1, "spec": "F1"})])
    with pytest.raises(ValueError, match="already follows"):
        cp.validate_group(g2, [g1], _accounts())
    g1["followers"][0]["enabled"] = False                            # a disabled follower does not block
    cp.validate_group(g2, [g1], _accounts())


async def test_feed_loss_can_pause_without_flattening(world, monkeypatch):
    monkeypatch.setattr(cp, "FEED_STALE_S", 0.05)
    r = cp.GroupRunner(1, _group(feed_loss_flatten_s=5, on_feed_loss="pause"))
    r.start()
    try:
        assert await _wait(lambda: r.feed_ok)
        world["leader"].positions = [{"accountId": 1, "contractId": 901, "netPos": 2}]
        ex = world["execs"]["F1"]
        assert await _wait(lambda: len(ex.of("place")) == 1)
        world["leader"].fail = True
        assert await _wait(lambda: not r.feed_ok)
        r.last_frame = time.monotonic() - 6
        await r.watchdog()
        assert r.paused and "keep their positions" in r.pause_reason
        assert len(ex.of("place")) == 1 and r.follower_pos[("F1", 901)] == 2      # nothing closed
    finally:
        await r.stop()

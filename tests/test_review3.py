"""Correctness review batch (alpha.61): broker adapters, copy engine, news lock."""
from __future__ import annotations

import asyncio

import pytest

from app import config, context, copy as cp, db, news, rithmic, state, tradovate
from tests.helpers import FakeExecutor
from tests.test_rithmic import FakeRithmicClient, rsess  # noqa: F401


# ------------------------------------------------------------------ rithmic
def test_rithmic_symbol_forms():
    assert rithmic._root("MNQZ6") == "MNQ" and rithmic._root("MNQZ26") == "MNQ" and rithmic._root("MNQ") == "MNQ"
    assert rithmic.exchange_for("YMZ26") == "CBOT" and rithmic.exchange_for("MNQZ26") == "CME"
    assert rithmic._rithmic_symbol("MNQZ26") == "MNQZ6" and rithmic._rithmic_symbol("MNQZ6") == "MNQZ6"


async def test_rithmic_cancel_honours_the_account_hint(rsess):
    s = rsess["s"]
    await s.connect()
    c = rsess["made"][0]
    # a twin reloaded after a restart is not in the basket map: the executor's account wins
    ex2 = tradovate.AccountExecutor(s, {"spec": "APEX-456", "id": s.accounts[1]["id"], "enabled": True})
    await ex2.cancel_order(7777)
    assert c.calls[-1] == ("cancel", {"basket_id": "7777", "account_id": "APEX-456"})
    with pytest.raises(tradovate.TradovateError, match="account unknown"):
        await s.cancel_order(7778)                      # two accounts on the login: never guess the primary


async def test_rithmic_one_bad_account_does_not_kill_the_feed(rsess):
    s = rsess["s"]
    await s.connect()
    c = rsess["made"][0]
    real = c.list_positions

    async def flaky(**kw):
        if kw.get("account_id") == "APEX-456":
            raise RuntimeError("account closed")
        return await real(**kw)
    c.list_positions = flaky
    rows = await s.positions_snapshot()
    assert [r["netPos"] for r in rows] == [2]
    assert sum(1 for e in state.recent_events() if "APEX-456" in e["message"] and "unavailable" in e["message"]) == 1
    await s.positions_snapshot()                        # reported once, not per tick
    assert sum(1 for e in state.recent_events() if "APEX-456" in e["message"] and "unavailable" in e["message"]) == 1

    async def dead(**kw):
        raise RuntimeError("gateway down")
    c.list_positions = dead
    with pytest.raises(tradovate.TradovateError, match="every account failed"):
        await s.positions_snapshot()


async def test_rithmic_timeout_is_outcome_unknown_not_rejected(rsess, monkeypatch):
    from app import alerts
    s = rsess["s"]
    await s.connect()
    c = rsess["made"][0]
    problems = []

    async def rec(title, message):
        problems.append(title)
    monkeypatch.setattr(alerts, "execution_problem", rec)

    async def slow(*a, **k):
        raise asyncio.TimeoutError
    c.submit_order = slow
    ex = tradovate.AccountExecutor(s, {"spec": "APEX-123", "id": s.accounts[0]["id"], "enabled": True})
    with context.use_area(1), pytest.raises(tradovate.OrderOutcomeUnknown, match="CHECK THE ACCOUNT"):
        await ex.place_order(symbol="MNQZ6", action="Buy", qty=1, order_type="Market")
    await asyncio.sleep(0.01)
    assert state.recent_orders()[0]["status"] == "unknown" and problems == ["Order outcome unknown on APEX-123"]
    assert not [x for x in c.calls if x[0] == "cancel"]


async def test_replaced_rithmic_client_is_disconnected(rsess):
    s = rsess["s"]
    await s.connect()
    first = rsess["made"][0]
    s.adopt_credentials({"rithmic_user": "u2", "rithmic_password": "p2"})
    await asyncio.sleep(0.01)
    assert first.plants["order"].is_connected is False   # the old client's sockets are gone
    await s.connect()
    assert len(rsess["made"]) == 2


# ------------------------------------------------------------- login save
async def test_switching_broker_resets_the_old_brokers_state(client, admin):
    r = await client.post("/api/token-accounts", json=[{"name": "L", "broker": "tradovate", "environment": "demo", "enabled": True,
                                                        "access_token": "tok", "account_spec": "DEMO1", "account_id": 11}])
    assert r.status_code == 200, r.text
    with context.use_area(1):
        config.update(lambda s: s["token_accounts"][0].__setitem__("accounts", [{"spec": "DEMO1", "id": 11, "enabled": True}]))
    r = await client.post("/api/token-accounts", json=[{"name": "L", "broker": "rithmic", "environment": "demo", "enabled": False,
                                                        "rithmic_user": "u", "rithmic_password": "p", "rithmic_system": "Rithmic Paper Trading"}])
    assert r.status_code == 200, r.text
    with context.use_area(1):
        t = config.load_settings()["token_accounts"][0]
    assert t["broker"] == "rithmic" and t["accounts"] == [] and t["account_spec"] == "" and not t["account_id"]
    assert t["access_token"] == "" and t["rithmic_user"] == "u" and t["lid"]


# ------------------------------------------------------------------- copy
def test_copy_groups_stay_within_one_broker(admin):
    accounts = [{"token_idx": 0, "lid": "a", "spec": "T1", "broker": "tradovate"},
                {"token_idx": 1, "lid": "b", "spec": "R1", "broker": "rithmic"},
                {"token_idx": 2, "lid": "c", "spec": "T2", "broker": "tradovate"}]
    g = cp.new_group("x")
    g.update({"leader": {"token_idx": 0, "lid": "a", "spec": "T1"}, "followers": [cp.normalize_follower({"token_idx": 1, "lid": "b", "spec": "R1"})]})
    with pytest.raises(ValueError, match="stays within one broker"):
        cp.validate_group(g, [], accounts)
    g["followers"] = [cp.normalize_follower({"token_idx": 2, "lid": "c", "spec": "T2"})]
    cp.validate_group(g, [], accounts)


async def test_release_followers_cancels_their_twins(admin, monkeypatch):
    g = cp.new_group("g")
    g.update({"enabled": True, "leader": {"token_idx": 0, "spec": "LEAD", "account_id": 1},
              "followers": [cp.normalize_follower({"token_idx": 1, "spec": "F1", "account_id": 2})]})
    r = cp.GroupRunner(1, g)
    cp._runners[(1, g["id"])] = r
    cancelled = []

    async def cancel_all(*, reason, spec=None, cid=None):
        cancelled.append((reason, spec))
        return 2
    monkeypatch.setattr(r.orders, "cancel_all", cancel_all)
    assert await cp.release_followers(1, g["id"], ["F1", ""]) == 2
    assert cancelled == [("follower left the group", "F1")]
    assert await cp.release_followers(1, "nope", ["F1"]) == 0
    cp._runners.clear()


def test_publisher_rows_never_carry_the_subscribers_account_name(admin):
    st = {"followers": [{"spec": "APEX-777", "external": True, "sub_id": 4, "error": "APEX-777: Buy 1 MNQZ6 rejected", "orders": []},
                        {"spec": "OWN", "external": False, "error": "OWN: fine", "orders": []}]}
    m = cp.masked_status(st)
    assert m["followers"][0]["spec"] == "subscriber #4" and "APEX-777" not in m["followers"][0]["error"]
    assert m["followers"][1]["error"] == "OWN: fine"


async def test_mirror_skips_a_workspace_flattened_for_news(admin, monkeypatch):
    g = cp.new_group("g")
    g.update({"enabled": True, "leader": {"token_idx": 0, "spec": "LEAD", "account_id": 1},
              "followers": [cp.normalize_follower({"token_idx": 1, "spec": "F1", "account_id": 2})]})
    with context.use_area(1):
        config.save_settings({"trading_enabled": True})
    r = cp.GroupRunner(1, g)
    ex = FakeExecutor("F1")
    monkeypatch.setattr(r, "_executor", lambda f: ex)
    monkeypatch.setattr(news, "flattened_lock", lambda area_id: {"title": "CPI"})
    r.leader_net[5] = 2
    await r._mirror_follower(r.followers[0], 5, "MNQZ6", 2, 2, True, "drift", None)
    assert ex.of("place") == []
    monkeypatch.setattr(news, "flattened_lock", lambda area_id: None)
    r.leader_net[5] = 3                                  # the leader moved while the drift check waited: the newer event handles it
    await r._mirror_follower(r.followers[0], 5, "MNQZ6", 2, 2, True, "drift", None)
    assert ex.of("place") == []
    await r._mirror_follower(r.followers[0], 5, "MNQZ6", 3, 3, True, "position", None)
    assert [(p["action"], p["qty"]) for p in ex.of("place")] == [("Buy", 3)]


def test_news_flattened_lock(admin):
    with context.use_area(1):
        config.save_settings({"news_lock": {"enabled": True, "action": "flatten", "manual": [], "before": 30, "after": 15}})
    assert news.flattened_lock(1) is None                 # no event → no lock

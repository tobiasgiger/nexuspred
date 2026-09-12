"""Edge cases from the second audit: settings surviving a re-discovery, bracket
slices, the risk guard's cadence, order priority under a penalty, front-month
resolution, reconcile on locked followers, the cancel fallback."""
from __future__ import annotations

import time
from datetime import datetime, timezone

import pytest

from app import config, context, pnl, risk, signals, tradovate
from app.engine import common
from tests.helpers import FakeExecutor
from tests.test_strategies import ENTRY, live, wh  # noqa: F401


def test_rediscovery_keeps_the_risk_block(admin):
    sess = tradovate.TradovateSession(0, {"name": "L", "environment": "demo", "enabled": True, "access_token": "t",
                                          "accounts": [{"spec": "A1", "id": 1, "enabled": True, "qty_multiplier": 2,
                                                        "risk": {"loss_limit": 500, "profit_limit": 0, "flatten_at": "21:30"}}]}, area_id=1)
    sess._merge_accounts([{"name": "A1", "id": 1}, {"name": "A2", "id": 2}])
    by = {a["spec"]: a for a in sess.accounts}
    assert by["A1"]["risk"]["loss_limit"] == 500 and by["A1"]["qty_multiplier"] == 2.0 and "risk" not in by["A2"]


async def test_bracket_tp_slices_never_exceed_the_entry(live):
    a = FakeExecutor("A", qty_multiplier=0.5)          # entry 3 → 2, tp 1 → 1 each, three TPs
    live.use(a)
    await signals.process({**ENTRY, "qty": 3, "tp1": 105.0, "tp2": 110.0, "tp3": 115.0, "sl": 90.0}, wh("bracket", default_qty=3, tp_qty=1))
    placed = a.of("place")
    assert placed[0]["qty"] == 2
    tps = [p["qty"] for p in placed if p["order_type"] == "Limit"]
    assert tps == [1, 1] and sum(tps) <= 2                 # the third TP is dropped
    assert [p["qty"] for p in placed if p["order_type"] == "Stop"] == [2]


def test_risk_rules_keep_the_pnl_poll_alive(admin):
    with context.use_area(1):
        s = {"pnl_poll_seconds": 0, "token_accounts": [{"accounts": [{"spec": "A", "risk": {"loss_limit": 100}}]}]}
        assert risk.any_active(s)
        # mirrors the loop's decision: off only when no rule exists
        assert not (float(s["pnl_poll_seconds"]) <= 0 and not risk.any_active(s))


async def test_orders_skip_the_pacing_queue_and_fail_fast_under_penalty(admin):
    sess = tradovate.TradovateSession(0, {"name": "L", "environment": "demo", "enabled": True, "access_token": "t",
                                          "account_spec": "A", "account_id": 1}, area_id=1)
    sent = []

    async def fake(method, path, **kw):
        sent.append(path)
        return {"orderId": 1}
    sess._request_raw = fake
    sess.penalty_until = time.monotonic() + 30
    t0 = time.monotonic()
    with pytest.raises(tradovate.RateLimited):
        await sess.place_order(symbol="MNQZ6", action="Buy", qty=1, order_type="Market")
    assert time.monotonic() - t0 < 0.5 and sent == []       # refused at once, nothing sent
    sess.penalty_until = 0
    sess._last_sent = time.monotonic()                        # a poll just went out …
    t0 = time.monotonic()
    await sess.cancel_order(5)                                # … the cancel does not wait for the spacing
    assert time.monotonic() - t0 < 0.15 and sent == ["/order/cancelorder"]


async def test_bare_root_skips_a_contract_past_its_roll_date(admin, monkeypatch):
    sess = tradovate.TradovateSession(0, {"name": "L", "environment": "demo", "enabled": True, "access_token": "t"}, area_id=1)

    async def fake(method, path, **kw):
        if path == "/contract/find":
            name = kw["params"]["name"]
            return {"name": "MNQH24" if name == "MNQ" else name}      # Tradovate still lists the expired March 2024
        if path == "/contract/suggest":
            return [{"name": "MNQH24"}, {"name": "MNQZ6"}, {"name": "MNQH7"}]
        raise AssertionError(path)
    sess._request_raw = fake
    assert await sess.resolve_contract("MNQ") == "MNQZ6"          # the expired month is skipped
    assert await sess.resolve_contract("MNQH24") == "MNQH24"      # an exact contract is taken as given


async def test_reconcile_skips_a_risk_locked_follower(admin):
    from app import copy as cp
    with context.use_area(1):
        risk._lock(1, "F1", "loss", "daily loss limit hit", -600)
    r = cp.GroupRunner(1, {**cp.new_group("G"), "leader": {"token_idx": 0, "spec": "LEAD", "account_id": 1},
                           "followers": [cp.normalize_follower({"token_idx": 1, "spec": "F1"})], "feed": "poll"})
    r.feed_ok = True
    r.leader_net[901], r.unit[901], r.contract_names[901] = 2, 2, "MNQZ6"
    called = []

    class Mgr:
        def all(self):
            class S:
                accounts = [{"id": 2, "spec": "F1"}]
                enabled, agent_id, name = True, 0, "S"

                def has_token(self):
                    return True

                async def _request(self, *a, **k):
                    called.append(a)
                    return []
            return [S(), S()]
    import app.copy as cpm
    orig = cpm.group_runner.tradovate.manager_for
    cpm.group_runner.tradovate.manager_for = lambda area_id: Mgr()
    try:
        assert await r.reconcile() == 0                            # locked follower: no correction attempted
        assert all(a[1] == "/position/list" for a in called)      # only the read, no order
    finally:
        cpm.group_runner.tradovate.manager_for = orig


async def test_cancel_fallback_leaves_unidentifiable_orders(admin):
    ex = FakeExecutor("A", working=[{"id": 1, "ordStatus": "Working"}, {"id": 2, "ordStatus": "Working"}])
    with context.use_area(1):
        n = await common._cancel_working(ex, "", contract="MNQZ6")
    assert n == 0 and not ex.of("cancel")
    with context.use_area(1):
        from app import state
        assert any("none cancelled" in e["message"] for e in state.recent_events())

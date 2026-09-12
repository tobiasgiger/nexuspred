"""Regression tests for the code-review fixes: broker rejections with HTTP 200,
stop placement after a live entry, closes that leave nothing behind, the risk
guard across the 17:00 New York roll, order pacing under a penalty."""
from __future__ import annotations

import asyncio
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app import alerts, config, context, risk, signals, state, tradovate
from app.engine import common
from tests.helpers import FakeExecutor
from tests.test_strategies import ENTRY, live, wh  # noqa: F401


# ------------------------------------------------------------ session results
def _session():
    sess = tradovate.TradovateSession(0, {"name": "L", "environment": "demo", "enabled": True, "access_token": "t",
                                          "account_spec": "A", "account_id": 1, "accounts": [{"spec": "A", "id": 1}]}, area_id=1)
    sess.answers: dict[str, object] = {}
    sent: list[str] = []

    async def fake(method, path, **kw):
        sent.append(path)
        if path == "/contract/find":
            return {"id": 901, "name": "MNQZ6"}
        return sess.answers.get(path, {"orderId": 5})
    sess._request_raw = fake
    return sess, sent


async def test_http_200_with_failure_reason_is_a_rejection(admin):
    sess, sent = _session()
    with context.use_area(1):
        sess.answers["/order/placeorder"] = {"failureReason": "MarginCheckFailed", "failureText": "Insufficient margin"}
        with pytest.raises(tradovate.TradovateError, match="Insufficient margin"):
            await sess.place_order(symbol="MNQZ6", action="Buy", qty=1, order_type="Market")
        assert state.recent_orders()[0]["status"] == "rejected"
        sess.answers["/order/placeorder"] = {}
        with pytest.raises(tradovate.TradovateError, match="no orderId"):
            await sess.place_order(symbol="MNQZ6", action="Buy", qty=1, order_type="Market")
        sess.answers["/order/cancelorder"] = {"failureReason": "UnknownOrder"}
        with pytest.raises(tradovate.TradovateError, match="UnknownOrder"):
            await sess.cancel_order(5)
        sess.answers["/order/modifyorder"] = {"failureText": "Order is filled"}
        with pytest.raises(tradovate.TradovateError, match="filled"):
            await sess.modify_order(5, qty=1, order_type="Stop", stop_price=1.0)
        sess.answers["/order/liquidateposition"] = {"failureReason": "AccountLocked"}
        with pytest.raises(tradovate.TradovateError, match="AccountLocked"):
            await sess.liquidate_position("MNQZ6")
        sess.answers["/order/placeoco"] = {"failureText": "Bad OCO"}
        with pytest.raises(tradovate.TradovateError, match="Bad OCO"):
            await sess.place_oco(symbol="MNQZ6", action="Sell", qty=1, order_type="Limit", price=2.0, stop_price=None,
                                 other={"action": "Sell", "order_type": "Stop", "stop_price": 1.0})
        # a clean answer still works
        sess.answers.clear()
        r = await sess.place_order(symbol="MNQZ6", action="Buy", qty=1, order_type="Market")
        assert r["status"] == "submitted" and r["order_id"] == 5


async def test_orders_are_spaced_and_wait_out_a_short_penalty(admin):
    sess, sent = _session()
    monkey_spacing = tradovate.PRIORITY_SPACING_S
    with context.use_area(1):
        t0 = time.monotonic()
        await asyncio.gather(*(sess.cancel_order(i) for i in range(4)))
        assert time.monotonic() - t0 >= 3 * monkey_spacing - 0.02      # a burst is spaced, not fired at once
        sess.penalty_until = time.monotonic() + 0.3                    # short penalty: waited out
        t0 = time.monotonic()
        await sess.cancel_order(9)
        assert time.monotonic() - t0 >= 0.25
        sess.penalty_until = time.monotonic() + 30                     # long penalty: refused at once
        with pytest.raises(tradovate.RateLimited):
            await sess.cancel_order(10)


async def test_health_check_ignores_a_throttle(admin, monkeypatch):
    sess, sent = _session()
    sess._token_expires = datetime.now(ZoneInfo("UTC")).replace(year=2099)
    fired = []
    monkeypatch.setattr(tradovate, "_fire", lambda coro: (fired.append(coro), coro.close()))
    with context.use_area(1):
        await sess._set_connected(True)
        async def limited(method, path, **kw):
            raise tradovate.RateLimited(path, "", 5.0)
        sess._request_raw = limited
        await sess.health_check()
        assert state.session_status("L")["connected"] is True and "rate limited" in state.session_status("L")["last_error"]


def test_rediscovery_never_persists_an_empty_list_and_keeps_toggles(admin):
    sess = tradovate.TradovateSession(0, {"name": "L", "environment": "demo", "enabled": True, "access_token": "t",
                                          "accounts": [{"spec": "A1", "id": 1, "enabled": False}]}, area_id=1)
    sess._merge_accounts([{"name": "A1", "id": 1}, {"name": "A2", "id": 2}])
    by = {a["spec"]: a for a in sess.accounts}
    assert by["A1"]["enabled"] is False and by["A2"]["enabled"] is True


async def test_token_renew_failure_keeps_a_still_valid_token(admin):
    sess, sent = _session()
    from datetime import timedelta, timezone
    sess._token_expires = datetime.now(timezone.utc) + timedelta(minutes=3)     # inside the 5-minute renew buffer

    async def failing_renew():
        raise tradovate.TradovateError("renew failed: boom")
    sess._renew = failing_renew
    assert await sess._get_token() == "t"


# --------------------------------------------------------------- strategies
class Flaky(FakeExecutor):
    """The stop fails ``fail_stops`` times before it works."""
    def __init__(self, name, fail_stops=1, **kw):
        super().__init__(name, **kw)
        self.fail_stops = fail_stops

    async def place_order(self, **kw):
        if kw.get("order_type") == "Stop" and self.fail_stops > 0:
            self.fail_stops -= 1
            raise tradovate.TradovateError("stop rejected")
        return await super().place_order(**kw)


@pytest.fixture
def problems(monkeypatch):
    calls = []

    async def rec(title, message):
        calls.append((title, message))
    monkeypatch.setattr(alerts, "execution_problem", rec)
    monkeypatch.setattr(tradovate, "_fire", lambda coro: asyncio.get_event_loop().create_task(coro))
    return calls


async def test_bracket_retries_the_stop_once(live, problems):
    a = Flaky("A", fail_stops=1)
    live.use(a)
    r = await signals.process({**ENTRY, "sl": 90.0}, wh("bracket"))
    assert [p["order_type"] for p in a.of("place")][-1] == "Stop" and r["accounts"]
    await asyncio.sleep(0)
    assert problems == []


async def test_bracket_closes_the_entry_again_when_the_stop_fails_twice(live, problems):
    a = Flaky("A", fail_stops=2)
    live.use(a)
    r = await signals.process({**ENTRY, "sl": 90.0, "tp1": 105.0}, wh("bracket", id="wh_naked"))
    await asyncio.sleep(0.01)
    assert r["accounts"] == [] and "wh_naked:MNQ" not in signals._active[1]        # not tracked: nothing of it is left
    assert problems and "Entry closed again" in problems[0][0]
    tp = [p for p in a.of("place") if p["order_type"] == "Limit"][0]
    assert a.of("cancel") == [{"order_id": tp["order_id"]}]                         # only the trade's own target
    assert a.of("place")[-1]["order_type"] == "Market" and a.of("place")[-1]["action"] == "Buy"        # ENTRY is a sell
    assert any("closed again at market" in e["message"] for e in state.recent_events())


async def test_ts_hunter_closes_the_entry_again_when_the_stop_fails(live, problems):
    a = Flaky("A", fail_stops=2)
    live.use(a)
    from tests.test_strategies import ts
    r = await signals.process({"event": "signal", "trade_id": "t9", "symbol": "MNQ1!", "side": "buy",
                               "risk": {"value": 100}, "sl": {"value": 95.0}, "tv": {"entry_price": 100.0}}, ts())
    await asyncio.sleep(0.01)
    assert r["status"] == "ok" and r["accounts"] == [] and "t9" not in signals._active[1] and problems
    assert [(p["action"], p["qty"], p["order_type"]) for p in a.of("place")] == [("Buy", 100, "Market"), ("Sell", 100, "Market")]


async def test_position_stays_and_is_alerted_when_the_close_fails_too(live, problems):
    class Stuck(Flaky):
        async def place_order(self, **kw):
            if kw.get("order_type") == "Market" and any(p["order_type"] == "Market" for p in self.of("place")):
                raise tradovate.TradovateError("gateway down")                       # the entry went through, the close does not
            return await super().place_order(**kw)
    a = Stuck("A", fail_stops=2)
    live.use(a)
    r = await signals.process({**ENTRY, "sl": 90.0}, wh("bracket", id="wh_stuck"))
    await asyncio.sleep(0.01)
    assert r["accounts"] and signals._active[1]["wh_stuck:MNQ"]["accounts"]["A"]["sl_order_id"] is None   # tracked, so close_all reaches it
    assert problems and "Unprotected position" in problems[0][0]


async def test_limit_entry_that_never_filled_is_cancelled_not_reversed(live, problems):
    """A resting limit entry whose stop fails is cancelled; a market close would
    open the opposite position. A partial fill is closed for the filled part."""
    config.save_settings({"entry_order_type": "Limit"})
    a = Flaky("A", fail_stops=2, positions=[])                                   # nothing filled
    live.use(a)
    r = await signals.process({**ENTRY, "sl": 90.0, "tp1": 105.0}, wh("bracket", id="wh_lim"))
    await asyncio.sleep(0.01)
    entry = a.of("place")[0]
    assert entry["order_type"] == "Limit" and r["accounts"] == [] and "wh_lim:MNQ" not in signals._active[1]
    assert {c["order_id"] for c in a.of("cancel")} == {entry["order_id"], a.of("place")[1]["order_id"]}   # entry + target
    assert not [p for p in a.of("place") if p["order_type"] == "Market"] and "Entry cancelled" in problems[0][0]
    b = Flaky("B", fail_stops=2, positions=[{"symbol": "MNQU6", "netPos": -1}])  # 1 of 2 lots filled (a sell entry)
    live.use(b)
    await signals.process({**ENTRY, "qty": 2, "sl": 90.0}, wh("bracket", id="wh_lim2"))
    await asyncio.sleep(0.01)
    closes = [p for p in b.of("place") if p["order_type"] == "Market"]
    assert [(p["action"], p["qty"]) for p in closes] == [("Buy", 1)]


async def test_unknown_stop_outcome_cancels_the_contracts_orders_before_the_close(live, problems):
    class Unknown(FakeExecutor):
        async def place_order(self, **kw):
            if kw.get("order_type") == "Stop":
                raise tradovate.OrderOutcomeUnknown("timeout after send")
            return await super().place_order(**kw)
    a = Unknown("A", working=[{"id": 41, "symbol": "MNQU6"}, {"id": 42, "symbol": "ESU6"}])
    live.use(a)
    await signals.process({**ENTRY, "sl": 90.0}, wh("bracket", id="wh_unk"))
    await asyncio.sleep(0.01)
    assert [c["order_id"] for c in a.of("cancel")] == [41]                           # every MNQ order (the stop may be working), ES untouched
    assert a.of("place")[-1]["order_type"] == "Market"


async def test_bracket_retires_the_stop_after_the_last_target(live):
    a = FakeExecutor("A")
    live.use(a)
    w = wh("bracket", default_qty=2, tp_qty=1, id="wh_retire")
    await signals.process({**ENTRY, "qty": 2, "tp1": 97.0, "tp2": 94.0, "sl": 110.0}, w)
    sl_id = [p for p in a.of("place") if p["order_type"] == "Stop"][0]["order_id"]
    await signals.process({"action": "move_sl", "symbol": "MNQ1!", "event": "tp1_hit", "new_sl": 100.0}, w)
    assert a.of("modify")[-1]["qty"] == 1
    await signals.process({"action": "move_sl", "symbol": "MNQ1!", "event": "tp2_hit", "new_sl": 96.0}, w)
    assert a.of("cancel")[-1] == {"order_id": sl_id}                 # flat: the stop is retired, not resized to 1


class CancelFails(FakeExecutor):
    def __init__(self, name, fails=1, **kw):
        super().__init__(name, **kw)
        self.fails = fails

    async def cancel_order(self, order_id):
        if self.fails > 0:
            self.fails -= 1
            raise tradovate.TradovateError("cancel refused")
        return await super().cancel_order(order_id)


async def test_close_all_retries_failed_cancels_after_the_liquidation(live, problems):
    a = CancelFails("A", fails=1, working=[{"id": 5, "symbol": "MNQU6"}])
    live.use(a)
    w = wh(id="wh_close")
    await signals.process({"action": "buy", "symbol": "MNQ1!", "qty": 1}, w)
    r = await signals.process({"action": "close_all", "symbol": "MNQ1!"}, w)
    assert a.of("liquidate") and r["cancelled"] == 1 and problems == []
    a2 = CancelFails("B", fails=5, working=[{"id": 6, "symbol": "MNQU6"}])
    live.use(a2)
    await signals.process({"action": "buy", "symbol": "MNQ1!", "qty": 1}, w)
    await signals.process({"action": "close_all", "symbol": "MNQ1!"}, w)
    await asyncio.sleep(0.01)
    assert problems and "Orders left working" in problems[-1][0]


# ---------------------------------------------------------------- risk guard
from tests.test_risk import Sess as RiskSess, _snap  # noqa: E402


async def test_risk_rule_does_not_refire_across_the_roll(admin, monkeypatch):
    sent = []

    async def rec(*a, **k):
        sent.append(a)
    monkeypatch.setattr(alerts, "risk_triggered", rec)
    sess = RiskSess()
    sess.accounts[0]["risk"] = {"loss_limit": 0, "profit_limit": 0, "flatten_at": "15:55", "flatten_tz": "local"}
    fixed = datetime(2026, 9, 10, 15, 55, tzinfo=ZoneInfo("Europe/Zurich"))
    monkeypatch.setattr(risk, "local_now", lambda area_id, settings=None: fixed)
    monkeypatch.setattr(risk, "trading_day", lambda now=None: "2026-09-10")
    assert (await risk.check_area(1, [sess], [_snap(5)]))[0]["kind"] == "time"
    # 23:00:05 Zurich = 17:00:05 New York: the trading day rolls, the lock expires …
    fixed = datetime(2026, 9, 10, 23, 0, 5, tzinfo=ZoneInfo("Europe/Zurich"))
    monkeypatch.setattr(risk, "local_now", lambda area_id, settings=None: fixed)
    monkeypatch.setattr(risk, "trading_day", lambda now=None: "2026-09-11")
    assert risk.is_locked(1, "DEMO11") is None
    sess.pos = [{"accountId": 11, "contractId": 901, "netPos": 1}]           # the user's evening trade
    fired = await risk.check_area(1, [sess], [_snap(5, 12.0)])
    assert fired == [] and sess.pos and len(sent) == 1                      # … but 15:55 does not fire again today
    # the next clock day it does
    fixed = datetime(2026, 9, 11, 15, 55, tzinfo=ZoneInfo("Europe/Zurich"))
    monkeypatch.setattr(risk, "local_now", lambda area_id, settings=None: fixed)
    assert (await risk.check_area(1, [sess], [_snap(5)]))[0]["kind"] == "time"


async def test_loss_rule_does_not_refire_on_the_same_realised_figure(admin, monkeypatch):
    monkeypatch.setattr(alerts, "risk_triggered", _noop)
    sess = RiskSess()                                                        # loss_limit 500
    monkeypatch.setattr(risk, "trading_day", lambda now=None: "2026-09-10")
    assert (await risk.check_area(1, [sess], [_snap(-600)]))[0]["kind"] == "loss"
    monkeypatch.setattr(risk, "trading_day", lambda now=None: "2026-09-11")   # roll; the cached snapshot still says -600
    assert await risk.check_area(1, [sess], [_snap(-600)]) == []
    assert (await risk.check_area(1, [sess], [_snap(-650)]))[0]["kind"] == "loss"   # a genuinely new loss fires


async def test_unlock_sticks_until_the_state_changes(admin, monkeypatch):
    monkeypatch.setattr(alerts, "risk_triggered", _noop)
    sess = RiskSess()
    assert (await risk.check_area(1, [sess], [_snap(-600)]))[0]["kind"] == "loss"
    assert risk.unlock(1, "DEMO11") and risk.is_locked(1, "DEMO11") is None
    assert await risk.check_area(1, [sess], [_snap(-600)]) == []              # not re-locked on the same figure
    assert risk.is_locked(1, "DEMO11") is None
    assert (await risk.check_area(1, [sess], [_snap(-700)]))[0]["kind"] == "loss"


async def test_lock_is_written_before_the_flatten(admin, monkeypatch):
    monkeypatch.setattr(alerts, "risk_triggered", _noop)
    sess = RiskSess()
    seen = []
    orig = risk.flatten_account

    async def spy(s, a):
        seen.append(risk.is_locked(1, "DEMO11"))
        return await orig(s, a)
    monkeypatch.setattr(risk, "flatten_account", spy)
    await risk.check_area(1, [sess], [_snap(-600)])
    assert seen and seen[0]                                                  # locked while flattening


async def test_locked_account_reflatten_uses_the_position_list(admin, monkeypatch):
    monkeypatch.setattr(alerts, "risk_triggered", _noop)
    sess = RiskSess()
    await risk.check_area(1, [sess], [_snap(-600)])
    sess.pos = [{"accountId": 11, "contractId": 901, "netPos": 1}]
    risk._reflatten_at.clear()
    await risk.check_area(1, [sess], [_snap(-600, 0.0)], positions={"L1": list(sess.pos)})   # open P&L exactly 0.00
    assert sess.pos == []


async def _noop(*a, **k):
    return None

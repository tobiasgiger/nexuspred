"""Characterisation of the signal engine (simple / bracket / TS-Hunter).

Live mode swaps the broker for ``FakeExecutor``s that record every call, so the
exact order flow — quantities, sides, order types, stop resizing, tracking — is
pinned down. Simulate mode exercises the real in-memory ``sim_client`` path.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import config, context, signals, state
from app.signals import SignalError
from tests.helpers import FakeExecutor, settle

FIXTURES = Path(__file__).parent / "fixtures"
TS_LIFECYCLE = json.loads((FIXTURES / "ts_hunter_lifecycle.json").read_text(encoding="utf-8"))


@pytest.fixture
def live(monkeypatch, admin):
    """Live (non-simulate) mode: trading on, fake executors, alerts captured."""
    config.save_settings({"trading_enabled": True})
    box: dict = {"execs": []}
    monkeypatch.setattr(signals, "_webhook_executors", lambda wh: list(box["execs"]))
    sent: list = []

    async def fake_trade_executed(*args, **kw):
        sent.append(args)

    monkeypatch.setattr(signals.alerts, "trade_executed", fake_trade_executed)

    def use(*fakes):
        box["execs"] = list(fakes)

    return SimpleNamespace(use=use, alerts=sent)


def wh(strategy="simple", **kw):
    w = config.new_webhook(name=f"{strategy}-wh", strategy=strategy,
                           default_qty=kw.pop("default_qty", 1), tp_qty=kw.pop("tp_qty", 1))
    w["id"] = kw.pop("id", w["id"])
    w.update(kw)
    return w


def active(key=None, simulate=False):
    trades = signals.active_trades(simulate=simulate)
    return trades if key is None else trades[key]


# ============================================================ generic guards
async def test_missing_action_or_symbol_raises(admin):
    with pytest.raises(SignalError):
        await signals.process({"symbol": "MNQ1!"}, wh(), simulate=True)
    with pytest.raises(SignalError):
        await signals.process({"action": "buy"}, wh(), simulate=True)


async def test_unmapped_symbol_rejected(admin):
    with pytest.raises(SignalError, match="not mapped"):
        await signals.process({"action": "buy", "symbol": "ZZZ1!"}, wh(), simulate=True)


async def test_unknown_action_raises(admin):
    with pytest.raises(SignalError, match="Unknown action"):
        await signals.process({"action": "dance", "symbol": "MNQ1!"}, wh(), simulate=True)


async def test_no_webhook_without_simulate_raises(admin):
    with pytest.raises(SignalError, match="No webhook context"):
        await signals.process({"action": "buy", "symbol": "MNQ1!"})


async def test_trading_disabled_is_skipped_in_live_mode(live):
    config.save_settings({"trading_enabled": False})
    live.use(FakeExecutor("A"))
    r = await signals.process({"action": "buy", "symbol": "MNQ1!"}, wh())
    assert r == {"status": "skipped", "reason": "trading_disabled", "action": "buy"}


async def test_no_executors_is_skipped(live):
    live.use()
    r = await signals.process({"action": "buy", "symbol": "MNQ1!"}, wh())
    assert r == {"status": "skipped", "reason": "no_enabled_accounts", "action": "buy"}


async def test_passphrase_enforced_live_only(live):
    config.save_settings({"webhook_passphrase": "pp"})
    live.use(FakeExecutor("A"))
    with pytest.raises(SignalError, match="Invalid passphrase"):
        await signals.process({"action": "buy", "symbol": "MNQ1!"}, wh())
    ok = await signals.process({"action": "buy", "symbol": "MNQ1!", "passphrase": "pp"}, wh())
    assert ok["status"] == "ok"
    sim = await signals.process({"action": "buy", "symbol": "MNQ1!"}, wh(), simulate=True)
    assert sim["status"] == "ok"  # simulate skips the passphrase guard


# ================================================================== simple
async def test_simple_qty_and_multiplier_rounding(live):
    a, b = FakeExecutor("A"), FakeExecutor("B", qty_multiplier=2.5)
    live.use(a, b)
    w = wh("simple", id="wh_s")
    r = await signals.process({"action": "buy", "symbol": "MNQ1!", "qty": 2}, w)

    assert r["status"] == "ok" and r["simulated"] is False
    assert r["contract"] == "MNQU6"  # symbol_map: MNQ1! -> MNQU6
    assert r["accounts"] == [{"account": "A", "qty": 2}, {"account": "B", "qty": 5}]
    pa, pb = a.of("place")[0], b.of("place")[0]
    assert (pa["symbol"], pa["action"], pa["qty"], pa["order_type"], pa["price"]) == \
        ("MNQU6", "Buy", 2, "Market", None)
    assert pb["qty"] == 5

    t = active("wh_s:MNQ")
    assert t["side"] == "buy" and t["root"] == "MNQ" and t["webhook_id"] == "wh_s"
    assert t["accounts"]["A"] == {"name": "A", "contract": "MNQU6", "qty": 2, "entry_qty": 2,
                                  "sl_order_id": None, "tp_order_ids": []}
    await settle()                        # trade alerts are sent off the request path
    assert live.alerts == [("simple-wh", "buy", "MNQU6", ["A", "B"])]


async def test_simple_rounds_half_up_for_multiplier(live):
    live.use(FakeExecutor("A", qty_multiplier=2.5))
    r = await signals.process({"action": "sell", "symbol": "MNQ1!", "qty": 1}, wh())
    assert r["accounts"][0]["qty"] == 3  # 2.5 → 3 (half up, the copy-trading rule)


async def test_simple_contracts_key_and_webhook_default(live):
    a = FakeExecutor("A")
    live.use(a)
    await signals.process({"action": "sell", "symbol": "MNQ1!", "contracts": 3}, wh())
    assert a.of("place")[-1]["qty"] == 3 and a.of("place")[-1]["action"] == "Sell"
    await signals.process({"action": "sell", "symbol": "MNQ1!"}, wh(default_qty=4))
    assert a.of("place")[-1]["qty"] == 4


async def test_simple_invalid_qty_raises(live):
    live.use(FakeExecutor("A"))
    with pytest.raises(SignalError, match="Invalid qty"):
        await signals.process({"action": "buy", "symbol": "MNQ1!", "qty": "abc"}, wh())
    with pytest.raises(SignalError, match="positive"):
        await signals.process({"action": "buy", "symbol": "MNQ1!", "qty": 0}, wh())


async def test_simple_limit_entry_passes_price(live):
    config.save_settings({"entry_order_type": "Limit"})
    a = FakeExecutor("A")
    live.use(a)
    await signals.process({"action": "buy", "symbol": "MNQ1!", "qty": 1, "entry": 100.5}, wh())
    p = a.of("place")[0]
    assert p["order_type"] == "Limit" and p["price"] == 100.5


async def test_simple_move_sl_is_skipped_not_error(live):
    live.use(FakeExecutor("A"))
    r = await signals.process({"action": "move_sl", "symbol": "MNQ1!", "new_sl": 1}, wh())
    assert r == {"status": "skipped", "reason": "move_sl_unsupported_simple", "action": "move_sl"}


async def test_simple_trail_active_is_acknowledged(live):
    live.use(FakeExecutor("A"))
    r = await signals.process({"action": "trail_active", "symbol": "MNQ1!"}, wh())
    assert r == {"status": "ok", "action": "trail_active", "note": "acknowledged", "simulated": False}


async def test_entry_failure_on_one_account_is_isolated(live):
    a, b = FakeExecutor("A"), FakeExecutor("B", fail_place=True)
    live.use(a, b)
    r = await signals.process({"action": "buy", "symbol": "MNQ1!", "qty": 1}, wh(id="wh_x"))
    assert r["status"] == "ok" and r["accounts"] == [{"account": "A", "qty": 1}]
    assert list(active("wh_x:MNQ")["accounts"]) == ["A"]
    await settle()                        # trade alerts are sent off the request path
    assert live.alerts[-1][3] == ["A"]


async def test_all_accounts_failing_tracks_nothing_and_alerts_nothing(live):
    live.use(FakeExecutor("A", fail_place=True))
    r = await signals.process({"action": "buy", "symbol": "MNQ1!", "qty": 1}, wh(id="wh_x"))
    assert r["status"] == "ok" and r["accounts"] == [] and r["orders"] == []
    await settle()                        # trade alerts are sent off the request path
    assert "wh_x:MNQ" not in active() and live.alerts == []


async def test_simple_close_all_cancels_liquidates_and_untracks(live):
    a = FakeExecutor("A", working=[{"id": 5, "symbol": "MNQU6"}, {"id": 6, "symbol": "MNQU6"}])
    live.use(a)
    w = wh(id="wh_c")
    await signals.process({"action": "buy", "symbol": "MNQ1!", "qty": 1}, w)
    r = await signals.process({"action": "close_all", "symbol": "MNQ1!"}, w)
    assert r == {"status": "ok", "action": "close_all", "accounts": 1, "cancelled": 2, "failed": [], "simulated": False}
    assert [c["order_id"] for c in a.of("cancel")] == [5, 6]
    assert a.of("liquidate") == [{"symbol": "MNQU6"}]
    assert "wh_c:MNQ" not in active()


# ================================================================= bracket
ENTRY = {"event": "entry", "action": "sell", "symbol": "MNQ1!", "entry": 100.0,
         "sl": 110.0, "tp1": 97.0, "tp2": 94.0, "tp3": 91.0}


async def test_bracket_entry_places_entry_tps_and_stop(live):
    a = FakeExecutor("A")
    live.use(a)
    w = wh("bracket", default_qty=3, tp_qty=1, id="wh_b")
    r = await signals.process(ENTRY, w)

    places = a.of("place")
    assert len(places) == 5 and len(r["orders"]) == 5
    entry, tp1, tp2, tp3, sl = places
    assert (entry["action"], entry["qty"], entry["order_type"], entry["price"]) == ("Sell", 3, "Market", 100.0)
    assert [(t["action"], t["qty"], t["order_type"], t["price"]) for t in (tp1, tp2, tp3)] == \
        [("Buy", 1, "Limit", 97.0), ("Buy", 1, "Limit", 94.0), ("Buy", 1, "Limit", 91.0)]
    assert (sl["action"], sl["qty"], sl["order_type"], sl["stop_price"]) == ("Buy", 3, "Stop", 110.0)

    info = active("wh_b:MNQ")["accounts"]["A"]
    assert info["entry_qty"] == 3 and info["tp_qty"] == 1 and info["qty"] == 3
    assert info["entry_price"] == 100.0 and info["sl_stop"] == 110.0 and info["sl_type"] == "Stop"
    assert info["sl_order_id"] == sl["order_id"] and info["tp_order_ids"] == [tp1["order_id"], tp2["order_id"], tp3["order_id"]]
    assert r["accounts"] == [{"account": "A", "qty": 3}]
    await settle()                        # trade alerts are sent off the request path
    assert live.alerts == [("bracket-wh", "sell", "MNQU6", ["A"])]


async def test_bracket_qty_from_payload_rounds_and_falls_back(live):
    a = FakeExecutor("A", qty_multiplier=1.5)
    live.use(a)
    w = wh("bracket", default_qty=3)
    await signals.process({**ENTRY, "qty": 2.9}, w)
    assert a.of("place")[0]["qty"] == 3          # int(2.9)=2 -> 2*1.5=3
    await signals.process({**ENTRY, "qty": "abc"}, w)
    assert a.of("place")[-5]["qty"] == 5         # invalid -> default 3 -> 3*1.5=4.5 -> 5 (half up, like copy trading)
    await signals.process({**ENTRY, "contracts": 0}, w)
    assert a.of("place")[-5]["qty"] == 5         # <=0 -> default


async def test_bracket_and_simple_round_half_up_alike(live):
    a = FakeExecutor("A", qty_multiplier=1.5)
    live.use(a)
    await signals.process({**ENTRY, "qty": 1}, wh("bracket"))
    assert a.of("place")[0]["qty"] == 2          # 1.5 -> 2
    await signals.process({"action": "sell", "symbol": "MNQ1!", "qty": 1}, wh("simple"))
    assert a.of("place")[-1]["qty"] == 2


async def test_bracket_entry_omits_missing_tps(live):
    a = FakeExecutor("A")
    live.use(a)
    await signals.process({"action": "buy", "symbol": "MNQ1!", "sl": 90.0, "tp1": 105.0}, wh("bracket", default_qty=2))
    assert [p["order_type"] for p in a.of("place")] == ["Market", "Limit", "Stop"]
    assert [p["action"] for p in a.of("place")] == ["Buy", "Sell", "Sell"]


async def test_bracket_partial_bracket_failure_is_logged_not_fatal(live, monkeypatch):
    a = FakeExecutor("A")
    calls = {"n": 0}
    orig = a.place_order

    async def flaky(**kw):
        calls["n"] += 1
        if kw.get("order_type") == "Stop":
            raise signals.TradovateError("stop rejected")
        return await orig(**kw)

    a.place_order = flaky
    live.use(a)
    r = await signals.process(ENTRY, wh("bracket", default_qty=3, id="wh_f"))
    # policy: an entry whose stop cannot be placed is closed again — the targets
    # are cancelled, the 3 lots are sold at market, the account is not tracked
    assert r["status"] == "ok" and r["orders"] == [] and r["accounts"] == []
    assert "wh_f:MNQ" not in active()
    tps = [p["order_id"] for p in a.of("place") if p["order_type"] == "Limit"]
    assert len(tps) == 3 and [c["order_id"] for c in a.of("cancel")] == tps
    assert a.of("place")[-1]["order_type"] == "Market" and a.of("place")[-1]["action"] == "Buy" and a.of("place")[-1]["qty"] == 3   # ENTRY is a sell
    assert any("closed again at market" in e["message"] for e in state.recent_events())


async def test_move_sl_at_tp1_goes_to_entry_price_and_resizes(live):
    a = FakeExecutor("A")
    live.use(a)
    w = wh("bracket", default_qty=3, tp_qty=1, id="wh_m")
    await signals.process(ENTRY, w)
    sl_id = active("wh_m:MNQ")["accounts"]["A"]["sl_order_id"]

    r = await signals.process({"event": "tp1_hit", "action": "move_sl", "symbol": "MNQ1!", "new_sl": 95.0}, w)
    m = a.of("modify")[-1]
    assert m == {"order_id": sl_id, "qty": 2, "order_type": "Stop", "stop_price": 100.0}
    assert r == {"status": "ok", "action": "move_sl", "new_sl": 100.0,
                 "breakeven_to_entry": True, "accounts": 1, "simulated": False}
    info = active("wh_m:MNQ")["accounts"]["A"]
    assert info["qty"] == 2 and info["sl_stop"] == 100.0


async def test_move_sl_breakeven_setting_off_uses_new_sl(live):
    config.save_settings({"breakeven_to_entry": False})
    a = FakeExecutor("A")
    live.use(a)
    w = wh("bracket", default_qty=3)
    await signals.process(ENTRY, w)
    r = await signals.process({"event": "tp1_hit", "action": "move_sl", "symbol": "MNQ1!", "new_sl": 95.0}, w)
    assert a.of("modify")[-1]["stop_price"] == 95.0 and r["breakeven_to_entry"] is False


async def test_move_sl_trailing_uses_new_sl_and_event_qty(live):
    a = FakeExecutor("A")
    live.use(a)
    w = wh("bracket", default_qty=3)
    await signals.process(ENTRY, w)
    await signals.process({"event": "tp2_hit", "action": "move_sl", "symbol": "MNQ1!", "new_sl": 97.0}, w)
    assert a.of("modify")[-1]["qty"] == 1 and a.of("modify")[-1]["stop_price"] == 97.0


async def test_move_sl_breakeven_message_without_tp_index_keeps_qty(live):
    a = FakeExecutor("A")
    live.use(a)
    w = wh("bracket", default_qty=3)
    await signals.process(ENTRY, w)
    await signals.process({"action": "move_sl", "symbol": "MNQ1!", "message": "SL to breakeven"}, w)
    assert a.of("modify")[-1] == {"order_id": a.of("place")[4]["order_id"], "qty": 3,
                                  "order_type": "Stop", "stop_price": 100.0}


async def test_move_sl_without_tracked_trade_is_skipped(live):
    live.use(FakeExecutor("A"))
    r = await signals.process({"action": "move_sl", "symbol": "MNQ1!", "new_sl": 1}, wh("bracket"))
    assert r == {"status": "skipped", "reason": "no_active_stop", "action": "move_sl"}


async def test_move_sl_missing_new_sl_raises_when_not_breakeven(live):
    a = FakeExecutor("A")
    live.use(a)
    w = wh("bracket", default_qty=3)
    await signals.process(ENTRY, w)
    with pytest.raises(SignalError, match="missing 'new_sl'"):
        await signals.process({"event": "tp2_hit", "action": "move_sl", "symbol": "MNQ1!"}, w)


async def test_trail_active_resizes_stop_price_unchanged(live):
    a = FakeExecutor("A")
    live.use(a)
    w = wh("bracket", default_qty=3)
    await signals.process(ENTRY, w)
    r = await signals.process({"event": "tp2_hit", "action": "trail_active", "symbol": "MNQ1!"}, w)
    assert a.of("modify")[-1]["qty"] == 1 and a.of("modify")[-1]["stop_price"] == 110.0
    assert r == {"status": "ok", "action": "trail_active", "accounts": 1, "simulated": False}
    r2 = await signals.process({"action": "trail_active", "symbol": "MNQ1!"}, w)  # no tp index
    assert r2["note"] == "acknowledged"


async def test_bracket_close_all_pops_tracking(live):
    a = FakeExecutor("A", working=[{"id": 1, "symbol": "MNQU6"}])
    live.use(a)
    w = wh("bracket", id="wh_z")
    await signals.process(ENTRY, w)
    r = await signals.process({"action": "close_all", "symbol": "MNQ1!"}, w)
    assert r["cancelled"] == 1 and "wh_z:MNQ" not in active()


# ---------------------------------------------------------------- set_sl_tp
async def test_set_sl_tp_places_stop_and_target_from_live_position(live):
    a = FakeExecutor("A", positions=[{"symbol": "MNQU6", "netPos": 2}])
    live.use(a)
    w = wh("simple", id="wh_st")
    r = await signals.process({"action": "set_sl_tp", "symbol": "MNQ1!", "stop_price": 90.0, "target_price": 120.0}, w)

    stop, tgt = a.of("place")
    assert (stop["action"], stop["qty"], stop["order_type"], stop["stop_price"]) == ("Sell", 2, "Stop", 90.0)
    assert (tgt["action"], tgt["qty"], tgt["order_type"], tgt["price"]) == ("Sell", 2, "Limit", 120.0)
    assert r == {"status": "ok", "action": "set_sl_tp", "accounts": 1, "sl": 90.0, "tp": 120.0, "simulated": False}
    info = active("wh_st:MNQ")["accounts"]["A"]
    assert info["sl_order_id"] == stop["order_id"] and info["tp_order_ids"] == [tgt["order_id"]]

    # A repeated move cancels the previous orders and places fresh ones.
    await signals.process({"action": "set_sl_tp", "symbol": "MNQ1!", "new_sl": 95.0}, w)
    assert [c["order_id"] for c in a.of("cancel")] == [stop["order_id"]]
    assert a.of("place")[-1]["stop_price"] == 95.0


async def test_set_sl_tp_short_position_exits_with_buy(live):
    a = FakeExecutor("A", positions=[{"symbol": "MNQU6", "netPos": -3}])
    live.use(a)
    await signals.process({"action": "set_sl_tp", "symbol": "MNQ1!", "tp": 80.0}, wh())
    assert a.of("place") == [a.of("place")[0]] and a.of("place")[0]["action"] == "Buy"
    assert a.of("place")[0]["qty"] == 3


async def test_set_sl_tp_skips_without_position_or_prices(live):
    live.use(FakeExecutor("A"))
    r = await signals.process({"action": "set_sl_tp", "symbol": "MNQ1!", "stop_price": 1}, wh())
    assert r == {"status": "skipped", "reason": "no_open_position", "action": "set_sl_tp"}
    r2 = await signals.process({"action": "set_sl_tp", "symbol": "MNQ1!"}, wh())
    assert r2 == {"status": "skipped", "reason": "no_sl_or_tp", "action": "set_sl_tp"}


# --------------------------------------------------------------- lock keys
async def test_trade_lock_keys_follow_area_mode_webhook_root(live):
    live.use(FakeExecutor("A"))
    await signals.process({"action": "buy", "symbol": "MNQ1!"}, wh(id="wh_k"))
    await signals.process({"action": "buy", "symbol": "MNQ1!"}, wh(id="wh_k"), simulate=True)
    assert "1:live:wh_k:MNQ" in signals._trade_locks
    assert "1:sim:wh_k:MNQ" in signals._trade_locks


async def test_active_trades_isolated_per_area(live):
    live.use(FakeExecutor("A"))
    await signals.process({"action": "buy", "symbol": "MNQ1!"}, wh(id="wh_a"))
    with context.use_area(2):
        assert signals.active_trades() == {}
    assert "wh_a:MNQ" in signals.active_trades()


# ================================================================ TS-Hunter
def ts(strategy="ts_hunter", **kw):
    return wh(strategy, **kw)


async def test_ts_hunter_full_lifecycle_from_real_payloads(live):
    a, b = FakeExecutor("A"), FakeExecutor("B", qty_multiplier=2)
    live.use(a, b)
    w = ts(id="wh_ts")
    entry, tp1, tp2, tp3, full = TS_LIFECYCLE
    tid = entry["trade_id"]

    r = await signals.process(entry, w)
    assert r["status"] == "ok" and r["action"] == "signal" and r["trade_id"] == tid
    assert r["contract"] == "MNQ" and r["accounts"] == [{"account": "A", "qty": 4}, {"account": "B", "qty": 8}]
    ea, sa = a.of("place")
    assert (ea["action"], ea["qty"], ea["order_type"]) == ("Sell", 4, "Market") and "price" not in ea
    assert (sa["action"], sa["qty"], sa["order_type"], sa["stop_price"]) == ("Buy", 4, "Stop", 29658.5)
    assert b.of("place")[0]["qty"] == 8 and b.of("place")[1]["qty"] == 8
    t = active(tid)
    assert t["side"] == "sell" and t["root"] == "MNQ" and t["trade_id"] == tid
    assert t["accounts"]["A"]["remaining_qty"] == 4 and t["accounts"]["A"]["entry_price"] == 29329.0
    await settle()                        # trade alerts are sent off the request path
    assert live.alerts == [("ts_hunter-wh", "sell", "MNQ", ["A", "B"])]

    for payload, rem_a, rem_b, closed_a, closed_b in ((tp1, 3, 6, 1, 2), (tp2, 2, 4, 1, 2), (tp3, 1, 2, 1, 2)):
        r = await signals.process(payload, w)
        assert r["status"] == "ok" and r["action"] == "partial_close_percent"
        assert r["lifecycle_stage"] == payload["lifecycle_stage"] and r["accounts"] == ["A", "B"]
        ca, cb = a.of("place")[-1], b.of("place")[-1]
        assert (ca["action"], ca["qty"], ca["order_type"]) == ("Buy", closed_a, "Market")
        assert cb["qty"] == closed_b
        ma, mb = a.of("modify")[-1], b.of("modify")[-1]
        assert ma == {"order_id": sa["order_id"], "qty": rem_a, "order_type": "Stop", "stop_price": 29658.5}
        assert mb["qty"] == rem_b and mb["stop_price"] == 29658.5
        assert active(tid)["accounts"]["A"]["remaining_qty"] == rem_a
        assert active(tid)["accounts"]["B"]["remaining_qty"] == rem_b

    r = await signals.process(full, w)
    assert r == {"status": "ok", "action": "full_close", "trade_id": tid, "accounts": 2,
                 "cancelled": 2, "failed": [], "untracked": [], "simulated": False}
    # isolated: the trade's own stop is cancelled and its remaining 1 / 2 lots are
    # bought back at market — nothing is liquidated, other trades in MNQ would survive
    assert a.of("liquidate") == [] and b.of("liquidate") == []
    assert a.of("cancel")[-1] == {"order_id": sa["order_id"]} and b.of("cancel")[-1] == {"order_id": [p for p in b.of("place") if p["order_type"] == "Stop"][0]["order_id"]}
    ca, cb = a.of("place")[-1], b.of("place")[-1]
    assert (ca["action"], ca["qty"], ca["order_type"]) == ("Buy", 1, "Market") and (cb["action"], cb["qty"]) == ("Buy", 2)
    assert tid not in active()
    # the stop price never moved (no break-even step in TS-Hunter)
    assert {m["stop_price"] for m in a.of("modify")} == {29658.5}


async def test_ts_hunter_buy_side_exits_with_sell(live):
    a = FakeExecutor("A")
    live.use(a)
    r = await signals.process({"event": "signal", "side": "BUY", "pair": "MNQ",
                               "risk": {"value": 2}, "sl": {"value": 90.0}, "trade_id": "B1"}, ts())
    assert r["status"] == "ok"
    e, s = a.of("place")
    assert e["action"] == "Buy" and s["action"] == "Sell" and s["stop_price"] == 90.0
    await signals.process({"event": "management", "action": "partial_close_percent", "symbol": "MNQ",
                           "percent": 50, "trade_id": "B1"}, ts())
    assert a.of("place")[-1]["action"] == "Sell" and a.of("place")[-1]["qty"] == 1


async def test_ts_hunter_without_stop_places_only_entry(live):
    a = FakeExecutor("A")
    live.use(a)
    await signals.process({"event": "signal", "side": "SELL", "symbol": "MNQ", "risk": {"value": 1}, "trade_id": "N1"}, ts())
    assert len(a.of("place")) == 1 and active("N1")["accounts"]["A"]["sl_order_id"] is None
    await signals.process({"event": "management", "action": "partial_close_percent", "symbol": "MNQ",
                           "percent": 100, "trade_id": "N1"}, ts())
    assert a.of("modify") == [] and a.of("cancel") == []


async def test_ts_hunter_multiplier_uses_round(live):
    live.use(FakeExecutor("A", qty_multiplier=1.5))
    r = await signals.process({"event": "signal", "side": "SELL", "symbol": "MNQ", "risk": {"value": 1}, "trade_id": "R1"}, ts())
    assert r["accounts"][0]["qty"] == 2


async def test_ts_hunter_validation_errors(live):
    live.use(FakeExecutor("A"))
    base = {"event": "signal", "side": "SELL", "symbol": "MNQ", "risk": {"value": 1}}
    with pytest.raises(SignalError, match="trade_id"):
        await signals.process(base, ts())
    with pytest.raises(SignalError, match="symbol"):
        await signals.process({**base, "symbol": "", "trade_id": "x"}, ts())
    with pytest.raises(SignalError, match="risk.value"):
        await signals.process({**base, "risk": {}, "trade_id": "x"}, ts())
    with pytest.raises(SignalError, match="positive"):
        await signals.process({**base, "risk": {"value": 0}, "trade_id": "x"}, ts())
    with pytest.raises(SignalError, match="side"):
        await signals.process({**base, "side": "flat", "trade_id": "x"}, ts())
    with pytest.raises(SignalError, match="Unknown TS-Hunter event"):
        await signals.process({"event": "bogus", "symbol": "MNQ", "trade_id": "x"}, ts())
    with pytest.raises(SignalError, match="management action"):
        await signals.process({"event": "management", "action": "nope", "symbol": "MNQ", "trade_id": "x"}, ts())
    with pytest.raises(SignalError, match="not mapped"):
        await signals.process({**base, "symbol": "ZZZ", "trade_id": "x"}, ts())


async def test_ts_hunter_percent_validation(live):
    a = FakeExecutor("A")
    live.use(a)
    await signals.process({"event": "signal", "side": "SELL", "symbol": "MNQ", "risk": {"value": 4}, "trade_id": "P1"}, ts())
    for bad in (None, "x", 0, 101):
        with pytest.raises(SignalError):
            await signals.process({"event": "management", "action": "partial_close_percent", "symbol": "MNQ",
                                   "percent": bad, "trade_id": "P1"}, ts())


async def test_ts_hunter_partial_close_untracked_is_skipped(live):
    live.use(FakeExecutor("A"))
    r = await signals.process({"event": "management", "action": "partial_close_percent", "symbol": "MNQ",
                               "percent": 25, "trade_id": "ghost"}, ts())
    assert r == {"status": "skipped", "reason": "no_active_trade", "action": "partial_close_percent"}


async def test_ts_hunter_full_close_untracked_flattens_every_executor(live):
    a, b = FakeExecutor("A", working=[{"id": 9, "symbol": "MNQ"}]), FakeExecutor("B")
    live.use(a, b)
    r = await signals.process({"event": "management", "action": "full_close", "symbol": "MNQ", "trade_id": "ghost"}, ts())
    assert r["accounts"] == 2 and r["cancelled"] == 1
    assert a.of("liquidate") == [{"symbol": "MNQ"}] and b.of("liquidate") == [{"symbol": "MNQ"}]


async def test_ts_hunter_disabled_account_is_skipped_on_management(live):
    a, b = FakeExecutor("A"), FakeExecutor("B")
    live.use(a, b)
    w = ts()
    await signals.process({"event": "signal", "side": "SELL", "symbol": "MNQ", "risk": {"value": 4},
                           "sl": {"value": 1.0}, "trade_id": "D1"}, w)
    live.use(a)  # B no longer routed
    r = await signals.process({"event": "management", "action": "partial_close_percent", "symbol": "MNQ",
                               "percent": 25, "trade_id": "D1"}, w)
    assert r["accounts"] == ["A"] and len(b.of("place")) == 2
    assert active("D1")["accounts"]["B"]["remaining_qty"] == 4
    r = await signals.process({"event": "management", "action": "full_close", "symbol": "MNQ", "trade_id": "D1"}, w)
    assert r["accounts"] == 1 and b.of("liquidate") == [] and "D1" not in active()


async def test_ts_hunter_full_close_uses_tracked_contract(live):
    a = FakeExecutor("A")
    live.use(a)
    w = ts()
    await signals.process({"event": "signal", "side": "SELL", "symbol": "MNQ1!", "risk": {"value": 1}, "trade_id": "C1"}, w)
    assert active("C1")["contract"] == "MNQU6"
    await signals.process({"event": "management", "action": "full_close", "symbol": "MNQ1!", "trade_id": "C1"}, w)
    assert a.of("liquidate") == [] and a.of("place")[-1]["symbol"] == "MNQU6" and a.of("place")[-1]["action"] == "Buy"


async def test_ts_hunter_stop_cancelled_when_position_flat(live):
    a = FakeExecutor("A")
    live.use(a)
    w = ts()
    await signals.process({"event": "signal", "side": "SELL", "symbol": "MNQ", "risk": {"value": 2},
                           "sl": {"value": 1.0}, "trade_id": "F1"}, w)
    sl_id = active("F1")["accounts"]["A"]["sl_order_id"]
    await signals.process({"event": "management", "action": "partial_close_percent", "symbol": "MNQ",
                           "percent": 100, "trade_id": "F1"}, w)
    assert a.of("cancel") == [{"order_id": sl_id}]
    info = active("F1")["accounts"]["A"]
    assert info["remaining_qty"] == 0 and info["sl_order_id"] is None
    r = await signals.process({"event": "management", "action": "partial_close_percent", "symbol": "MNQ",
                               "percent": 50, "trade_id": "F1"}, w)
    assert r["accounts"] == []  # nothing left to close


async def test_ts_hunter_concurrent_partial_closes_are_serialised(live):
    a = FakeExecutor("A", place_delay=0.01)
    live.use(a)
    w = ts()
    await signals.process({"event": "signal", "side": "SELL", "symbol": "MNQ", "risk": {"value": 4},
                           "sl": {"value": 1.0}, "trade_id": "L1"}, w)
    tp1 = {"event": "management", "action": "partial_close_percent", "symbol": "MNQ", "percent": 25, "trade_id": "L1"}
    tp2 = {**tp1, "percent": 33.33333333}
    await asyncio.gather(signals.process(tp1, w), signals.process(tp2, w))
    assert active("L1")["accounts"]["A"]["remaining_qty"] == 2  # 4 -> 3 -> 2, never a lost update
    assert "1:live:ts:L1" in signals._trade_locks


async def test_ts_hunter_guards_return_short_shapes(live):
    config.save_settings({"trading_enabled": False})
    live.use(FakeExecutor("A"))
    r = await signals.process({"event": "signal", "side": "SELL", "symbol": "MNQ", "risk": {"value": 1}, "trade_id": "G"}, ts())
    assert r == {"status": "skipped", "reason": "trading_disabled"}
    config.save_settings({"trading_enabled": True})
    live.use()
    r = await signals.process({"event": "signal", "side": "SELL", "symbol": "MNQ", "risk": {"value": 1}, "trade_id": "G"}, ts())
    assert r == {"status": "skipped", "reason": "no_enabled_accounts"}


async def test_ts_hunter_simulate_mode_uses_sim_client(admin):
    w = ts()
    entry, tp1, tp2, tp3, full = TS_LIFECYCLE
    r = await signals.process(entry, w, simulate=True)
    assert r["simulated"] is True and r["accounts"] == [{"account": "SIM", "qty": 4}]
    for p, rem in ((tp1, 3), (tp2, 2), (tp3, 1)):
        await signals.process(p, w, simulate=True)
        assert active(entry["trade_id"], simulate=True)["accounts"]["SIM"]["remaining_qty"] == rem
    assert "1:sim:ts:TS-HUNTER-SELL-1787239680000" in signals._trade_locks
    await signals.process(full, w, simulate=True)
    assert active(simulate=True) == {}
    # v5: the per-trade lock is released once the trade is closed (v4 kept it forever).
    assert "1:sim:ts:TS-HUNTER-SELL-1787239680000" not in signals._trade_locks


# ================================================================ simulate
async def test_simulate_without_webhook_uses_synthetic_bracket(admin):
    config.save_settings({"default_qty": 3, "tp_qty": 1})
    r = await signals.process(ENTRY, simulate=True)
    assert r["status"] == "ok" and len(r["orders"]) == 5 and r["simulated"] is True
    assert "sim:MNQ" in active(simulate=True)
    r2 = await signals.process({"event": "tp1_hit", "action": "move_sl", "symbol": "MNQ1!", "new_sl": 99}, simulate=True)
    assert r2["status"] == "ok" and r2["new_sl"] == 100.0
    signals.reset_simulation()
    assert active(simulate=True) == {}


# ------------------------------------------- close_all: nur der eigene Kontrakt
async def test_close_all_cancels_only_its_own_contract(live):
    """A close for one symbol must not strip the stops of other symbols on the
    same account (they would be left open and unprotected)."""
    a = FakeExecutor("A", contract_ids={"MNQU6": 111, "ESU6": 222}, working=[
        {"id": 1, "contractId": 111},          # MNQ stop  → must be cancelled
        {"id": 2, "contractId": 111},          # MNQ target → must be cancelled
        {"id": 3, "contractId": 222},          # ES stop   → must survive
    ])
    live.use(a)
    w = wh(id="wh_scope")
    await signals.process({"action": "buy", "symbol": "MNQ1!", "qty": 1}, w)
    r = await signals.process({"action": "close_all", "symbol": "MNQ1!"}, w)
    assert r["cancelled"] == 2
    assert [c["order_id"] for c in a.of("cancel")] == [1, 2]      # the ES stop is untouched
    assert a.of("liquidate") == [{"symbol": "MNQU6"}]


async def test_close_all_matches_orders_by_symbol_too(live):
    """The simulator (and any broker that names the contract) matches by name."""
    a = FakeExecutor("A", working=[{"id": 7, "symbol": "MNQU6"}, {"id": 8, "symbol": "ESU6"}])
    live.use(a)
    w = wh(id="wh_sym")
    await signals.process({"action": "buy", "symbol": "MNQ1!", "qty": 1}, w)
    r = await signals.process({"action": "close_all", "symbol": "MNQ1!"}, w)
    assert r["cancelled"] == 1 and [c["order_id"] for c in a.of("cancel")] == [7]


async def test_close_all_without_contract_info_leaves_orders_and_reports(live):
    """No contractId and no symbol on the orders → nothing is cancelled (that could
    strip other symbols' stops); an error event points at the account."""
    a = FakeExecutor("A", working=[{"id": 5}, {"id": 6}])
    live.use(a)
    w = wh(id="wh_blind")
    await signals.process({"action": "buy", "symbol": "MNQ1!", "qty": 1}, w)
    r = await signals.process({"action": "close_all", "symbol": "MNQ1!"}, w)
    assert r["cancelled"] == 0 and not a.of("cancel")
    assert any("none cancelled" in e["message"] for e in state.recent_events())


async def test_cancel_working_without_contract_is_account_wide():
    """No contract argument (the SOS flatten-all path) → every order goes."""
    from app.engine.common import _cancel_working
    a = FakeExecutor("A", contract_ids={"MNQU6": 111}, working=[
        {"id": 1, "contractId": 111}, {"id": 2, "contractId": 222}])
    assert await _cancel_working(a, "") == 2
    assert sorted(c["order_id"] for c in a.of("cancel")) == [1, 2]


# --------------------------------- Passphrase greift vor dem Marketplace-Fan-out
async def test_wrong_passphrase_reaches_no_subscriber(live, monkeypatch):
    """Knowing only the URL must not trade on subscribers' accounts: the check
    runs at the ingress, before the fan-out (subscribers execute trusted)."""
    config.save_settings({"webhook_passphrase": "pp"})
    forwarded: list[dict] = []
    monkeypatch.setattr(signals, "forward_to_subscribers",
                        lambda payload, webhook, publisher_area=None: forwarded.append(payload) or 0)
    executed: list[dict] = []

    async def spy(payload, webhook, *, trusted=False, **kw):
        executed.append(payload)
        return {"status": "ok"}
    monkeypatch.setattr(signals, "process", spy)

    signals.accept({"action": "buy", "symbol": "MNQ1!"}, wh())          # no passphrase
    await settle()
    assert forwarded == [] and executed == []
    signals.accept({"action": "buy", "symbol": "MNQ1!", "passphrase": "wrong"}, wh())
    await settle()
    assert forwarded == [] and executed == []
    assert any("invalid passphrase" in e["message"].lower() for e in state.recent_events())
    assert any(s["result"].startswith("error: Invalid passphrase") for s in state.recent_signals())

    signals.accept({"action": "buy", "symbol": "MNQ1!", "passphrase": "pp"}, wh())
    await settle()
    assert len(forwarded) == 1 and len(executed) == 1                    # correct one goes through


async def test_no_passphrase_configured_forwards_as_before(live, monkeypatch):
    forwarded: list[dict] = []
    monkeypatch.setattr(signals, "forward_to_subscribers",
                        lambda payload, webhook, publisher_area=None: forwarded.append(payload) or 0)

    async def spy(payload, webhook, *, trusted=False):
        return {"status": "ok"}
    monkeypatch.setattr(signals, "process", spy)
    signals.accept({"action": "buy", "symbol": "MNQ1!"}, wh())
    await settle()
    assert len(forwarded) == 1

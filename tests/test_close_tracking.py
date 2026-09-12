"""Regression tests for per-account tracking after mixed close outcomes."""
from __future__ import annotations

from app.engine.manage import handle_close_all
from app.engine.ts_hunter import handle_full_close
from app.tradovate import TradovateError
from tests.helpers import FakeExecutor


def _tracked(*names: str) -> dict:
    return {
        "accounts": {
            name: {"name": name, "contract": "MNQ"}
            for name in names
        }
    }


def _fail_liquidate_once(ex: FakeExecutor):
    original = ex.liquidate_position
    attempts = {"count": 0}

    async def fail_once(symbol: str):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise TradovateError("liquidate failed")
        return await original(symbol)

    ex.liquidate_position = fail_once
    return attempts


async def test_close_all_keeps_failed_account_and_retries_only_it(admin):
    a, b = FakeExecutor("A"), FakeExecutor("B")
    attempts = _fail_liquidate_once(b)
    key = "wh:MNQ"
    active = {key: _tracked("A", "B")}
    webhook = {"id": "wh", "name": "test"}

    await handle_close_all("MNQ", "MNQ", [a, b], active, "", webhook)

    assert set(active[key]["accounts"]) == {"B"}
    assert a.of("liquidate") == [{"symbol": "MNQ"}]
    assert attempts["count"] == 1

    await handle_close_all("MNQ", "MNQ", [a, b], active, "", webhook)

    assert key not in active
    assert a.of("liquidate") == [{"symbol": "MNQ"}]  # A is not flattened twice
    assert attempts["count"] == 2
    assert b.of("liquidate") == [{"symbol": "MNQ"}]


async def test_ts_full_close_keeps_failed_account_and_retries_only_it(admin):
    a, b = FakeExecutor("A"), FakeExecutor("B")
    attempts = _fail_liquidate_once(b)
    active = {"T1": _tracked("A", "B")}

    await handle_full_close({}, "T1", "MNQ", [a, b], active, "")

    assert set(active["T1"]["accounts"]) == {"B"}
    assert a.of("liquidate") == [{"symbol": "MNQ"}]
    assert attempts["count"] == 1

    await handle_full_close({}, "T1", "MNQ", [a, b], active, "")

    assert "T1" not in active
    assert a.of("liquidate") == [{"symbol": "MNQ"}]  # A is not flattened twice
    assert attempts["count"] == 2
    assert b.of("liquidate") == [{"symbol": "MNQ"}]


# --- accounts the record does not know about (lost entry answer, manual position)
async def test_close_all_also_closes_an_untracked_account_that_holds_the_contract(admin):
    a = FakeExecutor("A")
    c_flat = FakeExecutor("C", positions=[])
    d_pos = FakeExecutor("D", positions=[{"symbol": "MNQ", "netPos": 2}, {"symbol": "ESU6", "netPos": 1}])
    key = "wh:MNQ"
    active = {key: _tracked("A")}
    r = await handle_close_all("MNQ", "MNQ", [a, c_flat, d_pos], active, "", {"id": "wh", "name": "test"})
    assert key not in active and r["accounts"] == 2 and r["failed"] == []
    assert a.of("liquidate") == [{"symbol": "MNQ"}]
    assert c_flat.of("liquidate") == []                          # flat: no liquidate call, no rejection
    assert d_pos.of("liquidate") == [{"symbol": "MNQ"}]          # the untracked position is closed, ESU6 untouched


async def test_ts_full_close_leaves_an_untracked_account_alone_and_reports_it(admin):
    """Policy: a TS-Hunter full_close is isolated — an account the record does not
    list keeps its position (it may belong to another trade); it is reported."""
    from app import state
    a = FakeExecutor("A")
    d_pos = FakeExecutor("D", positions=[{"symbol": "MNQ", "netPos": -1}])
    c_flat = FakeExecutor("C", positions=[])
    active = {"T1": _tracked("A")}
    r = await handle_full_close({}, "T1", "MNQ", [a, d_pos, c_flat], active, "")
    assert "T1" not in active and r["accounts"] == 1 and r["untracked"] == ["D"] and r["status"] == "ok"
    assert d_pos.of("liquidate") == [] and d_pos.of("place") == [] and c_flat.of("liquidate") == []
    assert any("D hold(s) MNQ without a record of trade T1" in e["message"] for e in state.recent_events())


async def test_ts_full_close_closes_only_the_trades_quantity(admin):
    a = FakeExecutor("A")
    active = {"T1": {"side": "sell", "accounts": {"A": {"name": "A", "contract": "MNQ", "qty": 2, "remaining_qty": 2, "sl_order_id": 55}}}}
    r = await handle_full_close({}, "T1", "MNQ", [a], active, "")
    assert r["status"] == "ok" and r["cancelled"] == 1 and "T1" not in active
    assert a.of("cancel") == [{"order_id": 55}] and a.of("liquidate") == []
    assert [(p["action"], p["qty"], p["order_type"]) for p in a.of("place")] == [("Buy", 2, "Market")]
    # already flat after partial closes: only the stop goes, no order
    b = FakeExecutor("B")
    active = {"T2": {"side": "buy", "accounts": {"B": {"name": "B", "contract": "MNQ", "qty": 0, "remaining_qty": 0, "sl_order_id": None}}}}
    r = await handle_full_close({}, "T2", "MNQ", [b], active, "")
    assert r["status"] == "ok" and r["cancelled"] == 0 and b.of("place") == [] and b.of("cancel") == []


async def test_ts_full_close_stop_that_will_not_cancel_is_an_error(admin):
    a = FakeExecutor("A")

    async def refuse(order_id):
        raise TradovateError("cancel refused")
    a.cancel_order = refuse
    active = {"T1": {"side": "buy", "accounts": {"A": {"name": "A", "contract": "MNQ", "remaining_qty": 1, "sl_order_id": 9}}}}
    r = await handle_full_close({}, "T1", "MNQ", [a], active, "")
    assert r["status"] == "error" and r["failed"] == ["A"] and "T1" in active         # stays tracked for a retry
    assert [(p["action"], p["qty"]) for p in a.of("place")] == [("Sell", 1)]            # the position itself was closed


async def test_untracked_close_failure_is_reported_not_fatal(admin):
    from app import state
    a = FakeExecutor("A")
    d_pos = FakeExecutor("D", positions=[{"symbol": "MNQ", "netPos": 1}])

    async def boom(symbol):
        raise TradovateError("gateway down")
    d_pos.liquidate_position = boom
    key = "wh:MNQ"
    active = {key: _tracked("A")}
    r = await handle_close_all("MNQ", "MNQ", [a, d_pos], active, "", {"id": "wh", "name": "test"})
    assert r["failed"] == ["D"] and key not in active
    assert any("untracked D FAILED" in e["message"] for e in state.recent_events())

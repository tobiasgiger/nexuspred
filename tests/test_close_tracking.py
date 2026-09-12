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


async def test_ts_full_close_also_closes_an_untracked_account_that_holds_the_contract(admin):
    a = FakeExecutor("A")
    d_pos = FakeExecutor("D", positions=[{"symbol": "MNQ", "netPos": -1}])
    active = {"T1": _tracked("A")}
    r = await handle_full_close({}, "T1", "MNQ", [a, d_pos], active, "")
    assert "T1" not in active and r["accounts"] == 2
    assert d_pos.of("liquidate") == [{"symbol": "MNQ"}]


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

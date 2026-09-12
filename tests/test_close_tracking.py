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

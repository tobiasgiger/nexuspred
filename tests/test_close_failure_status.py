"""Regression coverage for close handlers reporting broker failures truthfully."""
from __future__ import annotations

from app.engine.manage import handle_close_all
from app.engine.ts_hunter import handle_full_close
from app.tradovate import TradovateError
from tests.helpers import FakeExecutor


def _tracked(*names: str) -> dict:
    return {"accounts": {name: {"name": name, "contract": "MNQ"} for name in names}}


def _fail_liquidate(ex: FakeExecutor) -> None:
    async def fail(symbol: str):
        raise TradovateError("liquidate failed")

    ex.liquidate_position = fail


async def test_close_all_returns_error_when_any_account_close_fails(admin):
    a, b = FakeExecutor("A"), FakeExecutor("B")
    _fail_liquidate(b)
    active = {"wh:MNQ": _tracked("A", "B")}

    result = await handle_close_all(
        "MNQ", "MNQ", [a, b], active, "", {"id": "wh", "name": "test"}
    )

    assert result["status"] == "error"
    assert result["failed"] == ["B"]


async def test_ts_full_close_returns_error_when_any_account_close_fails(admin):
    a, b = FakeExecutor("A"), FakeExecutor("B")
    _fail_liquidate(b)
    active = {"T1": _tracked("A", "B")}

    result = await handle_full_close({}, "T1", "MNQ", [a, b], active, "")

    assert result["status"] == "error"
    assert result["failed"] == ["B"]

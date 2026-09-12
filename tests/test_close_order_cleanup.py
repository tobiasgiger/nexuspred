"""Regression coverage for unresolved working orders after a flatten."""
from __future__ import annotations

import pytest

from app import alerts, tradovate
from app.engine.common import _close_contract
from app.engine.manage import handle_close_all
from app.tradovate import TradovateError
from tests.helpers import FakeExecutor


def _always_fail_cancel(ex: FakeExecutor):
    attempts = {"count": 0}

    async def fail(order_id: int):
        attempts["count"] += 1
        raise TradovateError("cancel rejected")

    ex.cancel_order = fail
    return attempts


async def test_close_contract_raises_when_order_survives_retry(admin, monkeypatch):
    ex = FakeExecutor("A", working=[{"id": 7, "symbol": "MNQ"}])
    attempts = _always_fail_cancel(ex)
    monkeypatch.setattr(alerts, "execution_problem", lambda *args, **kwargs: None)
    monkeypatch.setattr(tradovate, "_fire", lambda value: None)

    with pytest.raises(TradovateError, match="working orders remain after closing MNQ"):
        await _close_contract(ex, "", "MNQ")

    assert attempts["count"] == 2
    assert ex.of("liquidate") == [{"symbol": "MNQ"}]


async def test_close_all_keeps_tracking_when_order_cleanup_remains_unresolved(admin, monkeypatch):
    ex = FakeExecutor("A", working=[{"id": 7, "symbol": "MNQ"}])
    _always_fail_cancel(ex)
    monkeypatch.setattr(alerts, "execution_problem", lambda *args, **kwargs: None)
    monkeypatch.setattr(tradovate, "_fire", lambda value: None)
    key = "wh:MNQ"
    active = {key: {"accounts": {"A": {"name": "A", "contract": "MNQ"}}}}

    await handle_close_all("MNQ", "MNQ", [ex], active, "", {"id": "wh", "name": "test"})

    assert key in active
    assert "A" in active[key]["accounts"]


async def test_leftover_orders_are_reported_as_orders_not_position(admin, monkeypatch):
    from app import state
    ex = FakeExecutor("A", working=[{"id": 7, "symbol": "MNQ"}])
    _always_fail_cancel(ex)
    monkeypatch.setattr(alerts, "execution_problem", lambda *args, **kwargs: None)
    monkeypatch.setattr(tradovate, "_fire", lambda value: None)
    r = await handle_close_all("MNQ", "MNQ", [ex], {"wh:MNQ": {"accounts": {"A": {"name": "A", "contract": "MNQ"}}}}, "", {"id": "wh", "name": "test"})
    assert r["status"] == "error" and r["failed"] == ["A"]
    msg = next(e["message"] for e in state.recent_events() if "close_all FAILED" in e["message"])
    assert "orders are not" in msg and "may still be open" not in msg

"""Regression tests for duplicate TS-Hunter trade identities."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app import config, signals
from tests.helpers import FakeExecutor


@pytest.fixture
def live(monkeypatch, admin):
    config.save_settings({"trading_enabled": True})
    box = {"execs": []}
    monkeypatch.setattr(signals, "_webhook_executors", lambda webhook: list(box["execs"]))

    async def fake_trade_executed(*args, **kwargs):
        return None

    monkeypatch.setattr(signals.alerts, "trade_executed", fake_trade_executed)

    def use(*executors):
        box["execs"] = list(executors)

    return SimpleNamespace(use=use)


def webhook() -> dict:
    w = config.new_webhook(name="ts-dup", strategy="ts_hunter", default_qty=1, tp_qty=1)
    w["id"] = "ts-dup"
    return w


def payload() -> dict:
    return {
        "event": "signal",
        "side": "SELL",
        "symbol": "MNQ",
        "risk": {"value": 1},
        "trade_id": "TRADE-1",
    }


async def test_duplicate_active_trade_id_is_skipped(live):
    ex = FakeExecutor("A")
    live.use(ex)
    w = webhook()

    first = await signals.process(payload(), w)
    second = await signals.process(payload(), w)

    assert first["status"] == "ok"
    assert second == {
        "status": "skipped",
        "reason": "active_trade_exists",
        "action": "signal",
        "trade_id": "TRADE-1",
    }
    assert len(ex.of("place")) == 1


async def test_concurrent_duplicate_trade_id_executes_at_most_once(live):
    ex = FakeExecutor("A", place_delay=0.01)
    live.use(ex)
    w = webhook()

    results = await asyncio.gather(signals.process(payload(), w), signals.process(payload(), w))

    assert sorted(r["status"] for r in results) == ["ok", "skipped"]
    assert len(ex.of("place")) == 1

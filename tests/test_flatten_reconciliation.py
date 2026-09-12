"""Regression coverage for broker-truth reconciliation in emergency flatten paths."""
from __future__ import annotations

from app.engine import common
from app.engine.common import _flatten_account
from app.tradovate import OrderOutcomeUnknown
from tests.helpers import FakeExecutor


async def test_unknown_liquidation_is_reconciled_without_retry(admin, monkeypatch):
    monkeypatch.setattr(common, "FLATTEN_VERIFY_DELAY_S", 0)

    class LostReply(FakeExecutor):
        async def liquidate_position(self, symbol):
            self.calls.append(("liquidate", {"symbol": symbol}))
            # The broker executed the request, but its response was lost.
            self._positions = []
            raise OrderOutcomeUnknown("answer lost")

    ex = LostReply("A", positions=[{"symbol": "MNQ", "netPos": 1}])
    cancelled, flattened, errors = await _flatten_account(ex)

    assert cancelled == 0
    assert flattened == 1
    assert errors == []
    assert ex.of("liquidate") == [{"symbol": "MNQ"}]  # never blindly replayed


async def test_submitted_liquidation_with_residual_position_is_error(admin, monkeypatch):
    monkeypatch.setattr(common, "FLATTEN_VERIFY_DELAY_S", 0)

    class Residual(FakeExecutor):
        async def liquidate_position(self, symbol):
            self.calls.append(("liquidate", {"symbol": symbol}))
            return {}  # accepted/submitted, but broker truth remains non-flat

    ex = Residual("A", positions=[{"symbol": "MNQ", "netPos": 2}])
    cancelled, flattened, errors = await _flatten_account(ex)

    assert cancelled == 0
    assert flattened == 0
    assert ex.of("liquidate") == [{"symbol": "MNQ"}]
    assert any("broker still shows position" in err for err in errors)


async def test_duplicate_position_rows_issue_only_one_liquidation(admin, monkeypatch):
    monkeypatch.setattr(common, "FLATTEN_VERIFY_DELAY_S", 0)
    ex = FakeExecutor("A", positions=[
        {"symbol": "MNQ", "netPos": 1},
        {"symbol": "MNQ", "netPos": 1},
    ])

    _cancelled, flattened, errors = await _flatten_account(ex)

    assert flattened == 1
    assert errors == []
    assert ex.of("liquidate") == [{"symbol": "MNQ"}]

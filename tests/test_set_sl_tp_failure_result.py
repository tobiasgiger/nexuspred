"""Regression coverage for failed protective-order updates on an open position."""
from __future__ import annotations

from app.engine.manage import handle_set_sl_tp
from tests.helpers import FakeExecutor


async def test_set_sl_tp_does_not_report_no_position_when_placement_fails(admin):
    ex = FakeExecutor(
        "A",
        positions=[{"symbol": "MNQ", "netPos": 2}],
        fail_place=True,
    )
    active = {}

    result = await handle_set_sl_tp(
        {"stop_price": 100.0},
        "MNQ",
        "MNQ",
        [ex],
        active,
        "",
        {"id": "wh", "name": "test"},
    )

    assert result == {
        "status": "error",
        "reason": "protection_update_failed",
        "action": "set_sl_tp",
        "accounts": 0,
        "failed": ["A"],
        "sl": 100.0,
        "tp": None,
        "simulated": False,
    }
    assert "wh:MNQ" in active
    assert active["wh:MNQ"]["accounts"]["A"]["qty"] == 2

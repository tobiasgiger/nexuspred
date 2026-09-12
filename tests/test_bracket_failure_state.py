"""Regression tests for bracket stop-management failure semantics."""
from __future__ import annotations

from app import state
from app.engine import bracket
from app.tradovate import TradovateError
from tests.helpers import FakeExecutor


def _active() -> dict:
    return {
        "wh:MNQ": {
            "accounts": {
                "A": {
                    "name": "A",
                    "contract": "MNQ",
                    "entry_qty": 3,
                    "tp_qty": 1,
                    "qty": 3,
                    "entry_price": 100.0,
                    "sl_order_id": 77,
                    "sl_type": "Stop",
                    "sl_stop": 110.0,
                    "tp_order_ids": [],
                }
            }
        }
    }


async def test_trail_active_does_not_advance_state_when_modify_fails(admin):
    ex = FakeExecutor("A")

    async def fail_modify(order_id: int, **kwargs):
        raise TradovateError("modify failed")

    ex.modify_order = fail_modify
    active = _active()

    result = await bracket.handle_trail_active(
        {"event": "tp2_hit"}, "MNQ", [ex], active, "", {"id": "wh"}
    )

    assert result["accounts"] == 0
    assert active["wh:MNQ"]["accounts"]["A"]["qty"] == 3


async def test_move_sl_cancel_failure_keeps_original_broker_error_visible(admin):
    ex = FakeExecutor("A")

    async def fail_cancel(order_id: int):
        raise TradovateError("cancel failed")

    ex.cancel_order = fail_cancel
    active = _active()

    result = await bracket.handle_move_sl(
        {"event": "tp3_hit", "new_sl": 100.0}, "MNQ", [ex], active, "", {"id": "wh"}
    )

    assert result["accounts"] == 0
    info = active["wh:MNQ"]["accounts"]["A"]
    assert info["sl_order_id"] == 77 and info["qty"] == 3
    messages = [e["message"] for e in state.recent_events()]
    assert any("could not be retired" in m and "cancel failed" in m for m in messages)

"""The in-memory simulator: rehearse scenarios without a broker."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from .. import context, signals, state
from ..simulator import SCENARIOS, client_for
from ..tradovate import TradovateError

router = APIRouter(prefix="/api", tags=["simulator"])


@router.get("/scenarios")
async def api_scenarios() -> list[dict[str, Any]]:
    return SCENARIOS


@router.post("/simulate")
async def api_simulate(request: Request) -> dict[str, Any]:
    """Run a single signal through the pipeline in simulation mode (no broker)."""
    payload = await request.json()
    state.log_signal(payload, result="simulated")
    try:
        return await signals.process(payload, simulate=True)
    except (signals.SignalError, TradovateError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/simulate/state")
async def api_simulate_state() -> dict[str, Any]:
    return {
        "positions": await client_for(context.get_area()).positions(),
        "working_orders": await client_for(context.get_area()).working_orders(),
        "active_trades": signals.active_trades(simulate=True),
    }


@router.post("/simulate/reset")
async def api_simulate_reset() -> dict[str, Any]:
    signals.reset_simulation()
    return {"status": "reset"}

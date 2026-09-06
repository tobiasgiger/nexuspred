"""Shared test helpers: an in-memory executor that records every broker call.

``FakeExecutor`` mimics the interface :mod:`app.signals` expects from an
``AccountExecutor`` (``name``, ``qty_multiplier``, ``resolve_contract``,
``place_order``, ``modify_order``, ``cancel_order``, ``working_orders``,
``liquidate_position``, ``positions``) without touching the network, and keeps a
list of every call so tests can assert the exact order flow.
"""
from __future__ import annotations

import asyncio
from typing import Any

from app.tradovate import TradovateError


class FakeExecutor:
    def __init__(
        self,
        name: str,
        qty_multiplier: float = 1,
        *,
        positions: list[dict[str, Any]] | None = None,
        working: list[dict[str, Any]] | None = None,
        fail_place: bool = False,
        place_delay: float = 0.0,
    ) -> None:
        self.name = name
        self.qty_multiplier = qty_multiplier
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._next_id = 1000
        self._positions = list(positions or [])
        self.working = list(working or [])
        self.fail_place = fail_place
        self.place_delay = place_delay

    # -- helpers ---------------------------------------------------------
    def of(self, kind: str) -> list[dict[str, Any]]:
        return [c for k, c in self.calls if k == kind]

    # -- executor interface ---------------------------------------------
    async def resolve_contract(self, target: str) -> str:
        self.calls.append(("resolve", {"target": target}))
        return target

    async def place_order(self, **kw: Any) -> dict[str, Any]:
        if self.place_delay:
            await asyncio.sleep(self.place_delay)
        if self.fail_place:
            raise TradovateError("placeorder failed")
        self._next_id += 1
        rec = {"order_id": self._next_id, "status": "submitted", **kw}
        self.calls.append(("place", rec))
        return rec

    async def modify_order(self, order_id: int, **kw: Any) -> dict[str, Any]:
        rec = {"order_id": order_id, **kw}
        self.calls.append(("modify", rec))
        return {}

    async def cancel_order(self, order_id: int) -> dict[str, Any]:
        self.calls.append(("cancel", {"order_id": order_id}))
        return {}

    async def working_orders(self) -> list[dict[str, Any]]:
        return list(self.working)

    async def liquidate_position(self, symbol: str) -> dict[str, Any]:
        self.calls.append(("liquidate", {"symbol": symbol}))
        return {}

    async def positions(self) -> list[dict[str, Any]]:
        return list(self._positions)


async def settle(loops: int = 5) -> None:
    """Let pending background tasks run (e.g. the webhook's create_task)."""
    for _ in range(loops):
        await asyncio.sleep(0)

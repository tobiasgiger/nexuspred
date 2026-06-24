"""Simulation support: a fake order executor plus ready-made trade scenarios.

The simulator runs the *exact same* signal-processing logic as live trading, but
order placement is handled in-memory instead of being sent to Tradovate. This lets
you verify the full lifecycle (entry → TP/SL management → close) safely, with no
credentials and no risk of real orders.
"""
from __future__ import annotations

import threading
from typing import Any

from . import state


class SimulatedClient:
    """Drop-in stand-in for ``TradovateClient`` that fills orders in memory."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._next_id = 1000
        self._working: dict[int, dict[str, Any]] = {}
        # symbol -> {"net": signed qty, "avg": avg price}
        self._positions: dict[str, dict[str, float]] = {}

    def reset(self) -> None:
        with self._lock:
            self._next_id = 1000
            self._working.clear()
            self._positions.clear()
        state.log_event("info", "Simulation reset")

    async def resolve_contract(self, root: str) -> str:
        # No network — just hand back a plausible front-month contract symbol.
        return f"{root}Z5"

    async def place_order(
        self,
        *,
        symbol: str,
        action: str,
        qty: int,
        order_type: str,
        price: float | None = None,
        stop_price: float | None = None,
        account_spec: str | None = None,
        account_id: int | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            self._next_id += 1
            oid = self._next_id
            is_market = order_type.lower() == "market"
            if is_market:
                self._apply_fill(symbol, action, qty, price or stop_price or 0.0)
                status = "filled"
            else:
                self._working[oid] = {
                    "id": oid, "symbol": symbol, "action": action, "qty": qty,
                    "order_type": order_type, "price": price, "stop_price": stop_price,
                    "ordStatus": "Working",
                }
                status = "working"

        result = {
            "action": action, "symbol": symbol, "account": account_spec or "SIM",
            "qty": qty, "order_type": order_type, "price": price,
            "stop_price": stop_price, "order_id": oid, "status": status,
            "simulated": True,
        }
        state.log_order(result)
        return result

    def _apply_fill(self, symbol: str, action: str, qty: int, price: float) -> None:
        pos = self._positions.setdefault(symbol, {"net": 0.0, "avg": 0.0})
        signed = qty if action.lower() == "buy" else -qty
        new_net = pos["net"] + signed
        # Update average price only when increasing exposure in the same direction.
        if pos["net"] == 0 or (pos["net"] > 0) == (signed > 0):
            total = abs(pos["net"]) + abs(signed)
            if total:
                pos["avg"] = (
                    pos["avg"] * abs(pos["net"]) + price * abs(signed)
                ) / total
        pos["net"] = new_net
        if new_net == 0:
            pos["avg"] = 0.0

    async def modify_order(
        self, order_id: int, *, qty: int | None = None, order_type: str | None = None,
        price: float | None = None, stop_price: float | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            o = self._working.get(order_id)
            if o:
                if qty is not None:
                    o["qty"] = qty
                if price is not None:
                    o["price"] = price
                if stop_price is not None:
                    o["stop_price"] = stop_price
        state.log_order({
            "action": "Modify", "symbol": (o or {}).get("symbol", ""), "qty": qty,
            "order_type": order_type or (o or {}).get("order_type"),
            "price": price, "stop_price": stop_price, "order_id": order_id,
            "status": "modified", "simulated": True,
        })
        return {"ok": True, "simulated": True}

    async def cancel_order(self, order_id: int) -> dict[str, Any]:
        with self._lock:
            self._working.pop(order_id, None)
        return {"ok": True, "simulated": True}

    async def working_orders(self, account_id: int | None = None) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._working.values())

    async def liquidate_position(
        self, symbol: str, account_id: int | None = None
    ) -> dict[str, Any]:
        with self._lock:
            self._positions[symbol] = {"net": 0.0, "avg": 0.0}
            self._working = {
                k: v for k, v in self._working.items() if v["symbol"] != symbol
            }
        state.log_order(
            {"action": "Liquidate", "symbol": symbol, "qty": 0,
             "order_type": "Market", "status": "filled", "simulated": True}
        )
        return {"ok": True, "simulated": True}

    async def positions(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                {"contractId": sym, "symbol": sym, "netPos": int(p["net"]),
                 "netPrice": round(p["avg"], 2), "openPL": 0, "simulated": True}
                for sym, p in self._positions.items() if p["net"] != 0
            ]


sim_client = SimulatedClient()


# --------------------------------------------------------------------- scenarios
# Each scenario is an ordered list of steps. A step's ``signal`` is sent through
# the very same pipeline a real TradingView webhook would hit.
SCENARIOS: list[dict[str, Any]] = [
    {
        "id": "runner_exit_mnq",
        "name": "Runner trailed out — SELL MNQ",
        "description": "Short entry that moves the stop to break-even, trails, and is "
                       "finally flattened by a runner_exit (close_all) past TP3.",
        "steps": [
            {"label": "Entry SELL (market 3 + TP/SL brackets)",
             "signal": {"event": "entry", "action": "sell", "symbol": "MNQ1!",
                        "entry": 30267, "sl": 30285.07,
                        "tp1": 30261.58, "tp2": 30255.19, "tp3": 30247.04}},
            {"label": "TP1 hit → move stop to break-even",
             "signal": {"event": "tp1_hit", "action": "move_sl", "symbol": "MNQ1!",
                        "new_sl": 30267.0}},
            {"label": "TP2 hit → trailing active",
             "signal": {"event": "tp2_hit", "action": "trail_active", "symbol": "MNQ1!",
                        "trail_ema": "ema9", "trail_buffer": 0.15}},
            {"label": "Runner exit → close everything in profit",
             "signal": {"event": "runner_exit", "action": "close_all", "symbol": "MNQ1!",
                        "exit_price": 29761.94756, "realized_R": 1.7,
                        "message": "Runner trailed out past TP3 — closed in profit"}},
        ],
    },
    {
        "id": "win_sell_mnq",
        "name": "Winning trade — SELL MNQ",
        "description": "Short entry that runs through all three take-profits and "
                       "closes flat. Exercises entry, move_sl, trailing and close_all.",
        "steps": [
            {"label": "Entry SELL (market 3 + TP/SL brackets)",
             "signal": {"event": "entry", "action": "sell", "symbol": "MNQ1!",
                        "entry": 30267, "sl": 30285.07,
                        "tp1": 30261.58, "tp2": 30255.19, "tp3": 30247.04}},
            {"label": "TP1 hit → move stop to break-even",
             "signal": {"event": "tp1_hit", "action": "move_sl", "symbol": "MNQ1!",
                        "new_sl": 30267.0,
                        "message": "TP1 reached — SL moved to net-breakeven"}},
            {"label": "TP2 hit → trailing stop active",
             "signal": {"event": "tp2_hit", "action": "trail_active", "symbol": "MNQ1!",
                        "trail_ema": "ema9", "trail_buffer": 0.15,
                        "message": "TP2 reached — trailing stop active"}},
            {"label": "TP3 hit → close everything",
             "signal": {"event": "tp3_hit", "action": "close_all", "symbol": "MNQ1!",
                        "exit_price": 30247.04, "pnl": 250.70,
                        "message": "TP3 full kill — close all"}},
        ],
    },
    {
        "id": "loss_buy_mnq",
        "name": "Losing trade — BUY MNQ",
        "description": "Long entry that gets stopped out for a full loss.",
        "steps": [
            {"label": "Entry BUY (market 3 + TP/SL brackets)",
             "signal": {"event": "entry", "action": "buy", "symbol": "MNQ1!",
                        "entry": 30200, "sl": 30182.5,
                        "tp1": 30208, "tp2": 30215, "tp3": 30225}},
            {"label": "SL hit → close everything (full loss)",
             "signal": {"event": "sl_hit", "action": "close_all", "symbol": "MNQ1!",
                        "exit_price": 30182.5, "pnl": -180.91,
                        "message": "Stop-loss hit — full loss"}},
        ],
    },
    {
        "id": "win_buy_mes",
        "name": "Winning trade — BUY MES",
        "description": "Long MES entry, partial then full take-profit.",
        "steps": [
            {"label": "Entry BUY MES (market 3 + TP/SL brackets)",
             "signal": {"event": "entry", "action": "buy", "symbol": "MES1!",
                        "entry": 5320.0, "sl": 5316.0,
                        "tp1": 5323.0, "tp2": 5326.0, "tp3": 5330.0}},
            {"label": "TP1 hit → move stop to break-even",
             "signal": {"event": "tp1_hit", "action": "move_sl", "symbol": "MES1!",
                        "new_sl": 5320.0}},
            {"label": "TP3 hit → close everything",
             "signal": {"event": "tp3_hit", "action": "close_all", "symbol": "MES1!",
                        "exit_price": 5330.0, "pnl": 150.0}},
        ],
    },
    {
        "id": "manual_close",
        "name": "Manual close — SELL MNQ",
        "description": "Short entry that you flatten manually before any TP/SL.",
        "steps": [
            {"label": "Entry SELL (market 3 + TP/SL brackets)",
             "signal": {"event": "entry", "action": "sell", "symbol": "MNQ1!",
                        "entry": 30267, "sl": 30285,
                        "tp1": 30258, "tp2": 30250, "tp3": 30240}},
            {"label": "Manual close_all",
             "signal": {"event": "manual", "action": "close_all", "symbol": "MNQ1!",
                        "message": "Manual flat"}},
        ],
    },
]

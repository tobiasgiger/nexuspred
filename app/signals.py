"""Translate incoming TradingView webhook signals into Tradovate orders.

Supported signal shapes (see the screenshot / README for examples):

* ``action: "buy" | "sell"``  -> market entry (default qty) + TP limit orders
  (1 contract each) + a protective stop-loss covering the whole position.
* ``action: "close_all"``     -> cancel working orders for the symbol and flatten.
* ``action: "move_sl"``       -> move the protective stop to ``new_sl``.
* ``action: "trail_active"``  -> acknowledged/logged (trailing handled by the
  strategy, which keeps sending ``move_sl`` updates).

Order sizing rules (kept deliberately simple):
* Initial entry: ``default_qty`` contracts, **Market** order.
* Each take-profit present (tp1/tp2/tp3): ``tp_qty`` (1) contract, **Limit** order.
* Stop-loss: covers the full entry quantity so ``close_all`` flattens everything.

The same logic powers the **simulator**: passing ``simulate=True`` routes orders to
an in-memory executor and uses a separate trade-tracking map, so you can rehearse a
full scenario without credentials or risk.
"""
from __future__ import annotations

import threading
from typing import Any

from . import config, state
from .simulator import sim_client
from .tradovate import TradovateError, client


class SignalError(Exception):
    """Raised for malformed or rejected signals."""


# Per-symbol record of the active trade so management signals can find the
# stop-loss order to modify. Reset when the position is closed. Live and
# simulated trades are tracked separately so they never interfere.
_lock = threading.Lock()
_active: dict[str, dict[str, Any]] = {}
_sim_active: dict[str, dict[str, Any]] = {}


def _root(symbol: str) -> str:
    """Map a TradingView symbol (e.g. ``MNQ1!``) to its configured root."""
    s = config.load_settings()
    mapped = s.get("symbol_map", {}).get(symbol)
    if mapped:
        return mapped
    # Fall back: strip a trailing "1!" continuous-contract suffix.
    return symbol.replace("1!", "").strip()


def _opposite(action: str) -> str:
    return "Sell" if action.lower() == "buy" else "Buy"


async def process(payload: dict[str, Any], *, simulate: bool = False) -> dict[str, Any]:
    """Validate, authorise and execute a webhook payload. Returns a summary dict.

    When ``simulate`` is True, orders are filled in memory (no Tradovate calls) and
    the live-only guards (trading switch, passphrase) are skipped.
    """
    s = config.load_settings()
    exec_client = sim_client if simulate else client
    active_map = _sim_active if simulate else _active

    if not simulate:
        # passphrase (optional, defence in depth on top of the URL secret)
        if s.get("webhook_passphrase"):
            if payload.get("passphrase") != s["webhook_passphrase"]:
                raise SignalError("Invalid passphrase")

    action = str(payload.get("action", "")).lower().strip()
    tv_symbol = str(payload.get("symbol", "")).strip()
    if not action or not tv_symbol:
        raise SignalError("Payload missing 'action' or 'symbol'")

    root = _root(tv_symbol)
    if root not in s.get("allowed_symbols", []):
        raise SignalError(f"Symbol '{root}' not in allowed list")

    if not simulate and not s.get("trading_enabled"):
        state.log_event(
            "warn", f"Trading disabled — signal '{action}' for {root} not executed"
        )
        return {"status": "skipped", "reason": "trading_disabled", "action": action}

    contract = await exec_client.resolve_contract(root)
    tag = "[SIM] " if simulate else ""

    if action in ("buy", "sell"):
        return await _handle_entry(payload, action, root, contract, exec_client, active_map, tag)
    if action == "close_all":
        return await _handle_close_all(root, contract, exec_client, active_map, tag)
    if action == "move_sl":
        return await _handle_move_sl(payload, root, exec_client, active_map, tag)
    if action == "trail_active":
        state.log_event("info", f"{tag}Trailing active for {root} (handled by strategy)")
        return {"status": "ok", "action": action, "note": "acknowledged", "simulated": simulate}

    raise SignalError(f"Unknown action '{action}'")


async def _handle_entry(payload, action, root, contract, exec_client, active_map, tag):
    s = config.load_settings()
    entry_qty = int(s.get("default_qty", 3))
    tp_qty = int(s.get("tp_qty", 1))
    exit_side = _opposite(action)

    orders: list[dict[str, Any]] = []

    # 1) Market entry order.
    entry = await exec_client.place_order(
        symbol=contract,
        action="Buy" if action == "buy" else "Sell",
        qty=entry_qty,
        order_type=s.get("entry_order_type", "Market"),
        price=payload.get("entry"),
    )
    orders.append(entry)

    # 2) Take-profit limit orders — one contract each for tp1/tp2/tp3 present.
    tp_order_ids: list[int] = []
    for key in ("tp1", "tp2", "tp3"):
        if payload.get(key) is None:
            continue
        tp = await exec_client.place_order(
            symbol=contract,
            action=exit_side,
            qty=tp_qty,
            order_type=s.get("tp_order_type", "Limit"),
            price=float(payload[key]),
        )
        orders.append(tp)
        if tp.get("order_id"):
            tp_order_ids.append(tp["order_id"])

    # 3) Protective stop-loss covering the full position.
    sl_order_id = None
    if payload.get("sl") is not None:
        sl = await exec_client.place_order(
            symbol=contract,
            action=exit_side,
            qty=entry_qty,
            order_type=s.get("sl_order_type", "Stop"),
            stop_price=float(payload["sl"]),
        )
        orders.append(sl)
        sl_order_id = sl.get("order_id")

    with _lock:
        active_map[root] = {
            "contract": contract,
            "side": action,
            "qty": entry_qty,
            "sl_order_id": sl_order_id,
            "tp_order_ids": tp_order_ids,
        }

    state.log_event("info", f"{tag}Entry {action.upper()} {entry_qty} {contract} placed")
    return {"status": "ok", "action": action, "contract": contract,
            "orders": orders, "simulated": tag != ""}


async def _handle_close_all(root, contract, exec_client, active_map, tag):
    cancelled = 0
    try:
        for order in await exec_client.working_orders():
            try:
                await exec_client.cancel_order(order["id"])
                cancelled += 1
            except TradovateError:
                pass
    except TradovateError as exc:
        state.log_event("warn", f"{tag}Could not list working orders: {exc}")

    await exec_client.liquidate_position(contract)
    with _lock:
        active_map.pop(root, None)

    state.log_event(
        "info", f"{tag}Closed all for {contract} ({cancelled} working orders cancelled)"
    )
    return {"status": "ok", "action": "close_all", "cancelled": cancelled,
            "simulated": tag != ""}


async def _handle_move_sl(payload, root, exec_client, active_map, tag):
    new_sl = payload.get("new_sl", payload.get("sl"))
    if new_sl is None:
        raise SignalError("move_sl signal missing 'new_sl'")

    with _lock:
        active = active_map.get(root)
    if not active or not active.get("sl_order_id"):
        state.log_event("warn", f"{tag}No tracked stop-loss for {root} to move")
        return {"status": "skipped", "reason": "no_active_stop", "action": "move_sl"}

    await exec_client.modify_order(active["sl_order_id"], stop_price=float(new_sl))
    state.log_event("info", f"{tag}Stop-loss for {root} moved to {new_sl}")
    return {"status": "ok", "action": "move_sl", "new_sl": float(new_sl),
            "simulated": tag != ""}


def active_trades(simulate: bool = False) -> dict[str, Any]:
    with _lock:
        src = _sim_active if simulate else _active
        return {k: dict(v) for k, v in src.items()}


def reset_simulation() -> None:
    """Clear simulated positions, working orders and tracked trades."""
    sim_client.reset()
    with _lock:
        _sim_active.clear()

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
"""
from __future__ import annotations

import threading
from typing import Any

from . import config, state
from .tradovate import TradovateError, client


class SignalError(Exception):
    """Raised for malformed or rejected signals."""


# Per-symbol record of the active trade so management signals can find the
# stop-loss order to modify. Reset when the position is closed.
_lock = threading.Lock()
_active: dict[str, dict[str, Any]] = {}


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


async def process(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate, authorise and execute a webhook payload. Returns a summary dict."""
    s = config.load_settings()

    # --- passphrase (optional, defence in depth on top of the URL secret) -----
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

    if not s.get("trading_enabled"):
        state.log_event(
            "warn", f"Trading disabled — signal '{action}' for {root} not executed"
        )
        return {"status": "skipped", "reason": "trading_disabled", "action": action}

    contract = await client.resolve_contract(root)

    if action in ("buy", "sell"):
        return await _handle_entry(payload, action, root, contract)
    if action == "close_all":
        return await _handle_close_all(root, contract)
    if action == "move_sl":
        return await _handle_move_sl(payload, root)
    if action == "trail_active":
        state.log_event("info", f"Trailing active for {root} (handled by strategy)")
        return {"status": "ok", "action": action, "note": "acknowledged"}

    raise SignalError(f"Unknown action '{action}'")


async def _handle_entry(
    payload: dict[str, Any], action: str, root: str, contract: str
) -> dict[str, Any]:
    s = config.load_settings()
    entry_qty = int(s.get("default_qty", 3))
    tp_qty = int(s.get("tp_qty", 1))
    exit_side = _opposite(action)

    orders: list[dict[str, Any]] = []

    # 1) Market entry order.
    entry = await client.place_order(
        symbol=contract,
        action="Buy" if action == "buy" else "Sell",
        qty=entry_qty,
        order_type=s.get("entry_order_type", "Market"),
    )
    orders.append(entry)

    # 2) Take-profit limit orders — one contract each for tp1/tp2/tp3 present.
    tp_order_ids: list[int] = []
    for key in ("tp1", "tp2", "tp3"):
        if payload.get(key) is None:
            continue
        tp = await client.place_order(
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
        sl = await client.place_order(
            symbol=contract,
            action=exit_side,
            qty=entry_qty,
            order_type=s.get("sl_order_type", "Stop"),
            stop_price=float(payload["sl"]),
        )
        orders.append(sl)
        sl_order_id = sl.get("order_id")

    with _lock:
        _active[root] = {
            "contract": contract,
            "side": action,
            "qty": entry_qty,
            "sl_order_id": sl_order_id,
            "tp_order_ids": tp_order_ids,
        }

    state.log_event("info", f"Entry {action.upper()} {entry_qty} {contract} placed")
    return {
        "status": "ok",
        "action": action,
        "contract": contract,
        "orders": orders,
    }


async def _handle_close_all(root: str, contract: str) -> dict[str, Any]:
    cancelled = 0
    try:
        for order in await client.working_orders():
            try:
                await client.cancel_order(order["id"])
                cancelled += 1
            except TradovateError:
                pass
    except TradovateError as exc:
        state.log_event("warn", f"Could not list working orders: {exc}")

    await client.liquidate_position(contract)
    with _lock:
        _active.pop(root, None)

    state.log_event(
        "info", f"Closed all for {contract} ({cancelled} working orders cancelled)"
    )
    return {"status": "ok", "action": "close_all", "cancelled": cancelled}


async def _handle_move_sl(payload: dict[str, Any], root: str) -> dict[str, Any]:
    new_sl = payload.get("new_sl", payload.get("sl"))
    if new_sl is None:
        raise SignalError("move_sl signal missing 'new_sl'")

    with _lock:
        active = _active.get(root)
    if not active or not active.get("sl_order_id"):
        state.log_event("warn", f"No tracked stop-loss for {root} to move")
        return {"status": "skipped", "reason": "no_active_stop", "action": "move_sl"}

    await client.modify_order(active["sl_order_id"], stop_price=float(new_sl))
    state.log_event("info", f"Stop-loss for {root} moved to {new_sl}")
    return {"status": "ok", "action": "move_sl", "new_sl": float(new_sl)}


def active_trades() -> dict[str, Any]:
    with _lock:
        return {k: dict(v) for k, v in _active.items()}

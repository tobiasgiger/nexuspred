"""``simple`` strategy: one Market (or Limit) order per account, no TP/SL."""
from __future__ import annotations

import time

import asyncio
from typing import Any

from .. import alerts, config, state
from ..tradovate import _fire
from .common import SignalError, _lock, _trade_key
from ..sizing import account_qty


async def handle_entry(payload, action, root, target, executors, active_map, tag, webhook):
    """One Market (or Limit, if 'entry'/'price' given) order per account, sized by
    the payload's qty (or the webhook default), no TP/SL."""
    s = config.load_settings()
    default_qty = webhook.get("default_qty", 1)
    raw_qty = payload.get("qty", payload.get("contracts"))
    try:
        base_qty = float(raw_qty) if raw_qty is not None else float(default_qty)
    except (TypeError, ValueError):
        raise SignalError(f"Invalid qty '{raw_qty}'")
    if base_qty <= 0:
        raise SignalError("qty must be positive")

    entry_side = "Buy" if action == "buy" else "Sell"
    order_type = s.get("entry_order_type", "Market")
    price = payload.get("entry", payload.get("price"))

    async def place_for(ex):
        contract = await ex.resolve_contract(target)
        qty = account_qty(ex, base_qty)
        order = await ex.place_order(
            symbol=contract, action=entry_side, qty=qty,
            order_type=order_type, price=price,
        )
        info = {
            "name": ex.name, "contract": contract, "qty": qty, "entry_qty": qty,
            "sl_order_id": None, "tp_order_ids": [],
        }
        return ex.name, info, [order], contract

    results = await asyncio.gather(*(place_for(ex) for ex in executors), return_exceptions=True)

    orders: list[dict[str, Any]] = []
    acct_state: dict[str, dict[str, Any]] = {}
    summary: list[dict[str, Any]] = []
    contract = target
    for ex, res in zip(executors, results):
        if isinstance(res, Exception):
            state.log_event("error", f"{tag}Entry failed for {ex.name}: {res}")
            continue
        name, info, acc_orders, contract = res
        acct_state[name] = info
        orders.extend(acc_orders)
        summary.append({"account": name, "qty": info["qty"]})

    if acct_state:
        key = _trade_key(webhook["id"], root)
        with _lock:
            active_map[key] = {
                "webhook_id": webhook["id"], "webhook_name": webhook.get("name", ""),
                "root": root, "contract": contract, "side": action, "qty": base_qty,
                "accounts": acct_state, "ts": time.time(),
            }

    state.log_event(
        "info", f"{tag}[{webhook.get('name', '?')}] {action.upper()} {contract} on "
        f"{len(acct_state)}/{len(executors)} account(s): {', '.join(acct_state)}"
    )
    if acct_state and not tag:
        _fire(alerts.trade_executed(webhook.get("name", "?"), action, contract, list(acct_state)))   # never wait for SMTP
    return {"status": "ok", "action": action, "contract": contract,
            "accounts": summary, "orders": orders, "simulated": tag != ""}

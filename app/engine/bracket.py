"""``bracket`` strategy: market entry + TP1-3 limits + protective stop, then
``move_sl`` (break-even / trailing) and ``trail_active`` (stop resize)."""
from __future__ import annotations

import asyncio
from typing import Any

from .. import alerts, config, state
from .common import SignalError, _lock, _opposite, _tp_index_from_event, _trade_key
from ..sizing import account_qty


async def handle_entry(payload, action, root, target, executors, active_map, tag, webhook):
    s = config.load_settings()
    # Honour the signal's contract count (payload 'qty'/'contracts'); fall back to
    # the webhook default only when the signal doesn't specify one.
    default_qty = int(webhook.get("default_qty", 3))
    raw_qty = payload.get("qty", payload.get("contracts"))
    try:
        base_qty = int(float(raw_qty)) if raw_qty is not None else default_qty
    except (TypeError, ValueError):
        base_qty = default_qty
    if base_qty <= 0:
        base_qty = default_qty
    base_tp_qty = int(webhook.get("tp_qty", 1))
    entry_side = "Buy" if action == "buy" else "Sell"
    exit_side = _opposite(action)
    sl_type = s.get("sl_order_type", "Stop")

    async def place_for(ex):
        contract = await ex.resolve_contract(target)
        entry_qty = account_qty(ex, base_qty)
        tp_qty = min(entry_qty, account_qty(ex, base_tp_qty, of_entry=base_qty))

        # 1) Market entry first (so the position exists before the brackets).
        entry = await ex.place_order(
            symbol=contract, action=entry_side, qty=entry_qty,
            order_type=s.get("entry_order_type", "Market"), price=payload.get("entry"),
        )
        acc_orders = [entry]

        # 2) TP limit orders + protective stop, placed in parallel.
        bracket: list[tuple[str, Any]] = []
        remaining = entry_qty                       # the TP slices together never exceed the entry
        for key in ("tp1", "tp2", "tp3"):
            if payload.get(key) is not None and remaining > 0:
                slice_qty = min(tp_qty, remaining)
                remaining -= slice_qty
                bracket.append(("tp", ex.place_order(
                    symbol=contract, action=exit_side, qty=slice_qty,
                    order_type=s.get("tp_order_type", "Limit"), price=float(payload[key]))))
        if payload.get("sl") is not None:
            bracket.append(("sl", ex.place_order(
                symbol=contract, action=exit_side, qty=entry_qty,
                order_type=sl_type, stop_price=float(payload["sl"]))))

        tp_ids: list[int] = []
        sl_id = None
        if bracket:
            kinds = [k for k, _ in bracket]
            results = await asyncio.gather(*(c for _, c in bracket), return_exceptions=True)
            for kind, res in zip(kinds, results):
                if isinstance(res, Exception):
                    state.log_event("warn", f"{tag}{kind} order failed for {ex.name}: {res}")
                    continue
                acc_orders.append(res)
                if kind == "tp" and res.get("order_id"):
                    tp_ids.append(res["order_id"])
                elif kind == "sl":
                    sl_id = res.get("order_id")

        info = {
            "name": ex.name, "contract": contract, "entry_qty": entry_qty,
            "tp_qty": tp_qty, "qty": entry_qty, "entry_price": payload.get("entry"),
            "sl_order_id": sl_id, "sl_type": sl_type,
            "sl_stop": float(payload["sl"]) if payload.get("sl") is not None else None,
            "tp_order_ids": tp_ids,
        }
        return ex.name, info, acc_orders, contract

    # All enabled accounts execute simultaneously.
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
        summary.append({"account": name, "qty": info["entry_qty"]})

    if acct_state:
        key = _trade_key(webhook["id"], root)
        with _lock:
            active_map[key] = {
                "webhook_id": webhook["id"], "webhook_name": webhook.get("name", ""),
                "root": root, "contract": contract, "side": action, "qty": base_qty,
                "accounts": acct_state,
            }

    state.log_event(
        "info", f"{tag}[{webhook.get('name', '?')}] Entry {action.upper()} {contract} "
        f"placed on {len(acct_state)}/{len(executors)} account(s): {', '.join(acct_state)}"
    )
    if acct_state and not tag:
        await alerts.trade_executed(webhook.get("name", "?"), action, contract, list(acct_state))
    return {"status": "ok", "action": action, "contract": contract,
            "accounts": summary, "orders": orders, "simulated": tag != ""}


def _remaining_qty(info: dict[str, Any], tp_index: int | None) -> int:
    """Position left after ``tp_index`` take-profits filled (1 contract each by default)."""
    if tp_index is None:
        return int(info.get("qty") or info.get("entry_qty", 1))
    return max(1, int(info["entry_qty"]) - tp_index * int(info["tp_qty"]))


def _is_breakeven_move(payload: dict[str, Any], tp_index: int | None) -> bool:
    """A move_sl that means 'go to break-even' (TP1, or a breakeven message)."""
    msg = str(payload.get("message", "")).lower()
    return tp_index == 1 or "breakeven" in msg or "break-even" in msg


async def handle_move_sl(payload, root, executors, active_map, tag, webhook):
    s = config.load_settings()
    new_sl = payload.get("new_sl", payload.get("sl"))

    key = _trade_key(webhook["id"], root)
    with _lock:
        active = active_map.get(key)
    if not active or not active.get("accounts"):
        state.log_event("warn", f"{tag}No tracked stop-loss for {root} to move")
        return {"status": "skipped", "reason": "no_active_stop", "action": "move_sl"}

    tp_index = _tp_index_from_event(payload)   # e.g. tp1_hit -> 1 contract gone -> qty 2
    # Break-even = the original entry price (configurable); trailing moves use new_sl.
    use_entry = bool(s.get("breakeven_to_entry", True)) and _is_breakeven_move(payload, tp_index)
    if not use_entry and new_sl is None:
        raise SignalError("move_sl signal missing 'new_sl'")

    async def move_account(ex):
        info = active["accounts"].get(ex.name)
        if not info or not info.get("sl_order_id"):
            return None
        entry_price = info.get("entry_price")
        if use_entry and entry_price is not None:
            stop = float(entry_price)
        elif new_sl is not None:
            stop = float(new_sl)
        else:
            state.log_event("warn", f"{tag}move_sl for {root}: no stop price available")
            return None
        qty = _remaining_qty(info, tp_index)
        info["qty"] = qty
        info["sl_stop"] = stop
        await ex.modify_order(
            info["sl_order_id"], qty=qty,
            order_type=info.get("sl_type", "Stop"), stop_price=stop,
        )
        return stop

    results = await asyncio.gather(
        *(move_account(ex) for ex in executors), return_exceptions=True
    )
    stops = [r for r in results if isinstance(r, (int, float))]
    moved = len(stops)
    last_stop = stops[-1] if stops else None
    for r in results:
        if isinstance(r, Exception):
            state.log_event("warn", f"{tag}move_sl modify failed for {root}: {r}")

    where = "break-even/entry" if use_entry else "new_sl"
    state.log_event(
        "info", f"{tag}Stop-loss for {root} moved to {last_stop} ({where}, "
        f"qty→remaining) on {moved} account(s)"
    )
    return {"status": "ok", "action": "move_sl", "new_sl": last_stop,
            "breakeven_to_entry": use_entry, "accounts": moved, "simulated": tag != ""}


async def handle_trail_active(payload, root, executors, active_map, tag, webhook):
    """TP2 (trail_active): resize the stop to the remaining position; price unchanged."""
    key = _trade_key(webhook["id"], root)
    with _lock:
        active = active_map.get(key)
    tp_index = _tp_index_from_event(payload)
    if not active or not active.get("accounts") or tp_index is None:
        state.log_event("info", f"{tag}Trailing active for {root} (handled by strategy)")
        return {"status": "ok", "action": "trail_active", "note": "acknowledged",
                "simulated": tag != ""}

    async def resize_account(ex) -> bool:
        info = active["accounts"].get(ex.name)
        if not info or not info.get("sl_order_id"):
            return False
        qty = _remaining_qty(info, tp_index)
        info["qty"] = qty
        await ex.modify_order(
            info["sl_order_id"], qty=qty,
            order_type=info.get("sl_type", "Stop"), stop_price=info.get("sl_stop"),
        )
        return True

    results = await asyncio.gather(
        *(resize_account(ex) for ex in executors), return_exceptions=True
    )
    resized = sum(1 for r in results if r is True)

    state.log_event(
        "info", f"{tag}Trailing active for {root} — stop-loss qty→remaining "
        f"on {resized} account(s)"
    )
    return {"status": "ok", "action": "trail_active", "accounts": resized,
            "simulated": tag != ""}

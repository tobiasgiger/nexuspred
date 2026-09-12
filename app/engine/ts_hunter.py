"""``ts_hunter`` strategy: entry sized from ``risk.value`` with a protective
stop, then ``partial_close_percent`` slices and a final ``full_close`` — all
correlated by the payload's ``trade_id``.

Tracked in the same active-trade maps as bracket/simple, but keyed by
``trade_id`` (not webhook+symbol), so several concurrent trades on one symbol
never collide and the Monitor's Active Trades table shows both kinds for free.
"""
from __future__ import annotations

import time

import asyncio
from typing import Any

from .. import alerts, config, state
from ..tradovate import TradovateError, _fire
from .common import _close_contract, _close_untracked, _place_stop_with_retry, SignalError, _cancel_working, _lock, _opposite
from ..sizing import account_qty


async def handle_entry(payload, side, root, target, trade_id, executors, active_map, tag, webhook, *, settings=None):
    s = settings if settings is not None else config.load_settings()
    risk = payload.get("risk") or {}
    try:
        base_qty = float(risk.get("value"))
    except (TypeError, ValueError):
        raise SignalError("Payload missing numeric 'risk.value' (contract qty)")
    if base_qty <= 0:
        raise SignalError("'risk.value' must be positive")

    sl_price = (payload.get("sl") or {}).get("value")
    entry_price_ref = (payload.get("tv") or {}).get("entry_price")

    entry_side = "Buy" if side == "buy" else "Sell"
    exit_side = _opposite(side)
    sl_type = s.get("sl_order_type", "Stop")

    async def place_for(ex):
        contract = await ex.resolve_contract(target)
        qty = account_qty(ex, base_qty)

        # Entry first (always Market — the TS-Hunter contract is market-only).
        entry = await ex.place_order(
            symbol=contract, action=entry_side, qty=qty, order_type="Market",
        )
        acc_orders = [entry]

        sl_id = None
        if sl_price is not None:
            # the entry is live: a failed stop is retried and, if it still fails,
            # alerted — the account stays tracked so a later full_close reaches it
            sl = await _place_stop_with_retry(ex, symbol=contract, action=exit_side, qty=qty,
                                              order_type=sl_type, stop_price=float(sl_price), tag=tag)
            if sl is not None:
                acc_orders.append(sl)
                sl_id = sl.get("order_id")

        info = {
            "name": ex.name, "contract": contract, "qty": qty, "entry_qty": qty,
            "remaining_qty": qty, "sl_order_id": sl_id, "sl_type": sl_type,
            "sl_stop": float(sl_price) if sl_price is not None else None,
            "entry_price": float(entry_price_ref) if entry_price_ref is not None else None,
            "tp_order_ids": [],
        }
        return ex.name, info, acc_orders, contract

    results = await asyncio.gather(*(place_for(ex) for ex in executors), return_exceptions=True)

    orders: list[dict[str, Any]] = []
    acct_state: dict[str, dict[str, Any]] = {}
    summary: list[dict[str, Any]] = []
    contract = target
    for ex, res in zip(executors, results):
        if isinstance(res, Exception):
            state.log_event("error", f"{tag}TS-Hunter entry failed for {ex.name}: {res}")
            continue
        name, info, acc_orders, contract = res
        acct_state[name] = info
        orders.extend(acc_orders)
        summary.append({"account": name, "qty": info["qty"]})

    if acct_state:
        with _lock:
            active_map[trade_id] = {
                "webhook_id": webhook["id"], "webhook_name": webhook.get("name", ""),
                "root": root, "contract": contract, "side": side, "trade_id": trade_id,
                "accounts": acct_state, "ts": time.time(),
            }

    state.log_event(
        "info", f"{tag}[{webhook.get('name', '?')}] TS-Hunter {side.upper()} {contract} "
        f"(trade {trade_id}) on {len(acct_state)}/{len(executors)} account(s): {', '.join(acct_state)}"
    )
    if acct_state and not tag:
        _fire(alerts.trade_executed(webhook.get("name", "?"), side, contract, list(acct_state), settings=s))   # never wait for SMTP
    return {"status": "ok", "action": "signal", "contract": contract, "trade_id": trade_id,
            "accounts": summary, "orders": orders, "simulated": tag != ""}


async def handle_partial_close(payload, trade_id, executors, active_map, tag):
    with _lock:
        active = active_map.get(trade_id)
    if not active or not active.get("accounts"):
        state.log_event("warn", f"{tag}TS-Hunter: no tracked trade '{trade_id}' for partial close")
        return {"status": "skipped", "reason": "no_active_trade", "action": "partial_close_percent"}

    try:
        percent = float(payload.get("percent"))
    except (TypeError, ValueError):
        raise SignalError("partial_close_percent payload missing numeric 'percent'")
    if not (0 < percent <= 100):
        raise SignalError("'percent' must be between 0 and 100")

    stage = str(payload.get("lifecycle_stage") or payload.get("lifecycleStage") or "").upper().strip()

    exit_side = _opposite(active["side"])
    by_name = {ex.name: ex for ex in executors}

    async def close_for(name):
        info = active["accounts"][name]
        ex = by_name.get(name)
        if ex is None:
            state.log_event(
                "warn", f"{tag}TS-Hunter: account '{name}' no longer enabled, "
                "skipping partial close for it"
            )
            return None
        remaining = int(info.get("remaining_qty") or 0)
        if remaining <= 0:
            return None
        qty_to_close = max(1, min(remaining, round(remaining * percent / 100)))
        order = await ex.place_order(
            symbol=info["contract"], action=exit_side, qty=qty_to_close, order_type="Market",
        )

        new_remaining = remaining - qty_to_close
        # the close went through: the position is smaller — record that first,
        # then bring the stop in line; a failed stop change is reported loudly
        info["remaining_qty"] = new_remaining
        info["qty"] = new_remaining

        if info.get("sl_order_id"):
            if new_remaining > 0:
                try:
                    await ex.modify_order(
                        info["sl_order_id"], qty=new_remaining,
                        order_type=info.get("sl_type", "Stop"), stop_price=info.get("sl_stop"),
                    )
                except TradovateError as exc:
                    state.log_event("error", f"{tag}{ex.name}: stop could not be resized to {new_remaining} after the partial close: {exc} — it still covers {remaining}")
            else:
                try:
                    await ex.cancel_order(info["sl_order_id"])
                    info["sl_order_id"] = None
                except TradovateError as exc:
                    state.log_event("error", f"{tag}{ex.name}: stop could not be retired after the position closed: {exc} — cancel it by hand")

        return order

    names = list(active["accounts"])
    results = await asyncio.gather(*(close_for(n) for n in names), return_exceptions=True)

    orders: list[dict[str, Any]] = []
    closed_accounts: list[str] = []
    for name, r in zip(names, results):
        if isinstance(r, Exception):
            state.log_event("warn", f"{tag}TS-Hunter partial close failed for {name}: {r}")
            continue
        if r is not None:
            orders.append(r)
            closed_accounts.append(name)

    state.log_event(
        "info", f"{tag}TS-Hunter {stage or 'partial close'} for trade {trade_id}: "
        f"{percent:.2f}% of remaining closed on {len(closed_accounts)} account(s)"
    )
    return {"status": "ok", "action": "partial_close_percent", "lifecycle_stage": stage,
            "trade_id": trade_id, "accounts": closed_accounts, "orders": orders,
            "simulated": tag != ""}


async def handle_full_close(payload, trade_id, target, executors, active_map, tag):
    with _lock:
        active = active_map.get(trade_id)

    by_name = {ex.name: ex for ex in executors}
    if active and active.get("accounts"):
        targets = [(by_name[n], active["accounts"][n].get("contract", target))
                   for n in active["accounts"] if n in by_name]
        for n in active["accounts"]:
            if n not in by_name:
                state.log_event(
                    "warn", f"{tag}TS-Hunter: account '{n}' no longer enabled, "
                    "skipping full close for it"
                )
    else:
        # Untracked trade (e.g. the bridge restarted) — fall back to flattening
        # every currently-enabled account for this symbol, same safety net as
        # the simple/bracket close_all.
        targets = [(ex, target) for ex in executors]

    async def close_account(ex, contract) -> int:
        return await _close_contract(ex, tag, contract)

    results = await asyncio.gather(
        *(close_account(ex, c) for ex, c in targets), return_exceptions=True
    )
    cancelled = sum(r for r in results if isinstance(r, int))
    failed = [ex.name for (ex, _), r in zip(targets, results) if isinstance(r, Exception)]
    succeeded = [ex.name for (ex, _), r in zip(targets, results) if isinstance(r, int)]
    # enabled accounts the record does not list are closed too when they hold the contract
    tracked_now = {ex.name for ex, _ in targets}
    extra_closed, extra_failed = await _close_untracked(executors, tracked_now, tag, target) if (active and active.get("accounts")) else ([], [])
    for (ex, _), r in zip(targets, results):
        if isinstance(r, Exception):
            state.log_event("error", f"{tag}TS-Hunter full_close FAILED for {ex.name}: {r} — the position may still be open")

    # On a mixed broker outcome, remove only accounts whose close was confirmed;
    # failed accounts remain tracked so a retry cannot forget a live position or
    # re-flatten accounts that already succeeded. With no broker failure, preserve
    # the existing all-success tracking semantics (including disabled accounts).
    with _lock:
        cur = active_map.get(trade_id)
        if cur and cur.get("accounts"):
            if failed:
                accounts = cur["accounts"]
                for name in succeeded:
                    accounts.pop(name, None)
                if not accounts:
                    active_map.pop(trade_id, None)
            else:
                active_map.pop(trade_id, None)

    reason = payload.get("reason", "")
    suffix = f": {reason}" if reason else ""
    state.log_event(
        "info", f"{tag}TS-Hunter full_close for trade {trade_id} on {len(targets) + len(extra_closed)} "
        f"account(s) ({cancelled} working orders cancelled){suffix}"
        + (f"; untracked position closed on {', '.join(extra_closed)}" if extra_closed else "")
    )
    return {"status": "ok", "action": "full_close", "trade_id": trade_id,
            "accounts": len(targets) + len(extra_closed), "cancelled": cancelled, "failed": failed + extra_failed, "simulated": tag != ""}

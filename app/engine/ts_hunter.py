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
from .common import OrdersLeftWorking, _close_contract, _place_stop_with_retry, SignalError, _lock, _opposite
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

    with _lock:
        existing = active_map.get(trade_id)
    if existing and existing.get("accounts"):
        state.log_event("warn", f"{tag}[{webhook.get('name', '?')}] TS-Hunter trade {trade_id} ignored — an active trade with that id is already tracked")
        return {"status": "skipped", "reason": "active_trade_exists", "action": "signal", "trade_id": trade_id}

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
            # the entry is closed again (StopFailed → this account is not tracked)
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
    """Close **this trade**: on every tracked account its own stop is cancelled
    and its remaining quantity is closed at market — positions of other trades
    (or manual ones) in the same contract stay. Accounts the record does not
    list are never touched; when they hold the contract that is reported.
    An untracked trade (the bridge restarted) or a record without a quantity
    falls back to flattening the contract on the routed accounts."""
    with _lock:
        active = active_map.get(trade_id)

    by_name = {ex.name: ex for ex in executors}
    tracked = bool(active and active.get("accounts"))
    exit_side = _opposite(active["side"]) if tracked and active.get("side") in ("buy", "sell") else None
    if tracked:
        targets = [(by_name[n], active["accounts"][n]) for n in active["accounts"] if n in by_name]
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
        targets = [(ex, {"contract": target}) for ex in executors]

    async def close_account(ex, info) -> int:
        contract = info.get("contract") or target
        remaining = info.get("remaining_qty", info.get("qty"))
        if not tracked or exit_side is None or remaining is None:
            return await _close_contract(ex, tag, contract)           # nothing to isolate on
        remaining = int(remaining or 0)
        errors: list[str] = []
        cancelled = 0
        sid = info.get("sl_order_id")
        if sid:
            try:
                await ex.cancel_order(sid)
                cancelled = 1
            except TradovateError as exc:
                errors.append(f"cancel {sid}: {exc}")
        if remaining > 0:
            await ex.place_order(symbol=contract, action=exit_side, qty=remaining, order_type="Market")
        if errors and sid:
            try:                                                      # a stop left working on a flat trade would open a new one
                await ex.cancel_order(sid)
                cancelled, errors = 1, []
            except TradovateError as exc:
                errors = [f"cancel {sid}: {exc}"]
        if errors:
            detail = "; ".join(errors)
            state.log_event("error", f"{tag}{ex.name}: the trade's stop on {contract} could not be cancelled after the close: {detail} — cancel it by hand")
            _fire(alerts.execution_problem(f"Orders left working on {ex.name}",
                                           f"{contract}: trade {trade_id} was closed but its stop could not be cancelled: {detail[:300]}"))
            raise OrdersLeftWorking(f"stop remains after closing trade {trade_id} on {contract}: {detail}")
        return cancelled

    results = await asyncio.gather(
        *(close_account(ex, info) for ex, info in targets), return_exceptions=True
    )
    cancelled = sum(r for r in results if isinstance(r, int))
    failed = [ex.name for (ex, _), r in zip(targets, results) if isinstance(r, Exception)]
    succeeded = [ex.name for (ex, _), r in zip(targets, results) if isinstance(r, int)]
    # enabled accounts the record does not list are left alone — reported when they hold the contract
    tracked_now = {ex.name for ex, _ in targets}
    untracked = await _report_untracked(executors, tracked_now, tag, target, trade_id) if tracked else []
    for (ex, _), r in zip(targets, results):
        if isinstance(r, Exception):
            state.log_event("error", f"{tag}TS-Hunter full_close FAILED for {ex.name}: {r} — "
                                     + ("the position is closed but its orders are not: cancel them by hand" if isinstance(r, OrdersLeftWorking) else "the position may still be open"))

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
        "info", f"{tag}TS-Hunter full_close for trade {trade_id} on {len(targets)} "
        f"account(s) ({cancelled} working orders cancelled){suffix}"
        + (f"; left alone: untracked position on {', '.join(untracked)}" if untracked else "")
    )
    return {"status": "error" if failed else "ok", "action": "full_close", "trade_id": trade_id,
            "accounts": len(targets), "cancelled": cancelled,
            "failed": failed, "untracked": untracked, "simulated": tag != ""}


async def _report_untracked(executors, tracked_names, tag, target, trade_id) -> list[str]:
    """Accounts routed to the webhook but absent from the trade record that hold
    the contract: a lost entry answer, a manual position or another trade. An
    isolated full_close never closes them — it says so, once per close."""
    async def one(ex) -> bool:
        contract = await ex.resolve_contract(target)
        rows = await ex.positions()
        return any(str(p.get("symbol") or "") == contract and (p.get("netPos") or 0) for p in rows or [])

    extra = [ex for ex in executors if ex.name not in tracked_names]
    results = await asyncio.gather(*(one(ex) for ex in extra), return_exceptions=True)
    holding = [ex.name for ex, r in zip(extra, results) if r is True]
    if holding:
        state.log_event("warn", f"{tag}{', '.join(holding)} hold(s) {target} without a record of trade {trade_id} "
                                "(lost entry answer, manual position or another trade) — left open by the isolated full_close")
        _fire(alerts.execution_problem("Untracked position left open",
                                       f"{target}: full_close of trade {trade_id} closed only its own quantity; {', '.join(holding)} still hold(s) a position. Close it by hand if it belongs to this trade."))
    return holding

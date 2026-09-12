"""Strategy-agnostic position management: ``close_all`` and ``set_sl_tp``."""
from __future__ import annotations

import asyncio
import time
from typing import Any

from .. import config, state
from ..tradovate import TradovateError
from .common import _cancel_working, _close_contract, _close_untracked, _lock, _trade_key


async def handle_close_all(root, target, executors, active_map, tag, webhook):
    key = _trade_key(webhook["id"], root)
    with _lock:
        tracked = active_map.get(key)
        tracked_names = set((tracked or {}).get("accounts") or {})

    # For a partially failed retry, only target accounts that are still tracked.
    # Without tracking (for example after a restart), keep the existing safety-net
    # behaviour and flatten every currently enabled account for the symbol.
    targets = [ex for ex in executors if ex.name in tracked_names] if tracked_names else list(executors)

    async def close_account(ex) -> int:
        contract = await ex.resolve_contract(target)
        # Only this contract's working orders — other symbols keep their stops.
        return await _close_contract(ex, tag, contract)

    results = await asyncio.gather(*(close_account(ex) for ex in targets),
                                   return_exceptions=True)
    cancelled = sum(r for r in results if isinstance(r, int))
    failed = [ex.name for ex, r in zip(targets, results) if isinstance(r, Exception)]
    succeeded = [ex.name for ex, r in zip(targets, results) if isinstance(r, int)]
    # enabled accounts the record does not list are closed too when they hold the contract
    extra_closed, extra_failed = await _close_untracked(executors, {ex.name for ex in targets}, tag, target) if tracked_names else ([], [])
    for ex, r in zip(targets, results):
        if isinstance(r, Exception):
            state.log_event("error", f"{tag}close_all FAILED for {ex.name}: {r} — the position may still be open")

    # On a mixed broker outcome, remove only accounts whose close was confirmed;
    # failed accounts remain tracked so a retry cannot forget a live position or
    # re-flatten accounts that already succeeded. With no broker failure, preserve
    # the existing all-success tracking semantics.
    with _lock:
        cur = active_map.get(key)
        if cur and cur.get("accounts"):
            if failed:
                accounts = cur["accounts"]
                for name in succeeded:
                    accounts.pop(name, None)
                if not accounts:
                    active_map.pop(key, None)
            else:
                active_map.pop(key, None)

    state.log_event(
        "info", f"{tag}[{webhook.get('name', '?')}] Closed all for {root} on "
        f"{len(targets) + len(extra_closed)} account(s) ({cancelled} working orders cancelled)"
        + (f"; untracked position closed on {', '.join(extra_closed)}" if extra_closed else "")
    )
    return {"status": "ok", "action": "close_all", "accounts": len(targets) + len(extra_closed),
            "cancelled": cancelled, "failed": failed + extra_failed, "simulated": tag != ""}


async def handle_set_sl_tp(payload, root, target, executors, active_map, tag, webhook):
    """Set / replace the protective STOP and/or TARGET on the current open position
    to match a signal provider's latest stop/target.

    Unlike ``move_sl`` (which only *moves* a pre-existing tracked stop), this works
    when the entry placed no bracket yet — it looks at the live position, and:
      * places/replaces a **stop** order when the signal carries one,
      * places/replaces a **target** (limit) order when the signal carries one,
      * ignores the side that isn't present (e.g. a target-only move, stop = "—"),
      * flattens nothing — it only manages protective orders.
    Repeated moves cancel the previous order and place a fresh one.
    """
    s = config.load_settings()
    new_sl = payload.get("stop_price", payload.get("new_sl"))
    new_tp = payload.get("target_price", payload.get("tp"))
    if new_sl is None and new_tp is None:
        return {"status": "skipped", "reason": "no_sl_or_tp", "action": "set_sl_tp"}

    key = _trade_key(webhook["id"], root)
    with _lock:
        active = active_map.get(key)
    sl_type = s.get("sl_order_type", "Stop")
    tp_type = s.get("tp_order_type", "Limit")

    async def apply(ex):
        contract = await ex.resolve_contract(target)
        net = 0
        try:
            for p in await ex.positions():
                if p.get("symbol") == contract:
                    net = int(p.get("netPos") or 0)
                    break
        except TradovateError as exc:
            state.log_event("warn", f"{tag}set_sl_tp: position lookup failed for {ex.name}: {exc}")
            return None
        if net == 0:
            return None  # nothing open to protect
        qty = abs(net)
        exit_side = "Sell" if net > 0 else "Buy"

        tracked = (active or {}).get("accounts", {}).get(ex.name) or {}
        info = {**tracked, "name": ex.name, "contract": contract, "qty": qty}
        changed = False

        if new_sl is not None:
            # place the new stop first, then retire the old one: the position is
            # never without a stop in between, and a failed placement keeps the old
            old = info.get("sl_order_id")
            try:
                o = await ex.place_order(symbol=contract, action=exit_side, qty=qty,
                                         order_type=sl_type, stop_price=float(new_sl))
                info["sl_order_id"] = o.get("order_id")
                info["sl_stop"] = float(new_sl)
                changed = True
                if old:
                    try:
                        await ex.cancel_order(old)
                    except TradovateError as exc:
                        state.log_event("error", f"{tag}{ex.name}: old stop {old} could not be cancelled after the new one was placed: {exc} — two stops may be working")
            except TradovateError as exc:
                state.log_event("error", f"{tag}set stop for {ex.name} failed: {exc}" + (" (the previous stop stays)" if old else " — position without a stop"))

        if new_tp is not None:
            old_tps = list(info.get("tp_order_ids") or [])
            try:
                o = await ex.place_order(symbol=contract, action=exit_side, qty=qty,
                                         order_type=tp_type, price=float(new_tp))
                info["tp_order_ids"] = [o["order_id"]] if o.get("order_id") else []
                changed = True
                for oid in old_tps:                 # retire the previous target only once the new one works
                    try:
                        await ex.cancel_order(oid)
                    except TradovateError as exc:
                        state.log_event("error", f"{tag}{ex.name}: old target {oid} could not be cancelled after the new one was placed: {exc} — two targets may be working")
            except TradovateError as exc:
                state.log_event("error", f"{tag}set target for {ex.name} failed: {exc}" + (" (the previous target stays)" if old_tps else ""))

        return (ex.name, info, changed)

    results = await asyncio.gather(*(apply(ex) for ex in executors), return_exceptions=True)

    acct_state: dict[str, dict[str, Any]] = {}
    applied = 0
    for ex, r in zip(executors, results):
        if isinstance(r, Exception):
            state.log_event("warn", f"{tag}set_sl_tp failed for {ex.name}: {r}")
            continue
        if not r:
            continue
        name, info, changed = r
        acct_state[name] = info
        if changed:
            applied += 1

    if acct_state:
        with _lock:
            cur = active_map.get(key) or {
                "webhook_id": webhook["id"], "webhook_name": webhook.get("name", ""),
                "root": root, "contract": target, "side": (active or {}).get("side"),
                "accounts": {}, "ts": time.time(),
            }
            cur.setdefault("accounts", {}).update(acct_state)
            active_map[key] = cur

    parts = []
    if new_sl is not None:
        parts.append(f"SL {new_sl}")
    if new_tp is not None:
        parts.append(f"TP {new_tp}")
    if applied == 0:
        state.log_event("info", f"{tag}[{webhook.get('name', '?')}] {' & '.join(parts) or 'SL/TP'} "
                        f"— no open position to protect for {root}")
        return {"status": "skipped", "reason": "no_open_position", "action": "set_sl_tp"}
    state.log_event("info", f"{tag}[{webhook.get('name', '?')}] {' & '.join(parts)} set on "
                    f"{applied}/{len(executors)} account(s) for {root}")
    return {"status": "ok", "action": "set_sl_tp", "accounts": applied,
            "sl": new_sl, "tp": new_tp, "simulated": tag != ""}

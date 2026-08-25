"""Translate incoming TradingView webhook signals into Tradovate orders.

Every signal arrives through a specific **webhook** (see ``config.py`` — each
webhook has its own secret URL, its own routed trade accounts + qty
multipliers, and a ``strategy`` that decides how the payload is executed:

* ``"simple"``  -> ``action: "buy" | "sell"`` places a single Market (or Limit)
  order sized by ``qty`` in the payload (falling back to the webhook's
  ``default_qty``), scaled per account by that account's multiplier. No TP/SL
  orders — just the execution. ``close_all`` flattens the tracked position.
* ``"bracket"`` -> the original TP/SL flow: ``buy``/``sell`` opens a market
  entry (webhook's ``default_qty`` contracts) + a TP limit order per
  ``tp1``/``tp2``/``tp3`` present (webhook's ``tp_qty`` contracts each) + a
  protective stop (``sl``) covering the full position. ``close_all`` cancels
  working orders and flattens; ``move_sl`` moves the tracked stop;
  ``trail_active`` resizes the stop to the remaining position.
* ``"ts_hunter"`` -> the TS-Hunter contract (``contract_version:
  at_execution_command_v5``): a payload with ``event: "signal"`` opens a
  market entry sized from ``risk.value`` contracts with a protective stop at
  ``sl.value``; ``event: "management"`` then drives it, correlated by
  ``trade_id`` (not symbol, so several concurrent trades on the same symbol
  never collide) — ``action: "partial_close_percent"`` market-closes
  ``percent``% of whatever remains (TP1/TP2/TP3 each shave off a slice,
  leaving a runner), resizing the stop to match the new remaining quantity
  each time; ``action: "full_close"`` cancels working orders and liquidates
  whatever remains, regardless of tracked quantity.

The same logic powers the **simulator**: passing ``simulate=True`` routes orders to
an in-memory executor (a synthetic bracket webhook + account) and uses a separate
trade-tracking map, so you can rehearse a full scenario without credentials, risk,
or a configured webhook.
"""
from __future__ import annotations

import asyncio
import threading
import re
from typing import Any

from . import alerts, config, context, state
from .simulator import sim_client
from .tradovate import AccountExecutor, TradovateError, manager


class SignalError(Exception):
    """Raised for malformed or rejected signals."""


# Per-webhook, per-symbol record of the active trade so management signals can
# find the stop-loss order to modify. Keyed by "<webhook_id>:<root>" so two
# webhooks trading the same symbol never share state. Reset when the position
# is closed. Live and simulated trades are tracked separately.
_lock = threading.Lock()

# Per-trade async locks: serialise signals that touch the SAME position so two
# near-simultaneous events (e.g. two TP partial-closes) can't race on the shared
# active-trade state — otherwise both read the same "remaining qty" and one
# overwrites the other, so only one TP effectively executes. Signals for
# different trades/symbols still run in parallel.
_trade_locks: dict[str, asyncio.Lock] = {}
_trade_locks_guard = threading.Lock()


def _trade_lock(key: str) -> asyncio.Lock:
    with _trade_locks_guard:
        lk = _trade_locks.get(key)
        if lk is None:
            lk = _trade_locks[key] = asyncio.Lock()
        return lk


# Active-trade records are isolated per area (user workspace). Each maps
# "<webhook_id>:<root>" -> trade record. Live and simulated tracked separately.
_active: dict[int, dict[str, dict[str, Any]]] = {}
_sim_active: dict[int, dict[str, dict[str, Any]]] = {}


def _map_for(simulate: bool) -> dict[str, dict[str, Any]]:
    """The active-trade map for the current area (live or simulated)."""
    reg = _sim_active if simulate else _active
    aid = context.get_area()
    with _lock:
        m = reg.get(aid)
        if m is None:
            m = reg[aid] = {}
        return m


_CONTRACT_RE = re.compile(r"^([A-Z]{1,4})([FGHJKMNQUVXZ])(\d{1,2})$")
_TP_RE = re.compile(r"tp(\d)", re.IGNORECASE)


def _tp_index_from_event(payload: dict[str, Any]) -> int | None:
    """How many take-profits have filled, parsed from the event (e.g. ``tp2_hit`` → 2)."""
    m = _TP_RE.search(str(payload.get("event", "")))
    return int(m.group(1)) if m else None


def _base_root(name: str) -> str:
    """Reduce a contract/symbol to its root: ``MNQU6`` → ``MNQ``, ``MNQ1!`` → ``MNQ``."""
    m = _CONTRACT_RE.match(name)
    if m:
        return m.group(1)
    return name.replace("1!", "").strip()


def _resolve_symbol(s: dict[str, Any], tv_symbol: str) -> tuple[str, str, bool]:
    """Return (target_contract, base_root, allowed) for a TradingView symbol.

    The configured mapping (``symbol_map``) is the source of truth: if the symbol
    is mapped, that exact contract (e.g. ``MNQU6``) is traded and the signal is
    allowed. Unmapped symbols fall back to the stripped root and are gated by
    ``allowed_symbols``.
    """
    mapped = s.get("symbol_map", {}).get(tv_symbol)
    if mapped:
        return mapped, _base_root(mapped), True
    root = _base_root(tv_symbol)
    return root, root, root in s.get("allowed_symbols", [])


def _opposite(action: str) -> str:
    return "Sell" if action.lower() == "buy" else "Buy"


def _trade_key(webhook_id: str, root: str) -> str:
    return f"{webhook_id}:{root}"


def _synthetic_bracket_webhook(s: dict[str, Any]) -> dict[str, Any]:
    """A stand-in webhook used only for ``simulate=True`` calls (no real webhook
    context needed — the Simulator tab rehearses the bracket lifecycle)."""
    return {
        "id": "sim", "name": "Simulator", "strategy": "bracket",
        "default_qty": s.get("default_qty", 3), "tp_qty": s.get("tp_qty", 1),
        "accounts": [],
    }


def _webhook_executors(webhook: dict[str, Any]) -> list[Any]:
    """Executors for a webhook's enabled (login, trade account) selections."""
    out = []
    for a in webhook.get("accounts") or []:
        if not a.get("enabled"):
            continue
        ex = manager().executor_for(
            a.get("token_idx"), a.get("spec"), a.get("qty_multiplier", 1)
        )
        if ex is None:
            state.log_event(
                "warn", f"Webhook '{webhook.get('name')}': account '{a.get('spec')}' "
                "not found (deleted login/account?)"
            )
            continue
        out.append(ex)
    return out


async def process(
    payload: dict[str, Any], webhook: dict[str, Any] | None = None, *, simulate: bool = False
) -> dict[str, Any]:
    """Validate, authorise and execute a webhook payload. Returns a summary dict.

    ``webhook`` is the routing config (name/strategy/accounts) resolved by the
    caller from the URL token; required unless ``simulate`` is True, in which
    case a synthetic bracket webhook + the in-memory sim account is used.

    When ``simulate`` is True, orders are filled in memory (no Tradovate calls) and
    the live-only guards (trading switch, passphrase) are skipped.
    """
    s = config.load_settings()
    active_map = _map_for(simulate)

    if webhook is None:
        if not simulate:
            raise SignalError("No webhook context for this signal")
        webhook = _synthetic_bracket_webhook(s)

    if not simulate:
        # passphrase (optional, defence in depth on top of the URL secret)
        if s.get("webhook_passphrase"):
            if payload.get("passphrase") != s["webhook_passphrase"]:
                raise SignalError("Invalid passphrase")

    if webhook.get("strategy") == "ts_hunter":
        return await _process_ts_hunter(payload, webhook, active_map, simulate)

    action = str(payload.get("action", "")).lower().strip()
    tv_symbol = str(payload.get("symbol", "")).strip()
    if not action or not tv_symbol:
        raise SignalError("Payload missing 'action' or 'symbol'")

    target, root, allowed = _resolve_symbol(s, tv_symbol)
    if not allowed:
        raise SignalError(f"Symbol '{tv_symbol}' not mapped / not in allowed list")

    if not simulate and not s.get("trading_enabled"):
        state.log_event(
            "warn", f"Trading disabled — signal '{action}' for {root} not executed"
        )
        return {"status": "skipped", "reason": "trading_disabled", "action": action}

    executors = [sim_client] if simulate else _webhook_executors(webhook)
    if not executors:
        state.log_event(
            "warn", f"No enabled accounts on webhook '{webhook.get('name')}' — "
            f"signal '{action}' ignored"
        )
        return {"status": "skipped", "reason": "no_enabled_accounts", "action": action}

    tag = "[SIM] " if simulate else ""
    strategy = webhook.get("strategy", "simple")

    # Serialise all signals for this webhook+symbol so concurrent events (e.g. two
    # TP moves arriving together) don't race on the shared active-trade state.
    lock_key = f"{context.get_area()}:{'sim' if simulate else 'live'}:{webhook['id']}:{root}"
    async with _trade_lock(lock_key):
        if action in ("buy", "sell"):
            if strategy == "simple":
                return await _handle_simple_entry(payload, action, root, target, executors, active_map, tag, webhook)
            return await _handle_entry(payload, action, root, target, executors, active_map, tag, webhook)
        if action == "close_all":
            return await _handle_close_all(root, target, executors, active_map, tag, webhook)
        if action == "set_sl_tp":
            return await _handle_set_sl_tp(payload, root, target, executors, active_map, tag, webhook)
        if action == "move_sl":
            if strategy == "simple":
                # A 'simple' webhook has no tracked bracket to move — skip cleanly
                # (not an error) so a stop/target-move signal doesn't spam failures.
                state.log_event("info", f"{tag}move_sl ignored for {root} — 'simple' "
                                "strategy has no bracket to move")
                return {"status": "skipped", "reason": "move_sl_unsupported_simple", "action": action}
            return await _handle_move_sl(payload, root, executors, active_map, tag, webhook)
        if action == "trail_active":
            if strategy == "simple":
                state.log_event("info", f"{tag}Trailing active for {root} (no-op on 'simple' strategy)")
                return {"status": "ok", "action": action, "note": "acknowledged", "simulated": simulate}
            return await _handle_trail_active(payload, root, executors, active_map, tag, webhook)

    raise SignalError(f"Unknown action '{action}'")


async def _handle_simple_entry(payload, action, root, target, executors, active_map, tag, webhook):
    """Simple strategy: one Market (or Limit, if 'entry'/'price' given) order per
    account, sized by the payload's qty (or the webhook default), no TP/SL."""
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
        mult = getattr(ex, "qty_multiplier", 1) or 1
        qty = max(1, round(base_qty * mult))
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
                "accounts": acct_state,
            }

    state.log_event(
        "info", f"{tag}[{webhook.get('name', '?')}] {action.upper()} {contract} on "
        f"{len(acct_state)}/{len(executors)} account(s): {', '.join(acct_state)}"
    )
    if acct_state and not tag:
        await alerts.trade_executed(webhook.get("name", "?"), action, contract, list(acct_state))
    return {"status": "ok", "action": action, "contract": contract,
            "accounts": summary, "orders": orders, "simulated": tag != ""}


async def _handle_entry(payload, action, root, target, executors, active_map, tag, webhook):
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
        mult = getattr(ex, "qty_multiplier", 1) or 1
        entry_qty = max(1, int(base_qty * mult))
        tp_qty = max(1, int(base_tp_qty * mult))

        # 1) Market entry first (so the position exists before the brackets).
        entry = await ex.place_order(
            symbol=contract, action=entry_side, qty=entry_qty,
            order_type=s.get("entry_order_type", "Market"), price=payload.get("entry"),
        )
        acc_orders = [entry]

        # 2) TP limit orders + protective stop, placed in parallel.
        bracket: list[tuple[str, Any]] = []
        for key in ("tp1", "tp2", "tp3"):
            if payload.get(key) is not None:
                bracket.append(("tp", ex.place_order(
                    symbol=contract, action=exit_side, qty=tp_qty,
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


async def _handle_close_all(root, target, executors, active_map, tag, webhook):
    async def close_account(ex) -> int:
        cancelled = 0
        contract = await ex.resolve_contract(target)
        try:
            for order in await ex.working_orders():
                try:
                    await ex.cancel_order(order["id"])
                    cancelled += 1
                except TradovateError:
                    pass
        except TradovateError as exc:
            state.log_event("warn", f"{tag}Could not list working orders for {ex.name}: {exc}")
        await ex.liquidate_position(contract)
        return cancelled

    # Flatten every enabled account in parallel.
    results = await asyncio.gather(*(close_account(ex) for ex in executors),
                                   return_exceptions=True)
    cancelled = sum(r for r in results if isinstance(r, int))
    for ex, r in zip(executors, results):
        if isinstance(r, Exception):
            state.log_event("warn", f"{tag}close_all failed for {ex.name}: {r}")

    key = _trade_key(webhook["id"], root)
    with _lock:
        active_map.pop(key, None)

    state.log_event(
        "info", f"{tag}[{webhook.get('name', '?')}] Closed all for {root} on "
        f"{len(executors)} account(s) ({cancelled} working orders cancelled)"
    )
    return {"status": "ok", "action": "close_all", "accounts": len(executors),
            "cancelled": cancelled, "simulated": tag != ""}


async def flatten_all() -> dict[str, Any]:
    """EMERGENCY kill-switch: cancel every working order and flatten every open
    position on **all** trade accounts under **all** enabled logins in the current
    area — regardless of per-account execution toggles or webhook routing.

    Independent of the Trading switch: an emergency flatten must work even when
    trading is paused. Never raises; returns a summary of what it did.
    """
    mgr = manager()
    mgr.reload()
    executors: list[AccountExecutor] = []
    for s in mgr.all():
        if not s.enabled:
            continue
        for a in s.accounts:
            executors.append(AccountExecutor(s, a))

    if not executors:
        state.log_event("warn", "🆘 SOS flatten-all: no trade accounts found")
        return {"status": "ok", "accounts": 0, "cancelled": 0, "flattened": 0, "errors": []}

    async def flatten(ex: AccountExecutor) -> tuple[int, int, list[str]]:
        cancelled = 0
        flattened = 0
        errors: list[str] = []
        # 1) Cancel every working order first (so stops/targets don't re-fill).
        try:
            for order in await ex.working_orders():
                oid = order.get("id")
                if oid is None:
                    continue
                try:
                    await ex.cancel_order(oid)
                    cancelled += 1
                except TradovateError as exc:
                    errors.append(f"cancel {oid}: {exc}")
        except TradovateError as exc:
            errors.append(f"list orders: {exc}")
        # 2) Flatten every open position (any symbol) on this account.
        try:
            for pos in await ex.positions():
                sym = pos.get("symbol")
                if not sym:
                    continue
                try:
                    await ex.liquidate_position(sym)
                    flattened += 1
                except TradovateError as exc:
                    errors.append(f"flatten {sym}: {exc}")
        except TradovateError as exc:
            errors.append(f"list positions: {exc}")
        return cancelled, flattened, errors

    results = await asyncio.gather(*(flatten(ex) for ex in executors), return_exceptions=True)

    cancelled = flattened = 0
    all_errors: list[str] = []
    for ex, r in zip(executors, results):
        if isinstance(r, Exception):
            all_errors.append(f"{ex.name}: {r}")
            state.log_event("error", f"🆘 SOS flatten failed for {ex.name}: {r}")
            continue
        c, f, errs = r
        cancelled += c
        flattened += f
        all_errors += [f"{ex.name}: {e}" for e in errs]

    state.log_event(
        "warn",
        f"🆘 SOS flatten-all: {flattened} position(s) flattened, {cancelled} order(s) "
        f"cancelled across {len(executors)} account(s)"
        + (f"; {len(all_errors)} error(s)" if all_errors else ""),
    )
    return {"status": "ok", "accounts": len(executors), "cancelled": cancelled,
            "flattened": flattened, "errors": all_errors}


async def _handle_set_sl_tp(payload, root, target, executors, active_map, tag, webhook):
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
            old = info.get("sl_order_id")
            if old:
                try:
                    await ex.cancel_order(old)
                except TradovateError:
                    pass
            try:
                o = await ex.place_order(symbol=contract, action=exit_side, qty=qty,
                                         order_type=sl_type, stop_price=float(new_sl))
                info["sl_order_id"] = o.get("order_id")
                info["sl_stop"] = float(new_sl)
                changed = True
            except TradovateError as exc:
                state.log_event("warn", f"{tag}set stop for {ex.name} failed: {exc}")

        if new_tp is not None:
            for oid in info.get("tp_order_ids") or []:
                try:
                    await ex.cancel_order(oid)
                except TradovateError:
                    pass
            try:
                o = await ex.place_order(symbol=contract, action=exit_side, qty=qty,
                                         order_type=tp_type, price=float(new_tp))
                info["tp_order_ids"] = [o["order_id"]] if o.get("order_id") else []
                changed = True
            except TradovateError as exc:
                state.log_event("warn", f"{tag}set target for {ex.name} failed: {exc}")

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
                "accounts": {},
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


def _remaining_qty(info: dict[str, Any], tp_index: int | None) -> int:
    """Position left after ``tp_index`` take-profits filled (1 contract each by default)."""
    if tp_index is None:
        return int(info.get("qty") or info.get("entry_qty", 1))
    return max(1, int(info["entry_qty"]) - tp_index * int(info["tp_qty"]))


def _is_breakeven_move(payload: dict[str, Any], tp_index: int | None) -> bool:
    """A move_sl that means 'go to break-even' (TP1, or a breakeven message)."""
    msg = str(payload.get("message", "")).lower()
    return tp_index == 1 or "breakeven" in msg or "break-even" in msg


async def _handle_move_sl(payload, root, executors, active_map, tag, webhook):
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


async def _handle_trail_active(payload, root, executors, active_map, tag, webhook):
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


# ======================================================================= TS-Hunter
# Tracked separately from bracket/simple: keyed by the payload's own
# ``trade_id`` (shares the same _active/_sim_active dicts and _lock — the key
# scheme just differs, so several concurrent trades on one symbol never
# collide and the Monitor's Active Trades table shows both kinds for free).

async def _process_ts_hunter(payload, webhook, active_map, simulate):
    s = config.load_settings()
    tag = "[SIM] " if simulate else ""

    event = str(payload.get("event", "")).lower().strip()
    trade_id = str(payload.get("trade_id", "")).strip()
    if not trade_id:
        raise SignalError("Payload missing 'trade_id'")

    tv_symbol = str(payload.get("symbol") or payload.get("pair") or "").strip()
    if not tv_symbol:
        raise SignalError("Payload missing 'symbol'")

    target, root, allowed = _resolve_symbol(s, tv_symbol)
    if not allowed:
        raise SignalError(f"Symbol '{tv_symbol}' not mapped / not in allowed list")

    side = str(payload.get("side") or payload.get("direction") or "").lower().strip()
    if event == "signal" and side not in ("buy", "sell"):
        raise SignalError(f"Invalid/missing side '{side}'")

    if not simulate and not s.get("trading_enabled"):
        state.log_event(
            "warn", f"Trading disabled — TS-Hunter signal for {root} (trade {trade_id}) not executed"
        )
        return {"status": "skipped", "reason": "trading_disabled"}

    executors = [sim_client] if simulate else _webhook_executors(webhook)
    if not executors:
        state.log_event(
            "warn", f"No enabled accounts on webhook '{webhook.get('name')}' — "
            f"TS-Hunter signal ignored"
        )
        return {"status": "skipped", "reason": "no_enabled_accounts"}

    # Serialise all events for this trade_id so two TP/management signals arriving
    # together can't race on the trade's shared remaining-qty state.
    lock_key = f"{context.get_area()}:{'sim' if simulate else 'live'}:ts:{trade_id}"
    async with _trade_lock(lock_key):
        if event == "signal":
            return await _handle_ts_hunter_entry(
                payload, side, root, target, trade_id, executors, active_map, tag, webhook
            )
        if event == "management":
            mgmt_action = str(payload.get("action", "")).lower().strip()
            if mgmt_action == "partial_close_percent":
                return await _handle_ts_hunter_partial_close(
                    payload, trade_id, executors, active_map, tag
                )
            if mgmt_action == "full_close":
                return await _handle_ts_hunter_full_close(
                    payload, trade_id, target, executors, active_map, tag
                )
            raise SignalError(f"Unknown TS-Hunter management action '{mgmt_action}'")

    raise SignalError(f"Unknown TS-Hunter event '{event}'")


async def _handle_ts_hunter_entry(payload, side, root, target, trade_id, executors, active_map, tag, webhook):
    s = config.load_settings()
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
        mult = getattr(ex, "qty_multiplier", 1) or 1
        qty = max(1, round(base_qty * mult))

        # Entry first (always Market — the TS-Hunter contract is market-only).
        entry = await ex.place_order(
            symbol=contract, action=entry_side, qty=qty, order_type="Market",
        )
        acc_orders = [entry]

        sl_id = None
        if sl_price is not None:
            sl = await ex.place_order(
                symbol=contract, action=exit_side, qty=qty,
                order_type=sl_type, stop_price=float(sl_price),
            )
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
                "accounts": acct_state,
            }

    state.log_event(
        "info", f"{tag}[{webhook.get('name', '?')}] TS-Hunter {side.upper()} {contract} "
        f"(trade {trade_id}) on {len(acct_state)}/{len(executors)} account(s): {', '.join(acct_state)}"
    )
    if acct_state and not tag:
        await alerts.trade_executed(webhook.get("name", "?"), side, contract, list(acct_state))
    return {"status": "ok", "action": "signal", "contract": contract, "trade_id": trade_id,
            "accounts": summary, "orders": orders, "simulated": tag != ""}


async def _handle_ts_hunter_partial_close(payload, trade_id, executors, active_map, tag):
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
        info["remaining_qty"] = new_remaining
        info["qty"] = new_remaining

        if info.get("sl_order_id"):
            if new_remaining > 0:
                await ex.modify_order(
                    info["sl_order_id"], qty=new_remaining,
                    order_type=info.get("sl_type", "Stop"), stop_price=info.get("sl_stop"),
                )
            else:
                try:
                    await ex.cancel_order(info["sl_order_id"])
                except TradovateError:
                    pass
                info["sl_order_id"] = None

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


async def _handle_ts_hunter_full_close(payload, trade_id, target, executors, active_map, tag):
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
        cancelled = 0
        try:
            for order in await ex.working_orders():
                try:
                    await ex.cancel_order(order["id"])
                    cancelled += 1
                except TradovateError:
                    pass
        except TradovateError as exc:
            state.log_event("warn", f"{tag}Could not list working orders for {ex.name}: {exc}")
        await ex.liquidate_position(contract)
        return cancelled

    results = await asyncio.gather(
        *(close_account(ex, c) for ex, c in targets), return_exceptions=True
    )
    cancelled = sum(r for r in results if isinstance(r, int))
    for (ex, _), r in zip(targets, results):
        if isinstance(r, Exception):
            state.log_event("warn", f"{tag}TS-Hunter full_close failed for {ex.name}: {r}")

    with _lock:
        active_map.pop(trade_id, None)

    reason = payload.get("reason", "")
    suffix = f": {reason}" if reason else ""
    state.log_event(
        "info", f"{tag}TS-Hunter full_close for trade {trade_id} on {len(targets)} "
        f"account(s) ({cancelled} working orders cancelled){suffix}"
    )
    return {"status": "ok", "action": "full_close", "trade_id": trade_id,
            "accounts": len(targets), "cancelled": cancelled, "simulated": tag != ""}


def active_trades(simulate: bool = False) -> dict[str, Any]:
    src = _map_for(simulate)
    with _lock:
        return {k: dict(v) for k, v in src.items()}


def reset_simulation() -> None:
    """Clear simulated positions, working orders and tracked trades (this area)."""
    sim_client.reset()
    _map_for(True).clear()

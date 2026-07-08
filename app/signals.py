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

from . import alerts, config, state
from .simulator import sim_client
from .tradovate import TradovateError, manager


class SignalError(Exception):
    """Raised for malformed or rejected signals."""


# Per-webhook, per-symbol record of the active trade so management signals can
# find the stop-loss order to modify. Keyed by "<webhook_id>:<root>" so two
# webhooks trading the same symbol never share state. Reset when the position
# is closed. Live and simulated trades are tracked separately.
_lock = threading.Lock()
_active: dict[str, dict[str, Any]] = {}
_sim_active: dict[str, dict[str, Any]] = {}


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
        ex = manager.executor_for(
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
    active_map = _sim_active if simulate else _active

    if webhook is None:
        if not simulate:
            raise SignalError("No webhook context for this signal")
        webhook = _synthetic_bracket_webhook(s)

    if not simulate:
        # passphrase (optional, defence in depth on top of the URL secret)
        if s.get("webhook_passphrase"):
            if payload.get("passphrase") != s["webhook_passphrase"]:
                raise SignalError("Invalid passphrase")

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

    if action in ("buy", "sell"):
        if strategy == "simple":
            return await _handle_simple_entry(payload, action, root, target, executors, active_map, tag, webhook)
        return await _handle_entry(payload, action, root, target, executors, active_map, tag, webhook)
    if action == "close_all":
        return await _handle_close_all(root, target, executors, active_map, tag, webhook)
    if action == "move_sl":
        if strategy == "simple":
            raise SignalError("'move_sl' is not supported on a 'simple' strategy webhook")
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
    base_qty = int(webhook.get("default_qty", 3))
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


def active_trades(simulate: bool = False) -> dict[str, Any]:
    with _lock:
        src = _sim_active if simulate else _active
        return {k: dict(v) for k, v in src.items()}


def reset_simulation() -> None:
    """Clear simulated positions, working orders and tracked trades."""
    sim_client.reset()
    with _lock:
        _sim_active.clear()

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
import re
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


_CONTRACT_RE = re.compile(r"^([A-Z]{1,4})([FGHJKMNQUVXZ])(\d{1,2})$")


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

    target, root, allowed = _resolve_symbol(s, tv_symbol)
    if not allowed:
        raise SignalError(f"Symbol '{tv_symbol}' not mapped / not in allowed list")

    if not simulate and not s.get("trading_enabled"):
        state.log_event(
            "warn", f"Trading disabled — signal '{action}' for {root} not executed"
        )
        return {"status": "skipped", "reason": "trading_disabled", "action": action}

    accounts = _target_accounts(s, simulate)
    if not accounts:
        state.log_event("warn", f"No enabled accounts — signal '{action}' ignored")
        return {"status": "skipped", "reason": "no_enabled_accounts", "action": action}

    contract = await exec_client.resolve_contract(target)
    tag = "[SIM] " if simulate else ""

    if action in ("buy", "sell"):
        return await _handle_entry(payload, action, root, contract, exec_client, active_map, tag, accounts)
    if action == "close_all":
        return await _handle_close_all(root, contract, exec_client, active_map, tag, accounts)
    if action == "move_sl":
        return await _handle_move_sl(payload, root, exec_client, active_map, tag)
    if action == "trail_active":
        state.log_event("info", f"{tag}Trailing active for {root} (handled by strategy)")
        return {"status": "ok", "action": action, "note": "acknowledged", "simulated": simulate}

    raise SignalError(f"Unknown action '{action}'")


def _target_accounts(s: dict[str, Any], simulate: bool) -> list[dict[str, Any]]:
    """Accounts a signal should be routed to.

    Simulation uses a single synthetic account. Live trading uses every enabled
    account, falling back to the legacy single account if none are configured.
    """
    if simulate:
        return [{"id": 0, "name": "SIM", "qty_multiplier": 1}]
    enabled = [a for a in (s.get("accounts") or []) if a.get("enabled")]
    if enabled:
        return enabled
    if s.get("account_spec") and s.get("account_id"):
        return [{"id": s["account_id"], "name": s["account_spec"], "qty_multiplier": 1}]
    return []


async def _handle_entry(payload, action, root, contract, exec_client, active_map, tag, accounts):
    s = config.load_settings()
    base_qty = int(s.get("default_qty", 3))
    base_tp_qty = int(s.get("tp_qty", 1))
    entry_side = "Buy" if action == "buy" else "Sell"
    exit_side = _opposite(action)

    orders: list[dict[str, Any]] = []
    acct_state: dict[Any, dict[str, Any]] = {}
    summary: list[dict[str, Any]] = []

    for acc in accounts:
        spec, acc_id = acc["name"], acc["id"]
        mult = acc.get("qty_multiplier", 1) or 1
        entry_qty = max(1, int(base_qty * mult))
        tp_qty = max(1, int(base_tp_qty * mult))

        # 1) Market entry.
        entry = await exec_client.place_order(
            symbol=contract, action=entry_side, qty=entry_qty,
            order_type=s.get("entry_order_type", "Market"),
            price=payload.get("entry"), account_spec=spec, account_id=acc_id,
        )
        orders.append(entry)

        # 2) Take-profit limit orders for each tp present.
        tp_ids: list[int] = []
        for key in ("tp1", "tp2", "tp3"):
            if payload.get(key) is None:
                continue
            tp = await exec_client.place_order(
                symbol=contract, action=exit_side, qty=tp_qty,
                order_type=s.get("tp_order_type", "Limit"),
                price=float(payload[key]), account_spec=spec, account_id=acc_id,
            )
            orders.append(tp)
            if tp.get("order_id"):
                tp_ids.append(tp["order_id"])

        # 3) Protective stop covering the full position.
        sl_id = None
        if payload.get("sl") is not None:
            sl = await exec_client.place_order(
                symbol=contract, action=exit_side, qty=entry_qty,
                order_type=s.get("sl_order_type", "Stop"),
                stop_price=float(payload["sl"]), account_spec=spec, account_id=acc_id,
            )
            orders.append(sl)
            sl_id = sl.get("order_id")

        acct_state[acc_id] = {
            "name": spec, "qty": entry_qty, "sl_order_id": sl_id, "tp_order_ids": tp_ids,
        }
        summary.append({"account": spec, "qty": entry_qty})

    with _lock:
        active_map[root] = {
            "contract": contract, "side": action, "qty": base_qty,
            "accounts": acct_state,
        }

    names = ", ".join(a["name"] for a in accounts)
    state.log_event(
        "info", f"{tag}Entry {action.upper()} {contract} placed on {len(accounts)} "
        f"account(s): {names}"
    )
    return {"status": "ok", "action": action, "contract": contract,
            "accounts": summary, "orders": orders, "simulated": tag != ""}


async def _handle_close_all(root, contract, exec_client, active_map, tag, accounts):
    # Close in every currently-enabled account plus any still-tracked accounts.
    with _lock:
        active = active_map.get(root)
    targets: dict[Any, str] = {a["id"]: a["name"] for a in accounts}
    if active:
        for acc_id, info in active.get("accounts", {}).items():
            targets.setdefault(acc_id, info.get("name", str(acc_id)))

    cancelled = 0
    for acc_id, name in targets.items():
        try:
            for order in await exec_client.working_orders(account_id=acc_id):
                try:
                    await exec_client.cancel_order(order["id"])
                    cancelled += 1
                except TradovateError:
                    pass
        except TradovateError as exc:
            state.log_event("warn", f"{tag}Could not list working orders for {name}: {exc}")
        await exec_client.liquidate_position(contract, account_id=acc_id)

    with _lock:
        active_map.pop(root, None)

    state.log_event(
        "info", f"{tag}Closed all for {contract} on {len(targets)} account(s) "
        f"({cancelled} working orders cancelled)"
    )
    return {"status": "ok", "action": "close_all", "accounts": len(targets),
            "cancelled": cancelled, "simulated": tag != ""}


async def _handle_move_sl(payload, root, exec_client, active_map, tag):
    new_sl = payload.get("new_sl", payload.get("sl"))
    if new_sl is None:
        raise SignalError("move_sl signal missing 'new_sl'")

    with _lock:
        active = active_map.get(root)
    if not active or not active.get("accounts"):
        state.log_event("warn", f"{tag}No tracked stop-loss for {root} to move")
        return {"status": "skipped", "reason": "no_active_stop", "action": "move_sl"}

    moved = 0
    for info in active["accounts"].values():
        if info.get("sl_order_id"):
            await exec_client.modify_order(info["sl_order_id"], stop_price=float(new_sl))
            moved += 1

    state.log_event(
        "info", f"{tag}Stop-loss for {root} moved to {new_sl} on {moved} account(s)"
    )
    return {"status": "ok", "action": "move_sl", "new_sl": float(new_sl),
            "accounts": moved, "simulated": tag != ""}


def active_trades(simulate: bool = False) -> dict[str, Any]:
    with _lock:
        src = _sim_active if simulate else _active
        return {k: dict(v) for k, v in src.items()}


def reset_simulation() -> None:
    """Clear simulated positions, working orders and tracked trades."""
    sim_client.reset()
    with _lock:
        _sim_active.clear()

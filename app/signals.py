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

This module is the entry point: validation, routing to the strategy handlers
in :mod:`app.engine`, per-trade serialisation and active-trade tracking. The
same logic powers the **simulator**: passing ``simulate=True`` routes orders to
an in-memory executor (a synthetic bracket webhook + account) and uses a
separate trade-tracking map, so you can rehearse a full scenario without
credentials, risk, or a configured webhook.
"""
from __future__ import annotations

import asyncio
import hmac
import threading
import time
from typing import Any

from . import alerts, config, context, news, state, trade_window
from .engine import bracket, manage, simple, ts_hunter
from .engine.common import (  # noqa: F401 - re-exported for callers/tests
    SignalError,
    _base_root,
    _cancel_working,
    _lock,
    _resolve_symbol,
    _trade_key,
)
from .simulator import sim_client
from .tradovate import AccountExecutor, TradovateError, manager

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


def _release_trade_lock(key: str) -> None:
    """Drop a trade's lock once its position is closed (v4 kept every lock for
    the life of the process — unbounded for TS-Hunter's per-trade ids). Only
    when nobody holds or awaits it: a late signal already queued on the Lock
    keeps using that object, so a second Lock must never appear beside it."""
    with _trade_locks_guard:
        lk = _trade_locks.get(key)
        if lk is not None and not lk.locked() and not getattr(lk, "_waiters", None):
            _trade_locks.pop(key, None)


# Active-trade records are isolated per area (user workspace). Each maps
# "<webhook_id>:<root>" (simple/bracket) or "<trade_id>" (TS-Hunter) -> trade
# record, so management signals can find the stop-loss order to modify. Reset
# when the position is closed. Live and simulated trades are tracked separately.
_active: dict[int, dict[str, dict[str, Any]]] = {}
_sim_active: dict[int, dict[str, dict[str, Any]]] = {}


ACTIVE_TTL_S = 14 * 24 * 3600     # a record whose position closed without a signal (stop hit) is forgotten after this
_sweep_n = 0


def _map_for(simulate: bool) -> dict[str, dict[str, Any]]:
    """The active-trade map for the current area (live or simulated)."""
    global _sweep_n
    reg = _sim_active if simulate else _active
    aid = context.get_area()
    with _lock:
        m = reg.get(aid)
        if m is None:
            m = reg[aid] = {}
        _sweep_n += 1
        if _sweep_n % 200 == 0 and len(m) > 50:
            cutoff = time.time() - ACTIVE_TTL_S
            for key in [k for k, rec in m.items() if 0 < float(rec.get("ts") or 0) < cutoff]:
                m.pop(key, None)
        return m


def _synthetic_bracket_webhook(s: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": "sim", "name": "Simulator", "strategy": "bracket",
        "default_qty": s.get("default_qty", 3), "tp_qty": s.get("tp_qty", 1),
        "accounts": [],
    }


def _webhook_executors(webhook: dict[str, Any]) -> list[Any]:
    out = []
    for a in webhook.get("accounts") or []:
        if not a.get("enabled"):
            continue
        ex = manager().executor_for(
            a.get("token_idx"), a.get("spec"), a.get("qty_multiplier", 1), sizing=a.get("sizing"), lid=a.get("lid")
        )
        if ex is None:
            state.log_event("warn", f"Webhook '{webhook.get('name')}': account '{a.get('spec')}' not found (deleted login/account?)")
            continue
        out.append(ex)
    return out


_bg_tasks: set[asyncio.Task] = set()


def _spawn(coro: Any) -> asyncio.Task:
    task = asyncio.get_running_loop().create_task(coro)
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)
    return task


def passphrase_ok(payload: dict[str, Any], settings: dict[str, Any] | None = None) -> bool:
    s = settings if settings is not None else config.load_settings()
    want = str(s.get("webhook_passphrase") or "")
    if not want:
        return True
    given = str(payload.get("passphrase") or "")
    return hmac.compare_digest(given.encode(), want.encode())


def accept(payload: dict[str, Any], webhook: dict[str, Any], *, forward: bool = True) -> None:
    name = webhook.get("name", "")
    state.log_signal(payload, result="received", webhook=name)
    if not passphrase_ok(payload):
        state.log_event("error", "Signal rejected: invalid passphrase", payload=payload)
        state.log_signal(payload, result="error: Invalid passphrase", webhook=name)
        _spawn(alerts.webhook_failed(name or "?", "Invalid passphrase"))
        return
    _spawn(process_background(payload, webhook))
    if forward:
        forward_to_subscribers(payload, webhook)


def forward_to_subscribers(payload: dict[str, Any], webhook: dict[str, Any],
                           publisher_area: int | None = None) -> int:
    """Fan a published webhook out only to subscriptions still authorised now.

    Marketplace visibility is an execution-time authorization boundary, not only
    a subscribe-time UI rule. Removing a selected user therefore stops future
    financial fan-out immediately even if their subscription row still exists.
    """
    from . import db, marketplace

    if not marketplace.sharing_of(webhook)["enabled"]:
        return 0
    aid = publisher_area if publisher_area is not None else context.get_area()
    subs = db.active_subscriptions(aid, webhook.get("id", ""))
    shared = {k: v for k, v in payload.items() if not (isinstance(k, str) and "passphrase" in k.lower())}
    dispatched = 0
    revoked = 0
    for sub in subs:
        if not marketplace.subscription_allowed(webhook, sub):
            revoked += 1
            continue
        view = marketplace.subscription_view(webhook, sub, aid)
        with context.use_area(sub["area_id"]):
            state.log_signal(dict(shared), result="received", webhook=view.get("name", ""))
            _spawn(process_background(dict(shared), view, trusted=True))
        dispatched += 1
    if revoked:
        state.log_event("warn", f"[{webhook.get('name', '?')}] skipped {revoked} marketplace subscription(s) no longer authorised")
    if dispatched:
        state.log_event("info", f"[{webhook.get('name', '?')}] forwarded to {dispatched} subscriber(s)")
    return dispatched


async def process_background(payload: dict[str, Any], webhook: dict[str, Any], *, trusted: bool = False) -> None:
    name = webhook.get("name", "?")
    try:
        result = await process(payload, webhook, trusted=trusted)
        state.log_signal(payload, result=result.get("status", "ok"), webhook=name)
    except (SignalError, TradovateError) as exc:
        state.log_event("error", f"Signal error: {exc}", payload=payload)
        state.log_signal(payload, result=f"error: {exc}", webhook=name)
        await alerts.webhook_failed(name, str(exc))
    except Exception as exc:  # noqa: BLE001
        state.log_event("error", f"Signal failed: {exc}", payload=payload)
        state.log_signal(payload, result=f"error: {exc}", webhook=name)
        await alerts.webhook_failed(name, str(exc))


async def process(
    payload: dict[str, Any], webhook: dict[str, Any] | None = None, *,
    simulate: bool = False, trusted: bool = False,
) -> dict[str, Any]:
    s = config.load_settings()
    active_map = _map_for(simulate)

    if webhook is None:
        if not simulate:
            raise SignalError("No webhook context for this signal")
        webhook = _synthetic_bracket_webhook(s)

    if not simulate and not trusted and not passphrase_ok(payload, s):
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
        state.log_event("warn", f"Trading disabled — signal '{action}' for {root} not executed")
        return {"status": "skipped", "reason": "trading_disabled", "action": action}
    if not simulate and action in ("buy", "sell"):
        lock = news.active_lock(settings=s)
        if lock:
            state.log_event("warn", f"News lock ({lock['title']}) — entry '{action}' for {root} not executed; closes and stop moves still run")
            return {"status": "skipped", "reason": "news_lock", "action": action, "event": lock["title"]}
        opened, why = trade_window.is_open(webhook.get("trade_window"), default_tz=str(s.get("journal_timezone") or ""))
        if not opened:
            state.log_event("warn", f"Webhook '{webhook.get('name')}': entry '{action}' for {root} not executed — {why}; closes and stop moves still run")
            return {"status": "skipped", "reason": "trade_window", "action": action, "detail": why}

    executors = [sim_client] if simulate else _webhook_executors(webhook)
    if not executors:
        state.log_event("warn", f"No enabled accounts on webhook '{webhook.get('name')}' — signal '{action}' ignored")
        return {"status": "skipped", "reason": "no_enabled_accounts", "action": action}

    tag = "[SIM] " if simulate else ""
    strategy = webhook.get("strategy", "simple")

    async def run() -> dict[str, Any]:
        if action in ("buy", "sell"):
            if strategy == "simple":
                return await simple.handle_entry(payload, action, root, target, executors, active_map, tag, webhook, settings=s)
            return await bracket.handle_entry(payload, action, root, target, executors, active_map, tag, webhook, settings=s)
        if action == "close_all":
            return await manage.handle_close_all(root, target, executors, active_map, tag, webhook)
        if action == "set_sl_tp":
            return await manage.handle_set_sl_tp(payload, root, target, executors, active_map, tag, webhook)
        if action == "move_sl":
            if strategy == "simple":
                state.log_event("info", f"{tag}move_sl ignored for {root} — 'simple' strategy has no bracket to move")
                return {"status": "skipped", "reason": "move_sl_unsupported_simple", "action": action}
            return await bracket.handle_move_sl(payload, root, executors, active_map, tag, webhook)
        if action == "trail_active":
            if strategy == "simple":
                state.log_event("info", f"{tag}Trailing active for {root} (no-op on 'simple' strategy)")
                return {"status": "ok", "action": action, "note": "acknowledged", "simulated": simulate}
            return await bracket.handle_trail_active(payload, root, executors, active_map, tag, webhook)
        raise SignalError(f"Unknown action '{action}'")

    lock_key = f"{context.get_area()}:{'sim' if simulate else 'live'}:{webhook['id']}:{root}"
    async with _trade_lock(lock_key):
        result = await run()
    if action == "close_all":
        _release_trade_lock(lock_key)
    return result


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
        state.log_event("warn", f"Trading disabled — TS-Hunter signal for {root} (trade {trade_id}) not executed")
        return {"status": "skipped", "reason": "trading_disabled"}
    if not simulate and event == "signal":
        lock = news.active_lock(settings=s)
        if lock:
            state.log_event("warn", f"News lock ({lock['title']}) — TS-Hunter entry for {root} (trade {trade_id}) not executed")
            return {"status": "skipped", "reason": "news_lock", "event": lock["title"]}
        opened, why = trade_window.is_open(webhook.get("trade_window"), default_tz=str(s.get("journal_timezone") or ""))
        if not opened:
            state.log_event("warn", f"Webhook '{webhook.get('name')}': TS-Hunter entry for {root} (trade {trade_id}) not executed — {why}")
            return {"status": "skipped", "reason": "trade_window", "detail": why}

    executors = [sim_client] if simulate else _webhook_executors(webhook)
    if not executors:
        state.log_event("warn", f"No enabled accounts on webhook '{webhook.get('name')}' — TS-Hunter signal ignored")
        return {"status": "skipped", "reason": "no_enabled_accounts"}

    mgmt_action = str(payload.get("action", "")).lower().strip() if event == "management" else ""

    async def run() -> dict[str, Any]:
        if event == "signal":
            return await ts_hunter.handle_entry(payload, side, root, target, trade_id, executors, active_map, tag, webhook, settings=s)
        if event == "management":
            if mgmt_action == "partial_close_percent":
                return await ts_hunter.handle_partial_close(payload, trade_id, executors, active_map, tag)
            if mgmt_action == "full_close":
                return await ts_hunter.handle_full_close(payload, trade_id, target, executors, active_map, tag)
            raise SignalError(f"Unknown TS-Hunter management action '{mgmt_action}'")
        raise SignalError(f"Unknown TS-Hunter event '{event}'")

    lock_key = f"{context.get_area()}:{'sim' if simulate else 'live'}:ts:{trade_id}"
    async with _trade_lock(lock_key):
        result = await run()
    if mgmt_action == "full_close":
        _release_trade_lock(lock_key)
    return result


async def flatten_all() -> dict[str, Any]:
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
        errors: list[str] = []
        cancelled = await _cancel_working(ex, "", errors)
        flattened = 0
        try:
            positions = await ex.positions()
        except TradovateError as exc:
            errors.append(f"list positions: {exc}")
            positions = []
        symbols = [p.get("symbol") for p in positions if p.get("symbol")]
        results = await asyncio.gather(*(ex.liquidate_position(s) for s in symbols), return_exceptions=True)
        for sym, r in zip(symbols, results):
            if isinstance(r, TradovateError):
                errors.append(f"flatten {sym}: {r}")
            elif isinstance(r, BaseException):
                raise r
            else:
                flattened += 1
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

    state.log_event("warn", f"🆘 SOS flatten-all: {flattened} position(s) flattened, {cancelled} order(s) cancelled across {len(executors)} account(s)" + (f"; {len(all_errors)} error(s)" if all_errors else ""))
    return {"status": "ok", "accounts": len(executors), "cancelled": cancelled, "flattened": flattened, "errors": all_errors}


def active_trades(simulate: bool = False) -> dict[str, Any]:
    src = _map_for(simulate)
    with _lock:
        return {k: dict(v) for k, v in src.items()}


def reset_simulation() -> None:
    sim_client.reset()
    _map_for(True).clear()

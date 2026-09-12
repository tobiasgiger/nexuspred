"""Primitives shared by every strategy handler."""
from __future__ import annotations

import asyncio
import math
import re
import threading
from typing import Any

from .. import state
from ..tradovate import TradovateError


class SignalError(Exception):
    """Raised for malformed or rejected signals."""


# Guards the active-trade maps (see app.signals) while a handler reads/writes
# a trade record.
_lock = threading.Lock()

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


async def _orders_for_contract(ex: Any, orders: list[dict[str, Any]], contract: str,
                               tag: str) -> list[dict[str, Any]]:
    """The subset of ``orders`` that belongs to ``contract``.

    A close for one symbol must not strip the protective stops / targets of
    positions in other symbols on the same account. Tradovate orders carry a
    numeric ``contractId``; the simulator's carry the contract ``symbol``. When
    neither can be matched the order is left alone (and reported), because
    cancelling it could unprotect an unrelated position."""
    try:
        cid = int(await ex.contract_id(contract))
    except (TradovateError, AttributeError, TypeError, ValueError):
        cid = 0
    mine: list[dict[str, Any]] = []
    identifiable = 0
    for o in orders:
        name = str(o.get("symbol") or o.get("contract") or "")
        oid = o.get("contractId")
        has_id = isinstance(oid, int) and oid > 0
        if name or has_id:
            identifiable += 1
        if (name and name == contract) or (cid and has_id and oid == cid):
            mine.append(o)
    if orders and not identifiable:
        # Nothing about these orders says which contract they belong to (no
        # contractId, no symbol). Cancelling them all could strip the stops of
        # unrelated positions, so they are left alone and reported loudly.
        state.log_event("error", f"{tag}Working orders on {ex.name} carry no contract — none cancelled; "
                                 f"check the account for stops / targets left on {contract}")
        return []
    if len(mine) < identifiable:
        state.log_event("info", f"{tag}Keeping {identifiable - len(mine)} working order(s) on {ex.name} "
                                f"that belong to other contracts than {contract}")
    return mine


QTY_HARD_CAP = 1000               # the ceiling sizing.normalize enforces for fixed / max_contracts


def _price(value: Any, what: str) -> float:
    """A finite float from a payload field, or SignalError — parsed *before* any
    broker call so a malformed target never leaves a live entry untracked."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        raise SignalError(f"'{what}' is not a number: {value!r}")
    if not math.isfinite(f):
        raise SignalError(f"'{what}' must be a finite number")
    return f


def _signal_qty(raw: Any, default: Any, *, strict: bool) -> float:
    """The signal's contract count: ``raw`` when it is a finite positive number
    within QTY_HARD_CAP, else ``default`` (bracket) or SignalError (strict)."""
    try:
        q = float(raw) if raw is not None else float(default)
    except (TypeError, ValueError):
        if strict:
            raise SignalError(f"Invalid qty '{raw}'")
        q = float(default)
    if not math.isfinite(q) or q <= 0 or q > QTY_HARD_CAP:
        if strict:
            raise SignalError(f"qty must be positive (at most {QTY_HARD_CAP})")
        q = float(default)
    if not math.isfinite(q) or q <= 0 or q > QTY_HARD_CAP:
        raise SignalError(f"Webhook default qty must be between 1 and {QTY_HARD_CAP}")
    return q


def _untrack_after_close(active_map: dict[str, Any], key: str, succeeded: list[str], failed: list[str]) -> None:
    """After a close: on a mixed broker outcome remove only the accounts whose
    close was confirmed (failed ones stay tracked so a retry cannot forget a
    live position or re-flatten the others); with no failure drop the record."""
    with _lock:
        cur = active_map.get(key)
        if not cur or not cur.get("accounts"):
            return
        if failed:
            accounts = cur["accounts"]
            for name in succeeded:
                accounts.pop(name, None)
            if not accounts:
                active_map.pop(key, None)
        else:
            active_map.pop(key, None)


async def _resize_stop(ex: Any, info: dict[str, Any], qty: int, stop_price: Any) -> None:
    """Modify the tracked stop to ``qty`` @ ``stop_price``; the record follows
    the broker, never precedes it."""
    await ex.modify_order(info["sl_order_id"], qty=qty, order_type=info.get("sl_type", "Stop"), stop_price=stop_price)
    info["qty"] = qty


STOP_PENALTY_WAIT_S = 30.0        # the longest a protective stop waits for a 429 penalty before its retry


class StopFailed(TradovateError):
    """The protective stop could not be placed twice and the entry was closed
    again at market: the account holds nothing of this trade (policy: never
    leave an entry live without its stop)."""


async def _place_stop_with_retry(ex: Any, *, symbol: str, action: str, qty: int, order_type: str,
                                 stop_price: float, tag: str, what: str = "stop",
                                 cancel_ids: list[int] | None = None,
                                 resting_entry_id: int | None = None) -> dict[str, Any] | None:
    """Place a protective stop; one retry on failure. When it still fails the
    entry is **closed again**: the trade's own orders (``cancel_ids``, the
    bracket's targets) are cancelled — every working order of the contract when
    the stop's outcome is unknown, since a stop that did reach the broker would
    open a reverse trade on a flat account — then ``qty`` is flattened at
    market, the operator is alerted and ``StopFailed`` is raised so the caller
    drops the account from the trade. Only when that close fails too does the
    position stay live: reported at error level and on every alert channel.
    ``resting_entry_id`` names a limit entry that may not have filled: it is
    cancelled and only what the broker shows as filled is closed (a blind market
    order on an unfilled limit would open the opposite position).
    Returns the stop order."""
    from .. import alerts
    from ..tradovate import OrderOutcomeUnknown, RateLimited, _fire
    last: Exception | None = None
    for attempt in (1, 2):
        try:
            return await ex.place_order(symbol=symbol, action=action, qty=qty, order_type=order_type, stop_price=stop_price)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            last = exc
            if attempt == 1:
                # a 429 penalty set by some poll must not leave the entry naked: the
                # stop is protective, not latency-critical, so it waits the penalty out
                wait = min(float(getattr(exc, "retry_after", 0) or 0) + 0.2, STOP_PENALTY_WAIT_S) if isinstance(exc, RateLimited) else 0.5
                await asyncio.sleep(wait)
    state.log_event("error", f"{tag}{what} for {ex.name} on {symbol} FAILED twice ({last}) — closing the entry again")
    errors: list[str] = []
    if isinstance(last, OrderOutcomeUnknown):
        # the stop may be working after all: it must not survive on a flat account
        await _cancel_working(ex, tag, errors, contract=symbol)
    else:
        ids = [o for o in [*(cancel_ids or []), resting_entry_id] if o]
        results = await asyncio.gather(*(ex.cancel_order(oid) for oid in ids), return_exceptions=True)
        errors += [f"cancel {oid}: {r}" for oid, r in zip(ids, results) if isinstance(r, Exception)]
    close_qty = qty
    if resting_entry_id:
        # a limit entry: close only the part that filled (sign must be the entry's)
        try:
            rows = await ex.positions()
            net = sum(int(p.get("netPos") or 0) for p in rows or [] if str(p.get("symbol") or "") == symbol)
        except Exception as exc:  # noqa: BLE001 - unknown fill state → treat as filled (the safer error)
            state.log_event("warn", f"{tag}{ex.name}: position on {symbol} could not be read after the failed {what} ({exc}) — closing the full entry")
            net = qty if action == "Sell" else -qty
        filled = net if action == "Sell" else -net                 # exit Sell means the entry went long
        close_qty = max(0, min(qty, filled))
    if close_qty <= 0:
        state.log_event("error", f"{tag}{ex.name}: entry on {symbol} cancelled — the {what} could not be placed and nothing had filled")
        _fire(alerts.execution_problem(f"Entry cancelled on {ex.name}", f"{symbol}: the {what} could not be placed ({last}); the unfilled entry was cancelled."))
        raise StopFailed(f"{what} could not be placed ({last}); unfilled entry cancelled")
    try:
        await ex.place_order(symbol=symbol, action=action, qty=close_qty, order_type="Market")
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        state.log_event("error", f"{tag}{ex.name}: the close after the failed {what} FAILED too ({exc}) — position on {symbol} is unprotected")
        _fire(alerts.execution_problem(f"Unprotected position on {ex.name}",
                                       f"{symbol}: the {what} could not be placed ({last}) and the position could not be closed ({exc}). "
                                       "Set a stop by hand or close the position."))
        return None
    left = f"; {len(errors)} order(s) could not be cancelled: {'; '.join(errors)[:200]} — cancel them by hand" if errors else ""
    state.log_event("error", f"{tag}{ex.name}: {close_qty} × {symbol} closed again at market — the {what} could not be placed{left}")
    _fire(alerts.execution_problem(f"Entry closed again on {ex.name}",
                                   f"{symbol}: the {what} could not be placed ({last}); the {close_qty}-lot entry was closed at market{left}."))
    raise StopFailed(f"{what} could not be placed ({last}); entry closed again at market{left}")


class OrdersLeftWorking(TradovateError):
    """The position was closed but a working order survived both cancel
    attempts — the account is unresolved (a stop or target on a flat position
    would open a new trade), not "still open"."""


async def _close_contract(ex: Any, tag: str, contract: str) -> int:
    """Cancel the contract's working orders, then liquidate the position, then
    retry any cancel that failed — a stop or target left working on a flat
    position would open a new trade. Failures that survive the retry are
    reported, alerted, and raised so callers cannot treat the close as clean."""
    from .. import alerts
    errors: list[str] = []
    cancelled = await _cancel_working(ex, tag, errors, contract=contract)
    await ex.liquidate_position(contract)
    if errors:
        retry_errors: list[str] = []
        cancelled += await _cancel_working(ex, tag, retry_errors, contract=contract)
        if retry_errors:
            detail = "; ".join(retry_errors)
            state.log_event("error", f"{tag}{ex.name}: working orders on {contract} could not be cancelled after the close: "
                                     f"{detail} — cancel them by hand")
            from ..tradovate import _fire
            _fire(alerts.execution_problem(f"Orders left working on {ex.name}",
                                           f"{contract} was closed but {len(retry_errors)} working order(s) could not be cancelled: {detail[:300]}"))
            raise OrdersLeftWorking(f"working orders remain after closing {contract}: {detail}")
    return cancelled


async def _close_untracked(executors: list[Any], tracked_names: set[str], tag: str, target: str) -> tuple[list[str], list[str]]:
    """Accounts enabled on the webhook but absent from the trade record: an
    entry whose answer was lost ("outcome unknown"), or an account routed here
    after the entry. They are closed too — but only when the broker shows a
    position in the contract, so a flat account gets no liquidate call and no
    rejection. Returns (closed, failed) account names."""
    async def one(ex: Any) -> bool:
        contract = await ex.resolve_contract(target)
        rows = await ex.positions()
        if not any(str(p.get("symbol") or "") == contract and (p.get("netPos") or 0) for p in rows or []):
            return False
        state.log_event("warn", f"{tag}{ex.name} holds {contract} without a trade record (lost entry answer or manual position) — closing it too")
        await _close_contract(ex, tag, contract)
        return True

    extra = [ex for ex in executors if ex.name not in tracked_names]
    results = await asyncio.gather(*(one(ex) for ex in extra), return_exceptions=True)
    closed, failed = [], []
    for ex, r in zip(extra, results):
        if isinstance(r, Exception):
            failed.append(ex.name)
            state.log_event("error", f"{tag}close of untracked {ex.name} FAILED: {r} — check the account")
        elif r:
            closed.append(ex.name)
    return closed, failed


async def _cancel_working(ex: Any, tag: str, errors: list[str] | None = None,
                          contract: str | None = None) -> int:
    """Cancel working orders on one account with all cancels in flight at once.
    With ``contract`` only that contract's orders are cancelled (a symbol-scoped
    close); without it every working order goes, as the SOS flatten-all needs.
    Returns how many were cancelled. Tradovate rejections are collected
    (``errors``) or logged per order; anything else propagates, as in v4."""
    try:
        orders = await ex.working_orders()
    except TradovateError as exc:
        if errors is not None:
            errors.append(f"list orders: {exc}")
        else:
            state.log_event("error", f"{tag}Could not list working orders for {ex.name}: {exc} — nothing cancelled")
        return 0
    if contract:
        orders = await _orders_for_contract(ex, orders, contract, tag)
    ids = [o.get("id") for o in orders if o.get("id") is not None]
    results = await asyncio.gather(*(ex.cancel_order(oid) for oid in ids), return_exceptions=True)
    cancelled = 0
    for oid, r in zip(ids, results):
        if isinstance(r, TradovateError):
            if errors is not None:
                errors.append(f"cancel {oid}: {r}")
            else:
                state.log_event("error", f"{tag}{ex.name}: cancel of order {oid} failed: {r}")
        elif isinstance(r, BaseException):
            raise r
        else:
            cancelled += 1
    return cancelled

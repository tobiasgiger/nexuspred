"""Primitives shared by every strategy handler."""
from __future__ import annotations

import asyncio
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


STOP_PENALTY_WAIT_S = 30.0        # the longest a protective stop waits for a 429 penalty before its retry


async def _place_stop_with_retry(ex: Any, *, symbol: str, action: str, qty: int, order_type: str,
                                 stop_price: float, tag: str, what: str = "stop") -> dict[str, Any] | None:
    """Place a protective stop; one retry on failure. When it still fails the
    position is live without protection — that is reported at error level and
    through every alert channel so the operator acts now. Returns the order or None."""
    from .. import alerts
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
                from ..tradovate import RateLimited
                wait = min(float(getattr(exc, "retry_after", 0) or 0) + 0.2, STOP_PENALTY_WAIT_S) if isinstance(exc, RateLimited) else 0.5
                await asyncio.sleep(wait)
    state.log_event("error", f"{tag}{what} for {ex.name} on {symbol} FAILED twice — position is unprotected: {last}")
    from ..tradovate import _fire
    _fire(alerts.execution_problem(f"Unprotected position on {ex.name}",
                                   f"{symbol}: the {what} could not be placed ({last}). Set a stop by hand or close the position."))
    return None


async def _close_contract(ex: Any, tag: str, contract: str) -> int:
    """Cancel the contract's working orders, then liquidate the position, then
    retry any cancel that failed — a stop or target left working on a flat
    position would open a new trade. Failures that survive the retry are
    reported at error level and alerted. Returns how many orders were cancelled."""
    from .. import alerts
    errors: list[str] = []
    cancelled = await _cancel_working(ex, tag, errors, contract=contract)
    await ex.liquidate_position(contract)
    if errors:
        retry_errors: list[str] = []
        cancelled += await _cancel_working(ex, tag, retry_errors, contract=contract)
        if retry_errors:
            state.log_event("error", f"{tag}{ex.name}: working orders on {contract} could not be cancelled after the close: "
                                     f"{'; '.join(retry_errors)} — cancel them by hand")
            from ..tradovate import _fire
            _fire(alerts.execution_problem(f"Orders left working on {ex.name}",
                                           f"{contract} was closed but {len(retry_errors)} working order(s) could not be cancelled: {'; '.join(retry_errors)[:300]}"))
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

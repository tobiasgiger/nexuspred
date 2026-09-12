"""Live account P&L for the Overview.

Every few seconds (``pnl_poll_seconds``, default 5) the bridge asks Tradovate
for each trade account's **cash-balance snapshot** — the broker's own figures
for today's realised P&L, the open (unrealised) P&L of current positions, the
week's realised P&L and the cash balance — and pushes the result to every
open dashboard over the live stream (``kind: "pnl"``). No market-data feed is
needed: Tradovate marks the open positions itself.

Cadence is adaptive: the fast interval while someone is watching (an
``/api/stream`` subscriber exists for the area) or while trade-opened /
trade-closed alerts are on (they need a timely view of the broker's
positions, see :mod:`app.watch`), a slow tick otherwise; a rate-limit / error
answer backs the interval off up to two minutes.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Optional

from . import risk, config, context, db, drawdown, state, tradovate, watch

IDLE_INTERVAL_S = 60.0
RISK_CACHE_S = 300.0          # /userAccountAutoLiq/list per login at most this often
IDLE_SNAPSHOT_EVERY = 6       # flat accounts get a fresh cash snapshot every n-th tick
MAX_BACKOFF_S = 120.0
_backoff: dict[int, float] = {}


def _num(v: Any) -> float:
    try:
        return round(float(v), 2)
    except (TypeError, ValueError):
        return 0.0


async def snapshot_account(session: Any, account: dict[str, Any]) -> dict[str, Any]:
    """One account's live figures (raises on transport / API errors)."""
    data = await session.cash_snapshot(int(account["id"]))
    if not isinstance(data, dict) or not data:
        raise tradovate.TradovateError("unexpected snapshot answer")
    if data.get("errorText"):
        raise tradovate.TradovateError(str(data["errorText"]))
    for key in ("realizedPnL", "openPnL"):
        try:
            float(data[key])
        except (KeyError, TypeError, ValueError) as exc:
            raise tradovate.TradovateError(f"snapshot without a numeric {key}") from exc   # never read a missing figure as 0
    return {
        "account_id": int(account["id"]), "spec": account.get("spec") or "", "login": session.name,
        "environment": session.environment,
        "realized": _num(data.get("realizedPnL")), "open": _num(data.get("openPnL")),
        "total": _num(data.get("totalPnL")) if data.get("totalPnL") is not None
        else round(_num(data.get("realizedPnL")) + _num(data.get("openPnL")), 2),
        "week": _num(data.get("weekRealizedPnL")), "cash": _num(data.get("totalCashValue")),
        "ts": datetime.now(timezone.utc).isoformat(),
    }


_risk_cache: dict[tuple[int, str], tuple[float, dict[int, dict[str, Any]]]] = {}
_last_snap: dict[tuple[int, int], dict[str, Any]] = {}      # (area, account) → last snapshot
_tick: dict[int, int] = {}


def reset() -> None:
    _risk_cache.clear()
    _last_snap.clear()
    _tick.clear()


async def risk_settings_cached(area_id: int, session: Any) -> dict[int, dict[str, Any]]:
    """The risk record changes rarely — fetch it every few minutes per login."""
    import time
    key = (area_id, session.name)
    hit = _risk_cache.get(key)
    if hit and time.monotonic() - hit[0] < RISK_CACHE_S:
        return hit[1]
    recs = await risk_settings(session)
    _risk_cache[key] = (time.monotonic(), recs or (hit[1] if hit else {}))   # a failed refresh keeps the last records, and waits
    return _risk_cache[key][1]


async def leader_positions(session: Any) -> Optional[list[dict[str, Any]]]:
    """``/position/list`` of a login (None when unreachable)."""
    try:
        raw = await session.positions_snapshot()
    except Exception:  # noqa: BLE001
        return None
    return raw if isinstance(raw, list) else None      # an error object is not "no positions"


async def risk_settings(session: Any) -> dict[int, dict[str, Any]]:
    """Tradovate's auto-liquidation / risk record per account id (one call per
    login). Prop-firm accounts carry the **trailing max drawdown** here:
    ``trailingMaxDrawdown`` (its size), ``trailingMaxDrawdownLimit`` (the
    balance level the account is liquidated at) and ``trailingMaxDrawdownMode``
    (``EOD`` or ``RealTime`` = intraday). Never raises — an account without a
    readable record simply shows no drawdown."""
    try:
        rows = await session.auto_liq_rules()
    except Exception:  # noqa: BLE001
        return {}
    out: dict[int, dict[str, Any]] = {}
    for r in rows if isinstance(rows, list) else []:
        try:
            out[int(r.get("id") or r.get("accountId") or 0)] = r
        except (TypeError, ValueError):
            continue
    return out


async def refresh_area(area_id: int) -> dict[str, Any]:
    """Poll every enabled account of an area once; store + broadcast the result."""
    with context.use_area(area_id):
        sessions = [s for s in tradovate.manager_for(area_id).all()
                    if s.enabled and s.has_token() and state.session_status(s.name).get("connected")]
        accounts: list[dict[str, Any]] = []
        errors: list[str] = []
        tick = _tick[area_id] = _tick.get(area_id, 0) + 1
        positions_by_login: dict[str, Optional[list[dict[str, Any]]]] = {}
        for s in sessions:
            risk_recs = await risk_settings_cached(area_id, s)
            raw_positions = await leader_positions(s)
            positions_by_login[s.name] = raw_positions
            open_accounts = {int(p.get("accountId") or 0) for p in (raw_positions or []) if p.get("netPos")}
            for a in s.accounts:
                if not a.get("id"):
                    continue
                aid = int(a["id"])
                prev = _last_snap.get((area_id, aid))
                # a flat account's figures only move when something fills: refresh
                # it every few ticks, accounts with a position (or unknown) every tick
                due = (aid in open_accounts or raw_positions is None or prev is None
                       or (prev.get("open") or 0) != 0 or tick % IDLE_SNAPSHOT_EVERY == 0)
                if not due:
                    snap = dict(prev)
                else:
                    try:
                        snap = await snapshot_account(s, a)
                        _last_snap[(area_id, aid)] = dict(snap)
                    except Exception as exc:  # noqa: BLE001 - one account failing must not hide the others
                        errors.append(f"{a.get('spec') or a.get('id')}: {exc}")
                        continue
                try:
                    snap.update(drawdown.apply(area_id, snap, risk_recs.get(int(a["id"]))))
                except Exception as exc:  # noqa: BLE001 - the drawdown view must never break the P&L feed
                    state.log_event("warn", f"drawdown tracking failed for {snap.get('spec')}: {exc}")
                accounts.append(snap)
        summary = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "accounts": accounts,
            "realized": round(sum(a["realized"] for a in accounts), 2),
            "open": round(sum(a["open"] for a in accounts), 2),
            "week": round(sum(a["week"] for a in accounts), 2),
            "cash": round(sum(a["cash"] for a in accounts), 2),
            "error": "; ".join(errors)[:300],
        }
        summary["total"] = round(summary["realized"] + summary["open"], 2)
        try:
            await risk.check_area(area_id, sessions, accounts, positions=positions_by_login)
        except Exception as exc:  # noqa: BLE001 - the guard must never break the P&L feed
            state.log_event("warn", f"risk guard failed: {exc}")
        changed = state.set_pnl(summary, area_id)
        if changed:
            state.publish("pnl", summary, area_id)
        try:
            await watch.observe_area(area_id, sessions, accounts, positions=positions_by_login)
        except Exception as exc:  # noqa: BLE001 - alerts must never break the P&L feed
            state.log_event("warn", f"position watch failed: {exc}")
        return summary


AREA_TIMEOUT_S = 90.0        # one slow broker must not hold every other area's risk guard


async def _poll_area(aid: int) -> Optional[float]:
    """One area's tick + P&L refresh. Returns the delay it wants before the next
    look, or None when the area is switched off."""
    s = config.load_settings(area_id=aid)
    await watch.tick(aid)   # agent transitions + daily summary (no broker calls)
    fast = float(s.get("pnl_poll_seconds", 5) or 0)
    if fast <= 0 and not risk.any_active(s):
        return None  # switched off for this area (a risk rule keeps it running regardless)
    fast = max(2.0, fast if fast > 0 else 5.0)
    # Fast while a dashboard is open — or while trade alerts need a
    # timely view of the broker's positions.
    watched = state.subscriber_count(aid) or watch.trade_alerts_enabled(s) or risk.any_active(s)
    interval = fast if watched else IDLE_INTERVAL_S
    try:
        summary = await asyncio.wait_for(refresh_area(aid), timeout=AREA_TIMEOUT_S)
        if summary["error"] and not summary["accounts"]:
            _backoff[aid] = min(MAX_BACKOFF_S, max(interval, _backoff.get(aid, interval) * 2))
        else:
            _backoff.pop(aid, None)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        _backoff[aid] = min(MAX_BACKOFF_S, max(interval, _backoff.get(aid, interval) * 2))
        if isinstance(exc, asyncio.TimeoutError):
            state.log_event("warn", f"P&L refresh for area {aid} timed out after {int(AREA_TIMEOUT_S)} s")
    return _backoff.get(aid, interval)


async def pnl_loop() -> None:
    """Poll all areas — in parallel, each with its own timeout — fast while
    watched, slow while idle, backing off on errors."""
    while True:
        delay = IDLE_INTERVAL_S
        try:
            area_ids = db.all_area_ids()
            results = await asyncio.gather(*(_poll_area(a) for a in area_ids), return_exceptions=True)
            for r in results:
                if isinstance(r, (int, float)):
                    delay = min(delay, float(r))
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - the loop must survive anything
            delay = IDLE_INTERVAL_S
        await asyncio.sleep(delay)

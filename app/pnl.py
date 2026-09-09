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

from . import config, context, db, state, tradovate, watch

IDLE_INTERVAL_S = 60.0
MAX_BACKOFF_S = 120.0
_backoff: dict[int, float] = {}


def _num(v: Any) -> float:
    try:
        return round(float(v), 2)
    except (TypeError, ValueError):
        return 0.0


async def snapshot_account(session: Any, account: dict[str, Any]) -> dict[str, Any]:
    """One account's live figures (raises on transport / API errors)."""
    data = await session._request("POST", "/cashBalance/getcashbalancesnapshot",
                                  json={"accountId": int(account["id"])})
    if not isinstance(data, dict):
        raise tradovate.TradovateError("unexpected snapshot answer")
    if data.get("errorText"):
        raise tradovate.TradovateError(str(data["errorText"]))
    return {
        "account_id": int(account["id"]), "spec": account.get("spec") or "", "login": session.name,
        "environment": session.environment,
        "realized": _num(data.get("realizedPnL")), "open": _num(data.get("openPnL")),
        "total": _num(data.get("totalPnL")) if data.get("totalPnL") is not None
        else round(_num(data.get("realizedPnL")) + _num(data.get("openPnL")), 2),
        "week": _num(data.get("weekRealizedPnL")), "cash": _num(data.get("totalCashValue")),
        "ts": datetime.now(timezone.utc).isoformat(),
    }


async def risk_settings(session: Any) -> dict[int, dict[str, Any]]:
    """Tradovate's auto-liquidation / risk record per account id (one call per
    login). Prop-firm accounts carry the **trailing max drawdown** here:
    ``trailingMaxDrawdown`` (its size), ``trailingMaxDrawdownLimit`` (the
    balance level the account is liquidated at) and ``trailingMaxDrawdownMode``
    (``EOD`` or ``RealTime`` = intraday). Never raises — an account without a
    readable record simply shows no drawdown."""
    try:
        rows = await session._request("GET", "/userAccountAutoLiq/list") or []
    except Exception:  # noqa: BLE001
        return {}
    out: dict[int, dict[str, Any]] = {}
    for r in rows if isinstance(rows, list) else []:
        try:
            out[int(r.get("id") or r.get("accountId") or 0)] = r
        except (TypeError, ValueError):
            continue
    return out


def drawdown_fields(rec: Optional[dict[str, Any]], cash: float, open_pnl: float) -> dict[str, Any]:
    """Derived drawdown view for one account: mode, size, liquidation level and
    the room left (equity = balance + open P&L, minus the level)."""
    if not rec:
        return {"dd_mode": "", "dd_size": None, "dd_limit": None, "dd_room": None, "daily_loss_limit": None}
    size = rec.get("trailingMaxDrawdown")
    limit = rec.get("trailingMaxDrawdownLimit")
    mode = str(rec.get("trailingMaxDrawdownMode") or "")
    size_f = _num(size) if size not in (None, "") else None
    limit_f = _num(limit) if limit not in (None, "") else None
    room = round(cash + open_pnl - limit_f, 2) if limit_f is not None else None
    daily = rec.get("dailyLossAutoLiq")
    return {"dd_mode": "Intraday" if mode.lower() in ("realtime", "real_time", "intraday") else ("EOD" if mode.upper() == "EOD" else mode),
            "dd_size": size_f, "dd_limit": limit_f, "dd_room": room,
            "daily_loss_limit": _num(daily) if daily not in (None, "", 0) else None}


async def refresh_area(area_id: int) -> dict[str, Any]:
    """Poll every enabled account of an area once; store + broadcast the result."""
    with context.use_area(area_id):
        sessions = [s for s in tradovate.manager_for(area_id).all()
                    if s.enabled and s.has_token() and state.session_status(s.name).get("connected")]
        accounts: list[dict[str, Any]] = []
        errors: list[str] = []
        for s in sessions:
            risk = await risk_settings(s)
            for a in s.accounts:
                if not a.get("id"):
                    continue
                try:
                    snap = await snapshot_account(s, a)
                except Exception as exc:  # noqa: BLE001 - one account failing must not hide the others
                    errors.append(f"{a.get('spec') or a.get('id')}: {exc}")
                    continue
                snap.update(drawdown_fields(risk.get(int(a["id"])), snap["cash"], snap["open"]))
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
        changed = state.set_pnl(summary, area_id)
        if changed:
            state.publish("pnl", summary, area_id)
        try:
            await watch.observe_area(area_id, sessions, accounts)
        except Exception as exc:  # noqa: BLE001 - alerts must never break the P&L feed
            state.log_event("warn", f"position watch failed: {exc}")
        return summary


async def pnl_loop() -> None:
    """Poll all areas; fast while watched, slow while idle, backing off on errors."""
    while True:
        delay = IDLE_INTERVAL_S
        try:
            for aid in db.all_area_ids():
                s = config.load_settings(area_id=aid)
                await watch.tick(aid)   # agent transitions + daily summary (no broker calls)
                fast = float(s.get("pnl_poll_seconds", 5) or 0)
                if fast <= 0:
                    continue  # switched off for this area
                fast = max(2.0, fast)
                # Fast while a dashboard is open — or while trade alerts need a
                # timely view of the broker's positions.
                watched = state.subscriber_count(aid) or watch.trade_alerts_enabled(s)
                interval = fast if watched else IDLE_INTERVAL_S
                try:
                    summary = await refresh_area(aid)
                    if summary["error"] and not summary["accounts"]:
                        _backoff[aid] = min(MAX_BACKOFF_S, max(interval, _backoff.get(aid, interval) * 2))
                    else:
                        _backoff.pop(aid, None)
                except Exception:  # noqa: BLE001
                    _backoff[aid] = min(MAX_BACKOFF_S, max(interval, _backoff.get(aid, interval) * 2))
                delay = min(delay, _backoff.get(aid, interval))
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - the loop must survive anything
            delay = IDLE_INTERVAL_S
        await asyncio.sleep(delay)

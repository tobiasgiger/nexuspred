"""Live account P&L for the Overview.

Every few seconds (``pnl_poll_seconds``, default 5) the bridge asks Tradovate
for each trade account's **cash-balance snapshot** — the broker's own figures
for today's realised P&L, the open (unrealised) P&L of current positions, the
week's realised P&L and the cash balance — and pushes the result to every
open dashboard over the live stream (``kind: "pnl"``). No market-data feed is
needed: Tradovate marks the open positions itself.

Cadence is adaptive: the fast interval only while someone is watching (an
``/api/stream`` subscriber exists for the area), a slow tick otherwise; a
rate-limit / error answer backs the interval off up to two minutes.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from . import config, context, db, state, tradovate

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


async def refresh_area(area_id: int) -> dict[str, Any]:
    """Poll every enabled account of an area once; store + broadcast the result."""
    with context.use_area(area_id):
        sessions = [s for s in tradovate.manager_for(area_id).all()
                    if s.enabled and s.has_token() and state.session_status(s.name).get("connected")]
        accounts: list[dict[str, Any]] = []
        errors: list[str] = []
        for s in sessions:
            for a in s.accounts:
                if not a.get("id"):
                    continue
                try:
                    accounts.append(await snapshot_account(s, a))
                except Exception as exc:  # noqa: BLE001 - one account failing must not hide the others
                    errors.append(f"{a.get('spec') or a.get('id')}: {exc}")
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
        return summary


async def pnl_loop() -> None:
    """Poll all areas; fast while watched, slow while idle, backing off on errors."""
    while True:
        delay = IDLE_INTERVAL_S
        try:
            for aid in db.all_area_ids():
                s = config.load_settings(area_id=aid)
                fast = float(s.get("pnl_poll_seconds", 5) or 0)
                if fast <= 0:
                    continue  # switched off for this area
                fast = max(2.0, fast)
                interval = fast if state.subscriber_count(aid) else IDLE_INTERVAL_S
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

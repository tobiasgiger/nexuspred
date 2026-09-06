"""Trading journal: import executed trades from Tradovate, keep them in SQLite,
and report P&L per day / week / month.

Import (``import_area``) walks every enabled login of an area and reads, per
Tradovate session:

* ``/fill/list`` (+ ``/order/list`` for the account of each fill),
* ``/fillPair/list`` + ``/position/list`` — Tradovate's own entry/exit pairing,
* ``/fillFee/list`` — commissions & exchange fees per fill,
* ``/contract/items`` → ``/contractMaturity/items`` → ``/product/items`` for the
  symbol and the dollar value per point,
* ``/cashBalance/getcashbalancesnapshot`` per trade account (daily equity).

Each fill pair becomes one **round-trip trade** (``journal_trades``): side, qty,
entry/exit price & time, points, gross P&L, fees, net P&L. When the API offers no
pairs, the fills are paired FIFO per account and contract instead. Everything is
keyed by Tradovate ids, so re-importing never duplicates.

Tradovate's REST entity lists cover the current trading session, which is why
the scheduled import runs **once a day after the CME close** (default 23:30
Europe/Zurich, configurable) — plus on demand from the Journal page.

Reporting (``summary`` / ``stats`` / ``calendar``) buckets trades by their exit
time in the journal's timezone.
"""
from __future__ import annotations

import asyncio
from collections import defaultdict, deque
from datetime import date, datetime, time as dtime, timedelta, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

from . import config, context, db, state

FEE_KEYS = ("commission", "clearingFee", "exchangeFee", "nfaFee", "brokerageFee",
            "ipFee", "orderRoutingFee")


# ------------------------------------------------------------------ helpers
def _num(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _ts(v: Any) -> str:
    """Normalise a Tradovate timestamp to ISO-8601 UTC."""
    if not v:
        return ""
    s = str(v).replace("Z", "+00:00")
    try:
        d = datetime.fromisoformat(s)
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d.astimezone(timezone.utc).isoformat()
    except ValueError:
        return s


def _root(symbol: str) -> str:
    from .rollover import parse_contract
    p = parse_contract(symbol)
    return p[0] if p else symbol


def tz() -> ZoneInfo:
    name = str(config.load_settings().get("journal_timezone") or "Europe/Zurich")
    try:
        return ZoneInfo(name)
    except Exception:  # noqa: BLE001 - bad tz name in settings → sane default
        return ZoneInfo("Europe/Zurich")


def fee_total(fee: Optional[dict[str, Any]]) -> float:
    return round(sum(_num((fee or {}).get(k)) for k in FEE_KEYS), 4)


# --------------------------------------------------------- trade building
def build_trade(*, pair_id: Any, buy: dict[str, Any], sell: dict[str, Any], qty: int,
                buy_price: float, sell_price: float, account: dict[str, Any],
                symbol: str, value_per_point: float, fees: dict[Any, dict[str, Any]],
                source: str) -> dict[str, Any]:
    """One round-trip from a buy fill and a sell fill (Tradovate pair or FIFO)."""
    buy_ts, sell_ts = _ts(buy.get("timestamp")), _ts(sell.get("timestamp"))
    long = (buy_ts, int(buy.get("id") or 0)) <= (sell_ts, int(sell.get("id") or 0))
    entry, exit_ = (buy, sell) if long else (sell, buy)
    entry_price, exit_price = (buy_price, sell_price) if long else (sell_price, buy_price)
    points = round(sell_price - buy_price, 6)
    gross = round(points * qty * value_per_point, 2)
    fee = 0.0
    for f in (buy, sell):
        fq = _num(f.get("qty"), 0) or qty
        fee += fee_total(fees.get(f.get("id"))) * (qty / fq)
    fee = round(fee, 2)
    return {
        "pair_id": str(pair_id), "source": source,
        "account_id": int(account.get("id") or 0), "account_spec": str(account.get("spec") or ""),
        "account_name": str(account.get("name") or account.get("spec") or ""),
        "environment": str(account.get("environment") or ""),
        "contract_id": int(entry.get("contractId") or 0), "symbol": symbol, "root": _root(symbol),
        "side": "long" if long else "short", "qty": int(qty),
        "entry_price": entry_price, "exit_price": exit_price,
        "entry_ts": _ts(entry.get("timestamp")), "exit_ts": _ts(exit_.get("timestamp")),
        "entry_fill_id": int(entry.get("id") or 0), "exit_fill_id": int(exit_.get("id") or 0),
        "points": points, "value_per_point": value_per_point,
        "gross_pnl": gross, "fees": fee, "net_pnl": round(gross - fee, 2),
    }


def fifo_pairs(fills: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Pair fills of ONE account+contract first-in-first-out. Returns
    ``{"buy", "sell", "qty", "buy_price", "sell_price"}`` records; partial fills
    are split across matches. Fills that never close stay open (ignored)."""
    ordered = sorted(fills, key=lambda f: (_ts(f.get("timestamp")), int(f.get("id") or 0)))
    open_: deque[dict[str, Any]] = deque()  # lots: {fill, remaining}
    open_side = ""
    out: list[dict[str, Any]] = []
    for f in ordered:
        side = str(f.get("action") or "").lower()
        remaining = int(_num(f.get("qty"), 0))
        if remaining <= 0 or side not in ("buy", "sell"):
            continue
        while remaining > 0 and open_ and open_side != side:
            lot = open_[0]
            take = min(remaining, lot["remaining"])
            buy, sell = (lot["fill"], f) if open_side == "buy" else (f, lot["fill"])
            out.append({"buy": buy, "sell": sell, "qty": take,
                        "buy_price": _num(buy.get("price")), "sell_price": _num(sell.get("price"))})
            lot["remaining"] -= take
            remaining -= take
            if lot["remaining"] == 0:
                open_.popleft()
        if remaining > 0:
            if not open_:
                open_side = side
            open_.append({"fill": f, "remaining": remaining})
    return out


# ---------------------------------------------------------------- importer
class _Reader:
    """Thin wrapper over a session's ``_request`` with tolerant defaults."""

    def __init__(self, session: Any) -> None:
        self.s = session

    async def list(self, path: str) -> list[dict[str, Any]]:
        try:
            data = await self.s._request("GET", path)
        except Exception as exc:  # noqa: BLE001
            raise ImportProblem(f"{path}: {exc}") from exc
        return list(data or []) if isinstance(data, list) else []

    async def items(self, path: str, ids: list[int]) -> dict[int, dict[str, Any]]:
        out: dict[int, dict[str, Any]] = {}
        ids = sorted({int(i) for i in ids if i})
        for i in range(0, len(ids), 50):
            chunk = ids[i:i + 50]
            try:
                data = await self.s._request("GET", path, params={"ids": ",".join(map(str, chunk))})
            except Exception:  # noqa: BLE001 - name lookups are best effort
                continue
            for it in data or []:
                if isinstance(it, dict) and it.get("id") is not None:
                    out[int(it["id"])] = it
        return out


class ImportProblem(Exception):
    pass


async def _contract_info(r: _Reader, contract_ids: list[int]) -> dict[int, tuple[str, float]]:
    """contract id → (symbol, value per point)."""
    contracts = await r.items("/contract/items", contract_ids)
    maturities = await r.items("/contractMaturity/items",
                               [c.get("contractMaturityId") for c in contracts.values()])
    products = await r.items("/product/items", [m.get("productId") for m in maturities.values()])
    out: dict[int, tuple[str, float]] = {}
    for cid, c in contracts.items():
        mat = maturities.get(int(c.get("contractMaturityId") or 0), {})
        prod = products.get(int(mat.get("productId") or 0), {})
        out[cid] = (str(c.get("name") or cid), _num(prod.get("valuePerPoint"), 1.0) or 1.0)
    return out


async def _snapshot(r: _Reader, account_id: int) -> Optional[dict[str, Any]]:
    try:
        data = await r.s._request("POST", "/cashBalance/getcashbalancesnapshot", json={"accountId": account_id})
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(data, dict):
        return None
    return {"total_cash": _num(data.get("totalCashValue")), "realized_pnl": _num(data.get("realizedPnL")),
            "open_pnl": _num(data.get("openPnL")), "week_realized_pnl": _num(data.get("weekRealizedPnL")),
            "total_pnl": _num(data.get("totalPnL"))}


async def import_session(area_id: int, session: Any, *, today: Optional[date] = None) -> dict[str, Any]:
    """Import one login's fills/trades/snapshots. Returns counters."""
    r = _Reader(session)
    accounts_by_id: dict[int, dict[str, Any]] = {}
    for a in session.accounts:
        if a.get("id"):
            accounts_by_id[int(a["id"])] = {"id": int(a["id"]), "spec": a.get("spec") or "",
                                            "name": a.get("spec") or session.name,
                                            "environment": session.environment}
    fills = await r.list("/fill/list")
    orders = {int(o["id"]): o for o in await r.list("/order/list") if o.get("id") is not None}
    fills = [f for f in fills if f.get("id") is not None]
    # account of each fill via its order
    for f in fills:
        o = orders.get(int(f.get("orderId") or 0), {})
        f["_accountId"] = int(o.get("accountId") or 0)
        if not f.get("contractId"):
            f["contractId"] = o.get("contractId")
    fills = [f for f in fills if f["_accountId"] in accounts_by_id]
    fees_list = await r.list("/fillFee/list") if fills else []
    fees = {int(x["id"]): x for x in fees_list if x.get("id") is not None}
    info = await _contract_info(r, [int(f.get("contractId") or 0) for f in fills]) if fills else {}

    fills_new = 0
    for f in fills:
        sym, _vpp = info.get(int(f.get("contractId") or 0), (str(f.get("contractId")), 1.0))
        fills_new += db.upsert_journal_fill(area_id, {
            "fill_id": int(f["id"]), "order_id": int(f.get("orderId") or 0),
            "account_id": f["_accountId"], "contract_id": int(f.get("contractId") or 0),
            "symbol": sym, "ts": _ts(f.get("timestamp")), "action": str(f.get("action") or ""),
            "qty": int(_num(f.get("qty"))), "price": _num(f.get("price")),
            "fees": fee_total(fees.get(int(f["id"]))),
        })

    fills_by_id = {int(f["id"]): f for f in fills}
    trades: list[dict[str, Any]] = []
    pairs: list[dict[str, Any]] = []
    try:
        pairs = await r.list("/fillPair/list")
    except ImportProblem:
        pairs = []
    positions = {int(p["id"]): p for p in await r.list("/position/list") if p.get("id") is not None} if pairs else {}
    used_fill_ids: set[int] = set()
    for p in pairs:
        buy, sell = fills_by_id.get(int(p.get("buyFillId") or 0)), fills_by_id.get(int(p.get("sellFillId") or 0))
        if not buy or not sell:
            continue
        acct = accounts_by_id.get(buy["_accountId"])
        if not acct:
            continue
        cid = int(buy.get("contractId") or positions.get(int(p.get("positionId") or 0), {}).get("contractId") or 0)
        sym, vpp = info.get(cid, (str(cid), 1.0))
        trades.append(build_trade(
            pair_id=f"tv:{p.get('id')}", buy=buy, sell=sell, qty=int(_num(p.get("qty"), 0) or 0),
            buy_price=_num(p.get("buyPrice"), _num(buy.get("price"))),
            sell_price=_num(p.get("sellPrice"), _num(sell.get("price"))),
            account=acct, symbol=sym, value_per_point=vpp, fees=fees, source="fillpair"))
        used_fill_ids.update((int(buy["id"]), int(sell["id"])))
    if not pairs:
        # No pairing from the API: FIFO per account + contract over the fills we have.
        groups: dict[tuple[int, int], list[dict[str, Any]]] = defaultdict(list)
        for f in fills:
            groups[(f["_accountId"], int(f.get("contractId") or 0))].append(f)
        for (aid, cid), fs in groups.items():
            acct = accounts_by_id[aid]
            sym, vpp = info.get(cid, (str(cid), 1.0))
            for m in fifo_pairs(fs):
                trades.append(build_trade(
                    pair_id=f"fifo:{m['buy']['id']}:{m['sell']['id']}:{m['qty']}", buy=m["buy"], sell=m["sell"],
                    qty=m["qty"], buy_price=m["buy_price"], sell_price=m["sell_price"],
                    account=acct, symbol=sym, value_per_point=vpp, fees=fees, source="fifo"))
    trades = [t for t in trades if t["qty"] > 0]
    trades_new = sum(db.upsert_journal_trade(area_id, t) for t in trades)

    day = (today or datetime.now(tz())).isoformat() if isinstance(today, date) else datetime.now(tz()).date().isoformat()
    snapshots = 0
    for aid, acct in accounts_by_id.items():
        snap = await _snapshot(r, aid)
        if snap:
            db.upsert_journal_snapshot(area_id, {"account_id": aid, "account_spec": acct["spec"], "day": day, **snap})
            snapshots += 1
    return {"login": session.name, "accounts": len(accounts_by_id), "fills": len(fills),
            "fills_new": fills_new, "trades": len(trades), "trades_new": trades_new, "snapshots": snapshots}


_locks: dict[int, asyncio.Lock] = {}


async def import_area(area_id: int, *, trigger: str = "manual", user_email: str = "") -> dict[str, Any]:
    """Import every enabled login of an area; record the run in ``journal_imports``."""
    from . import tradovate
    lock = _locks.setdefault(area_id, asyncio.Lock())
    if lock.locked():
        return {"status": "running", "detail": "An import is already running for this area"}
    async with lock:
        started = datetime.now(timezone.utc)
        results: list[dict[str, Any]] = []
        errors: list[str] = []
        with context.use_area(area_id):
            mgr = tradovate.manager_for(area_id)
            sessions = [s for s in mgr.all() if s.enabled and s.has_token()]
            if not sessions:
                errors.append("no enabled Tradovate login with a token")
            for s in sessions:
                try:
                    results.append(await import_session(area_id, s))
                except Exception as exc:  # noqa: BLE001 - one login failing must not stop the others
                    errors.append(f"{s.name}: {exc}")
            totals = {k: sum(r.get(k, 0) for r in results) for k in ("accounts", "fills", "fills_new", "trades", "trades_new", "snapshots")}
            status = "ok" if results and not errors else ("partial" if results else "error")
            rec = {"ts": started.isoformat(), "trigger": trigger, "status": status, "by": user_email,
                   "logins": len(sessions), "error": "; ".join(errors)[:1000], **totals,
                   "duration_ms": round((datetime.now(timezone.utc) - started).total_seconds() * 1000)}
            db.insert_journal_import(area_id, rec)
            config.save_settings({"journal_last_import": rec["ts"]}, area_id=area_id)
            if status == "error":
                state.log_event("warn", f"Journal import failed: {rec['error']}")
            else:
                state.log_event("info", f"Journal import ({trigger}): {totals['trades_new']} new trade(s), "
                                        f"{totals['fills_new']} new fill(s) from {len(results)} login(s)"
                                        + (f" — errors: {rec['error']}" if errors else ""))
        return rec


# --------------------------------------------------------------- scheduler
def next_run(now: datetime, hhmm: str, zone: ZoneInfo) -> datetime:
    """The next occurrence of ``hhmm`` (local time in ``zone``) after ``now``."""
    try:
        hh, mm = (int(x) for x in str(hhmm).split(":")[:2])
    except ValueError:
        hh, mm = 23, 30
    local = now.astimezone(zone)
    candidate = datetime.combine(local.date(), dtime(hh, mm), tzinfo=zone)
    if candidate <= local:
        candidate = datetime.combine(local.date() + timedelta(days=1), dtime(hh, mm), tzinfo=zone)
    return candidate.astimezone(timezone.utc)


async def scheduler_loop() -> None:
    """Import every area at its configured local time, once a day."""
    while True:
        try:
            due: list[tuple[int, datetime]] = []
            now = datetime.now(timezone.utc)
            for aid in db.all_area_ids():
                s = config.load_settings(area_id=aid)
                if not s.get("journal_auto_import", True):
                    continue
                try:
                    zone = ZoneInfo(str(s.get("journal_timezone") or "Europe/Zurich"))
                except Exception:  # noqa: BLE001
                    zone = ZoneInfo("Europe/Zurich")
                due.append((aid, next_run(now, str(s.get("journal_import_time") or "23:30"), zone)))
            if not due:
                await asyncio.sleep(300)
                continue
            soonest = min(t for _, t in due)
            await asyncio.sleep(max(1.0, (soonest - datetime.now(timezone.utc)).total_seconds()))
            now = datetime.now(timezone.utc)
            for aid, when in due:
                if when <= now + timedelta(seconds=30):
                    try:
                        await import_area(aid, trigger="scheduled")
                    except Exception as exc:  # noqa: BLE001
                        with context.use_area(aid):
                            state.log_event("warn", f"Scheduled journal import failed: {exc}")
            await asyncio.sleep(60)  # never fire twice for the same minute
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - the loop must survive anything
            await asyncio.sleep(60)


# --------------------------------------------------------------- reporting
def _local_day(ts: str, zone: ZoneInfo) -> date:
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(zone).date()


def _bucket_key(d: date, period: str) -> str:
    if period == "month":
        return d.strftime("%Y-%m")
    if period == "week":
        y, w, _ = d.isocalendar()
        return f"{y}-W{w:02d}"
    return d.isoformat()


def _bucket_start(d: date, period: str) -> date:
    if period == "month":
        return d.replace(day=1)
    if period == "week":
        return d - timedelta(days=d.weekday())
    return d


def _agg(trades: list[dict[str, Any]]) -> dict[str, Any]:
    wins = [t for t in trades if t["net_pnl"] > 0]
    losses = [t for t in trades if t["net_pnl"] < 0]
    net = round(sum(t["net_pnl"] for t in trades), 2)
    gross_win = sum(t["net_pnl"] for t in wins)
    gross_loss = -sum(t["net_pnl"] for t in losses)
    return {
        "trades": len(trades), "wins": len(wins), "losses": len(losses),
        "net_pnl": net, "gross_pnl": round(sum(t["gross_pnl"] for t in trades), 2),
        "fees": round(sum(t["fees"] for t in trades), 2),
        "win_rate": round(len(wins) / len(trades), 4) if trades else 0.0,
        "avg_win": round(gross_win / len(wins), 2) if wins else 0.0,
        "avg_loss": round(-gross_loss / len(losses), 2) if losses else 0.0,
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss else (None if not gross_win else None),
        "expectancy": round(net / len(trades), 2) if trades else 0.0,
        "largest_win": round(max((t["net_pnl"] for t in wins), default=0.0), 2),
        "largest_loss": round(min((t["net_pnl"] for t in losses), default=0.0), 2),
        "contracts": sum(t["qty"] for t in trades),
    }


def summary(trades: list[dict[str, Any]], period: str, zone: ZoneInfo) -> list[dict[str, Any]]:
    """Per-bucket P&L (day / week / month), chronological, with a running total."""
    period = period if period in ("day", "week", "month") else "day"
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    starts: dict[str, date] = {}
    for t in trades:
        d = _local_day(t["exit_ts"], zone)
        key = _bucket_key(d, period)
        groups[key].append(t)
        starts[key] = _bucket_start(d, period)
    out = []
    running = 0.0
    for key in sorted(groups):
        a = _agg(groups[key])
        running = round(running + a["net_pnl"], 2)
        out.append({"bucket": key, "start": starts[key].isoformat(), **a, "cumulative": running})
    return out


def stats(trades: list[dict[str, Any]], zone: ZoneInfo) -> dict[str, Any]:
    """Overall figures plus breakdowns by symbol root, account, weekday, hour and
    side, the equity curve (per trade) and the longest win/loss streaks."""
    ordered = sorted(trades, key=lambda t: (t["exit_ts"], t["id"]))
    total = _agg(ordered)

    def breakdown(keyfn) -> list[dict[str, Any]]:
        g: dict[Any, list[dict[str, Any]]] = defaultdict(list)
        for t in ordered:
            g[keyfn(t)].append(t)
        return [{"key": k, **_agg(v)} for k, v in sorted(g.items(), key=lambda kv: str(kv[0]))]

    curve, running, peak, max_dd = [], 0.0, 0.0, 0.0
    streak_w = streak_l = best_w = best_l = 0
    for t in ordered:
        running = round(running + t["net_pnl"], 2)
        peak = max(peak, running)
        max_dd = min(max_dd, round(running - peak, 2))
        curve.append({"ts": t["exit_ts"], "equity": running, "trade_id": t["id"]})
        if t["net_pnl"] > 0:
            streak_w, streak_l = streak_w + 1, 0
        elif t["net_pnl"] < 0:
            streak_l, streak_w = streak_l + 1, 0
        best_w, best_l = max(best_w, streak_w), max(best_l, streak_l)
    days = {_local_day(t["exit_ts"], zone) for t in ordered}
    return {
        **total, "max_drawdown": max_dd, "trading_days": len(days),
        "avg_per_day": round(total["net_pnl"] / len(days), 2) if days else 0.0,
        "longest_win_streak": best_w, "longest_loss_streak": best_l,
        "by_symbol": breakdown(lambda t: t["root"] or t["symbol"]),
        "by_account": breakdown(lambda t: t["account_name"] or t["account_spec"] or str(t["account_id"])),
        "by_weekday": breakdown(lambda t: _local_day(t["exit_ts"], zone).weekday()),
        "by_hour": breakdown(lambda t: datetime.fromisoformat(t["exit_ts"].replace("Z", "+00:00")).astimezone(zone).hour),
        "by_side": breakdown(lambda t: t["side"]),
        "equity": curve,
    }


def calendar(trades: list[dict[str, Any]], year: int, month: int, zone: ZoneInfo) -> dict[str, Any]:
    """Net P&L and trade count for every day of a month (for the heat-map)."""
    days: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for t in trades:
        d = _local_day(t["exit_ts"], zone)
        if d.year == year and d.month == month:
            days[d.isoformat()].append(t)
    first = date(year, month, 1)
    nxt = date(year + (month == 12), (month % 12) + 1, 1)
    out = []
    d = first
    while d < nxt:
        a = _agg(days.get(d.isoformat(), []))
        out.append({"day": d.isoformat(), "weekday": d.weekday(), "net_pnl": a["net_pnl"], "trades": a["trades"]})
        d += timedelta(days=1)
    return {"year": year, "month": month, "days": out, **_agg([t for v in days.values() for t in v])}


def range_bounds(preset: str, zone: ZoneInfo, today: Optional[date] = None) -> tuple[str, str]:
    """ISO-UTC (from, to) for a named range in the journal timezone."""
    today = today or datetime.now(zone).date()
    if preset == "today":
        start, end = today, today
    elif preset == "week":
        start, end = today - timedelta(days=today.weekday()), today
    elif preset == "month":
        start, end = today.replace(day=1), today
    elif preset == "ytd":
        start, end = today.replace(month=1, day=1), today
    elif preset == "90d":
        start, end = today - timedelta(days=89), today
    elif preset == "30d":
        start, end = today - timedelta(days=29), today
    else:  # "all"
        return "", ""
    frm = datetime.combine(start, dtime.min, tzinfo=zone).astimezone(timezone.utc).isoformat()
    to = datetime.combine(end + timedelta(days=1), dtime.min, tzinfo=zone).astimezone(timezone.utc).isoformat()
    return frm, to

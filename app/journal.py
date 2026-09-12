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

**History.** The entity *lists* above cover the current session, but the
account's **cash-balance log** (``/cashBalanceLog/list``) is the broker's
book-keeping and reaches back over the account's life: one entry per realised
fill pair (``fillPairId``, ``delta`` = realised P&L) and per fee (``fillId``).
Every import walks that log, looks up the pairs it has not processed yet by id
(``/fillPair/items`` → ``/fill/items`` → contract/product), stores them as
trades with the broker's own realised P&L and fees, and turns the log's
running balance into one equity snapshot per account and trading day. Pair ids
are remembered (``journal_seen``), so after the first run this is incremental.
Tradovate's CSV exports remain available as a manual fallback
(:mod:`app.journal_csv`).

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

from . import config, context, db, http, state

FEE_KEYS = ("commission", "clearingFee", "exchangeFee", "nfaFee", "brokerageFee",
            "ipFee", "orderRoutingFee")

# Dollar value of one full point per contract — used when the product record is
# not available (CSV back-fill, product lookup failure). Unknown roots → 1.0
# (P&L then equals points × qty; the trade still records correctly and the
# multiplier can be fixed later).
VALUE_PER_POINT: dict[str, float] = {
    "ES": 50, "MES": 5, "NQ": 20, "MNQ": 2, "RTY": 50, "M2K": 5, "YM": 5, "MYM": 0.5, "NKD": 5, "EMD": 100,
    "GC": 100, "MGC": 10, "SI": 5000, "SIL": 1000, "HG": 25000, "MHG": 2500, "PL": 50, "PA": 100,
    "CL": 1000, "MCL": 100, "QM": 500, "NG": 10000, "QG": 2500, "MNG": 1000, "RB": 42000, "HO": 42000, "BZ": 1000,
    "ZC": 50, "ZS": 50, "ZW": 50, "ZM": 100, "ZL": 600, "ZO": 50, "KE": 50, "XC": 10, "XK": 10, "XW": 10,
    "ZB": 1000, "ZN": 1000, "ZF": 1000, "ZT": 2000, "UB": 1000, "TN": 1000, "ZQ": 4167, "SR3": 2500,
    "6E": 125000, "6J": 12500000, "6B": 62500, "6A": 100000, "6C": 100000, "6S": 125000, "6N": 100000, "6M": 500000,
    "M6E": 12500, "M6A": 10000, "M6B": 6250, "E7": 62500, "J7": 6250000,
    "BTC": 5, "MBT": 0.1, "ETH": 50, "MET": 0.1,
}


def value_per_point(root: str) -> float:
    return float(VALUE_PER_POINT.get((root or "").upper(), 1.0))


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


def _fid(v: Any) -> int:
    """A fill id as int; a non-numeric id (a CSV column, a broker string id) is hashed, never a crash."""
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return fill_key("id", 0, str(v))


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
    long = (buy_ts, _fid(buy.get("id"))) <= (sell_ts, _fid(sell.get("id")))
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
        "entry_fill_id": _fid(entry.get("id")), "exit_fill_id": _fid(exit_.get("id")),
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
    """Thin wrapper over a session's ``_request`` with tolerant defaults. Records
    what every endpoint returned (counts, column names, a few distinct values —
    never prices or ids) in ``diag`` so an empty import can be explained."""

    def __init__(self, session: Any) -> None:
        self.s = session
        self.diag: dict[str, Any] = {}

    def _note(self, path: str, data: Any, error: str = "") -> None:
        d: dict[str, Any] = {"count": len(data) if isinstance(data, list) else (1 if isinstance(data, dict) else 0)}
        if error:
            d["error"] = error[:300]
        sample = data[0] if isinstance(data, list) and data and isinstance(data[0], dict) else (data if isinstance(data, dict) else None)
        if sample:
            d["columns"] = sorted(sample.keys())[:40]
        prev = self.diag.get(path)
        if prev and "count" in prev:
            d["count"] += prev["count"]
        self.diag[path] = d

    async def list(self, path: str) -> list[dict[str, Any]]:
        try:
            data = await self.s.raw_get(path)
        except Exception as exc:  # noqa: BLE001
            self._note(path, None, str(exc))
            raise ImportProblem(f"{path}: {exc}") from exc
        out = list(data or []) if isinstance(data, list) else []
        self._note(path, out)
        return out

    async def items(self, path: str, ids: list[int]) -> dict[int, dict[str, Any]]:
        out: dict[int, dict[str, Any]] = {}
        ids = sorted({int(i) for i in ids if i})
        for i in range(0, len(ids), 50):
            chunk = ids[i:i + 50]
            try:
                data = await self.s.raw_get(path, params={"ids": ",".join(map(str, chunk))})
            except Exception as exc:  # noqa: BLE001 - name lookups are best effort
                self._note(path, None, str(exc))
                continue
            self._note(path, data if isinstance(data, list) else [])
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
        name = str(c.get("name") or cid)
        vpp = _num(prod.get("valuePerPoint"), 0.0) or value_per_point(_root(name))
        out[cid] = (name, vpp)
    return out


async def _snapshot(r: _Reader, account_id: int) -> Optional[dict[str, Any]]:
    try:
        data = await r.s.cash_snapshot(account_id)
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(data, dict) or not data:
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
    if not accounts_by_id or any(not a.get("id") for a in session.accounts):
        # Accounts saved before "Connect & Verify" carry no ids: ask the broker.
        try:
            for a in await r.list("/account/list"):
                if a.get("id") and a.get("name"):
                    accounts_by_id.setdefault(int(a["id"]), {"id": int(a["id"]), "spec": str(a["name"]),
                                                              "name": str(a["name"]), "environment": session.environment})
        except ImportProblem:
            pass
    r.diag["accounts"] = [{"id": a["id"], "spec": a["spec"]} for a in accounts_by_id.values()]
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
    # Pairs whose fills are not in the session list (the pair list reaching
    # further back than the fill list): fetch those fills by id.
    missing = [int(p.get(k) or 0) for p in pairs for k in ("buyFillId", "sellFillId")
               if int(p.get(k) or 0) and int(p.get(k) or 0) not in fills_by_id]
    if missing:
        wanted = set(missing)
        extra = {fid: f for fid, f in (await r.items("/fill/items", missing)).items() if fid in wanted}
        if extra:
            extra_orders = await r.items("/order/items", [f.get("orderId") for f in extra.values()])
            for fid, f in extra.items():
                o = extra_orders.get(int(f.get("orderId") or 0), {})
                f["_accountId"] = int(o.get("accountId") or 0)
                if not f.get("contractId"):
                    f["contractId"] = o.get("contractId")
                fills_by_id[fid] = f
            more = await _contract_info(r, [int(f.get("contractId") or 0) for f in extra.values()
                                            if int(f.get("contractId") or 0) not in info])
            info.update(more)
    used_fill_ids: set[int] = set()
    for p in pairs:
        buy, sell = fills_by_id.get(int(p.get("buyFillId") or 0)), fills_by_id.get(int(p.get("sellFillId") or 0))
        if not buy or not sell:
            continue
        acct = accounts_by_id.get(buy.get("_accountId") or 0)
        if not acct:
            continue
        cid = int(buy.get("contractId") or positions.get(int(p.get("positionId") or 0), {}).get("contractId") or 0)
        sym, vpp = info.get(cid, (str(cid), 1.0))
        trades.append(build_trade(
            pair_id=f"pair:{buy['id']}:{sell['id']}", buy=buy, sell=sell, qty=int(_num(p.get("qty"), 0) or 0),
            buy_price=_num(p.get("buyPrice"), _num(buy.get("price"))),
            sell_price=_num(p.get("sellPrice"), _num(sell.get("price"))),
            account=acct, symbol=sym, value_per_point=vpp, fees=fees, source="fillpair"))
        used_fill_ids.update((int(buy["id"]), int(sell["id"])))
    if not pairs:
        # No pairing from the API: FIFO per account + contract over every stored
        # fill (the session list alone can start inside an open position).
        groups: dict[tuple[int, int], list[dict[str, Any]]] = defaultdict(list)
        for row in db.journal_fills_for(area_id, list(accounts_by_id)):
            groups[(int(row["account_id"]), int(row["contract_id"] or 0))].append(
                {"id": int(row["fill_id"]), "timestamp": row["ts"], "action": row["action"], "qty": int(row["qty"] or 0),
                 "price": float(row["price"] or 0), "contractId": int(row["contract_id"] or 0), "symbol": row["symbol"]})
            fees.setdefault(int(row["fill_id"]), {"commission": float(row["fees"] or 0)})
        for (aid, cid), fs in groups.items():
            acct = accounts_by_id[aid]
            sym, vpp = info.get(cid, (str(fs[0].get("symbol") or cid), value_per_point(_root(str(fs[0].get("symbol") or "")))))
            for m in fifo_pairs(fs):
                trades.append(build_trade(
                    pair_id=f"fifo:{m['buy']['id']}:{m['sell']['id']}:{m['qty']}", buy=m["buy"], sell=m["sell"],
                    qty=m["qty"], buy_price=m["buy_price"], sell_price=m["sell_price"],
                    account=acct, symbol=sym, value_per_point=vpp, fees=fees, source="fifo"))
    trades = [t for t in trades if t["qty"] > 0]
    trades_new = sum(db.upsert_journal_trade(area_id, t) for t in trades
                     if not db.find_similar_journal_trade(area_id, t))

    # Past sessions from the cash-balance log (incremental; see module docstring).
    hist = {"history_pairs": 0, "history_new": 0, "history_snapshots": 0, "history_error": ""}
    try:
        rep = await _history_from_reports(area_id, session, accounts_by_id, r.diag, today=today if isinstance(today, date) else None)
        hist["history_new"] += rep["report_new"]
        hist["report_windows"] = rep["report_windows"]
        hist["report_rows"] = rep["report_rows"]
        hist["report_pending_days"] = rep.get("report_pending_days", 0)
        if rep["report_error"]:
            hist["history_error"] = rep["report_error"]
    except Exception as exc:  # noqa: BLE001 - history must never break the session import
        hist["history_error"] = f"reports: {exc}"
    try:
        book = await _history_from_cash_log(area_id, r, accounts_by_id, fees, info)
        hist["history_pairs"] = book["history_pairs"]
        hist["history_new"] += book["history_new"]
        hist["history_snapshots"] = book["history_snapshots"]
        if book["history_error"]:
            hist["history_error"] = (hist["history_error"] + "; " + book["history_error"]).strip("; ")
    except Exception as exc:  # noqa: BLE001 - history must never break the session import
        hist["history_error"] = (hist["history_error"] + f"; cash log: {exc}").strip("; ")

    day = today.isoformat() if isinstance(today, date) else datetime.now(tz()).date().isoformat()
    snapshots = 0
    for aid, acct in accounts_by_id.items():
        snap = await _snapshot(r, aid)
        if snap:
            db.upsert_journal_snapshot(area_id, {"account_id": aid, "account_spec": acct["spec"], "day": day, **snap})
            snapshots += 1
    r.diag["fills_for_my_accounts"] = len(fills)
    r.diag["pairs_resolved"] = len(trades)
    return {"login": session.name, "accounts": len(accounts_by_id), "fills": len(fills),
            "fills_new": fills_new, "trades": len(trades), "trades_new": trades_new, "snapshots": snapshots,
            **hist, "diag": r.diag}


# --------------------------------------------------- reporting service
# The Tradovate web platform builds its Reports tab (Performance, Fills, …)
# through a separate reporting service — hosts ``rpt-live`` / ``rpt-demo`` on
# the API domain, ``GET /v1/reports/requestreportdefinitions`` and
# ``POST /v1/reports/requestreport`` — authenticated with the same access
# token. Unlike the entity lists it covers any date range, which is what makes
# the journal's history import possible without a manual export.
REPORT_HOSTS = {"live": "https://rpt-live.tradovateapi.com", "demo": "https://rpt-demo.tradovateapi.com"}
REPORT_WINDOW_DAYS = 30       # first try; halved on "Too long range" until the service accepts it
REPORT_MIN_WINDOW_DAYS = 1
REPORT_OVERLAP_DAYS = 3       # re-read the last days so late bookings are not missed
REPORT_MAX_WINDOWS_PER_RUN = 150  # requests per login per run; the rest follows next run
REPORT_PAUSE_S = 0.25         # be gentle with the reporting service


async def _request_report(session: Any, name: str, params: list[tuple[str, str]],
                          timezone_minutes: int = 0) -> dict[str, Any]:
    """``POST reports/requestreport`` on the reporting host of the session's
    environment. Returns the JSON body (``data`` = CSV text on success,
    ``errorText`` on failure). Honours Tradovate's time-penalty answer once."""
    token = await session._get_token()
    host = REPORT_HOSTS["live" if session.environment == "live" else "demo"]
    body = {"name": name, "params": [{"name": k, "value": v} for k, v in params],
            "representationType": "csv", "timezone": timezone_minutes}
    for attempt in (1, 2):
        resp = await http.client("tradovate").post(
            f"{host}/v1/reports/requestreport", json=body,
            headers={"Authorization": f"Bearer {token}"}, timeout=90.0)
        if resp.status_code >= 400:
            raise ImportProblem(f"reports/requestreport {name}: {resp.status_code} {resp.text[:200]}")
        data = resp.json() if resp.text else {}
        if isinstance(data, dict) and data.get("p-ticket") and attempt == 1:
            await asyncio.sleep(min(30.0, float(data.get("p-time") or 1)))
            body["p-ticket"] = data["p-ticket"]
            continue
        return data if isinstance(data, dict) else {"data": data}
    return {}


async def _report_definitions(session: Any) -> list[dict[str, Any]]:
    token = await session._get_token()
    host = REPORT_HOSTS["live" if session.environment == "live" else "demo"]
    resp = await http.client("tradovate").get(f"{host}/v1/reports/requestreportdefinitions",
                                              headers={"Authorization": f"Bearer {token}"}, timeout=30.0)
    if resp.status_code >= 400:
        raise ImportProblem(f"reports/requestreportdefinitions: {resp.status_code} {resp.text[:200]}")
    data = resp.json() if resp.text else []
    return list(data) if isinstance(data, list) else []


def _mmdd(d: date) -> str:
    return d.strftime("%m/%d/%Y")


async def _history_from_reports(area_id: int, session: Any, accounts_by_id: dict[int, dict[str, Any]],
                                diag: dict[str, Any], *, today: Optional[date] = None) -> dict[str, Any]:
    """Pull the **Performance** report (one row per round trip, with the
    broker's P&L) per account for every day not yet covered, parse it like a
    CSV export and store the trades. A per-account cursor in the area settings
    makes later runs incremental. Returns counters."""
    from . import journal_csv
    from zoneinfo import ZoneInfo as _Z
    s = config.load_settings(area_id=area_id)
    today = today or datetime.now(tz()).date()
    history_days = max(1, int(s.get("journal_history_days", 365) or 365))
    fee_per_side = float(s.get("journal_fee_per_side", 0) or 0)
    cursors: dict[str, str] = dict(s.get("journal_report_cursor") or {})
    window = int(s.get("journal_report_window") or REPORT_WINDOW_DAYS)
    window = max(REPORT_MIN_WINDOW_DAYS, min(window, REPORT_WINDOW_DAYS))
    out = {"report_windows": 0, "report_rows": 0, "report_new": 0, "report_error": "", "report_pending_days": 0}
    rdiag: dict[str, Any] = {}
    diag["reports"] = rdiag
    try:
        defs = await _report_definitions(session)
        rdiag["definitions"] = [{"name": d.get("name"), "params": [p.get("name") for p in (d.get("params") or [])]}
                                for d in defs if isinstance(d, dict)][:30]
    except Exception as exc:  # noqa: BLE001 - definitions are informational only
        rdiag["definitions_error"] = str(exc)[:300]
    errors: list[str] = []
    windows_done = 0
    for aid, acct in accounts_by_id.items():
        spec = acct.get("spec") or ""
        if not spec:
            continue
        cursor = cursors.get(spec)
        try:
            start = date.fromisoformat(cursor) - timedelta(days=REPORT_OVERLAP_DAYS) if cursor else today - timedelta(days=history_days)
        except ValueError:
            start = today - timedelta(days=history_days)
        adiag = rdiag.setdefault(spec, {"windows": 0, "rows": 0, "new": 0})
        reached: Optional[date] = None
        while start <= today and windows_done < REPORT_MAX_WINDOWS_PER_RUN:
            end = min(start + timedelta(days=window - 1), today)
            windows_done += 1
            adiag["windows"] += 1
            if windows_done > 1:
                await asyncio.sleep(REPORT_PAUSE_S)
            try:
                body = await _request_report(session, "Performance", [
                    ("startDate", _mmdd(start)), ("endDate", _mmdd(end)), ("account", spec)])
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{spec} {start}..{end}: {exc}")
                adiag["error"] = str(exc)[:300]
                break
            text = body.get("data") if isinstance(body, dict) else None
            if not isinstance(text, str):
                err = str((body or {}).get("errorText") or f"unexpected answer keys {sorted((body or {}).keys())[:8]}")
                if "long range" in err.lower() and window > REPORT_MIN_WINDOW_DAYS:
                    # The service caps the span per request: shrink and retry the same start.
                    window = max(REPORT_MIN_WINDOW_DAYS, window // 2)
                    adiag["window_days"] = window
                    continue
                errors.append(f"{spec} {start}..{end}: {err}")
                adiag["error"] = err[:300]
                break
            if text.strip():
                try:
                    parsed = journal_csv.parse(text, zone=_Z("UTC"), account=acct, fee_per_side=fee_per_side, source="report")
                except journal_csv.CsvError as exc:
                    errors.append(f"{spec} {start}..{end}: {exc}")
                    adiag["error"] = str(exc)[:300]
                    adiag["columns"] = text.split("\n", 1)[0][:300]
                    break
                adiag["rows"] += parsed["rows"]
                adiag.setdefault("columns", text.split("\n", 1)[0].strip()[:300])
                new = 0
                for t in parsed["trades"]:
                    if db.find_similar_journal_trade(area_id, t):
                        continue
                    new += db.upsert_journal_trade(area_id, t)
                adiag["new"] += new
                out["report_rows"] += parsed["rows"]
                out["report_new"] += new
            out["report_windows"] += 1
            reached = end
            start = end + timedelta(days=1)
        if reached is not None and (not cursor or reached >= date.fromisoformat(cursor)):
            cursors[spec] = reached.isoformat()  # only what was actually covered
        if reached is not None and reached < today:
            out["report_pending_days"] += (today - reached).days
            adiag["pending_days"] = (today - reached).days
    updates: dict[str, Any] = {}
    if cursors != (s.get("journal_report_cursor") or {}):
        updates["journal_report_cursor"] = cursors
    if window != int(s.get("journal_report_window") or REPORT_WINDOW_DAYS):
        updates["journal_report_window"] = window  # remember the span the service accepts
    if updates:
        config.save_settings(updates, area_id=area_id)
    out["report_error"] = "; ".join(errors)[:800]
    return out


FEE_CHANGE_TYPES = {"commission", "clearingfee", "exchangefee", "nfafee", "brokeragefee", "ipfee",
                    "orderroutingfee", "fee", "otherfee"}
HISTORY_MAX_PAIRS_PER_RUN = 2000  # ≈ 40 + 80 + … batched requests; the rest follows next run


def _trade_day(entry: dict[str, Any], zone: ZoneInfo) -> str:
    td = entry.get("tradeDate")
    if isinstance(td, dict) and td.get("year"):
        try:
            return date(int(td["year"]), int(td["month"]), int(td["day"])).isoformat()
        except (ValueError, TypeError):
            pass
    elif isinstance(td, str) and len(td) >= 10:
        return td[:10]
    ts = _ts(entry.get("timestamp"))
    if ts:
        return datetime.fromisoformat(ts).astimezone(zone).date().isoformat()
    return ""


async def _history_from_cash_log(area_id: int, r: _Reader, accounts_by_id: dict[int, dict[str, Any]],
                                 fees: dict[int, dict[str, Any]], info: dict[int, tuple[str, float]]) -> dict[str, Any]:
    """Trades + daily equity for past sessions, from ``/cashBalanceLog/list``."""
    raw_log = await r.list("/cashBalanceLog/list")
    if not raw_log:
        # Some tenants only answer per account.
        for aid in accounts_by_id:
            try:
                raw_log += await r.list(f"/cashBalanceLog/deps?masterid={aid}")
            except ImportProblem:
                pass
    from collections import Counter
    zone = tz()
    r.diag["cash_log"] = {
        "entries": len(raw_log),
        "accounts": sorted({int(e.get("accountId") or 0) for e in raw_log})[:20],
        "change_types": dict(Counter(str(e.get("cashChangeType") or "?") for e in raw_log).most_common(15)),
        "with_fillPairId": sum(1 for e in raw_log if e.get("fillPairId")),
        "with_tradeId": sum(1 for e in raw_log if e.get("tradeId")),
        "with_fillId": sum(1 for e in raw_log if e.get("fillId")),
        "with_delta": sum(1 for e in raw_log if e.get("delta") is not None),
        "first_trade_date": min((_trade_day(e, zone) for e in raw_log), default=""),
        "last_trade_date": max((_trade_day(e, zone) for e in raw_log), default=""),
    }
    log = [e for e in raw_log if int(e.get("accountId") or 0) in accounts_by_id]
    r.diag["cash_log"]["entries_for_my_accounts"] = len(log)
    out = {"history_pairs": 0, "history_new": 0, "history_snapshots": 0, "history_error": ""}
    if not log:
        return out
    zone = tz()

    def pair_ref(e: dict[str, Any]) -> int:
        """The fill-pair id of a realised-P&L entry: ``fillPairId``, else the
        ``tradeId`` of a FillPair-typed entry (field naming differs by tenant)."""
        pid = int(e.get("fillPairId") or e.get("fillPairID") or 0)
        if not pid and "fillpair" in str(e.get("cashChangeType") or "").replace("_", "").lower():
            pid = int(e.get("tradeId") or e.get("pairId") or 0)
        return pid

    # --- fees per fill and realised P&L per pair, straight from the book -------
    pair_pnl: dict[int, float] = defaultdict(float)
    pair_account: dict[int, int] = {}
    fee_by_fill: dict[int, float] = defaultdict(float)
    for e in log:
        pid = pair_ref(e)
        fid = int(e.get("fillId") or 0)
        delta = _num(e.get("delta"), _num(e.get("realizedPnL"), 0.0)) if pid else _num(e.get("delta"), 0.0)
        kind = str(e.get("cashChangeType") or "").replace("_", "").lower()
        if pid:
            pair_pnl[pid] += delta
            pair_account[pid] = int(e.get("accountId") or 0)
        elif fid and (kind in FEE_CHANGE_TYPES or delta < 0):
            fee_by_fill[fid] += -delta

    # --- pairs not processed before ---------------------------------------
    r.diag["cash_log"]["pairs_in_book"] = len(pair_pnl)
    todo = db.journal_unseen(area_id, "fillpair", sorted(pair_pnl, reverse=True))[:HISTORY_MAX_PAIRS_PER_RUN]
    out["history_pairs"] = len(todo)
    trades: list[dict[str, Any]] = []
    done: list[int] = []
    if todo:
        pairs = await r.items("/fillPair/items", todo)
        fill_ids = [int(p.get(k) or 0) for p in pairs.values() for k in ("buyFillId", "sellFillId")]
        fills = await r.items("/fill/items", fill_ids)
        need_orders = [f.get("orderId") for f in fills.values() if not f.get("contractId")]
        orders = await r.items("/order/items", need_orders) if need_orders else {}
        for f in fills.values():
            if not f.get("contractId"):
                f["contractId"] = orders.get(int(f.get("orderId") or 0), {}).get("contractId")
        more = await _contract_info(r, [int(f.get("contractId") or 0) for f in fills.values()
                                        if int(f.get("contractId") or 0) not in info])
        info.update(more)
        for pid in todo:
            p = pairs.get(int(pid))
            if not p:
                continue  # not retrievable (yet) — retried next run
            buy, sell = fills.get(int(p.get("buyFillId") or 0)), fills.get(int(p.get("sellFillId") or 0))
            if not buy or not sell:
                continue
            acct = accounts_by_id.get(pair_account.get(int(pid), 0))
            if not acct:
                done.append(pid)
                continue
            qty = int(_num(p.get("qty"), 0) or 0)
            cid = int(buy.get("contractId") or sell.get("contractId") or 0)
            sym, vpp = info.get(cid, (str(cid), 1.0))
            # fees: the book's per-fill fee (or the fillFee record), pro-rated by qty
            fee_recs: dict[Any, dict[str, Any]] = {}
            for f in (buy, sell):
                fid = int(f.get("id") or 0)
                if fid in fee_by_fill:
                    fee_recs[fid] = {"commission": round(fee_by_fill[fid], 4)}
                elif fid in fees:
                    fee_recs[fid] = fees[fid]
            t = build_trade(pair_id=f"pair:{buy['id']}:{sell['id']}", buy=buy, sell=sell, qty=qty,
                            buy_price=_num(p.get("buyPrice"), _num(buy.get("price"))),
                            sell_price=_num(p.get("sellPrice"), _num(sell.get("price"))),
                            account=acct, symbol=sym, value_per_point=vpp, fees=fee_recs, source="history")
            realised = round(pair_pnl[int(pid)], 2)
            if realised and t["points"] and qty:   # the book's figure is authoritative
                t["gross_pnl"] = realised
                t["value_per_point"] = round(abs(realised / (t["points"] * qty)), 6)
                t["net_pnl"] = round(realised - t["fees"], 2)
            if t["qty"] > 0:
                trades.append(t)
                done.append(pid)
        out["history_new"] = sum(db.upsert_journal_trade(area_id, t) for t in trades
                                 if not db.find_similar_journal_trade(area_id, t))
        db.journal_mark_seen(area_id, "fillpair", done)

    # --- daily equity from the running balance -----------------------------
    by_day: dict[tuple[int, str], dict[str, Any]] = {}
    for e in sorted(log, key=lambda e: (_ts(e.get("timestamp")), int(e.get("id") or 0))):
        aid = int(e.get("accountId") or 0)
        day = _trade_day(e, zone)
        if not day:
            continue
        rec = by_day.setdefault((aid, day), {"realized": 0.0, "amount": None})
        rec["realized"] += _num(e.get("delta"), 0.0) if pair_ref(e) else 0.0
        if e.get("amount") is not None:
            rec["amount"] = _num(e.get("amount"))
    today_iso = datetime.now(zone).date().isoformat()
    for (aid, day), rec in by_day.items():
        if day == today_iso or rec["amount"] is None:
            continue  # today comes from the live snapshot
        db.upsert_journal_snapshot(area_id, {"account_id": aid, "account_spec": accounts_by_id[aid]["spec"], "day": day,
                                             "total_cash": rec["amount"], "realized_pnl": round(rec["realized"], 2),
                                             "open_pnl": 0.0, "week_realized_pnl": 0.0, "total_pnl": 0.0})
        out["history_snapshots"] += 1
    return out


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
                errors.append("no enabled broker login with credentials")
            for s in sessions:
                try:
                    results.append(await IMPORTERS.get(getattr(s, "kind", "tradovate"), import_session)(area_id, s))
                except Exception as exc:  # noqa: BLE001 - one login failing must not stop the others
                    errors.append(f"{s.name}: {exc}")
            totals = {k: sum(r.get(k, 0) for r in results)
                      for k in ("accounts", "fills", "fills_new", "trades", "trades_new", "snapshots", "history_pairs", "history_new", "history_snapshots")}
            errors += [f"{r.get('login')}: history: {r['history_error']}" for r in results if r.get("history_error")]
            status = "ok" if results and not errors else ("partial" if results else "error")
            import json as _json
            detail = _json.dumps({res.get("login", "?"): res.get("diag", {}) for res in results}, default=str)[:20000]
            rec = {"ts": started.isoformat(), "trigger": trigger, "status": status, "by": user_email,
                   "logins": len(sessions), "error": "; ".join(errors)[:1000], "detail": detail, **totals,
                   "duration_ms": round((datetime.now(timezone.utc) - started).total_seconds() * 1000)}
            db.insert_journal_import(area_id, rec)
            config.save_settings({"journal_last_import": rec["ts"]}, area_id=area_id)
            if status == "error":
                state.log_event("warn", f"Journal import failed: {rec['error']}")
            else:
                state.log_event("info", f"Journal import ({trigger}): {totals['trades_new']} new trade(s), "
                                        f"{totals['fills_new']} new fill(s), {totals['history_new']} from history "
                                        f"({totals['history_snapshots']} daily balances) from {len(results)} login(s)"
                                        + (f" — errors: {rec['error']}" if errors else ""))
        return rec


# ------------------------------------------- other brokers (ProjectX, Rithmic)
# Neither exposes Tradovate's fill pairs: their trade / fill history is read as
# fills and paired FIFO per account + contract (the same pairing the Tradovate
# importer falls back to). The first run of an account reaches
# ``journal_history_days`` back, later runs re-read the last OTHER_OVERLAP_DAYS
# (stored fills and trades are idempotent, so overlap is free).
OTHER_OVERLAP_DAYS = 7
EQUITY_POINTS_MAX = 2000
OTHER_MAX_HISTORY_DAYS = 365


def fill_key(broker: str, account_id: int, raw_id: Any) -> int:
    """A stable 63-bit id for a ProjectX / Rithmic execution: namespaced by
    broker and account so it can never collide with a Tradovate fill id in the
    same workspace (fill ids are unique per area in ``journal_fills``)."""
    import hashlib
    digest = hashlib.blake2b(f"{broker}:{account_id}:{raw_id}".encode(), digest_size=8).digest()
    return (int.from_bytes(digest, "big") & 0x7FFF_FFFF_FFFF_FFFF) or 1


def _lookback_days(area_id: int, account_id: int, settings: dict[str, Any]) -> int:
    known = {int(a.get("account_id") or 0) for a in db.journal_accounts(area_id)}
    if account_id in known:
        return OTHER_OVERLAP_DAYS
    try:
        days = int(settings.get("journal_history_days") or 365)
    except (TypeError, ValueError):
        days = 365
    return max(1, min(OTHER_MAX_HISTORY_DAYS, days))


def _accounts_of(session: Any) -> dict[int, dict[str, Any]]:
    out: dict[int, dict[str, Any]] = {}
    for a in session.accounts:
        if a.get("id") and a.get("enabled", True):
            out[int(a["id"])] = {"id": int(a["id"]), "spec": str(a.get("spec") or ""),
                                 "name": str(a.get("spec") or session.name), "environment": session.environment}
    return out


async def _import_fills(area_id: int, session: Any, accounts_by_id: dict[int, dict[str, Any]],
                        fills: list[dict[str, Any]], fees: dict[int, dict[str, Any]],
                        info: dict[int, tuple[str, float]], diag: dict[str, Any], *, today: Optional[date] = None) -> dict[str, Any]:
    """Store broker-neutral fills, then pair the account's **whole stored fill
    history** FIFO (a fetch window that starts inside an open position would
    otherwise turn its exit into a phantom entry), store the trades and today's
    balance snapshot. A fill: ``id, orderId, contractId, timestamp, action
    (Buy/Sell), qty, price, _accountId``; ``fees`` per fill id; ``info`` per
    contract id → (symbol, value per point). The database work runs on a worker
    thread: a year of fills must not stall the order path."""
    fills = [f for f in fills if f.get("id") is not None and f.get("_accountId") in accounts_by_id and int(_num(f.get("qty"))) > 0]

    def store() -> tuple[int, int, int]:
        fills_new = 0
        for f in fills:
            sym, _vpp = info.get(int(f.get("contractId") or 0), (str(f.get("contractId")), 1.0))
            fills_new += db.upsert_journal_fill(area_id, {
                "fill_id": int(f["id"]), "order_id": int(f.get("orderId") or 0),
                "account_id": int(f["_accountId"]), "contract_id": int(f.get("contractId") or 0),
                "symbol": sym, "ts": _ts(f.get("timestamp")), "action": str(f.get("action") or ""),
                "qty": int(_num(f.get("qty"))), "price": _num(f.get("price")),
                "fees": fee_total(fees.get(int(f["id"]))),
            })
        # the pairing input: every stored fill of these accounts (fresh ones included)
        stored = db.journal_fills_for(area_id, list(accounts_by_id))
        all_fees: dict[Any, dict[str, Any]] = {r["fill_id"]: {"commission": float(r["fees"] or 0)} for r in stored}
        groups: dict[tuple[int, int], list[dict[str, Any]]] = defaultdict(list)
        for r in stored:
            groups[(int(r["account_id"]), int(r["contract_id"] or 0))].append(
                {"id": int(r["fill_id"]), "timestamp": r["ts"], "action": r["action"], "qty": int(r["qty"] or 0),
                 "price": float(r["price"] or 0), "contractId": int(r["contract_id"] or 0), "symbol": r["symbol"]})
        trades: list[dict[str, Any]] = []
        for (aid, cid), fs in groups.items():
            acct = accounts_by_id[aid]
            sym, vpp = info.get(cid, (str(fs[0].get("symbol") or cid), value_per_point(_root(str(fs[0].get("symbol") or "")))))
            for m in fifo_pairs(fs):
                trades.append(build_trade(
                    pair_id=f"fifo:{aid}:{m['buy']['id']}:{m['sell']['id']}:{m['qty']}", buy=m["buy"], sell=m["sell"],
                    qty=m["qty"], buy_price=m["buy_price"], sell_price=m["sell_price"],
                    account=acct, symbol=sym, value_per_point=vpp, fees=all_fees, source="fifo"))
        trades = [t for t in trades if t["qty"] > 0]
        trades_new = sum(db.upsert_journal_trade(area_id, t) for t in trades if not db.find_similar_journal_trade(area_id, t))
        return fills_new, len(trades), trades_new

    fills_new, n_trades, trades_new = await asyncio.to_thread(store)
    day = today.isoformat() if isinstance(today, date) else datetime.now(tz()).date().isoformat()
    snapshots = 0
    for aid, acct in accounts_by_id.items():
        try:
            data = await session.cash_snapshot(aid)
        except Exception as exc:  # noqa: BLE001 - a balance is a nicety, never a failed import
            diag[f"snapshot {acct['spec']}"] = {"error": str(exc)[:200]}
            continue
        if isinstance(data, dict) and data:
            db.upsert_journal_snapshot(area_id, {"account_id": aid, "account_spec": acct["spec"], "day": day,
                                                 "total_cash": _num(data.get("totalCashValue")), "realized_pnl": _num(data.get("realizedPnL")),
                                                 "open_pnl": _num(data.get("openPnL")), "week_realized_pnl": _num(data.get("weekRealizedPnL")),
                                                 "total_pnl": _num(data.get("totalPnL"))})
            snapshots += 1
    diag["accounts"] = [{"id": a["id"], "spec": a["spec"]} for a in accounts_by_id.values()]
    diag["fills_for_my_accounts"] = len(fills)
    diag["pairs_resolved"] = n_trades
    return {"login": session.name, "accounts": len(accounts_by_id), "fills": len(fills), "fills_new": fills_new,
            "trades": n_trades, "trades_new": trades_new, "snapshots": snapshots,
            "history_pairs": 0, "history_new": 0, "history_snapshots": 0, "history_error": "", "diag": diag}


def _side_fee(settings: dict[str, Any], qty: int) -> float:
    return round(_num(settings.get("journal_fee_per_side"), 0.0) * max(0, qty), 4)


async def import_projectx(area_id: int, session: Any, *, today: Optional[date] = None) -> dict[str, Any]:
    """ProjectX (TopstepX, Bulenox, …): ``POST /api/Trade/search`` per account is
    the fill list (each row one execution with size, price, side and fees)."""
    from .projectx import _int_id
    settings = config.load_settings(area_id=area_id)
    accounts_by_id = _accounts_of(session)
    diag: dict[str, Any] = {}
    now = datetime.now(timezone.utc)
    fills: list[dict[str, Any]] = []
    fees: dict[int, dict[str, Any]] = {}
    info: dict[int, tuple[str, float]] = {}
    errors: list[str] = []
    for aid, acct in accounts_by_id.items():
        start = now - timedelta(days=_lookback_days(area_id, aid, settings))
        try:
            data = await session._post("/api/Trade/search", {"accountId": aid, "startTimestamp": start.isoformat()})
        except Exception as exc:  # noqa: BLE001 - one account failing must not stop the others
            errors.append(f"{acct['spec']}: {exc}")
            diag[f"Trade/search {acct['spec']}"] = {"error": str(exc)[:300]}
            continue
        rows = [t for t in (data.get("trades") or []) if isinstance(t, dict)] if isinstance(data, dict) else []
        diag[f"Trade/search {acct['spec']}"] = {"count": len(rows), "columns": sorted(rows[0].keys())[:40] if rows else [], "since": start.date().isoformat()}
        for t in rows:
            if t.get("voided") or t.get("id") is None:
                continue
            gid = str(t.get("contractId") or "")
            if not gid:
                continue
            cid = _int_id(gid)
            if cid not in info:
                name = await session._contract_name(gid)
                ci = await session.contract_info(cid)
                sym = str(ci.get("name") or name or gid).upper()
                tick_size, tick_value = _num(ci.get("tickSize"), 0.0), _num(ci.get("tickValue"), 0.0)
                vpp = round(tick_value / tick_size, 6) if tick_size > 0 and tick_value > 0 else value_per_point(_root(sym))
                info[cid] = (sym, vpp)
            fid = fill_key("projectx", aid, t["id"])
            qty = int(_num(t.get("size")))
            fills.append({"id": fid, "orderId": _int_id(str(t.get("orderId") or 0)), "contractId": cid,
                          "timestamp": _ts(t.get("creationTimestamp")), "action": "Buy" if int(_num(t.get("side"), 0)) == 0 else "Sell",
                          "qty": qty, "price": _num(t.get("price")), "_accountId": aid})
            fee = _num(t.get("fees"), 0.0)
            fees[fid] = {"commission": round(abs(fee), 4) if fee else _side_fee(settings, qty)}
    if errors and len(errors) == len(accounts_by_id) and accounts_by_id:
        raise ImportProblem("; ".join(errors))
    out = await _import_fills(area_id, session, accounts_by_id, fills, fees, info, diag, today=today)
    if errors:
        out["history_error"] = "; ".join(errors)[:500]
    return out


async def import_rithmic(area_id: int, session: Any, *, today: Optional[date] = None) -> dict[str, Any]:
    """Rithmic: the order plant's fill history (``RequestShowFillHistory``) per
    account. Rithmic reports no fees — ``journal_fee_per_side`` applies."""
    from .rithmic import _int_id, _root as _rroot
    settings = config.load_settings(area_id=area_id)
    accounts_by_id = _accounts_of(session)
    diag: dict[str, Any] = {}
    now = datetime.now(timezone.utc)
    client = await session._ensure()
    fills: list[dict[str, Any]] = []
    fees: dict[int, dict[str, Any]] = {}
    info: dict[int, tuple[str, float]] = {}
    errors: list[str] = []
    for aid, acct in accounts_by_id.items():
        start = now - timedelta(days=_lookback_days(area_id, aid, settings))
        try:
            rows = await client.get_fill_history(start, now, account_id=acct["spec"])
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{acct['spec']}: {exc}")
            diag[f"fill history {acct['spec']}"] = {"error": str(exc)[:300]}
            continue
        rows = list(rows or [])
        diag[f"fill history {acct['spec']}"] = {"count": len(rows), "since": start.date().isoformat()}
        for r in rows:
            sym = str(getattr(r, "symbol", "") or "").upper()
            qty = int(_num(getattr(r, "fill_size", 0)))
            if not sym or qty <= 0:
                continue
            exch = str(getattr(r, "exchange", "") or "") or None
            cid = session._cid(sym, exch)
            info.setdefault(cid, (sym, value_per_point(_rroot(sym))))
            tt = str(getattr(r, "transaction_type", "") or "")
            action = "Buy" if tt in ("1", "BUY") or "BUY" in tt.upper() else "Sell"
            ssboe, usecs = int(_num(getattr(r, "ssboe", 0))), int(_num(getattr(r, "usecs", 0)))
            ts = datetime.fromtimestamp(ssboe + usecs / 1e6, tz=timezone.utc).isoformat() if ssboe else _ts(getattr(r, "fill_time", ""))
            raw_id = str(getattr(r, "fill_id", "") or "") or f"{getattr(r, 'basket_id', '')}:{ssboe}:{usecs}"
            fid = fill_key("rithmic", aid, raw_id)
            fills.append({"id": fid, "orderId": _int_id(str(getattr(r, "basket_id", "") or 0)), "contractId": cid,
                          "timestamp": ts, "action": action, "qty": qty,
                          "price": _num(getattr(r, "fill_price", None), _num(getattr(r, "price", 0))), "_accountId": aid})
            fees[fid] = {"commission": _side_fee(settings, qty)}
    if errors and len(errors) == len(accounts_by_id) and accounts_by_id:
        raise ImportProblem("; ".join(errors))
    out = await _import_fills(area_id, session, accounts_by_id, fills, fees, info, diag, today=today)
    if errors:
        out["history_error"] = "; ".join(errors)[:500]
    return out


IMPORTERS = {"tradovate": import_session, "projectx": import_projectx, "rithmic": import_rithmic}


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


_ran_on: dict[int, str] = {}        # area → local date of the last scheduled run


async def scheduler_tick(now: Optional[datetime] = None) -> list[int]:
    """One pass: run every workspace whose local import time has passed today
    and that did not run today yet. Returns the areas imported."""
    ran: list[int] = []
    for aid in db.all_area_ids():
        s = config.load_settings(area_id=aid)
        if not s.get("journal_auto_import", True):
            continue
        try:
            zone = ZoneInfo(str(s.get("journal_timezone") or "Europe/Zurich"))
        except Exception:  # noqa: BLE001
            zone = ZoneInfo("Europe/Zurich")
        local = (now or datetime.now(timezone.utc)).astimezone(zone)
        hh, mm = (str(s.get("journal_import_time") or "23:30").split(":") + ["0"])[:2]
        try:
            at = local.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
        except ValueError:
            continue
        today = local.date().isoformat()
        if local < at or _ran_on.get(aid) == today:
            continue
        _ran_on[aid] = today
        try:
            rec = await import_area(aid, trigger="scheduled")
            if rec.get("status") == "running":
                _ran_on.pop(aid, None)              # a manual import holds the lock: try again next minute
            else:
                ran.append(aid)
        except Exception as exc:  # noqa: BLE001
            with context.use_area(aid):
                state.log_event("warn", f"Scheduled journal import failed: {exc}")
    return ran


async def scheduler_loop() -> None:
    """Import every area at its configured local time, once a day. Checked
    every minute: an import that overruns (or a manual one holding the lock)
    delays another area's run instead of skipping it, and a changed time or
    switch takes effect within a minute."""
    while True:
        try:
            await scheduler_tick()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - the loop must survive anything
            pass
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
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss else None,
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
    if len(curve) > EQUITY_POINTS_MAX:                 # a multi-year history stays a few hundred KB on every dashboard load
        step = -(-len(curve) // EQUITY_POINTS_MAX)
        curve = curve[::step] + ([curve[-1]] if (len(curve) - 1) % step else [])
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

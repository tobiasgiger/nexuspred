"""Back-fill the journal from Tradovate's CSV exports.

Tradovate's REST API only exposes the current trading session, so past days come
from the platform's own reports (web/desktop: **Reports → Performance / Orders →
Export**). Three layouts are recognised by their column names — header order
and letter case do not matter:

* **Performance** (best): one row per round trip —
  ``symbol, buyFillId, sellFillId, qty, buyPrice, sellPrice, pnl,
  boughtTimestamp, soldTimestamp, …``. Trades are keyed by the two fill ids,
  exactly like the API import, so the same trade never appears twice however
  it arrived.
* **Orders**: filled orders —
  ``Order ID, B/S, Contract, Filled Qty / filledQty, Avg Fill Price / avgPrice,
  Fill Time, Status, Account, …``; paired FIFO per contract.
* **Fills** (generic): ``Fill ID / fillId, Timestamp, Contract / Symbol,
  B/S / Action, Qty, Price``; paired FIFO per contract.

Timestamps in the exports carry no timezone; the caller says which one the
platform displayed (default: the journal timezone). Fees are not in the
exports either — an optional per-contract-per-side amount is applied.
"""
from __future__ import annotations

import csv
import io
import re
from datetime import datetime, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

from . import config, context, db, journal, state

_TS_FORMATS = ("%m/%d/%Y %H:%M:%S", "%m/%d/%Y %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S",
               "%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M", "%m/%d/%y %H:%M:%S", "%Y-%m-%d %H:%M",
               "%m/%d/%Y %I:%M:%S %p", "%m/%d/%Y %I:%M %p")


class CsvError(ValueError):
    pass


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def _money(v: Any) -> Optional[float]:
    """``$20.00`` → 20.0, ``$(15.50)`` / ``-$15.50`` / ``(15.5)`` → -15.5."""
    if v is None:
        return None
    s = str(v).strip().replace(",", "").replace("$", "").replace("USD", "").strip()
    if not s:
        return None
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("()").strip()
    try:
        n = float(s)
    except ValueError:
        return None
    return -abs(n) if neg else n


def parse_ts(v: Any, zone: ZoneInfo) -> str:
    """A platform-local timestamp → ISO-8601 UTC. Empty string when unparseable."""
    s = str(v or "").strip()
    if not s:
        return ""
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=zone)
        return d.astimezone(timezone.utc).isoformat()
    except ValueError:
        pass
    for fmt in _TS_FORMATS:
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=zone).astimezone(timezone.utc).isoformat()
        except ValueError:
            continue
    return ""


def _rows(text: str) -> tuple[list[str], list[dict[str, str]]]:
    text = text.lstrip("﻿")
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.reader(io.StringIO(text), dialect)
    header: list[str] = []
    out: list[dict[str, str]] = []
    for row in reader:
        if not any(c.strip() for c in row):
            continue
        if not header:
            header = [c.strip() for c in row]
            continue
        out.append({_norm(h): (row[i].strip() if i < len(row) else "") for i, h in enumerate(header)})
    return header, out


def _pick(row: dict[str, str], *names: str) -> str:
    for n in names:
        v = row.get(_norm(n))
        if v not in (None, ""):
            return v
    return ""


def detect_format(header: list[str]) -> str:
    cols = {_norm(h) for h in header}
    if "buyfillid" in cols and "sellfillid" in cols:
        return "performance"
    if ("filltime" in cols or "avgfillprice" in cols or "avgprice" in cols) and ("bs" in cols or "side" in cols or "action" in cols):
        return "orders"
    if ("fillid" in cols or "id" in cols) and ("timestamp" in cols or "time" in cols) and ("price" in cols or "fillprice" in cols):
        return "fills"
    raise CsvError("Unrecognised CSV: expected a Tradovate Performance, Orders or Fills export "
                   "(columns like buyFillId/sellFillId, Fill Time/B/S, or Fill ID/Timestamp/Price)")


def _fees_dict(fill_ids: list[Any], per_side: float, qty: int) -> dict[Any, dict[str, Any]]:
    """Flat per-contract-per-side fee expressed like a Tradovate fillFee record
    (a fill's fee covers its whole quantity)."""
    return {fid: {"commission": round(per_side * qty, 4)} for fid in fill_ids} if per_side else {}


def parse(text: str, *, zone: ZoneInfo, account: dict[str, Any], fee_per_side: float = 0.0,
          source: str = "csv") -> dict[str, Any]:
    """Turn a CSV export into journal trades (not yet stored)."""
    header, rows = _rows(text)
    if not header:
        raise CsvError("The file is empty")
    fmt = detect_format(header)
    trades: list[dict[str, Any]] = []
    skipped: list[str] = []
    if fmt == "performance":
        for i, r in enumerate(rows, start=2):
            symbol = _pick(r, "symbol", "contract").upper()
            qty = int(journal._num(_pick(r, "qty", "quantity"), 0))
            buy_price, sell_price = _money(_pick(r, "buyPrice")), _money(_pick(r, "sellPrice"))
            bts, sts = parse_ts(_pick(r, "boughtTimestamp", "buyTimestamp", "buyTime"), zone), parse_ts(_pick(r, "soldTimestamp", "sellTimestamp", "sellTime"), zone)
            bid, sid = _pick(r, "buyFillId"), _pick(r, "sellFillId")
            if not (symbol and qty > 0 and buy_price is not None and sell_price is not None and bts and sts and bid and sid):
                skipped.append(f"row {i}: missing symbol/qty/prices/timestamps/fill ids")
                continue
            root = journal._root(symbol)
            pnl = _money(_pick(r, "pnl", "profit", "p&l", "realizedPnL"))
            points = round(sell_price - buy_price, 6)
            vpp = journal.value_per_point(root)
            if pnl is not None and points and qty:
                derived = abs(pnl / (points * qty))
                if derived > 0:
                    vpp = round(derived, 6)
            buy = {"id": bid, "timestamp": bts, "qty": qty, "contractId": 0}
            sell = {"id": sid, "timestamp": sts, "qty": qty, "contractId": 0}
            # Own key family per origin: the live fill-pair import uses ``pair:``, and
            # the cross-family dedup (db.find_similar_journal_trade) is what stops the
            # same round trip from being stored twice when it arrives from both.
            family = "rpt" if source == "report" else "csv"
            t = journal.build_trade(pair_id=f"{family}:{bid}:{sid}", buy=buy, sell=sell, qty=qty, buy_price=buy_price,
                                    sell_price=sell_price, account=account, symbol=symbol, value_per_point=vpp,
                                    fees=_fees_dict([bid, sid], fee_per_side, qty), source=source)
            if pnl is not None:
                t["gross_pnl"] = round(pnl, 2)
                t["net_pnl"] = round(pnl - t["fees"], 2)
            trades.append(t)
    else:
        fills: dict[str, list[dict[str, Any]]] = {}
        for i, r in enumerate(rows, start=2):
            if fmt == "orders":
                status = _pick(r, "status").lower()
                if status and "fill" not in status:
                    continue  # cancelled / rejected / working
                qty = int(journal._num(_pick(r, "filledQty", "Filled Qty", "qty", "quantity"), 0))
                price = _money(_pick(r, "avgFillPrice", "Avg Fill Price", "avgPrice", "price"))
                ts = parse_ts(_pick(r, "Fill Time", "fillTime", "timestamp", "time"), zone)
                fid = _pick(r, "orderId", "Order ID", "id") or f"row{i}"
            else:
                qty = int(journal._num(_pick(r, "qty", "quantity", "filledQty"), 0))
                price = _money(_pick(r, "price", "fillPrice", "avgPrice"))
                ts = parse_ts(_pick(r, "timestamp", "time", "fillTime"), zone)
                fid = _pick(r, "fillId", "Fill ID", "id") or f"row{i}"
            symbol = _pick(r, "contract", "symbol", "product").upper()
            side = _pick(r, "B/S", "bs", "side", "action").lower()
            # "0"/"1" = the platform's OrderAction codes (Buy/Sell) in raw reports
            side = "buy" if side.startswith("b") or side == "0" else "sell" if side.startswith("s") or side == "1" else ""
            if not (symbol and qty > 0 and price is not None and ts and side):
                skipped.append(f"row {i}: missing contract/side/qty/price/time")
                continue
            fills.setdefault(symbol, []).append({"id": fid, "timestamp": ts, "action": side, "qty": qty, "price": price, "contractId": 0})
        for symbol, fs in fills.items():
            vpp = journal.value_per_point(journal._root(symbol))
            for m in journal.fifo_pairs(fs):
                trades.append(journal.build_trade(
                    pair_id=f"{'ord' if fmt == 'orders' else 'fill'}:{m['buy']['id']}:{m['sell']['id']}:{m['qty']}",
                    buy=m["buy"], sell=m["sell"], qty=m["qty"], buy_price=m["buy_price"], sell_price=m["sell_price"],
                    account=account, symbol=symbol, value_per_point=vpp,
                    fees=_fees_dict([m["buy"]["id"], m["sell"]["id"]], fee_per_side, m["qty"]), source=source))
    return {"format": fmt, "rows": len(rows), "trades": trades, "skipped": skipped}


def import_csv(area_id: int, text: str, *, account: dict[str, Any], tz_name: str = "",
               fee_per_side: float = 0.0, user_email: str = "", filename: str = "") -> dict[str, Any]:
    """Parse + store a CSV export. Returns an import record (also persisted)."""
    with context.use_area(area_id):
        try:
            zone = ZoneInfo(tz_name) if tz_name else journal.tz()
        except Exception as exc:  # noqa: BLE001
            raise CsvError(f"Unknown timezone '{tz_name}'") from exc
        started = datetime.now(timezone.utc)
        parsed = parse(text, zone=zone, account=account, fee_per_side=fee_per_side)
        new = dup = 0
        for t in parsed["trades"]:
            if db.find_similar_journal_trade(area_id, t):
                dup += 1
                continue
            n = db.upsert_journal_trade(area_id, t)
            new += n
            dup += 0 if n else 1
        rec = {"ts": started.isoformat(), "trigger": "csv", "status": "ok" if parsed["trades"] or not parsed["skipped"] else "partial",
               "by": user_email, "logins": 0, "accounts": 1, "fills": parsed["rows"], "fills_new": 0,
               "trades": len(parsed["trades"]), "trades_new": new, "snapshots": 0,
               "error": "; ".join(parsed["skipped"][:5]) + (f" (+{len(parsed['skipped']) - 5} more)" if len(parsed["skipped"]) > 5 else ""),
               "duration_ms": round((datetime.now(timezone.utc) - started).total_seconds() * 1000)}
        db.insert_journal_import(area_id, rec)
        config.save_settings({"journal_last_import": rec["ts"]}, area_id=area_id)
        state.log_event("info", f"Journal CSV import ({parsed['format']}{', ' + filename if filename else ''}): "
                                f"{new} new trade(s), {dup} duplicate(s), {len(parsed['skipped'])} row(s) skipped")
        return {**rec, "format": parsed["format"], "duplicates": dup, "skipped": len(parsed["skipped"]),
                "skipped_rows": parsed["skipped"][:20]}

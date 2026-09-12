"""Safe ProjectX/Rithmic journal adapters.

Broker ids are only unique inside a broker/login namespace.  This module maps
those external ids into deterministic 63-bit journal ids before they enter the
shared SQLite tables and rebuilds FIFO pairs from persisted fills, so an entry
older than the rolling API overlap can still be paired when it closes.

``install()`` replaces the alpha.71 ProjectX/Rithmic importer registrations; the
Tradovate importer is unchanged.
"""
from __future__ import annotations

import hashlib
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

from . import config, db, journal


def _jid(session: Any, account_spec: str, kind: str, raw: Any) -> int:
    """A stable positive SQLite integer in the login/account identity namespace."""
    broker = str(getattr(session, "kind", "") or "broker")
    login = str(getattr(session, "lid", "") or getattr(session, "name", "") or "login")
    seed = f"v1\0{broker}\0{login}\0{account_spec}\0{kind}\0{raw}".encode("utf-8")
    value = int.from_bytes(hashlib.blake2b(seed, digest_size=8).digest(), "big") & 0x7FFF_FFFF_FFFF_FFFF
    return value or 1


def _accounts_of(session: Any) -> dict[int, dict[str, Any]]:
    """Broker-native account id -> journal account record with namespaced id."""
    raw = journal._accounts_of(session)
    out: dict[int, dict[str, Any]] = {}
    for broker_id, account in raw.items():
        acct = dict(account)
        acct["broker_id"] = int(broker_id)
        acct["id"] = _jid(session, str(acct.get("spec") or ""), "account", broker_id)
        out[int(broker_id)] = acct
    return out


def _known_account(area_id: int, journal_account_id: int) -> bool:
    db.init()
    with db._connect() as c:  # package-internal persistence primitive; no new schema required
        row = c.execute(
            "SELECT 1 FROM journal_fills WHERE area_id=? AND account_id=? LIMIT 1",
            (area_id, int(journal_account_id)),
        ).fetchone()
        if row:
            return True
        return c.execute(
            "SELECT 1 FROM journal_trades WHERE area_id=? AND account_id=? LIMIT 1",
            (area_id, int(journal_account_id)),
        ).fetchone() is not None


def _lookback_days(area_id: int, journal_account_id: int, settings: dict[str, Any]) -> int:
    if _known_account(area_id, journal_account_id):
        return journal.OTHER_OVERLAP_DAYS
    try:
        days = int(settings.get("journal_history_days") or 365)
    except (TypeError, ValueError):
        days = 365
    return max(1, min(journal.OTHER_MAX_HISTORY_DAYS, days))


def _stored_fills(area_id: int, account_id: int, contract_id: int) -> tuple[list[dict[str, Any]], dict[int, dict[str, Any]]]:
    """All persisted fills for one FIFO lane plus their persisted fees."""
    db.init()
    with db._connect() as c:
        rows = c.execute(
            "SELECT fill_id,order_id,contract_id,symbol,ts,action,qty,price,fees "
            "FROM journal_fills WHERE area_id=? AND account_id=? AND contract_id=? "
            "ORDER BY ts, fill_id",
            (area_id, int(account_id), int(contract_id)),
        ).fetchall()
    fills: list[dict[str, Any]] = []
    fees: dict[int, dict[str, Any]] = {}
    for row in rows:
        fid = int(row["fill_id"])
        fills.append({
            "id": fid, "orderId": int(row["order_id"] or 0), "contractId": int(row["contract_id"] or 0),
            "timestamp": str(row["ts"] or ""), "action": str(row["action"] or ""),
            "qty": int(row["qty"] or 0), "price": float(row["price"] or 0),
        })
        fees[fid] = {"commission": float(row["fees"] or 0)}
    return fills, fees


async def _import_fills(area_id: int, session: Any, accounts_by_id: dict[int, dict[str, Any]],
                        fills: list[dict[str, Any]], fees: dict[int, dict[str, Any]],
                        info: dict[int, tuple[str, float]], diag: dict[str, Any], *,
                        today: Optional[date] = None) -> dict[str, Any]:
    """Store new fills then pair every touched lane from durable broker history."""
    fills = [f for f in fills if f.get("id") is not None and int(f.get("_accountId") or 0) in accounts_by_id
             and int(journal._num(f.get("qty"))) > 0]
    fills_new = 0
    touched: set[tuple[int, int]] = set()  # broker account id, namespaced contract id
    for f in fills:
        broker_aid = int(f["_accountId"])
        acct = accounts_by_id[broker_aid]
        cid = int(f.get("contractId") or 0)
        sym, _vpp = info.get(cid, (str(cid), 1.0))
        fills_new += db.upsert_journal_fill(area_id, {
            "fill_id": int(f["id"]), "order_id": int(f.get("orderId") or 0),
            "account_id": int(acct["id"]), "contract_id": cid,
            "symbol": sym, "ts": journal._ts(f.get("timestamp")), "action": str(f.get("action") or ""),
            "qty": int(journal._num(f.get("qty"))), "price": journal._num(f.get("price")),
            "fees": journal.fee_total(fees.get(int(f["id"]))),
        })
        touched.add((broker_aid, cid))

    trades: list[dict[str, Any]] = []
    for broker_aid, cid in sorted(touched):
        acct = accounts_by_id[broker_aid]
        stored, stored_fees = _stored_fills(area_id, int(acct["id"]), cid)
        sym, vpp = info.get(cid, (str(cid), journal.value_per_point(journal._root(str(cid)))))
        # The current batch always has symbol metadata for a touched contract; the
        # fallback above exists only for defensive direct/test calls.
        for match in journal.fifo_pairs(stored):
            trades.append(journal.build_trade(
                pair_id=f"fifo:{match['buy']['id']}:{match['sell']['id']}:{match['qty']}",
                buy=match["buy"], sell=match["sell"], qty=match["qty"],
                buy_price=match["buy_price"], sell_price=match["sell_price"],
                account=acct, symbol=sym, value_per_point=vpp, fees=stored_fees, source="fifo"))
    trades = [t for t in trades if t["qty"] > 0]
    trades_new = sum(db.upsert_journal_trade(area_id, t) for t in trades
                     if not db.find_similar_journal_trade(area_id, t))

    day = today.isoformat() if isinstance(today, date) else datetime.now(journal.tz()).date().isoformat()
    snapshots = 0
    for broker_aid, acct in accounts_by_id.items():
        try:
            data = await session.cash_snapshot(broker_aid)
        except Exception as exc:  # noqa: BLE001 - balance is auxiliary to the trade import
            diag[f"snapshot {acct['spec']}"] = {"error": str(exc)[:200]}
            continue
        if isinstance(data, dict) and data:
            db.upsert_journal_snapshot(area_id, {
                "account_id": int(acct["id"]), "account_spec": acct["spec"], "day": day,
                "total_cash": journal._num(data.get("totalCashValue")),
                "realized_pnl": journal._num(data.get("realizedPnL")),
                "open_pnl": journal._num(data.get("openPnL")),
                "week_realized_pnl": journal._num(data.get("weekRealizedPnL")),
                "total_pnl": journal._num(data.get("totalPnL")),
            })
            snapshots += 1
    diag["accounts"] = [{"id": a["id"], "spec": a["spec"]} for a in accounts_by_id.values()]
    diag["fills_for_my_accounts"] = len(fills)
    diag["pairs_resolved"] = len(trades)
    return {"login": session.name, "accounts": len(accounts_by_id), "fills": len(fills), "fills_new": fills_new,
            "trades": len(trades), "trades_new": trades_new, "snapshots": snapshots,
            "history_pairs": 0, "history_new": 0, "history_snapshots": 0, "history_error": "", "diag": diag}


def _side_fee(settings: dict[str, Any], qty: int) -> float:
    return round(journal._num(settings.get("journal_fee_per_side"), 0.0) * max(0, qty), 4)


async def import_projectx(area_id: int, session: Any, *, today: Optional[date] = None) -> dict[str, Any]:
    """ProjectX trades, namespaced by login/account and paired from durable fills."""
    from .projectx import _int_id

    settings = config.load_settings(area_id=area_id)
    accounts_by_id = _accounts_of(session)
    diag: dict[str, Any] = {}
    now = datetime.now(timezone.utc)
    fills: list[dict[str, Any]] = []
    fees: dict[int, dict[str, Any]] = {}
    info: dict[int, tuple[str, float]] = {}
    errors: list[str] = []

    for broker_aid, acct in accounts_by_id.items():
        start = now - timedelta(days=_lookback_days(area_id, int(acct["id"]), settings))
        try:
            data = await session._post("/api/Trade/search", {"accountId": broker_aid, "startTimestamp": start.isoformat()})
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{acct['spec']}: {exc}")
            diag[f"Trade/search {acct['spec']}"] = {"error": str(exc)[:300]}
            continue
        rows = [t for t in (data.get("trades") or []) if isinstance(t, dict)] if isinstance(data, dict) else []
        diag[f"Trade/search {acct['spec']}"] = {"count": len(rows), "columns": sorted(rows[0].keys())[:40] if rows else [],
                                                    "since": start.date().isoformat()}
        for trade in rows:
            if trade.get("voided") or trade.get("id") is None:
                continue
            raw_gid = str(trade.get("contractId") or "")
            if not raw_gid:
                continue
            raw_cid = _int_id(raw_gid)
            cid = _jid(session, acct["spec"], "contract", raw_gid)
            if cid not in info:
                name = await session._contract_name(raw_gid)
                ci = await session.contract_info(raw_cid)
                sym = str(ci.get("name") or name or raw_gid).upper()
                tick_size, tick_value = journal._num(ci.get("tickSize"), 0.0), journal._num(ci.get("tickValue"), 0.0)
                vpp = round(tick_value / tick_size, 6) if tick_size > 0 and tick_value > 0 else journal.value_per_point(journal._root(sym))
                info[cid] = (sym, vpp)
            raw_fill = str(trade["id"])
            fid = _jid(session, acct["spec"], "fill", raw_fill)
            raw_order = str(trade.get("orderId") or 0)
            qty = int(journal._num(trade.get("size")))
            fills.append({
                "id": fid, "orderId": _jid(session, acct["spec"], "order", raw_order), "contractId": cid,
                "timestamp": journal._ts(trade.get("creationTimestamp")),
                "action": "Buy" if int(journal._num(trade.get("side"), 0)) == 0 else "Sell",
                "qty": qty, "price": journal._num(trade.get("price")), "_accountId": broker_aid,
            })
            fee = journal._num(trade.get("fees"), 0.0)
            fees[fid] = {"commission": round(abs(fee), 4) if fee else _side_fee(settings, qty)}

    if errors and len(errors) == len(accounts_by_id) and accounts_by_id:
        raise journal.ImportProblem("; ".join(errors))
    out = await _import_fills(area_id, session, accounts_by_id, fills, fees, info, diag, today=today)
    if errors:
        out["history_error"] = "; ".join(errors)[:500]
    return out


async def import_rithmic(area_id: int, session: Any, *, today: Optional[date] = None) -> dict[str, Any]:
    """Rithmic fill history, namespaced by login/account and paired durably."""
    from .rithmic import _root as rithmic_root

    settings = config.load_settings(area_id=area_id)
    accounts_by_id = _accounts_of(session)
    diag: dict[str, Any] = {}
    now = datetime.now(timezone.utc)
    client = await session._ensure()
    fills: list[dict[str, Any]] = []
    fees: dict[int, dict[str, Any]] = {}
    info: dict[int, tuple[str, float]] = {}
    errors: list[str] = []

    for broker_aid, acct in accounts_by_id.items():
        start = now - timedelta(days=_lookback_days(area_id, int(acct["id"]), settings))
        try:
            rows = await client.get_fill_history(start, now, account_id=acct["spec"])
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{acct['spec']}: {exc}")
            diag[f"fill history {acct['spec']}"] = {"error": str(exc)[:300]}
            continue
        rows = list(rows or [])
        diag[f"fill history {acct['spec']}"] = {"count": len(rows), "since": start.date().isoformat()}
        for row in rows:
            sym = str(getattr(row, "symbol", "") or "").upper()
            qty = int(journal._num(getattr(row, "fill_size", 0)))
            if not sym or qty <= 0:
                continue
            exch = str(getattr(row, "exchange", "") or "") or None
            cid = _jid(session, acct["spec"], "contract", f"{exch or ''}:{sym}")
            info.setdefault(cid, (sym, journal.value_per_point(rithmic_root(sym))))
            tt = str(getattr(row, "transaction_type", "") or "")
            action = "Buy" if tt in ("1", "BUY") or "BUY" in tt.upper() else "Sell"
            ssboe = int(journal._num(getattr(row, "ssboe", 0)))
            usecs = int(journal._num(getattr(row, "usecs", 0)))
            ts = datetime.fromtimestamp(ssboe + usecs / 1e6, tz=timezone.utc).isoformat() if ssboe else journal._ts(getattr(row, "fill_time", ""))
            raw_fill = str(getattr(row, "fill_id", "") or "") or f"{acct['spec']}:{getattr(row, 'basket_id', '')}:{ssboe}:{usecs}"
            raw_order = str(getattr(row, "basket_id", "") or 0)
            fid = _jid(session, acct["spec"], "fill", raw_fill)
            fills.append({
                "id": fid, "orderId": _jid(session, acct["spec"], "order", raw_order), "contractId": cid,
                "timestamp": ts, "action": action, "qty": qty,
                "price": journal._num(getattr(row, "fill_price", None), journal._num(getattr(row, "price", 0))),
                "_accountId": broker_aid,
            })
            fees[fid] = {"commission": _side_fee(settings, qty)}

    if errors and len(errors) == len(accounts_by_id) and accounts_by_id:
        raise journal.ImportProblem("; ".join(errors))
    out = await _import_fills(area_id, session, accounts_by_id, fills, fees, info, diag, today=today)
    if errors:
        out["history_error"] = "; ".join(errors)[:500]
    return out


def install() -> None:
    """Register the safe non-Tradovate adapters (idempotent)."""
    journal.import_projectx = import_projectx  # type: ignore[attr-defined]
    journal.import_rithmic = import_rithmic    # type: ignore[attr-defined]
    journal.IMPORTERS["projectx"] = import_projectx
    journal.IMPORTERS["rithmic"] = import_rithmic

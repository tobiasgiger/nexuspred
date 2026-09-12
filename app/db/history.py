"""Persisted signals and orders."""
from __future__ import annotations
import json
import sqlite3
from typing import Any, Optional
from .core import _connect, _now, init


def _json(value: Any) -> str:
    try:
        return json.dumps(value, default=str)
    except (TypeError, ValueError):
        return json.dumps(str(value))


def insert_signal(area_id: int, entry: dict[str, Any]) -> int:
    init()
    with _connect() as c:
        cur = c.execute(
            "INSERT INTO signal_log(area_id,ts,result,webhook,payload,webhook_id) VALUES(?,?,?,?,?,?)",
            (area_id, entry.get("ts") or _now(), str(entry.get("result") or "")[:200],
             str(entry.get("webhook") or "")[:200], _json(entry.get("payload")), str(entry.get("webhook_id") or "")[:80]))
        return int(cur.lastrowid or 0)


def insert_order(area_id: int, entry: dict[str, Any]) -> int:
    init()
    data = {k: v for k, v in entry.items() if k != "ts"}
    with _connect() as c:
        cur = c.execute(
            "INSERT INTO order_log(area_id,ts,action,symbol,account,status,data) VALUES(?,?,?,?,?,?,?)",
            (area_id, entry.get("ts") or _now(), str(entry.get("action") or "")[:40],
             str(entry.get("symbol") or "")[:40], str(entry.get("account") or "")[:120],
             str(entry.get("status") or "")[:60], _json(data)))
        return int(cur.lastrowid or 0)


def _signal_row(r: sqlite3.Row) -> dict[str, Any]:
    try:
        payload = json.loads(r["payload"])
    except (TypeError, ValueError):
        payload = {"raw": r["payload"]}
    return {"id": r["id"], "ts": r["ts"], "result": r["result"], "webhook": r["webhook"], "webhook_id": r["webhook_id"], "payload": payload}


def _order_row(r: sqlite3.Row) -> dict[str, Any]:
    try:
        data = json.loads(r["data"])
    except (TypeError, ValueError):
        data = {}
    return {"id": r["id"], "ts": r["ts"], **data,
            "action": r["action"], "symbol": r["symbol"], "account": r["account"], "status": r["status"]}


def list_signals(area_id: int, *, limit: int = 100, before: Optional[int] = None,
                 result: str = "", q: str = "", webhook_id: str = "", since_ts: str = "") -> dict[str, Any]:
    """Newest-first page of an area's signals. ``before`` = id cursor from the
    previous page's ``next_before``; ``result`` = prefix filter (``ok``,
    ``error``…); ``q`` = substring of the payload / webhook name."""
    init()
    limit = max(1, min(int(limit), 500))
    where = ["area_id=?"]
    params: list[Any] = [area_id]
    if before:
        where.append("id<?"); params.append(int(before))
    if result:
        where.append("result LIKE ?"); params.append(f"{result}%")
    if q:
        where.append("(payload LIKE ? OR webhook LIKE ?)"); params += [f"%{q}%", f"%{q}%"]
    if webhook_id:
        where.append("webhook_id=?"); params.append(webhook_id)
    if since_ts:
        where.append("ts>=?"); params.append(since_ts)
    with _connect() as c:
        rows = c.execute(f"SELECT * FROM signal_log WHERE {' AND '.join(where)} ORDER BY id DESC LIMIT ?",
                         (*params, limit + 1)).fetchall()
    items = [_signal_row(r) for r in rows[:limit]]
    return {"items": items, "next_before": items[-1]["id"] if len(rows) > limit else None}


def list_orders(area_id: int, *, limit: int = 100, before: Optional[int] = None,
                symbol: str = "", account: str = "") -> dict[str, Any]:
    init()
    limit = max(1, min(int(limit), 500))
    where = ["area_id=?"]
    params: list[Any] = [area_id]
    if before:
        where.append("id<?"); params.append(int(before))
    if symbol:
        where.append("symbol LIKE ?"); params.append(f"{symbol}%")
    if account:
        where.append("account LIKE ?"); params.append(f"%{account}%")
    with _connect() as c:
        rows = c.execute(f"SELECT * FROM order_log WHERE {' AND '.join(where)} ORDER BY id DESC LIMIT ?",
                         (*params, limit + 1)).fetchall()
    items = [_order_row(r) for r in rows[:limit]]
    return {"items": items, "next_before": items[-1]["id"] if len(rows) > limit else None}


_EXECUTED_SQL = ("SUM(CASE WHEN result NOT IN ('received','skipped','test','simulated') "
                 "AND result NOT LIKE 'error%' THEN 1 ELSE 0 END)")


def signal_stats(area_id: int, webhook_id: str, since_ts: str = "") -> dict[str, Any]:
    """Outcome counts of one webhook's signals (``received`` rows are the ingress
    echo of an executed / skipped / errored row and are reported separately)."""
    init()
    where, params = "area_id=? AND webhook_id=?", [area_id, webhook_id]
    if since_ts:
        where += " AND ts>=?"; params.append(since_ts)
    with _connect() as c:
        r = c.execute(
            "SELECT SUM(CASE WHEN result='received' THEN 1 ELSE 0 END) received, "
            "SUM(CASE WHEN result LIKE 'error%' THEN 1 ELSE 0 END) errors, "
            "SUM(CASE WHEN result='skipped' THEN 1 ELSE 0 END) skipped, "
            f"{_EXECUTED_SQL} executed, MIN(ts) first_ts, MAX(ts) last_ts "
            f"FROM signal_log WHERE {where}", params).fetchone()
    return {"received": int(r["received"] or 0), "executed": int(r["executed"] or 0), "skipped": int(r["skipped"] or 0),
            "errors": int(r["errors"] or 0), "first_at": r["first_ts"], "last_at": r["last_ts"]}


def history_stats(area_id: int, since_ts: str) -> dict[str, Any]:
    """Signal outcomes and order counts since ``since_ts`` (ISO), per day and in total."""
    init()
    with _connect() as c:
        sig = c.execute(
            "SELECT substr(ts,1,10) day, "
            "SUM(CASE WHEN result='received' THEN 1 ELSE 0 END) received, "
            "SUM(CASE WHEN result LIKE 'error%' THEN 1 ELSE 0 END) errors, "
            "SUM(CASE WHEN result='skipped' THEN 1 ELSE 0 END) skipped, "
            "SUM(CASE WHEN result NOT IN ('received','skipped','test','simulated') "
            "         AND result NOT LIKE 'error%' THEN 1 ELSE 0 END) executed "
            "FROM signal_log WHERE area_id=? AND ts>=? GROUP BY day ORDER BY day",
            (area_id, since_ts)).fetchall()
        orders = c.execute(
            "SELECT substr(ts,1,10) day, COUNT(*) n, "
            "SUM(CASE WHEN status LIKE '%reject%' THEN 1 ELSE 0 END) rejected "
            "FROM order_log WHERE area_id=? AND ts>=? GROUP BY day ORDER BY day",
            (area_id, since_ts)).fetchall()
    days: dict[str, dict[str, int]] = {}
    for r in sig:
        days.setdefault(r["day"], {})
        days[r["day"]].update(received=r["received"] or 0, executed=r["executed"] or 0,
                              errors=r["errors"] or 0, skipped=r["skipped"] or 0)
    for r in orders:
        days.setdefault(r["day"], {})
        days[r["day"]].update(orders=r["n"] or 0, rejected=r["rejected"] or 0)
    keys = ("received", "executed", "errors", "skipped", "orders", "rejected")
    totals = {k: sum(d.get(k, 0) for d in days.values()) for k in keys}
    return {"since": since_ts, "totals": totals,
            "days": [{"day": d, **{k: v.get(k, 0) for k in keys}} for d, v in sorted(days.items())]}


def prune_history(cutoff_ts: str) -> int:
    init()
    with _connect() as c:
        a = c.execute("DELETE FROM signal_log WHERE ts<?", (cutoff_ts,)).rowcount
        b = c.execute("DELETE FROM order_log WHERE ts<?", (cutoff_ts,)).rowcount
    return int(a or 0) + int(b or 0)

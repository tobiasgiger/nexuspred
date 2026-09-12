"""Trading journal: fills, trades, snapshots, imports."""
from __future__ import annotations
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from .core import _connect, _now, init


_TRADE_COLS = ("pair_id", "source", "account_id", "account_spec", "account_name", "environment",
               "contract_id", "symbol", "root", "side", "qty", "entry_price", "exit_price", "entry_ts",
               "exit_ts", "entry_fill_id", "exit_fill_id", "points", "value_per_point", "gross_pnl",
               "fees", "net_pnl")


def upsert_journal_fill(area_id: int, f: dict[str, Any]) -> int:
    """Insert a fill (idempotent by Tradovate fill id). Returns 1 when new."""
    init()
    with _connect() as c:
        cur = c.execute(
            "INSERT OR IGNORE INTO journal_fills(area_id,fill_id,order_id,account_id,contract_id,symbol,ts,action,qty,price,fees) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (area_id, f["fill_id"], f.get("order_id", 0), f.get("account_id", 0), f.get("contract_id", 0),
             f.get("symbol", ""), f.get("ts", ""), f.get("action", ""), f.get("qty", 0), f.get("price", 0), f.get("fees", 0)))
        return int(cur.rowcount or 0)


def journal_fills_for(area_id: int, account_ids: list[int]) -> list[dict[str, Any]]:
    """Every stored fill of the given accounts (the FIFO pairing runs over the
    whole history, so a window that starts inside an open position never
    fabricates a trade)."""
    init()
    if not account_ids:
        return []
    marks = ",".join("?" * len(account_ids))
    with _connect() as c:
        rows = c.execute(f"SELECT fill_id, order_id, account_id, contract_id, symbol, ts, action, qty, price, fees "
                         f"FROM journal_fills WHERE area_id=? AND account_id IN ({marks}) ORDER BY ts, fill_id",
                         (area_id, *[int(a) for a in account_ids])).fetchall()
    return [dict(r) for r in rows]


def upsert_journal_trade(area_id: int, t: dict[str, Any]) -> int:
    """Insert a round-trip trade (idempotent by pair id). Returns 1 when new."""
    init()
    cols = ",".join(_TRADE_COLS)
    marks = ",".join("?" * len(_TRADE_COLS))
    with _connect() as c:
        cur = c.execute(
            f"INSERT OR IGNORE INTO journal_trades(area_id,{cols},imported_at) VALUES(?,{marks},?)",
            (area_id, *[t.get(k) for k in _TRADE_COLS], _now()))
        return int(cur.rowcount or 0)


def find_similar_journal_trade(area_id: int, t: dict[str, Any], tolerance_s: int = 5) -> Optional[int]:
    """Id of an already-stored trade that is the same round trip under a key of a
    *different family* (``pair:`` = fill ids from the API or a Performance export,
    ``ord:``/``fill:``/``fifo:`` = FIFO-paired): same account, symbol, side, qty,
    entry/exit price and an exit within ``tolerance_s`` seconds. Same-family
    trades are keyed exactly, so two genuinely identical split fills (two 1-lot
    pairs at the same price and second) are never collapsed."""
    init()
    try:
        exit_dt = datetime.fromisoformat(str(t.get("exit_ts")).replace("Z", "+00:00"))
    except ValueError:
        return None
    lo = (exit_dt - timedelta(seconds=tolerance_s)).isoformat()
    hi = (exit_dt + timedelta(seconds=tolerance_s)).isoformat()
    family = str(t.get("pair_id", "")).split(":", 1)[0] + ":"
    efid, xfid = int(t.get("entry_fill_id") or 0), int(t.get("exit_fill_id") or 0)
    with _connect() as c:
        if efid and xfid:
            # Same broker fill ids under another key family (e.g. the Performance
            # report vs the live fill pairs) → the same round trip, whatever the
            # report's timestamps say.
            r = c.execute(
                "SELECT id FROM journal_trades WHERE area_id=? AND account_id=? AND entry_fill_id=? AND exit_fill_id=? "
                "AND substr(pair_id, 1, instr(pair_id, ':')) <> ? LIMIT 1",
                (area_id, t.get("account_id", 0), efid, xfid, family)).fetchone()
            if r:
                return int(r["id"])
        r = c.execute(
            "SELECT id FROM journal_trades WHERE area_id=? AND account_id=? AND symbol=? AND side=? AND qty=? "
            "AND ABS(entry_price-?)<1e-6 AND ABS(exit_price-?)<1e-6 AND exit_ts BETWEEN ? AND ? "
            "AND substr(pair_id, 1, instr(pair_id, ':')) <> ? LIMIT 1",
            (area_id, t.get("account_id", 0), t.get("symbol", ""), t.get("side", ""), t.get("qty", 0),
             float(t.get("entry_price") or 0), float(t.get("exit_price") or 0), lo, hi, family)).fetchone()
    return int(r["id"]) if r else None


_SOURCE_RANK = {"history": 0, "fillpair": 1, "report": 2, "csv": 3, "fifo": 4}


def dedupe_journal_trades(area_id: int, tolerance_s: int = 5) -> int:
    """Collapse round trips stored more than once because they arrived from
    different imports (live fill pairs, the Performance report, a CSV upload):
    same account, symbol, side, qty, entry/exit price and an exit within
    ``tolerance_s`` seconds, under a *different source or key family*. Two rows
    from the same source and family with different keys are genuine split fills
    and are left alone. Keeps the row from the most authoritative source (the
    book's fill pairs first), carries over a note / tags the survivor lacks, and
    returns how many rows were deleted."""
    init()
    with _connect() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT id, pair_id, source, account_id, symbol, side, qty, entry_price, exit_price, exit_ts, note, tags, "
            "entry_fill_id, exit_fill_id FROM journal_trades WHERE area_id=? ORDER BY exit_ts, id", (area_id,)).fetchall()]
    def fam(r): return str(r["pair_id"]).split(":", 1)[0]
    # pass 1: identical broker fill ids under different sources / families
    by_fills: dict[tuple[int, int, int], list[dict[str, Any]]] = {}
    for r in rows:
        if r["entry_fill_id"] and r["exit_fill_id"]:
            by_fills.setdefault((r["account_id"], r["entry_fill_id"], r["exit_fill_id"]), []).append(r)
    fill_groups = [g for g in by_fills.values() if len(g) > 1 and len({(m["source"], fam(m)) for m in g}) > 1]
    grouped_ids = {m["id"] for g in fill_groups for m in g}
    rows = [r for r in rows if r["id"] not in grouped_ids]
    def ts(r):
        try:
            return datetime.fromisoformat(str(r["exit_ts"]).replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    def same_trip(a, b):
        return (a["account_id"] == b["account_id"] and a["symbol"] == b["symbol"] and a["side"] == b["side"]
                and a["qty"] == b["qty"] and abs(float(a["entry_price"]) - float(b["entry_price"])) < 1e-6
                and abs(float(a["exit_price"]) - float(b["exit_price"])) < 1e-6
                and (a["source"] != b["source"] or fam(a) != fam(b)))
    groups: list[list[dict[str, Any]]] = []
    for r in rows:
        t = ts(r)
        placed = False
        if t is not None:
            for g in reversed(groups):
                gt = ts(g[0])
                if gt is None or t - gt > tolerance_s:
                    break
                if all(same_trip(r, m) for m in g):
                    g.append(r); placed = True
                    break
        if not placed:
            groups.append([r])
    removed = 0
    with _connect() as c:
        for g in fill_groups + groups:
            if len(g) < 2:
                continue
            g.sort(key=lambda r: (_SOURCE_RANK.get(r["source"], 9), r["id"]))
            keep, drop = g[0], g[1:]
            note = keep["note"] or next((d["note"] for d in drop if d["note"]), "")
            tags = keep["tags"] or next((d["tags"] for d in drop if d["tags"]), "")
            if (note, tags) != (keep["note"], keep["tags"]):
                c.execute("UPDATE journal_trades SET note=?, tags=? WHERE id=?", (note, tags, keep["id"]))
            c.execute(f"DELETE FROM journal_trades WHERE area_id=? AND id IN ({','.join('?' * len(drop))})",
                      (area_id, *[d["id"] for d in drop]))
            removed += len(drop)
    return removed


def _trade_row(r: sqlite3.Row) -> dict[str, Any]:
    d = dict(r)
    d["tags"] = [x for x in (d.get("tags") or "").split(",") if x]
    return d


def list_journal_trades(area_id: int, *, frm: str = "", to: str = "", account: str = "",
                        symbol: str = "", side: str = "", limit: int = 0,
                        before: Optional[int] = None, accounts: Optional[list[str]] = None) -> list[dict[str, Any]]:
    """Trades closed in [frm, to) (ISO-UTC; empty = open-ended), newest first
    when ``limit`` is set, else chronological (for aggregation)."""
    init()
    where = ["area_id=?"]
    params: list[Any] = [area_id]
    if frm:
        where.append("exit_ts>=?"); params.append(frm)
    if to:
        where.append("exit_ts<?"); params.append(to)
    if account:
        where.append("(account_spec=? OR account_name=? OR CAST(account_id AS TEXT)=?)"); params += [account, account, account]
    if accounts is not None:
        if not accounts:
            return []
        marks = ",".join("?" * len(accounts))
        where.append(f"(account_spec IN ({marks}) OR account_name IN ({marks}))"); params += [*accounts, *accounts]
    if symbol:
        where.append("(root=? OR symbol=?)"); params += [symbol, symbol]
    if side:
        where.append("side=?"); params.append(side)
    if before:
        where.append("id<?"); params.append(int(before))
    order = "ORDER BY exit_ts DESC, id DESC" if limit else "ORDER BY exit_ts ASC, id ASC"
    lim = f" LIMIT {int(limit)}" if limit else ""
    with _connect() as c:
        rows = c.execute(f"SELECT * FROM journal_trades WHERE {' AND '.join(where)} {order}{lim}", params).fetchall()
    return [_trade_row(r) for r in rows]


def get_journal_trade(area_id: int, trade_id: int) -> Optional[dict[str, Any]]:
    init()
    with _connect() as c:
        r = c.execute("SELECT * FROM journal_trades WHERE area_id=? AND id=?", (area_id, trade_id)).fetchone()
    return _trade_row(r) if r else None


def update_journal_trade_note(area_id: int, trade_id: int, note: str, tags: list[str]) -> bool:
    init()
    clean = ",".join(sorted({t.strip().lower()[:30] for t in tags if t and t.strip()}))
    with _connect() as c:
        cur = c.execute("UPDATE journal_trades SET note=?, tags=? WHERE area_id=? AND id=?",
                        (note[:2000], clean, area_id, trade_id))
    return bool(cur.rowcount)


def journal_accounts(area_id: int) -> list[dict[str, Any]]:
    init()
    with _connect() as c:
        rows = c.execute(
            "SELECT account_id, account_spec, account_name, environment, COUNT(*) n, MIN(exit_ts) first_ts, MAX(exit_ts) last_ts "
            "FROM journal_trades WHERE area_id=? GROUP BY account_id ORDER BY account_name", (area_id,)).fetchall()
    return [dict(r) for r in rows]


def journal_symbols(area_id: int) -> list[str]:
    init()
    with _connect() as c:
        return [r["root"] for r in c.execute(
            "SELECT DISTINCT root FROM journal_trades WHERE area_id=? ORDER BY root", (area_id,)).fetchall()]


def upsert_journal_snapshot(area_id: int, s: dict[str, Any]) -> None:
    init()
    with _connect() as c:
        c.execute(
            "INSERT INTO journal_snapshots(area_id,account_id,account_spec,day,total_cash,realized_pnl,open_pnl,week_realized_pnl,total_pnl,taken_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(area_id,account_id,day) DO UPDATE SET "
            "total_cash=excluded.total_cash, realized_pnl=excluded.realized_pnl, open_pnl=excluded.open_pnl, "
            "week_realized_pnl=excluded.week_realized_pnl, total_pnl=excluded.total_pnl, taken_at=excluded.taken_at",
            (area_id, s["account_id"], s.get("account_spec", ""), s["day"], s.get("total_cash", 0), s.get("realized_pnl", 0),
             s.get("open_pnl", 0), s.get("week_realized_pnl", 0), s.get("total_pnl", 0), _now()))


def list_journal_snapshots(area_id: int, *, days: int = 90, account: str = "") -> list[dict[str, Any]]:
    init()
    since = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
    where, params = "area_id=? AND day>=?", [area_id, since]
    if account:
        where += " AND (account_spec=? OR CAST(account_id AS TEXT)=?)"; params += [account, account]
    with _connect() as c:
        return [dict(r) for r in c.execute(
            f"SELECT * FROM journal_snapshots WHERE {where} ORDER BY day, account_id", params).fetchall()]


def insert_journal_import(area_id: int, rec: dict[str, Any]) -> int:
    init()
    with _connect() as c:
        cur = c.execute(
            "INSERT INTO journal_imports(area_id,ts,trigger,status,by,logins,accounts,fills,fills_new,trades,trades_new,snapshots,duration_ms,error,history_new,detail) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (area_id, rec.get("ts") or _now(), rec.get("trigger", ""), rec.get("status", ""), rec.get("by", ""),
             rec.get("logins", 0), rec.get("accounts", 0), rec.get("fills", 0), rec.get("fills_new", 0),
             rec.get("trades", 0), rec.get("trades_new", 0), rec.get("snapshots", 0), rec.get("duration_ms", 0),
             rec.get("error", ""), rec.get("history_new", 0), rec.get("detail", "")))
        return int(cur.lastrowid or 0)


def journal_unseen(area_id: int, kind: str, refs: list[Any]) -> list[Any]:
    """The subset of ``refs`` not yet marked as processed for ``kind``."""
    init()
    refs = [r for r in refs if r is not None]
    if not refs:
        return []
    out: list[Any] = []
    with _connect() as c:
        for i in range(0, len(refs), 400):
            chunk = refs[i:i + 400]
            marks = ",".join("?" * len(chunk))
            seen = {r["ref"] for r in c.execute(
                f"SELECT ref FROM journal_seen WHERE area_id=? AND kind=? AND ref IN ({marks})",
                (area_id, kind, *[str(x) for x in chunk])).fetchall()}
            out.extend(x for x in chunk if str(x) not in seen)
    return out


def journal_mark_seen(area_id: int, kind: str, refs: list[Any]) -> None:
    init()
    with _connect() as c:
        c.executemany("INSERT OR IGNORE INTO journal_seen(area_id,kind,ref) VALUES(?,?,?)",
                      [(area_id, kind, str(x)) for x in refs if x is not None])


def list_journal_imports(area_id: int, limit: int = 30) -> list[dict[str, Any]]:
    init()
    with _connect() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM journal_imports WHERE area_id=? ORDER BY id DESC LIMIT ?", (area_id, int(limit))).fetchall()]

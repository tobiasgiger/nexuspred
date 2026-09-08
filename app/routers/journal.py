"""Trading journal API: import, reporting, trades with notes, CSV export."""
from __future__ import annotations

import csv
import io
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from .. import config, context, db, journal, journal_csv

router = APIRouter(prefix="/api/journal", tags=["journal"])

_PRESETS = ("today", "week", "month", "30d", "90d", "ytd", "all")


def _bounds(range_: str, frm: str, to: str) -> tuple[str, str]:
    """Explicit ISO dates (``frm``/``to`` = local calendar days, inclusive) win
    over a named preset."""
    zone = journal.tz()
    if frm or to:
        from datetime import date, time as dtime, timedelta
        try:
            f = datetime.combine(date.fromisoformat(frm), dtime.min, tzinfo=zone).astimezone(timezone.utc).isoformat() if frm else ""
            t = datetime.combine(date.fromisoformat(to) + timedelta(days=1), dtime.min, tzinfo=zone).astimezone(timezone.utc).isoformat() if to else ""
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="from/to must be YYYY-MM-DD") from exc
        return f, t
    return journal.range_bounds(range_ if range_ in _PRESETS else "month", zone)


def _trades(range_: str, frm: str, to: str, account: str, symbol: str, side: str) -> list[dict[str, Any]]:
    f, t = _bounds(range_, frm, to)
    return db.list_journal_trades(context.get_area(), frm=f, to=t, account=account[:120],
                                  symbol=symbol[:40].upper(), side=side if side in ("long", "short") else "")


@router.get("/overview")
async def api_overview(range: str = "month", frm: str = "", to: str = "", account: str = "",
                       symbol: str = "", side: str = "", period: str = "day") -> dict[str, Any]:
    """Everything the Journal page needs for one filter slice: totals & breakdowns,
    per-period buckets, equity curve, plus filter options and the last import."""
    trades = _trades(range, frm, to, account, symbol, side)
    zone = journal.tz()
    s = config.load_settings()
    f, t = _bounds(range, frm, to)
    return {
        "range": {"preset": range, "from": f, "to": t, "timezone": str(zone)},
        "stats": journal.stats(trades, zone),
        "summary": journal.summary(trades, period, zone),
        "accounts": db.journal_accounts(context.get_area()),
        "trade_accounts": [a.get("spec") or a.get("account_spec") or "" for t in (s.get("token_accounts") or [])
                           for a in (t.get("accounts") or []) if a.get("spec") or a.get("account_spec")],
        "symbols": db.journal_symbols(context.get_area()),
        "last_import": s.get("journal_last_import") or "",
        "schedule": {"enabled": bool(s.get("journal_auto_import", True)),
                     "time": s.get("journal_import_time") or "23:30", "timezone": str(zone)},
    }


@router.get("/summary")
async def api_summary(period: str = "day", range: str = "month", frm: str = "", to: str = "",
                      account: str = "", symbol: str = "", side: str = "") -> dict[str, Any]:
    trades = _trades(range, frm, to, account, symbol, side)
    return {"period": period, "buckets": journal.summary(trades, period, journal.tz())}


@router.get("/calendar")
async def api_calendar(month: str = "", account: str = "", symbol: str = "") -> dict[str, Any]:
    zone = journal.tz()
    try:
        y, m = (int(x) for x in (month or datetime.now(zone).strftime("%Y-%m")).split("-")[:2])
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="month must be YYYY-MM") from exc
    from datetime import date, time as dtime
    first = date(y, m, 1)
    nxt = date(y + (m == 12), (m % 12) + 1, 1)
    f = datetime.combine(first, dtime.min, tzinfo=zone).astimezone(timezone.utc).isoformat()
    t = datetime.combine(nxt, dtime.min, tzinfo=zone).astimezone(timezone.utc).isoformat()
    trades = db.list_journal_trades(context.get_area(), frm=f, to=t, account=account[:120], symbol=symbol[:40].upper())
    return journal.calendar(trades, y, m, zone)


@router.get("/trades")
async def api_trades(range: str = "month", frm: str = "", to: str = "", account: str = "", symbol: str = "",
                     side: str = "", limit: int = 200, before: Optional[int] = None) -> dict[str, Any]:
    f, t = _bounds(range, frm, to)
    limit = max(1, min(int(limit), 1000))
    rows = db.list_journal_trades(context.get_area(), frm=f, to=t, account=account[:120], symbol=symbol[:40].upper(),
                                  side=side if side in ("long", "short") else "", limit=limit + 1, before=before)
    items = rows[:limit]
    return {"items": items, "next_before": items[-1]["id"] if len(rows) > limit else None}


@router.put("/trades/{trade_id}")
async def api_trade_note(trade_id: int, request: Request) -> dict[str, Any]:
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="JSON object expected")
    cur = db.get_journal_trade(context.get_area(), trade_id)
    if not cur:
        raise HTTPException(status_code=404, detail="Trade not found")
    note = str(body.get("note", cur["note"]) or "")
    tags = body.get("tags", cur["tags"])
    if isinstance(tags, str):
        tags = tags.split(",")
    if not isinstance(tags, list):
        raise HTTPException(status_code=400, detail="tags must be a list")
    db.update_journal_trade_note(context.get_area(), trade_id, note, [str(x) for x in tags])
    return db.get_journal_trade(context.get_area(), trade_id) or {}


@router.post("/dedupe")
async def api_dedupe() -> dict[str, Any]:
    """Remove round trips stored twice (e.g. report + fill-pair import)."""
    removed = db.dedupe_journal_trades(context.get_area())
    if removed:
        from .. import state
        state.log_event("info", f"Journal: removed {removed} duplicate trade(s)")
    return {"removed": removed}


@router.post("/import")
async def api_import(request: Request) -> dict[str, Any]:
    """Import fills / trades from every enabled Tradovate login now."""
    user = getattr(request.state, "user", None) or {}
    rec = await journal.import_area(context.get_area(), trigger="manual", user_email=user.get("email", ""))
    if rec.get("status") == "running":
        raise HTTPException(status_code=409, detail=rec.get("detail"))
    return rec


def _account_from_label(label: str) -> dict[str, Any]:
    """Resolve the account the user picked for a CSV: a configured Tradovate
    trade account (by spec) keeps its id/environment; anything else becomes a
    manual account named by the label."""
    label = (label or "").strip()[:120]
    if not label:
        raise HTTPException(status_code=400, detail="Pick or name the account the export belongs to")
    for idx, t in enumerate(config.load_settings().get("token_accounts") or []):
        for a in (t.get("accounts") or []):
            spec = a.get("spec") or a.get("account_spec") or ""
            if spec == label:
                return {"id": int(a.get("id") or a.get("account_id") or 0), "spec": spec, "name": spec,
                        "environment": t.get("environment") or "demo"}
    # Manual account: a stable synthetic id derived from the label (negative to
    # never collide with Tradovate ids).
    import zlib
    return {"id": -(zlib.crc32(label.lower().encode()) % 1_000_000_000 + 1), "spec": label, "name": label,
            "environment": "csv"}


@router.post("/import-csv")
async def api_import_csv(request: Request) -> dict[str, Any]:
    """Back-fill from a Tradovate CSV export (multipart: ``file``, ``account``,
    optional ``timezone`` and ``fee_per_side``)."""
    form = await request.form()
    upload = form.get("file")
    if upload is None or not hasattr(upload, "read"):
        raise HTTPException(status_code=400, detail="Attach the CSV export as 'file'")
    raw = await upload.read()
    if not raw:
        raise HTTPException(status_code=400, detail="The file is empty")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")
    account = _account_from_label(str(form.get("account") or ""))
    try:
        fee = float(str(form.get("fee_per_side") or "0").replace(",", "."))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="fee_per_side must be a number") from exc
    user = getattr(request.state, "user", None) or {}
    try:
        return journal_csv.import_csv(context.get_area(), text, account=account, tz_name=str(form.get("timezone") or ""),
                                      fee_per_side=max(0.0, fee), user_email=user.get("email", ""),
                                      filename=getattr(upload, "filename", "") or "")
    except journal_csv.CsvError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/imports")
async def api_imports() -> list[dict[str, Any]]:
    return db.list_journal_imports(context.get_area())


@router.get("/snapshots")
async def api_snapshots(days: int = 90, account: str = "") -> list[dict[str, Any]]:
    """Daily account equity snapshots (cash balance, realized / open P&L)."""
    return db.list_journal_snapshots(context.get_area(), days=max(1, min(int(days), 3660)), account=account[:120])


@router.get("/export.csv")
async def api_export(range: str = "all", frm: str = "", to: str = "", account: str = "",
                     symbol: str = "", side: str = "") -> Response:
    trades = _trades(range, frm, to, account, symbol, side)
    cols = ["id", "exit_ts", "entry_ts", "account_name", "account_spec", "environment", "symbol", "root", "side",
            "qty", "entry_price", "exit_price", "points", "value_per_point", "gross_pnl", "fees", "net_pnl",
            "source", "note", "tags"]
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(cols)
    for t in trades:
        w.writerow([",".join(t[c]) if c == "tags" else t.get(c, "") for c in cols])
    return Response(content=buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": 'attachment; filename="journal.csv"'})

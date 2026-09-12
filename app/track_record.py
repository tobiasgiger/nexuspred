"""Verified track record of a published signal or copy group, and the
subscriber's journal of one subscription.

The record is built from the publisher's *trading journal* — round trips paired
from broker fills the bridge imported itself (never a figure the publisher
typed in). A webhook's record covers the trade accounts it routes to; a copy
group's record is the leader account. Trades that came from a CSV upload are
counted separately (``verified_share``) because the bridge did not see those
fills. Subscribers never see the publisher's account names.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

from . import config, db, journal

CACHE_TTL_S = 120.0
EQUITY_POINTS = 120
VERIFIED_SOURCES = frozenset({"history", "fillpair", "report", "fifo"})
_cache: dict[tuple[str, int, str], tuple[float, dict[str, Any]]] = {}


def _zone(area_id: int) -> ZoneInfo:
    name = str(config.load_settings(area_id=area_id).get("journal_timezone") or "Europe/Zurich")
    try:
        return ZoneInfo(name)
    except Exception:  # noqa: BLE001
        return ZoneInfo("Europe/Zurich")


def _iso_days_ago(days: int, now: Optional[datetime] = None) -> str:
    return ((now or datetime.now(timezone.utc)) - timedelta(days=days)).isoformat()


def _compress(curve: list[dict[str, Any]], n: int = EQUITY_POINTS) -> list[dict[str, Any]]:
    if len(curve) <= n:
        return [{"ts": p["ts"], "equity": p["equity"]} for p in curve]
    step = -(-len(curve) // n)
    picked = curve[::step]
    if picked[-1] is not curve[-1]:
        picked.append(curve[-1])
    return [{"ts": p["ts"], "equity": p["equity"]} for p in picked]


def summarize_trades(trades: list[dict[str, Any]], zone: ZoneInfo, *, now: Optional[datetime] = None,
                     detail: bool = True) -> dict[str, Any]:
    """The figures of a set of journal trades (chronological or not)."""
    now = now or datetime.now(timezone.utc)
    st = journal.stats(trades, zone) if trades else None
    d30, d90 = _iso_days_ago(30, now), _iso_days_ago(90, now)
    t30 = [t for t in trades if t["exit_ts"] >= d30]
    t90 = [t for t in trades if t["exit_ts"] >= d90]
    verified = sum(1 for t in trades if t.get("source") in VERIFIED_SOURCES)
    out: dict[str, Any] = {
        "trades": len(trades), "wins": st["wins"] if st else 0, "losses": st["losses"] if st else 0,
        "win_rate": st["win_rate"] if st else 0.0, "profit_factor": st["profit_factor"] if st else None,
        "net_pnl": st["net_pnl"] if st else 0.0, "gross_pnl": st["gross_pnl"] if st else 0.0, "fees": st["fees"] if st else 0.0,
        "expectancy": st["expectancy"] if st else 0.0, "avg_win": st["avg_win"] if st else 0.0, "avg_loss": st["avg_loss"] if st else 0.0,
        "largest_win": st["largest_win"] if st else 0.0, "largest_loss": st["largest_loss"] if st else 0.0,
        "max_drawdown": st["max_drawdown"] if st else 0.0, "trading_days": st["trading_days"] if st else 0,
        "longest_win_streak": st["longest_win_streak"] if st else 0, "longest_loss_streak": st["longest_loss_streak"] if st else 0,
        "first_trade_at": min((t["exit_ts"] for t in trades), default=None),
        "last_trade_at": max((t["exit_ts"] for t in trades), default=None),
        "net_30d": round(sum(t["net_pnl"] for t in t30), 2), "trades_30d": len(t30),
        "net_90d": round(sum(t["net_pnl"] for t in t90), 2), "trades_90d": len(t90),
        "verified_share": round(verified / len(trades), 4) if trades else 1.0,
        "verified": bool(trades) and verified == len(trades),
    }
    if detail:
        months = journal.summary(trades, "month", zone) if trades else []
        out["monthly"] = [{"bucket": m["bucket"], "trades": m["trades"], "net_pnl": m["net_pnl"], "win_rate": m["win_rate"],
                           "cumulative": m["cumulative"]} for m in months[-12:]]
        out["by_symbol"] = [{"root": b["key"], "trades": b["trades"], "net_pnl": b["net_pnl"], "win_rate": b["win_rate"]}
                            for b in sorted(st["by_symbol"], key=lambda b: -b["trades"])[:6]] if st else []
        out["equity"] = _compress(st["equity"]) if st else []
    return out


def _cached(kind: str, area_id: int, key: str, build) -> dict[str, Any]:
    ck = (kind, area_id, key)
    hit = _cache.get(ck)
    now = time.monotonic()
    if hit and now - hit[0] < CACHE_TTL_S:
        return hit[1]
    value = build()
    _cache[ck] = (now, value)
    if len(_cache) > 2000:
        for k in [k for k, v in _cache.items() if now - v[0] >= CACHE_TTL_S]:
            _cache.pop(k, None)
    return value


def webhook_record(publisher_area_id: int, webhook: dict[str, Any], *, detail: bool = False) -> dict[str, Any]:
    """The track record of a published webhook: the publisher's journal trades on
    the accounts the webhook routes to, plus its signal outcome counts."""
    wid = str(webhook.get("id") or "")

    def build() -> dict[str, Any]:
        specs = sorted({str(a.get("spec") or "") for a in (webhook.get("accounts") or []) if a.get("enabled") and a.get("spec")})
        trades = db.list_journal_trades(publisher_area_id, accounts=specs) if specs else []
        out = summarize_trades(trades, _zone(publisher_area_id), detail=True)
        out.update({"basis": "accounts" if specs else "none", "accounts_n": len(specs),
                    "signals": db.signal_stats(publisher_area_id, wid),
                    "signals_30d": db.signal_stats(publisher_area_id, wid, _iso_days_ago(30)),
                    "computed_at": datetime.now(timezone.utc).isoformat()})
        return out
    rec = _cached("webhook", publisher_area_id, wid, build)
    return rec if detail else compact(rec)


def copy_record(publisher_area_id: int, group: dict[str, Any], *, detail: bool = False) -> dict[str, Any]:
    """The track record of a copy group: the leader account's journal trades."""
    gid = str(group.get("id") or "")

    def build() -> dict[str, Any]:
        spec = str((group.get("leader") or {}).get("spec") or "")
        trades = db.list_journal_trades(publisher_area_id, accounts=[spec]) if spec else []
        out = summarize_trades(trades, _zone(publisher_area_id), detail=True)
        out.update({"basis": "leader" if spec else "none", "accounts_n": 1 if spec else 0, "signals": None, "signals_30d": None,
                    "computed_at": datetime.now(timezone.utc).isoformat()})
        return out
    rec = _cached("copy", publisher_area_id, gid, build)
    return rec if detail else compact(rec)


COMPACT_KEYS = ("basis", "accounts_n", "verified", "verified_share", "trades", "win_rate", "profit_factor", "net_pnl", "max_drawdown",
                "net_30d", "trades_30d", "net_90d", "trading_days", "first_trade_at", "last_trade_at", "signals", "signals_30d", "computed_at")


def compact(rec: dict[str, Any]) -> dict[str, Any]:
    return {k: rec.get(k) for k in COMPACT_KEYS}


# ------------------------------------------------------- subscription journal
def _signal_view(row: dict[str, Any]) -> dict[str, Any]:
    p = row.get("payload") if isinstance(row.get("payload"), dict) else {}
    return {"id": row["id"], "ts": row["ts"], "result": row["result"],
            "action": str(p.get("action") or p.get("side") or p.get("event") or ""), "symbol": str(p.get("symbol") or ""),
            "qty": p.get("qty") if isinstance(p.get("qty"), (int, float)) else None}


def subscription_journal(area_id: int, sub: dict[str, Any], *, limit: int = 50) -> dict[str, Any]:
    """What one subscription did for the subscriber: the signals it received and
    their outcomes, and the subscriber's own journal trades on the routed
    accounts since the subscription was created."""
    specs = sorted({str(a.get("spec") or "") for a in (sub.get("accounts") or []) if a.get("spec")})
    since = str(sub.get("created_at") or "")
    trades = db.list_journal_trades(area_id, frm=since, accounts=specs) if specs else []
    pnl = summarize_trades(trades, _zone(area_id), detail=False)
    is_copy = str(sub.get("webhook_id") or "").startswith("copy:")
    out: dict[str, Any] = {"subscription_id": sub.get("id"), "since": since, "accounts_n": len(specs), "pnl": pnl, "kind": "copy" if is_copy else "webhook"}
    if is_copy:
        gid = str(sub["webhook_id"])[5:]
        events = db.list_copy_events(int(sub["publisher_area_id"]), gid, limit=limit, followers=specs)
        out["copy_events"] = [{"ts": e["ts"], "kind": e["kind"], "symbol": e["symbol"], "detail": e["detail"], "latency_ms": e.get("latency_ms")} for e in events]
        out["signals"] = None
        out["recent"] = []
    else:
        wid = f"sub{int(sub['publisher_area_id'])}_{sub['webhook_id']}"
        out["signals"] = db.signal_stats(area_id, wid)
        out["signals_30d"] = db.signal_stats(area_id, wid, _iso_days_ago(30))
        out["recent"] = [_signal_view(r) for r in db.list_signals(area_id, limit=limit, webhook_id=wid)["items"]]
        out["copy_events"] = []
    return out


def reset() -> None:
    _cache.clear()

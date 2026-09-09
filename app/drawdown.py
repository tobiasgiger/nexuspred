"""Trailing max drawdown per trade account (prop-firm style).

Tradovate's risk record (``userAccountAutoLiq``) only carries the drawdown
*size* (``trailingMaxDrawdown``), the *cap* beyond which the threshold stops
trailing (``trailingMaxDrawdownLimit``) and the *mode*. The liquidation
threshold itself is not exposed — it follows the account's **peak**:

* **Intraday** (Tradovate ``RealTime``): the highest equity ever reached,
  including open P&L, updated tick by tick.
* **EOD**: the highest end-of-day balance (session close 17:00 New York).

``threshold = min(peak, cap) − size``, ``room = equity − threshold``.

The bridge tracks the peak itself from the moment it watches an account
(persisted per area in the ``dd_state`` setting), seeds the EOD peak from the
journal's daily balances where history was imported, and lets the user pin the
threshold shown by the prop firm (``seed_level``) so figures are exact from
that moment on. A peak only ever ratchets up.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

from . import config, db

ET = ZoneInfo("America/New_York")


def _num(v: Any) -> Optional[float]:
    try:
        return round(float(v), 2)
    except (TypeError, ValueError):
        return None


def session_day(now: datetime) -> str:
    """The futures trading day a timestamp belongs to (rolls at 17:00 New York)."""
    local = now.astimezone(ET)
    if local.hour >= 17:
        local += timedelta(days=1)
    return local.date().isoformat()


def normalize_mode(raw: Any) -> str:
    m = str(raw or "").strip().lower().replace("_", "").replace("-", "").replace(" ", "")
    if m in ("realtime", "intraday", "live"):
        return "Intraday"
    if m == "eod":
        return "EOD"
    return str(raw or "")


def _empty() -> dict[str, Any]:
    return {"dd_mode": "", "dd_size": None, "dd_cap": None, "dd_peak": None, "dd_level": None,
            "dd_room": None, "dd_seeded": False, "dd_since": None, "daily_loss_limit": None}


def _load(area_id: int) -> dict[str, Any]:
    st = config.load_settings(area_id=area_id).get("dd_state")
    return dict(st) if isinstance(st, dict) else {}


def _save(area_id: int, st: dict[str, Any]) -> None:
    config.save_settings({"dd_state": st}, area_id=area_id)


def _journal_eod_peak(area_id: int, account_id: int, spec: str) -> Optional[float]:
    """Highest end-of-day balance the journal knows for this account."""
    try:
        rows = db.list_journal_snapshots(area_id, days=3650, account=spec or str(account_id))
    except Exception:  # noqa: BLE001
        return None
    vals = [float(r["total_cash"]) for r in rows if int(r.get("account_id") or 0) == account_id and r.get("total_cash") is not None]
    return round(max(vals), 2) if vals else None


def apply(area_id: int, snap: dict[str, Any], rec: Optional[dict[str, Any]], *,
          now: Optional[datetime] = None) -> dict[str, Any]:
    """Derive the drawdown view for one account snapshot and advance its peak."""
    out = _empty()
    if not rec:
        return out
    now = now or datetime.now(timezone.utc)
    mode = normalize_mode(rec.get("trailingMaxDrawdownMode"))
    size = _num(rec.get("trailingMaxDrawdown")) if rec.get("trailingMaxDrawdown") not in (None, "") else None
    cap = _num(rec.get("trailingMaxDrawdownLimit")) if rec.get("trailingMaxDrawdownLimit") not in (None, "") else None
    daily = rec.get("dailyLossAutoLiq")
    out.update({"dd_mode": mode, "dd_size": size, "dd_cap": cap or None,
                "daily_loss_limit": _num(daily) if daily not in (None, "", 0) else None})
    if not size:
        return out

    account_id = int(snap.get("account_id") or 0)
    cash = float(snap.get("cash") or 0.0)
    equity = round(cash + float(snap.get("open") or 0.0), 2)
    st = _load(area_id)
    key = str(account_id)
    a = dict(st.get(key) or {})
    before = dict(a)

    candidates: list[float] = []
    if a.get("peak") is not None:
        candidates.append(float(a["peak"]))
    if a.get("seed_level") is not None:
        candidates.append(round(float(a["seed_level"]) + size, 2))
    if mode == "Intraday":
        candidates.append(equity)
    else:
        # EOD: the balance at the close of the previous session becomes a peak
        # candidate once the session has rolled; today's balance is only a
        # candidate-in-waiting. Journal history seeds the peak for past days.
        sd = session_day(now)
        if a.get("eod_day") and a["eod_day"] != sd and a.get("eod_cash") is not None:
            candidates.append(float(a["eod_cash"]))
        a["eod_day"], a["eod_cash"] = sd, cash
        today = now.astimezone(ET).date().isoformat()
        if a.get("hist_day") != today:
            hist = _journal_eod_peak(area_id, account_id, str(snap.get("spec") or ""))
            a["hist_day"], a["hist_peak"] = today, hist
        if a.get("hist_peak") is not None:
            candidates.append(float(a["hist_peak"]))
        if not candidates:
            candidates.append(cash)   # first observation: today's balance is all we know
    peak = round(max(candidates), 2)
    if a.get("peak") != peak:
        a["peak"] = peak
    a.setdefault("since", now.isoformat())

    eff = min(peak, cap) if cap else peak
    level = round(eff - size, 2)
    out.update({"dd_peak": peak, "dd_level": level, "dd_room": round(equity - level, 2),
                "dd_seeded": a.get("seed_level") is not None, "dd_since": a.get("since")})
    if a != before:
        st[key] = a
        _save(area_id, st)
    return out


def set_seed(area_id: int, account_id: int, level: Optional[float]) -> dict[str, Any]:
    """Pin the threshold the prop firm shows (``level``), or clear all tracking
    for the account when ``level`` is None. Returns the stored record."""
    st = _load(area_id)
    key = str(account_id)
    if level is None:
        st.pop(key, None)
        _save(area_id, st)
        return {}
    a = dict(st.get(key) or {})
    a["seed_level"] = round(float(level), 2)
    a["seed_at"] = datetime.now(timezone.utc).isoformat()
    a.pop("peak", None)            # re-derived from the seed on the next poll
    a["since"] = a["seed_at"]
    st[key] = a
    _save(area_id, st)
    return a

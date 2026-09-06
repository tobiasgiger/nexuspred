"""Contract-rollover warnings for the dated contracts in ``symbol_map``.

A mapping like ``"MNQ1!": "MNQU6"`` trades one specific contract month. When
that month expires (or reaches first notice) the alert keeps pointing at a dead
or deliverable contract — a classic, silent way to lose a whole day of signals.

Once a day per area this module parses every mapped contract, estimates its
roll date per product family, and when it is within ``rollover_warn_days``
(default 10) — or already past — it:

* records the warning in the area's runtime state (``/api/status`` →
  ``rollover``; the Overview shows a banner),
* writes an event-log line, and
* sends one alert (Discord + email, switch ``alert_on_rollover``) per contract
  and stage (*upcoming*, then *expired*), never repeating the same one.

Roll dates are estimated from exchange conventions (no network):

* equity index (ES/NQ/RTY/YM + micros): expiry = 3rd Friday of the contract
  month; the CME roll is the Thursday eight days before it.
* FX (6E/6J/… + micros): 2nd business day before the 3rd Wednesday.
* crypto (BTC/MBT/ETH/MET): last Friday of the contract month.
* metals, energy, grains, treasuries: physically delivered — the key date is
  **first notice / last trade shortly before the contract month**, so the
  reference date is the last business day of the *previous* month (energy: three
  business days before the 25th of the previous month).
* anything else: the first day of the contract month (conservative).

When a Tradovate session is connected the exact ``expirationDate`` from the
contract's maturity record is used instead of the estimate.
"""
from __future__ import annotations

import asyncio
import calendar
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

from . import alerts, config, context, state

MONTH_CODES = {"F": 1, "G": 2, "H": 3, "J": 4, "K": 5, "M": 6,
               "N": 7, "Q": 8, "U": 9, "V": 10, "X": 11, "Z": 12}
CODE_BY_MONTH = {v: k for k, v in MONTH_CODES.items()}
QUARTERLY = (3, 6, 9, 12)

FAMILIES: dict[str, tuple[str, ...]] = {
    "index": ("ES", "NQ", "RTY", "YM", "MES", "MNQ", "M2K", "MYM", "EMD", "NKD", "MME"),
    "fx": ("6E", "6J", "6B", "6A", "6C", "6S", "6N", "6M", "M6E", "M6A", "M6B", "MJY", "E7", "J7"),
    "crypto": ("BTC", "MBT", "ETH", "MET"),
    "metal": ("GC", "MGC", "SI", "SIL", "HG", "MHG", "PL", "PA", "QO", "QI"),
    "energy": ("CL", "MCL", "QM", "NG", "QG", "MNG", "RB", "HO", "BZ"),
    "grain": ("ZC", "ZS", "ZW", "ZM", "ZL", "ZO", "KE", "XC", "XK", "XW"),
    "treasury": ("ZB", "ZN", "ZF", "ZT", "UB", "TN", "ZQ", "SR3", "SR1"),
}
_FAMILY_OF = {root: fam for fam, roots in FAMILIES.items() for root in roots}


# ----------------------------------------------------------------- parsing
def parse_contract(name: str, today: Optional[date] = None) -> Optional[tuple[str, int, int]]:
    """``"MNQU6"`` → ``("MNQ", 9, 2026)``; ``"MNQU26"`` too. ``None`` for bare
    roots / continuous symbols (``MNQ``, ``MNQ1!``)."""
    n = (name or "").strip().upper()
    if len(n) < 3:
        return None
    digits = ""
    while n and n[-1].isdigit():
        digits = n[-1] + digits
        n = n[:-1]
    if not digits or len(digits) > 2 or not n or n[-1] not in MONTH_CODES:
        return None
    root, code = n[:-1], n[-1]
    if not root or not root[-1].isalnum():
        return None
    today = today or datetime.now(timezone.utc).date()
    if len(digits) == 1:
        # One digit = year within a decade: pick the decade that puts the
        # contract between "last year" and "eight years out".
        year = today.year - (today.year % 10) + int(digits)
        if year < today.year - 1:       # "6" seen in 2029 → 2036, not 2026
            year += 10
        elif year > today.year + 8:     # "9" seen in Jan 2030 → 2029, not 2039
            year -= 10
    else:
        year = 2000 + int(digits)
    return root, MONTH_CODES[code], year


def family(root: str) -> str:
    return _FAMILY_OF.get(root.upper(), "other")


# ----------------------------------------------------------- date helpers
def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    last = date(year, month, calendar.monthrange(year, month)[1])
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def _prev_month(year: int, month: int) -> tuple[int, int]:
    return (year - 1, 12) if month == 1 else (year, month - 1)


def _business_days_before(d: date, n: int) -> date:
    while n > 0:
        d -= timedelta(days=1)
        if d.weekday() < 5:
            n -= 1
    return d


def _last_business_day(year: int, month: int) -> date:
    d = date(year, month, calendar.monthrange(year, month)[1])
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def roll_date(root: str, month: int, year: int) -> tuple[date, str]:
    """Estimated (reference date, what it is) for a contract month."""
    fam = family(root)
    if fam == "index":
        return _nth_weekday(year, month, 4, 3), "expiry (3rd Friday)"
    if fam == "fx":
        return _business_days_before(_nth_weekday(year, month, 2, 3), 2), "last trade"
    if fam == "crypto":
        return _last_weekday(year, month, 4), "expiry (last Friday)"
    py, pm = _prev_month(year, month)
    if fam == "energy":
        return _business_days_before(date(py, pm, 25), 3), "last trade"
    if fam in ("metal", "grain", "treasury"):
        return _last_business_day(py, pm), "first notice"
    return date(year, month, 1), "contract month"


def next_contract(root: str, month: int, year: int) -> str:
    """The contract to roll into: next quarterly month for index/FX/treasury/
    crypto products, next calendar month otherwise."""
    fam = family(root)
    if fam in ("index", "fx", "treasury", "crypto") or month in QUARTERLY and fam == "other":
        nxt = next((m for m in QUARTERLY if m > month), None)
        month, year = (nxt, year) if nxt else (3, year + 1)
    else:
        month, year = (1, year + 1) if month == 12 else (month + 1, year)
    return f"{root}{CODE_BY_MONTH[month]}{year % 10}"


# ---------------------------------------------------------------- checking
def evaluate(symbol_map: dict[str, str], warn_days: int, today: Optional[date] = None,
             exact: Optional[dict[str, date]] = None) -> list[dict[str, Any]]:
    """Warnings for every mapped contract within ``warn_days`` of its roll date
    (or past it). ``exact`` may carry broker-provided expiry dates by contract."""
    today = today or datetime.now(timezone.utc).date()
    out: list[dict[str, Any]] = []
    for tv_symbol, contract in (symbol_map or {}).items():
        parsed = parse_contract(str(contract), today)
        if not parsed:
            continue
        root, month, year = parsed
        ref, what = roll_date(root, month, year)
        source = "estimate"
        if exact and exact.get(str(contract).upper()):
            ref, what, source = exact[str(contract).upper()], "expiry", "broker"
        days_left = (ref - today).days
        if days_left > warn_days:
            continue
        stage = "expired" if days_left < 0 else "upcoming"
        out.append({
            "tv_symbol": tv_symbol, "contract": str(contract).upper(), "root": root,
            "family": family(root), "date": ref.isoformat(), "date_kind": what, "source": source,
            "days_left": days_left, "stage": stage, "next": next_contract(root, month, year),
        })
    out.sort(key=lambda w: w["days_left"])
    return out


def _message(items: list[dict[str, Any]]) -> str:
    lines = []
    for w in items:
        when = (f"in {w['days_left']} day(s)" if w["days_left"] > 0
                else "today" if w["days_left"] == 0 else f"{-w['days_left']} day(s) ago")
        lines.append(f"• `{w['tv_symbol']}` → **{w['contract']}** — {w['date_kind']} {w['date']} "
                     f"({when}); suggested: **{w['next']}**")
    head = "📅 **Contract rollover** — update Settings → Symbol map:"
    return head + "\n" + "\n".join(lines)


async def _exact_dates(area_id: int, contracts: list[str]) -> dict[str, date]:
    """Expiry dates from a connected Tradovate session (best effort, silent)."""
    from . import tradovate
    out: dict[str, date] = {}
    sessions = [s for s in tradovate.manager_for(area_id).all()
                if state.session_status(s.name).get("connected")]
    if not sessions or not contracts:
        return out
    sess = sessions[0]
    for name in contracts:
        try:
            found = await sess._request("GET", "/contract/find", params={"name": name})
            mid = (found or {}).get("contractMaturityId")
            if not mid:
                continue
            mat = await sess._request("GET", "/contractMaturity/item", params={"id": mid})
            exp = (mat or {}).get("expirationDate")
            if exp:
                out[name.upper()] = datetime.fromisoformat(str(exp).replace("Z", "+00:00")).date()
        except Exception:  # noqa: BLE001 - estimates are the fallback
            continue
    return out


_last_run: dict[int, date] = {}


def reset() -> None:
    _last_run.clear()


async def check_area(area_id: int, *, force: bool = False, today: Optional[date] = None) -> list[dict[str, Any]]:
    """Run the daily check for one area (no-op if already done today unless
    ``force``). Returns the current warnings."""
    today = today or datetime.now(timezone.utc).date()
    if not force and _last_run.get(area_id) == today:
        return state.rollover_warnings(area_id)
    _last_run[area_id] = today
    with context.use_area(area_id):
        s = config.load_settings()
        warn_days = int(s.get("rollover_warn_days", 10) or 0)
        symbol_map = s.get("symbol_map") or {}
        dated = [c for c in symbol_map.values() if parse_contract(str(c), today)]
        exact = await _exact_dates(area_id, dated) if dated else {}
        warnings = evaluate(symbol_map, warn_days, today, exact)
        state.set_rollover_warnings(warnings, area_id)

        notified: dict[str, str] = dict(s.get("rollover_notified") or {})
        fresh = [w for w in warnings if notified.get(w["contract"]) != w["stage"]]
        if fresh:
            for w in fresh:
                state.log_event("warn" if w["stage"] == "upcoming" else "error",
                                f"Rollover: {w['tv_symbol']} → {w['contract']} {w['date_kind']} {w['date']} "
                                f"({'in ' + str(w['days_left']) + 'd' if w['days_left'] >= 0 else 'passed'}); "
                                f"suggested {w['next']}")
            if s.get("alert_on_rollover", True):
                try:
                    await alerts.contract_rollover(_message(fresh))
                except Exception as exc:  # noqa: BLE001
                    state.log_event("warn", f"rollover alert failed: {exc}")
            for w in fresh:
                notified[w["contract"]] = w["stage"]
        # Forget contracts that are no longer mapped, so a re-mapped symbol alerts again.
        keep = {w["contract"] for w in warnings}
        notified = {k: v for k, v in notified.items() if k in keep}
        if notified != (s.get("rollover_notified") or {}):
            config.save_settings({"rollover_notified": notified})
        return warnings


async def check_all(today: Optional[date] = None) -> None:
    from . import db
    try:
        area_ids = db.all_area_ids()
    except Exception:  # noqa: BLE001
        return
    await asyncio.gather(*(check_area(a, today=today) for a in area_ids), return_exceptions=True)

"""Per-trade-account risk guard: daily loss limit, daily profit limit and a
fixed flatten time.

Configured per trade account under Settings → Broker Accounts (``risk`` key
on the account entry: ``loss_limit``, ``profit_limit`` in account currency,
``flatten_at`` as ``HH:MM`` local time in ``journal_timezone``; 0 / empty =
off). The guard rides on the live P&L poll: whenever today's P&L (realised +
open, the broker's own figures) crosses a limit, or the clock passes the
flatten time, the account is **flattened** (every working order cancelled,
every position closed at market) and **locked** for the rest of the local day.

A locked account refuses every order the bridge would send it — webhooks,
Discord signals, marketplace subscriptions and copy-trading mirrors alike —
because the check sits in :meth:`TradovateSession.place_order`, the one path
all of them use. Flattening itself and the SOS flatten-all run with the guard
bypassed. While locked, a position that reappears (a manual trade in the
Tradovate UI) is flattened again on the next poll. The lock lasts the Tradovate
trading day (17:00 New York roll) or until *Unlock* on the account. A rule
never fires twice on the same state: a flatten time fires once per clock day,
a loss / profit rule not again while the broker still shows the P&L figure it
fired on (which happens across the roll, when the cached snapshot lags).
"""
from __future__ import annotations

import asyncio
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

from . import alerts, config, context, state
from .drawdown import ET, session_day

_bypass: ContextVar[bool] = ContextVar("risk_bypass", default=False)
_flatten_lock: dict[tuple[int, str], asyncio.Lock] = {}
_reflatten_at: dict[tuple[int, str], float] = {}     # (area, spec) → monotonic of the last re-flatten
REFLATTEN_EVERY_S = 30.0


def reset() -> None:
    _flatten_lock.clear()
    _reflatten_at.clear()
    _warned_no_id.clear()


class bypass:
    """``with risk.bypass():`` — let flatten / SOS orders through a lock."""
    def __enter__(self) -> None:
        self._tok = _bypass.set(True)

    def __exit__(self, *exc: Any) -> None:
        _bypass.reset(self._tok)


def bypassed() -> bool:
    return bool(_bypass.get())


# ----------------------------------------------------------------- config
def normalize(raw: Any) -> dict[str, Any]:
    """Validate an account's risk block; raises ValueError."""
    r = raw if isinstance(raw, dict) else {}
    out: dict[str, Any] = {}
    for key in ("loss_limit", "profit_limit"):
        v = r.get(key)
        v = 0.0 if v in (None, "") else float(v)
        if v < 0 or v > 1e7:
            raise ValueError(f"{key} must be between 0 and 10,000,000")
        out[key] = round(v, 2)
    tz = str(r.get("flatten_tz") or "local")
    if tz not in ("local", "ny"):
        raise ValueError("flatten_tz must be local or ny")
    out["flatten_tz"] = tz
    t = str(r.get("flatten_at") or "").strip()
    if t:
        try:
            hh, mm = (int(x) for x in t.split(":")[:2])
        except ValueError as exc:
            raise ValueError("flatten_at must be HH:MM") from exc
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            raise ValueError("flatten_at must be HH:MM")
        t = f"{hh:02d}:{mm:02d}"
    out["flatten_at"] = t
    return out


def active(r: Optional[dict[str, Any]]) -> bool:
    return bool(r and (r.get("loss_limit") or r.get("profit_limit") or r.get("flatten_at")))


def any_active(settings: dict[str, Any]) -> bool:
    """Whether any account of the area has a risk rule (→ fast P&L cadence)."""
    for t in settings.get("token_accounts") or []:
        for a in t.get("accounts") or []:
            if active(a.get("risk")):
                return True
    return False


def config_for(area_id: int, spec: str) -> dict[str, Any]:
    for t in config.load_settings(area_id=area_id).get("token_accounts") or []:
        for a in t.get("accounts") or []:
            if (a.get("spec") or a.get("account_spec")) == spec:
                return dict(a.get("risk") or {})
    return {}


# ------------------------------------------------------------------ state
def _zone(settings: dict[str, Any]) -> ZoneInfo:
    try:
        return ZoneInfo(str(settings.get("journal_timezone") or "Europe/Zurich"))
    except Exception:  # noqa: BLE001
        return ZoneInfo("Europe/Zurich")


def local_now(area_id: int, settings: Optional[dict[str, Any]] = None) -> datetime:
    s = settings if settings is not None else config.load_settings(area_id=area_id)
    return datetime.now(timezone.utc).astimezone(_zone(s))


def trading_day(now: Optional[datetime] = None) -> str:
    """The Tradovate trading day a lock belongs to: rolls at 17:00 New York,
    like the broker's own daily P&L (a Zurich-midnight day would unlock an
    account while Tradovate still counts the loss as today's)."""
    return session_day(now or datetime.now(timezone.utc))


def _state(area_id: int) -> dict[str, Any]:
    st = config.setting("risk_state", area_id=area_id)
    return dict(st) if isinstance(st, dict) else {}


def lock_of(area_id: int, spec: str, *, settings: Optional[dict[str, Any]] = None) -> Optional[dict[str, Any]]:
    """The account's lock record for today, or None. Runs before every order:
    without ``settings`` only the ``risk_state`` key is read."""
    raw = settings.get("risk_state") if settings is not None else config.setting("risk_state", area_id=area_id)
    st = raw if isinstance(raw, dict) else {}
    rec = st.get(spec)
    if not isinstance(rec, dict) or rec.get("unlocked"):
        return None
    if rec.get("day") != trading_day():
        return None
    return dict(rec)


def is_locked(area_id: int, spec: str) -> Optional[str]:
    """The lock reason if the account is locked today, else None."""
    rec = lock_of(area_id, spec)
    return str(rec.get("reason") or "risk guard") if rec else None


def unlock(area_id: int, spec: str) -> bool:
    """Lift today's lock by hand. The record stays (marked ``unlocked``) so the
    same rule does not fire again on the very state it fired on."""
    st = _state(area_id)
    rec = st.get(spec)
    if not isinstance(rec, dict) or rec.get("unlocked"):
        return False
    rec["unlocked"] = True
    rec["unlocked_at"] = datetime.now(timezone.utc).isoformat()
    st[spec] = rec
    config.save_settings({"risk_state": st}, area_id=area_id)
    return True


def _lock(area_id: int, spec: str, kind: str, reason: str, total: float, *,
          realized: Optional[float] = None, clock_day: str = "") -> None:
    st = _state(area_id)
    st[spec] = {"day": trading_day(), "kind": kind, "reason": reason,
                "pnl": round(total, 2), "at": datetime.now(timezone.utc).isoformat(),
                "realized": None if realized is None else round(float(realized), 2), "clock_day": clock_day}
    config.save_settings({"risk_state": st}, area_id=area_id)


def _already_fired(rec: Any, kind: str, realized: float, clock_day: str) -> bool:
    """The rule already fired on this very state: a time rule once per clock
    day; a loss / profit rule while the broker still reports the realised
    figure it fired on (it resets at the 17:00 New York roll, the cached
    snapshot a little later)."""
    if not isinstance(rec, dict) or rec.get("kind") != kind:
        return False
    if kind == "time":
        return bool(clock_day) and rec.get("clock_day") == clock_day
    stored = rec.get("realized")
    return stored is not None and round(float(realized), 2) == round(float(stored), 2)


# ----------------------------------------------------------------- engine
def evaluate(r: dict[str, Any], total: float, now_local: datetime, now_ny: Optional[datetime] = None) -> Optional[tuple[str, str]]:
    """(kind, reason) when a rule is hit for today's P&L ``total``. The flatten
    time is read in the journal timezone, or in New York time when the rule
    says so (``flatten_tz: ny``) — the exchange's clock, unaffected by the two
    weeks a year Europe and the US disagree on daylight saving."""
    loss = float(r.get("loss_limit") or 0)
    profit = float(r.get("profit_limit") or 0)
    if loss and total <= -loss:
        return "loss", f"daily loss limit {loss:,.2f} hit (P&L {total:+,.2f})"
    if profit and total >= profit:
        return "profit", f"daily profit target {profit:,.2f} reached (P&L {total:+,.2f})"
    t = str(r.get("flatten_at") or "")
    if t:
        hh, mm = (int(x) for x in t.split(":")[:2])
        clock = (now_ny or now_local.astimezone(ET)) if str(r.get("flatten_tz") or "local") == "ny" else now_local
        if (clock.hour, clock.minute) >= (hh, mm):
            return "time", f"flatten time {t}{' New York' if clock is not now_local else ''} reached (P&L {total:+,.2f})"
    return None


async def flatten_account(session: Any, account: dict[str, Any]) -> tuple[int, int, list[str]]:
    """Cancel every working order and close every position of one account.
    Returns (cancelled, flattened, errors)."""
    from .engine.common import _cancel_working
    from .tradovate import AccountExecutor, TradovateError
    ex = AccountExecutor(session, account)
    errors: list[str] = []
    with bypass():
        cancelled = await _cancel_working(ex, "", errors)
        try:
            positions = await ex.positions()
        except TradovateError as exc:
            errors.append(f"list positions: {exc}")
            positions = []
        symbols = [p.get("symbol") for p in positions if p.get("symbol")]
        results = await asyncio.gather(*(ex.liquidate_position(s) for s in symbols), return_exceptions=True)
    flattened = 0
    for sym, r in zip(symbols, results):
        if isinstance(r, TradovateError):
            errors.append(f"flatten {sym}: {r}")
        elif isinstance(r, BaseException):
            errors.append(f"flatten {sym}: {type(r).__name__}: {r}")
        else:
            flattened += 1
    return cancelled, flattened, errors


_warned_no_id: set[tuple[int, str]] = set()


async def check_area(area_id: int, sessions: list[Any], snapshots: list[dict[str, Any]], *,
                     positions: Optional[dict[str, Optional[list[dict[str, Any]]]]] = None,
                     settings: Optional[dict[str, Any]] = None) -> list[dict[str, Any]]:
    """One P&L tick: apply every account's rules. Returns the triggers fired
    (also used by tests). Annotates each snapshot with its ``risk`` view.
    ``positions`` may carry the raw ``/position/list`` per login (from the P&L
    poll) so "still holds a position" is judged by the broker's list, not by
    an open P&L that can be exactly 0.00. ``settings`` is the tick's snapshot."""
    s = settings if settings is not None else config.load_settings(area_id=area_id)
    now_local = local_now(area_id, s)
    now_ny = now_local.astimezone(ET)
    raw_state = s.get("risk_state") if isinstance(s.get("risk_state"), dict) else {}
    # keyed by (login, broker account id): id spaces of different brokers on one
    # workspace may overlap, and a wrong pairing here would flatten the wrong account
    by_id: dict[tuple[str, int], tuple[Any, dict[str, Any]]] = {}
    by_aid: dict[int, list[tuple[Any, dict[str, Any]]]] = {}      # fallback for a snapshot without a login name
    open_accounts: Optional[set[tuple[str, int]]] = None
    if positions:
        open_accounts = set()
        for login, raw in positions.items():
            for p in raw or []:
                if p.get("netPos"):
                    open_accounts.add((login, int(p.get("accountId") or 0)))
    for sess in sessions:
        for a in sess.accounts:
            if a.get("id"):
                by_id[(sess.name, int(a["id"]))] = (sess, a)
                by_aid.setdefault(int(a["id"]), []).append((sess, a))
            elif active(a.get("risk")) and (area_id, a.get("spec") or "") not in _warned_no_id:
                _warned_no_id.add((area_id, a.get("spec") or ""))
                state.log_event("error", f"Risk guard: {a.get('spec')} has rules but no broker account id — "
                                         "it is NOT guarded; run Connect & Verify on its login")
    fired: list[dict[str, Any]] = []
    for snap in snapshots:
        snap_aid = int(snap.get("account_id") or 0)
        pair = by_id.get((str(snap.get("login") or ""), snap_aid))
        if not pair and not snap.get("login") and len(by_aid.get(snap_aid, [])) == 1:
            pair = by_aid[snap_aid][0]                         # unambiguous without a login name
        if not pair:
            continue
        sess, acc = pair
        spec = acc.get("spec") or snap.get("spec") or ""
        r = acc.get("risk") or {}
        lock = lock_of(area_id, spec, settings=s)
        snap["risk"] = {"loss_limit": r.get("loss_limit") or 0, "profit_limit": r.get("profit_limit") or 0,
                        "flatten_at": r.get("flatten_at") or "", "flatten_tz": r.get("flatten_tz") or "local", "locked": bool(lock),
                        "reason": (lock or {}).get("reason", ""), "kind": (lock or {}).get("kind", "")}
        total = float(snap.get("realized") or 0) + float(snap.get("open") or 0)
        if lock:
            # still locked: a position that came back (manual trade) is closed again
            holds = ((sess.name, snap_aid) in open_accounts) if open_accounts is not None else bool(float(snap.get("open") or 0))
            if holds and _due(area_id, spec):
                c, f, errs = await flatten_account(sess, acc)
                if f or errs:
                    state.log_event("warn", f"🔒 {spec} is locked ({lock.get('reason')}): position closed again"
                                            + (f" — {'; '.join(errs)}" if errs else ""))
            continue
        if not active(r):
            continue
        hit = evaluate(r, total, now_local, now_ny)
        if not hit:
            continue
        kind, reason = hit
        clock = now_ny if str(r.get("flatten_tz") or "local") == "ny" else now_local
        realized = float(snap.get("realized") or 0)
        if _already_fired(raw_state.get(spec), kind, realized, clock.date().isoformat()):
            continue                                   # fired on this state already (roll / unlock)
        key = (area_id, spec)
        lk = _flatten_lock.setdefault(key, asyncio.Lock())
        if lk.locked():
            continue
        async with lk:
            # lock first: from this moment every bridge order for the account is
            # refused, so nothing can slip in while the flatten is under way
            _lock(area_id, spec, kind, reason, total, realized=realized, clock_day=clock.date().isoformat())
            c, f, errs = await flatten_account(sess, acc)
            snap["risk"].update({"locked": True, "reason": reason, "kind": kind})
            fired.append({"spec": spec, "kind": kind, "reason": reason, "cancelled": c, "flattened": f, "errors": errs, "pnl": total})
            state.log_event("warn", f"🔒 Risk guard: {spec} flattened and locked for today — {reason}"
                                    f" ({c} order(s) cancelled, {f} position(s) closed" + (f"; errors: {'; '.join(errs)}" if errs else "") + ")")
            with context.use_area(area_id):
                try:
                    await alerts.risk_triggered(spec, kind, reason, total, errs)
                except Exception as exc:  # noqa: BLE001
                    state.log_event("warn", f"risk alert failed: {exc}")
    return fired


def _due(area_id: int, spec: str) -> bool:
    import time
    key = (area_id, spec)
    now = time.monotonic()
    if now - _reflatten_at.get(key, -1e9) < REFLATTEN_EVERY_S:
        return False
    _reflatten_at[key] = now
    return True


def overview(area_id: int) -> list[dict[str, Any]]:
    """Every account's risk config + lock (for the settings page)."""
    s = config.load_settings(area_id=area_id)
    out = []
    for idx, t in enumerate(s.get("token_accounts") or []):
        for a in t.get("accounts") or []:
            spec = a.get("spec") or a.get("account_spec") or ""
            lock = lock_of(area_id, spec, settings=s)
            out.append({"token_idx": idx, "spec": spec, "risk": dict(a.get("risk") or {}),
                        "locked": bool(lock), "lock": lock, "guarded": bool(a.get("id")) or not active(a.get("risk"))})
    return out

"""Per-trade-account risk guard: daily loss limit, daily profit limit and a
fixed flatten time.

Configured per trade account under Settings → Tradovate Accounts (``risk`` key
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
Tradovate UI) is flattened again on the next poll. The lock clears with the
next local day or by hand (*Unlock* on the account).
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
    st = config.load_settings(area_id=area_id).get("risk_state")
    return dict(st) if isinstance(st, dict) else {}


def lock_of(area_id: int, spec: str, *, settings: Optional[dict[str, Any]] = None) -> Optional[dict[str, Any]]:
    """The account's lock record for today, or None."""
    s = settings if settings is not None else config.load_settings(area_id=area_id)
    st = s.get("risk_state") if isinstance(s.get("risk_state"), dict) else {}
    rec = st.get(spec)
    if not isinstance(rec, dict):
        return None
    if rec.get("day") != trading_day():
        return None
    return dict(rec)


def is_locked(area_id: int, spec: str) -> Optional[str]:
    """The lock reason if the account is locked today, else None."""
    rec = lock_of(area_id, spec)
    return str(rec.get("reason") or "risk guard") if rec else None


def unlock(area_id: int, spec: str) -> bool:
    st = _state(area_id)
    if spec not in st:
        return False
    st.pop(spec, None)
    config.save_settings({"risk_state": st}, area_id=area_id)
    return True


def _lock(area_id: int, spec: str, kind: str, reason: str, total: float) -> None:
    st = _state(area_id)
    st[spec] = {"day": trading_day(), "kind": kind, "reason": reason,
                "pnl": round(total, 2), "at": datetime.now(timezone.utc).isoformat()}
    config.save_settings({"risk_state": st}, area_id=area_id)


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


async def check_area(area_id: int, sessions: list[Any], snapshots: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One P&L tick: apply every account's rules. Returns the triggers fired
    (also used by tests). Annotates each snapshot with its ``risk`` view."""
    s = config.load_settings(area_id=area_id)
    now_local = local_now(area_id, s)
    by_id: dict[int, tuple[Any, dict[str, Any]]] = {}
    for sess in sessions:
        for a in sess.accounts:
            if a.get("id"):
                by_id[int(a["id"])] = (sess, a)
    fired: list[dict[str, Any]] = []
    for snap in snapshots:
        pair = by_id.get(int(snap.get("account_id") or 0))
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
            if float(snap.get("open") or 0) and _due(area_id, spec):
                c, f, errs = await flatten_account(sess, acc)
                if f or errs:
                    state.log_event("warn", f"🔒 {spec} is locked ({lock.get('reason')}): position closed again"
                                            + (f" — {'; '.join(errs)}" if errs else ""))
            continue
        if not active(r):
            continue
        hit = evaluate(r, total, now_local, now_local.astimezone(ET))
        if not hit:
            continue
        kind, reason = hit
        key = (area_id, spec)
        lk = _flatten_lock.setdefault(key, asyncio.Lock())
        if lk.locked():
            continue
        async with lk:
            c, f, errs = await flatten_account(sess, acc)
            _lock(area_id, spec, kind, reason, total)
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
                        "locked": bool(lock), "lock": lock})
    return out

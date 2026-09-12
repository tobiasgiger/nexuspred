"""Position, execution-agent and daily-summary watcher → alert channels.

Rides on the live P&L poll (:mod:`app.pnl`): every tick each connected login
is asked for its ``/position/list`` and each account for its cash-balance
snapshot. Comparing two consecutive ticks tells us, per account and contract,
whether a position was **opened**, **added to**, **reduced** or **closed** —
regardless of *who* did it (a bridge signal, a broker-side stop / target fill,
or a manual click in the Tradovate UI). A close is attributed the account's
realised-P&L change between the two ticks, which is the broker's own figure.

The first observation of an area only seeds the baseline (no alerts for
positions that were already open when the bridge started). The same tick also
watches execution agents going offline / online and sends one daily summary at
the configured local time.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from . import alerts, config, context, db, relay, state
from . import events as bus

# area → (account_id, contract_id) → {"qty", "price", "symbol", "account", "opened_at"}
_positions: dict[int, dict[tuple[int, int], dict[str, Any]]] = {}
_seeded: set[int] = set()
_realized: dict[tuple[int, int], float] = {}          # (area, account_id) → last realised P&L
_names: dict[tuple[int, int], str] = {}               # (area, contract_id) → contract name
_agents: dict[int, dict[int, bool]] = {}              # area → agent_id → online?
_summary_sent: dict[int, str] = {}                    # area → local date of the last summary
_closed_today: dict[int, list[dict[str, Any]]] = {}   # area → closes since the last summary
CLOSES_KEPT = 500


def reset() -> None:
    _positions.clear()
    _seeded.clear()
    _realized.clear()
    _names.clear()
    _agents.clear()
    _agent_lists.clear()
    _summary_sent.clear()
    _closed_today.clear()


def trade_alerts_enabled(settings: dict[str, Any]) -> bool:
    """Whether positions must be polled at the fast cadence for alerts."""
    return bool(settings.get("alert_on_trade_opened", True) or settings.get("alert_on_trade_closed", True))


def _direction(net: float) -> str:
    return "LONG" if net > 0 else "SHORT"


def _duration(opened_at: Optional[str]) -> str:
    if not opened_at:
        return ""
    try:
        secs = (datetime.now(timezone.utc) - datetime.fromisoformat(opened_at)).total_seconds()
    except ValueError:
        return ""
    if secs < 90:
        return f"{int(secs)} s"
    if secs < 5400:
        return f"{int(secs // 60)} min"
    return f"{secs / 3600:.1f} h"


async def _symbol(session: Any, area_id: int, cid: int) -> str:
    key = (area_id, int(cid))
    name = _names.get(key)
    if name:
        return name
    try:
        item = await session.contract_info(cid)
        name = str((item or {}).get("name") or cid)
    except Exception:  # noqa: BLE001 - the id is still a usable label
        name = str(cid)
    _names[key] = name
    return name


async def _current_positions(area_id: int, sessions: list[Any],
                             positions: Optional[dict[str, Optional[list[dict[str, Any]]]]] = None) -> tuple[dict[tuple[int, int], dict[str, Any]], set[int]]:
    """(positions keyed by (account, contract), account ids that were polled OK).
    ``positions`` may carry the raw ``/position/list`` per login already fetched
    by the P&L poll (None for a login that failed) so it is not read twice."""
    current: dict[tuple[int, int], dict[str, Any]] = {}
    polled: set[int] = set()
    for s in sessions:
        ids = {int(a["id"]): (a.get("spec") or str(a["id"])) for a in s.accounts if a.get("id")}
        if not ids:
            continue
        if positions is not None and s.name in positions:
            raw = positions[s.name]
            if raw is None:
                continue
        else:
            try:
                raw = await s.positions_snapshot()
            except Exception:  # noqa: BLE001 - an unreachable login must not look like "everything closed"
                continue
        polled.update(ids)
        for p in raw if isinstance(raw, list) else []:
            aid = p.get("accountId")
            net = float(p.get("netPos") or 0)
            if aid not in ids or not net:
                continue
            cid = int(p.get("contractId") or 0)
            current[(int(aid), cid)] = {
                "qty": net, "price": p.get("netPrice"), "account": ids[int(aid)],
                "symbol": await _symbol(s, area_id, cid),
            }
    return current, polled


async def observe_area(area_id: int, sessions: list[Any], snapshots: list[dict[str, Any]], *,
                       positions: Optional[dict[str, Optional[list[dict[str, Any]]]]] = None,
                       settings: Optional[dict[str, Any]] = None) -> list[dict[str, Any]]:
    """One tick: diff positions against the last tick, fire trade alerts.
    Returns the list of events (also used by tests)."""
    s = settings if settings is not None else config.load_settings(area_id=area_id)
    want_open = bool(s.get("alert_on_trade_opened", True))
    want_close = bool(s.get("alert_on_trade_closed", True))
    if not (want_open or want_close):
        return []
    current, polled = await _current_positions(area_id, sessions, positions)
    prev = _positions.setdefault(area_id, {})
    now = datetime.now(timezone.utc).isoformat()

    realized_now = {int(a["account_id"]): float(a.get("realized") or 0.0) for a in snapshots}
    delta: dict[int, Optional[float]] = {}
    for acc, val in realized_now.items():
        old = _realized.get((area_id, acc))
        delta[acc] = round(val - old, 2) if old is not None else None
        _realized[(area_id, acc)] = val

    events: list[dict[str, Any]] = []
    for key, pos in current.items():
        old = prev.get(key)
        if old is None:
            pos["opened_at"] = now
            events.append({"kind": "opened", "account_id": key[0], **pos})
        elif (old["qty"] > 0) != (pos["qty"] > 0):          # reversed: close the old side, open the new
            events.append({"kind": "closed", "account_id": key[0], **old})
            pos["opened_at"] = now
            events.append({"kind": "opened", "account_id": key[0], **pos})
        else:
            pos["opened_at"] = old.get("opened_at")
            if abs(pos["qty"]) > abs(old["qty"]):
                events.append({"kind": "added", "account_id": key[0], **pos, "added": abs(pos["qty"]) - abs(old["qty"])})
            elif abs(pos["qty"]) < abs(old["qty"]):
                events.append({"kind": "reduced", "account_id": key[0], **old, "remaining": abs(pos["qty"])})
    for key, old in prev.items():
        if key not in current and key[0] in polled:
            events.append({"kind": "closed", "account_id": key[0], **old})
    # the account's realised change this tick is the broker's figure for what just closed
    for ev in events:
        if ev["kind"] in ("closed", "reduced"):
            ev["pnl"] = delta.get(ev["account_id"])
            ev["duration"] = _duration(ev.get("opened_at"))

    # store the new picture (keep entries of logins that could not be polled)
    for key in [k for k in prev if k[0] in polled]:
        prev.pop(key)
    prev.update(current)

    if area_id not in _seeded:
        _seeded.add(area_id)
        return []
    with context.use_area(area_id):
        for ev in events:
            if not alerts.account_alerts_on(ev["account"], s):
                continue  # tracked, but this account is not on the alert list
            if ev["kind"] == "opened" and want_open:
                bus.emit("position.opened", account=ev["account"], symbol=ev["symbol"], direction=_direction(ev["qty"]), qty=abs(ev["qty"]), price=ev.get("price"))
            elif ev["kind"] == "added" and want_open:
                bus.emit("position.added", account=ev["account"], symbol=ev["symbol"], direction=_direction(ev["qty"]), added=ev["added"], total=abs(ev["qty"]))
            elif ev["kind"] == "reduced" and want_close:
                bus.emit("position.closed", account=ev["account"], symbol=ev["symbol"], direction=_direction(ev["qty"]), qty=abs(ev["qty"]) - ev["remaining"],
                            pnl=ev.get("pnl"), duration=ev.get("duration", ""), remaining=ev["remaining"])
            elif ev["kind"] == "closed":
                closes = _closed_today.setdefault(area_id, [])
                closes.append({"account": ev["account"], "symbol": ev["symbol"], "pnl": ev.get("pnl")})
                if len(closes) > CLOSES_KEPT:
                    del closes[:-CLOSES_KEPT]           # no daily summary configured: never grow without bound
                if want_close:
                    bus.emit("position.closed", account=ev["account"], symbol=ev["symbol"], direction=_direction(ev["qty"]), qty=abs(ev["qty"]),
                                pnl=ev.get("pnl"), duration=ev.get("duration", ""), remaining=0)
    return events


# ------------------------------------------------------------ agents
AGENT_LIST_TTL_S = 30.0          # the agent table changes on pairing / removal only: no query per tick
_agent_lists: dict[int, tuple[float, list[dict[str, Any]]]] = {}


def _agents_of(area_id: int) -> list[dict[str, Any]]:
    import time
    hit = _agent_lists.get(area_id)
    if hit and time.monotonic() - hit[0] < AGENT_LIST_TTL_S:
        return hit[1]
    rows = db.list_agents(area_id)
    _agent_lists[area_id] = (time.monotonic(), rows)
    return rows


async def observe_agents(area_id: int, settings: Optional[dict[str, Any]] = None) -> None:
    s = settings if settings is not None else config.load_settings(area_id=area_id)
    if not (s.get("alert_on_agent_lost", True) or s.get("alert_on_agent_restored", True)):
        return
    known = _agents.setdefault(area_id, {})
    seen: set[int] = set()
    with context.use_area(area_id):
        for agent in _agents_of(area_id):
            aid = int(agent["id"])
            seen.add(aid)
            online = relay.is_online(aid)
            if aid not in known:
                known[aid] = online  # first observation: baseline only
                continue
            if online == known[aid]:
                continue
            known[aid] = online
            if online and s.get("alert_on_agent_restored", True):
                await bus.emit_async("agent.restored", name=agent["name"])
            elif not online and s.get("alert_on_agent_lost", True):
                await bus.emit_async("agent.lost", name=agent["name"], last_ip=agent.get("last_ip") or "")
    for aid in [a for a in known if a not in seen]:
        known.pop(aid, None)


# ------------------------------------------------------ daily summary
def _local_now(area_id: int, settings: Optional[dict[str, Any]] = None) -> datetime:
    from zoneinfo import ZoneInfo
    s = settings if settings is not None else config.load_settings(area_id=area_id)
    name = str(s.get("journal_timezone") or "Europe/Zurich")
    try:
        zone = ZoneInfo(name)
    except Exception:  # noqa: BLE001
        zone = ZoneInfo("Europe/Zurich")
    return datetime.now(timezone.utc).astimezone(zone)


async def maybe_daily_summary(area_id: int, settings: Optional[dict[str, Any]] = None) -> bool:
    """Send the area's daily summary once the configured local time has passed."""
    s = settings if settings is not None else config.load_settings(area_id=area_id)
    if not s.get("alert_daily_summary", True):
        return False
    local = _local_now(area_id, s)
    try:
        hh, mm = (int(x) for x in str(s.get("daily_summary_time") or "22:05").split(":")[:2])
    except ValueError:
        hh, mm = 22, 5
    today = local.date().isoformat()
    if _summary_sent.get(area_id) == today:
        return False
    if (local.hour, local.minute) < (hh, mm):
        return False
    _summary_sent[area_id] = today
    if area_id not in _seeded and not state.pnl(area_id).get("accounts"):
        return False  # nothing observed yet (e.g. started after the summary time)
    summary = state.pnl(area_id)
    closes = _closed_today.pop(area_id, [])
    with context.use_area(area_id):
        await bus.emit_async("daily.summary", pnl=summary, closes=closes, day=today)
    return True


async def tick(area_id: int, settings: Optional[dict[str, Any]] = None) -> None:
    """Agent transitions + daily summary for one area (cheap, no broker calls).
    ``settings`` is the P&L loop's snapshot for this tick."""
    try:
        await observe_agents(area_id, settings)
    except Exception as exc:  # noqa: BLE001
        state.log_event("warn", f"agent watch failed: {exc}")
    try:
        await maybe_daily_summary(area_id, settings)
    except Exception as exc:  # noqa: BLE001
        state.log_event("warn", f"daily summary failed: {exc}")

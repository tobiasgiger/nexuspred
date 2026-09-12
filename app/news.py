"""Economic calendar and the **news lock**.

High-impact releases (FOMC, CPI, NFP …) move futures violently; most prop firms
forbid holding or opening positions around them. This module fetches the weekly
calendar, keeps it in the database (shared by every workspace) and, per
workspace, decides whether a *lock window* is active right now:

* ``news_lock`` in the area settings: ``enabled``, the ``currencies`` and
  ``impacts`` that count, minutes ``before`` / ``after`` the release, ``action``
  (``block`` = refuse new entries, ``flatten`` = block and close every position
  at the window start) and ``manual`` events the feed does not carry.
* :func:`active_lock` is the chokepoint the signal engine asks before any entry
  (``buy`` / ``sell`` / TS-Hunter ``signal``). Closes, stop moves and the copy
  mirror are never blocked — leaving a position unmanaged would be worse.
* :func:`news_loop` refreshes the feed every few hours, alerts when a window
  opens and, with ``action = flatten``, closes the workspace's positions once.

Feed: the ForexFactory weekly JSON (no key needed). When it is unreachable the
last good copy is used; with nothing cached, only manual events lock.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

from . import config, context, db, events, http, state

log = logging.getLogger(__name__)

FEED_URLS = ("https://nfs.faireconomy.media/ff_calendar_thisweek.json",)   # the only public weekly file
# The weekly file stops at Sunday; the following weeks come from TradingView's
# public calendar endpoint (the one its widget uses) until the weekly file
# delivers them — then its rows replace the preview (titles differ slightly).
TV_URL = "https://economic-calendar.tradingview.com/events"
TV_COUNTRIES = "US,EU,GB,JP,CA,AU,NZ,CH,CN"
TV_CURRENCY = {"US": "USD", "EU": "EUR", "GB": "GBP", "JP": "JPY", "CA": "CAD", "AU": "AUD", "NZ": "NZD", "CH": "CHF", "CN": "CNY"}
TV_IMPACT = {1: "High", 0: "Medium", -1: "Low"}
PREVIEW_DAYS = 15
REFRESH_S = 6 * 3600
KEEP_DAYS = 90               # the feed carries the current week only: past weeks are kept locally
LOOP_TICK_S = 30.0
IMPACTS = ("High", "Medium", "Low", "Holiday")
DEFAULTS: dict[str, Any] = {"enabled": False, "currencies": ["USD"], "impacts": ["High"], "before": 5, "after": 5,
                            "action": "block", "manual": [], "alert": True}

_events: list[dict[str, Any]] = []            # merged feed + parse cache (UTC datetimes as ISO strings)
_fetched_at: float = 0.0
_refresh_lock = asyncio.Lock()
_feed_error: str = ""
_alerted: set[tuple[int, str]] = set()        # (area, event key) already alerted / flattened
_flattened: set[tuple[int, str]] = set()
_flatten_tries: dict[tuple[int, str], int] = {}
FLATTEN_TRIES = 3


# ---------------------------------------------------------------- settings
def normalize(raw: Any) -> dict[str, Any]:
    """A clean ``news_lock`` block; raises ValueError on nonsense."""
    r = dict(raw) if isinstance(raw, dict) else {}
    out = dict(DEFAULTS)
    out["enabled"] = bool(r.get("enabled", False))
    cur = r.get("currencies", DEFAULTS["currencies"])
    if isinstance(cur, str):
        cur = [c for c in cur.replace(";", ",").split(",")]
    out["currencies"] = sorted({str(c).strip().upper() for c in (cur or []) if str(c).strip()}) or ["USD"]
    imp = r.get("impacts", DEFAULTS["impacts"])
    if isinstance(imp, str):
        imp = imp.split(",")
    out["impacts"] = [i for i in IMPACTS if i.lower() in {str(x).strip().lower() for x in (imp or [])}] or ["High"]
    for k in ("before", "after"):
        v = int(float(r.get(k, DEFAULTS[k])))
        if not 0 <= v <= 240:
            raise ValueError(f"{k} must be 0–240 minutes")
        out[k] = v
    action = str(r.get("action") or "block").lower()
    if action not in ("block", "flatten"):
        raise ValueError("action must be block or flatten")
    out["action"] = action
    out["alert"] = bool(r.get("alert", True))
    manual = []
    for m in r.get("manual") or []:
        if not isinstance(m, dict):
            continue
        title = str(m.get("title") or "").strip()[:80]
        at = _parse_ts(m.get("at"))
        if not title or at is None:
            raise ValueError(f"manual event needs a title and a time (ISO 8601): {m}")
        manual.append({"title": title, "at": at.isoformat()})
    out["manual"] = sorted(manual, key=lambda m: m["at"])[:200]
    return out


LOCK_MEMO_S = 2.0
_lock_memo: dict[int, tuple[float, tuple[Any, Any], Optional[dict[str, Any]]]] = {}


def settings_for(area_id: int, settings: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """The area's normalized news-lock settings; ``settings`` is an already
    loaded area settings dict (the signal path passes its snapshot)."""
    try:
        raw = settings.get("news_lock") if settings is not None else config.setting("news_lock", area_id=area_id)
        return normalize(raw)
    except ValueError:
        return dict(DEFAULTS)


# ---------------------------------------------------------------- feed
def _parse_ts(v: Any) -> Optional[datetime]:
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    if not isinstance(v, str) or not v.strip():
        return None
    try:
        d = datetime.fromisoformat(v.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def _normalize_feed(raw: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for e in raw if isinstance(raw, list) else []:
        if not isinstance(e, dict):
            continue
        at = _parse_ts(e.get("date"))
        title = str(e.get("title") or "").strip()
        if at is None or not title:
            continue
        impact = str(e.get("impact") or "").strip().title()
        out.append({"title": title[:120], "currency": str(e.get("country") or "").strip().upper()[:8],
                    "impact": impact if impact in IMPACTS else "Low", "at": at.isoformat(),
                    "forecast": str(e.get("forecast") or "")[:20], "previous": str(e.get("previous") or "")[:20]})
    out.sort(key=lambda x: x["at"])
    # the two weekly files overlap at the edges
    seen: set[tuple[str, str, str]] = set()
    dedup = []
    for e in out:
        k = (e["title"], e["currency"], e["at"])
        if k not in seen:
            seen.add(k)
            dedup.append(e)
    return dedup


async def refresh(force: bool = False) -> dict[str, Any]:
    """Fetch the weekly calendars; on failure keep the last good copy (memory,
    then the database). Returns ``{"events": n, "error": str, "fetched_at": iso}``."""
    global _events, _fetched_at, _feed_error
    if not force and _fetched_at and time.monotonic() - _fetched_at < REFRESH_S:
        return {"events": len(_events), "error": _feed_error, "cached": True}      # also after a failure (retry in ten minutes)
    async with _refresh_lock:
        if not force and _fetched_at and time.monotonic() - _fetched_at < REFRESH_S:
            return {"events": len(_events), "error": _feed_error, "cached": True}  # another caller just did it
        return await _refresh_now()


def _normalize_tv(raw: Any) -> list[dict[str, Any]]:
    """TradingView calendar rows in the feed's shape."""
    out: list[dict[str, Any]] = []
    rows = raw.get("result") if isinstance(raw, dict) else raw
    for e in rows if isinstance(rows, list) else []:
        if not isinstance(e, dict):
            continue
        at = _parse_ts(e.get("date"))
        title = str(e.get("title") or e.get("indicator") or "").strip()
        if at is None or not title:
            continue
        cur = str(e.get("currency") or TV_CURRENCY.get(str(e.get("country") or ""), "") or "").strip().upper()
        try:
            imp = TV_IMPACT.get(int(e.get("importance")), "Low")
        except (TypeError, ValueError):
            imp = "Low"
        fmt = lambda v: "" if v in (None, "") else (f"{v:g}" if isinstance(v, (int, float)) else str(v))[:20]  # noqa: E731
        out.append({"title": title[:120], "currency": cur[:8], "impact": imp, "at": at.isoformat(),
                    "forecast": fmt(e.get("forecast")), "previous": fmt(e.get("previous")), "source": "tv"})
    out.sort(key=lambda x: x["at"])
    return out


async def _fetch_preview(client: Any, after: Optional[str], errors: list[str]) -> list[dict[str, Any]]:
    """Events after the weekly file's last entry (or from now) for the next
    PREVIEW_DAYS days, from TradingView."""
    now = datetime.now(timezone.utc)
    lo = max(_parse_ts(after) or now, now - timedelta(days=1)) if after else now - timedelta(days=1)
    hi = now + timedelta(days=PREVIEW_DAYS)
    try:
        r = await client.get(TV_URL, params={"from": lo.strftime("%Y-%m-%dT%H:%M:%S.000Z"), "to": hi.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                                             "countries": TV_COUNTRIES},
                             headers={"User-Agent": "Fluxbridge/1.0", "Origin": "https://www.tradingview.com", "Referer": "https://www.tradingview.com/"},
                             timeout=15.0)
        r.raise_for_status()
        rows = _normalize_tv(r.json())
    except Exception as exc:  # noqa: BLE001
        errors.append(f"preview: {type(exc).__name__}: {exc}"[:160])
        return []
    return [e for e in rows if not after or e["at"] > after]


async def _refresh_now() -> dict[str, Any]:
    global _events, _fetched_at, _feed_error
    raw: list[Any] = []
    errors = []
    client = http.client("outbound")
    for url in FEED_URLS:
        try:
            r = await client.get(url, headers={"User-Agent": "Fluxbridge/1.0"}, timeout=15.0)
            r.raise_for_status()
            data = r.json()
            raw.extend(data if isinstance(data, list) else [])
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{url.rsplit('/', 1)[-1]}: {type(exc).__name__}: {exc}"[:160])
    events = _normalize_feed(raw)
    weekly_to = events[-1]["at"] if events else None
    events = events + await _fetch_preview(client, weekly_to, errors)
    if events:
        if not _events:
            _load_cached()
        events = _merge(_events, events)
        _events, _fetched_at, _feed_error = events, time.monotonic(), "; ".join(errors)
        try:
            await asyncio.to_thread(db.meta_set, "news_events", json.dumps({"fetched": datetime.now(timezone.utc).isoformat(), "events": events}))
        except Exception as exc:  # noqa: BLE001
            log.warning("news: cache write failed: %s", exc)
    else:
        _feed_error = "; ".join(errors) or "feed returned no events"
        if not _events:
            _load_cached()
        _fetched_at = time.monotonic() - REFRESH_S + 600     # retry in ten minutes, not every tick
    return {"events": len(_events), "error": _feed_error, "cached": False}


def _merge(old: list[dict[str, Any]], new: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The feed only ever carries the current week: new rows replace same-keyed
    old ones, older weeks stay (up to KEEP_DAYS) so the calendar keeps a history."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=KEEP_DAYS)).isoformat()
    by_key = {_key(e): e for e in old if e["at"] >= cutoff}
    # a span the sources re-deliver replaces its whole span (events get rescheduled /
    # removed); the weekly rows win over a preview of the same days
    if new:
        lo, hi = new[0]["at"][:10], new[-1]["at"][:10]
        by_key = {k: e for k, e in by_key.items() if not (lo <= e["at"][:10] <= hi)}
    for e in new:
        by_key[_key(e)] = e
    return sorted(by_key.values(), key=lambda x: x["at"])


def _load_cached() -> None:
    global _events
    try:
        raw = db.meta_get("news_events")
        if raw:
            data = json.loads(raw)
            _events = _normalize_feed([{"title": e["title"], "country": e["currency"], "impact": e["impact"], "date": e["at"],
                                        "forecast": e.get("forecast"), "previous": e.get("previous")} for e in data.get("events") or []])
    except Exception as exc:  # noqa: BLE001
        log.warning("news: cache read failed: %s", exc)


def reset() -> None:
    global _events, _fetched_at, _feed_error
    _events, _fetched_at, _feed_error = [], 0.0, ""
    _alerted.clear()
    _flattened.clear()
    _flatten_tries.clear()
    _lock_memo.clear()


# ---------------------------------------------------------------- windows
def _key(e: dict[str, Any]) -> str:
    return f"{e['at']}|{e['title']}"


def windows(area_id: int, *, hours: float = 72.0, now: Optional[datetime] = None,
            settings: Optional[dict[str, Any]] = None) -> list[dict[str, Any]]:
    """The events that count for this workspace within ``hours`` (past 6 h to
    future ``hours``), each with its lock window and whether it locks *now*."""
    s = settings or settings_for(area_id)
    now = now or datetime.now(timezone.utc)
    if not _events:
        _load_cached()
    lo, hi = now - timedelta(hours=6), now + timedelta(hours=hours)
    before, after = timedelta(minutes=s["before"]), timedelta(minutes=s["after"])
    out = []
    candidates = [dict(e, source=e.get("source") or "feed") for e in _events
                  if e["currency"] in s["currencies"] and e["impact"] in s["impacts"]]
    candidates += [{"title": m["title"], "currency": "", "impact": "Manual", "at": m["at"], "source": "manual"} for m in s["manual"]]
    for e in candidates:
        at = _parse_ts(e["at"])
        if at is None or not (lo <= at <= hi):
            continue
        start, end = at - before, at + after
        out.append({**e, "at": at.isoformat(), "lock_from": start.isoformat(), "lock_until": end.isoformat(),
                    "active": s["enabled"] and start <= now <= end, "key": _key(e)})
    out.sort(key=lambda x: x["at"])
    return out


def calendar(area_id: int, *, start: datetime, end: datetime, currencies: Optional[set[str]] = None,
             impacts: Optional[set[str]] = None, query: str = "", relevant_only: bool = False,
             now: Optional[datetime] = None) -> list[dict[str, Any]]:
    """Every calendar entry between ``start`` and ``end`` (feed + this workspace's
    manual events), each flagged ``relevant`` when the lock settings would count
    it, with its lock window when so. Filters narrow the list; ``relevant_only``
    keeps only entries the lock would act on."""
    s = settings_for(area_id)
    now = now or datetime.now(timezone.utc)
    if not _events:
        _load_cached()
    before, after = timedelta(minutes=s["before"]), timedelta(minutes=s["after"])
    q = query.strip().lower()
    out = []
    rows = [dict(e, source=e.get("source") or "feed") for e in _events]
    rows += [{"title": m["title"], "currency": "", "impact": "Manual", "at": m["at"], "forecast": "", "previous": "", "source": "manual"} for m in s["manual"]]
    for e in rows:
        at = _parse_ts(e["at"])
        if at is None or not (start <= at <= end):
            continue
        relevant = e["source"] == "manual" or (e["currency"] in s["currencies"] and e["impact"] in s["impacts"])
        if relevant_only and not relevant:
            continue
        if currencies and e["currency"] not in currencies and e["source"] != "manual":
            continue
        if impacts and e["impact"] not in impacts and e["source"] != "manual":
            continue
        if q and q not in e["title"].lower() and q not in e["currency"].lower():
            continue
        row = {**e, "at": at.isoformat(), "relevant": relevant, "key": _key(e)}
        if relevant:
            lock_from, lock_until = at - before, at + after
            row.update({"lock_from": lock_from.isoformat(), "lock_until": lock_until.isoformat(), "active": s["enabled"] and lock_from <= now <= lock_until})
        else:
            row.update({"lock_from": None, "lock_until": None, "active": False})
        out.append(row)
    out.sort(key=lambda x: x["at"])
    return out


def feed_currencies() -> list[str]:
    if not _events:
        _load_cached()
    return sorted({e["currency"] for e in _events if e["currency"]})


def active_lock(area_id: Optional[int] = None, *, now: Optional[datetime] = None,
                settings: Optional[dict[str, Any]] = None) -> Optional[dict[str, Any]]:
    """The event locking new entries right now for this workspace, else None.
    ``settings`` is the caller's already loaded area settings dict."""
    aid = area_id if area_id is not None else context.get_area()
    if now is None:
        # Sits in the order path: the answer only changes when the settings or
        # the feed do, or a window edge passes — a 2 s memo per area covers a
        # burst of signals with one scan.
        hit = _lock_memo.get(aid)
        if hit and time.monotonic() - hit[0] < LOCK_MEMO_S and hit[1] == (config._generation, _fetched_at):
            return dict(hit[2]) if hit[2] else None
    s = settings_for(aid, settings)
    lock = None
    if s["enabled"]:
        for w in windows(aid, hours=6, now=now, settings=s):
            if w["active"]:
                lock = w
                break
    if now is None:
        _lock_memo[aid] = (time.monotonic(), (config._generation, _fetched_at), dict(lock) if lock else None)
    return lock


def flattened_lock(area_id: int) -> Optional[dict[str, Any]]:
    """The active lock when the workspace flattens for news (positions must stay
    closed for its duration), else None."""
    lock = active_lock(area_id)
    if not lock:
        return None
    return lock if settings_for(area_id).get("action") == "flatten" else None


def status(area_id: Optional[int] = None) -> dict[str, Any]:
    aid = area_id if area_id is not None else context.get_area()
    s = settings_for(aid)
    lock = nxt = None
    if s["enabled"]:
        now = datetime.now(timezone.utc)
        for w in windows(aid, hours=48, settings=s):          # one scan: the active window and the next one
            if w["active"] and lock is None:
                lock = w
            elif nxt is None and _parse_ts(w["lock_from"]) > now:
                nxt = w
                break
    return {"enabled": s["enabled"], "action": s["action"], "active": lock, "next": nxt,
            "feed_events": len(_events), "feed_error": _feed_error,
            "feed_from": _events[0]["at"] if _events else None, "feed_to": _events[-1]["at"] if _events else None,
            "weekly_to": next((e["at"] for e in reversed(_events) if e.get("source") != "tv"), None),
            "preview_events": sum(1 for e in _events if e.get("source") == "tv"),
            "feed_age_s": int(time.monotonic() - _fetched_at) if _fetched_at else None}


# ---------------------------------------------------------------- loop
async def _tick_area(area_id: int, settings: Optional[dict[str, Any]] = None) -> None:
    s = settings if settings is not None else settings_for(area_id)
    if not s["enabled"]:
        return
    now = datetime.now(timezone.utc)
    # the lock may start up to ``before`` minutes ahead: scan as far as the widest setting
    for w in windows(area_id, hours=max(1.0, float(s.get("before") or 0) / 60.0 + 0.1), now=now, settings=s):
        if not w["active"]:
            continue
        k = (area_id, w["key"])
        if k not in _alerted:
            _alerted.add(k)
            with context.use_area(area_id):
                state.log_event("warn", f"News lock: {w['title']} ({w['currency'] or 'manual'}) — no new entries until "
                                        f"{_local(w['lock_until'], area_id)}" + (" — flattening open positions" if s["action"] == "flatten" else ""))
                if s["alert"]:
                    try:
                        await events.emit_async("news.lock", title=w["title"], currency=w["currency"], until=_local(w["lock_until"], area_id), flatten=s["action"] == "flatten")
                    except Exception as exc:  # noqa: BLE001
                        log.warning("news alert failed: %s", exc)
        if s["action"] == "flatten" and k not in _flattened and _flatten_tries.get(k, 0) < FLATTEN_TRIES:
            from . import signals
            with context.use_area(area_id):
                try:
                    r = await signals.flatten_all()
                    _flattened.add(k)                      # only a completed flatten counts; a failed one is retried next tick
                    state.log_event("warn", f"News lock flatten: {r.get('flattened', 0)} position(s) closed, {r.get('cancelled', 0)} order(s) cancelled"
                                            + (f" — errors: {'; '.join(r.get('errors') or [])[:200]}" if r.get("errors") else ""))
                except Exception as exc:  # noqa: BLE001
                    _flatten_tries[k] = _flatten_tries.get(k, 0) + 1
                    state.log_event("error", f"News lock flatten failed ({_flatten_tries[k]}/{FLATTEN_TRIES}): {exc}")
    # forget keys older than a day so the sets stay small
    cutoff = (now - timedelta(days=1)).isoformat()
    for st in (_alerted, _flattened):
        for k in [k for k in st if k[0] == area_id and k[1].split("|", 1)[0] < cutoff]:
            st.discard(k)


def _local(iso: str, area_id: int) -> str:
    d = _parse_ts(iso)
    if d is None:
        return iso
    tz = config.load_settings(area_id=area_id).get("journal_timezone") or "UTC"
    try:
        return d.astimezone(ZoneInfo(tz)).strftime("%H:%M %Z")
    except Exception:  # noqa: BLE001
        return d.strftime("%H:%M UTC")


async def news_loop() -> None:
    """Refresh the feed every few hours; every 30 s check each workspace's windows."""
    while True:
        try:
            per_area = {a: settings_for(a) for a in db.all_area_ids()}
            if any(s["enabled"] for s in per_area.values()):
                await refresh()
                for aid, s in per_area.items():
                    try:
                        await _tick_area(aid, s)
                    except Exception as exc:  # noqa: BLE001
                        log.warning("news tick failed for area %s: %s", aid, exc)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("news loop: %s", exc)
        await asyncio.sleep(LOOP_TICK_S)

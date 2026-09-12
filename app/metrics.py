"""Prometheus text exposition for ``GET /metrics``.

Counters and histograms are fed by the event bus (nothing in the order path
knows about metrics); gauges are read at scrape time. The endpoint is off
unless ``NEXUSPRED_METRICS_TOKEN`` is set and is authenticated with that
token as a bearer (never a session cookie — a scraper is not a user).
"""
from __future__ import annotations

import os
import secrets
import threading
import time
from collections import defaultdict
from typing import Any, Iterable

from . import config, events

STARTED_AT = time.time()
LATENCY_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)

_lock = threading.Lock()
_counters: dict[str, dict[tuple[tuple[str, str], ...], float]] = defaultdict(lambda: defaultdict(float))
_hist_counts: dict[tuple[tuple[str, str], ...], list[int]] = {}
_hist_sum: dict[tuple[tuple[str, str], ...], float] = defaultdict(float)
_hist_n: dict[tuple[tuple[str, str], ...], int] = defaultdict(int)


def token() -> str:
    return (os.environ.get("NEXUSPRED_METRICS_TOKEN") or "").strip()


def enabled() -> bool:
    return bool(token())


def authorized(header: str | None) -> bool:
    t = token()
    if not t or not header:
        return False
    scheme, _, value = header.partition(" ")
    return scheme.lower() == "bearer" and secrets.compare_digest(value.strip(), t)


def _labels(**kw: Any) -> tuple[tuple[str, str], ...]:
    return tuple(sorted((k, str(v)) for k, v in kw.items()))


def inc(name: str, value: float = 1.0, **labels: Any) -> None:
    with _lock:
        _counters[name][_labels(**labels)] += value


def observe_latency(seconds: float, **labels: Any) -> None:
    key = _labels(**labels)
    with _lock:
        counts = _hist_counts.get(key)
        if counts is None:
            counts = _hist_counts[key] = [0] * (len(LATENCY_BUCKETS) + 1)
        for i, edge in enumerate(LATENCY_BUCKETS):
            if seconds <= edge:
                counts[i] += 1
        counts[-1] += 1
        _hist_sum[key] += seconds
        _hist_n[key] += 1


def counter(name: str, **labels: Any) -> float:
    with _lock:
        return _counters.get(name, {}).get(_labels(**labels), 0.0)


def reset() -> None:
    with _lock:
        _counters.clear()
        _hist_counts.clear()
        _hist_sum.clear()
        _hist_n.clear()


# ----------------------------------------------------------- bus listeners
def _on_event(kind: str, data: dict[str, Any]) -> None:
    inc("fluxbridge_events_total", kind=kind)
    if kind == "signal.done":
        inc("fluxbridge_signals_total", status=str(data.get("status") or "ok"))
        secs = data.get("seconds")
        if isinstance(secs, (int, float)) and secs >= 0:
            observe_latency(float(secs), status=str(data.get("status") or "ok"))
    elif kind == "trade.executed":
        inc("fluxbridge_trades_total", action=str(data.get("action") or ""))
    elif kind == "execution.problem":
        inc("fluxbridge_execution_problems_total")
    elif kind == "risk.triggered":
        inc("fluxbridge_risk_triggers_total", kind=str(data.get("kind") or ""))
    elif kind in ("connection.lost", "connection.restored"):
        inc("fluxbridge_connection_changes_total", state=kind.split(".")[1], broker=str(data.get("broker") or ""))
    elif kind == "automation.fired":
        inc("fluxbridge_automations_fired_total", action=str(data.get("action") or ""))
    elif kind == "copy.alert":
        inc("fluxbridge_copy_alerts_total")
    elif kind == "signal.failed":
        inc("fluxbridge_signal_failures_total")


events.subscribe("*", _on_event)


# --------------------------------------------------------------- rendering
def _esc(v: str) -> str:
    return v.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _fmt_labels(labels: Iterable[tuple[str, str]]) -> str:
    items = [f'{k}="{_esc(v)}"' for k, v in labels]
    return "{" + ",".join(items) + "}" if items else ""


def _num(v: float) -> str:
    return str(int(v)) if float(v).is_integer() else repr(float(v))


def _gauges() -> list[tuple[str, str, str, list[tuple[tuple[tuple[str, str], ...], float]]]]:
    """(name, type, help, samples) read at scrape time."""
    from . import db, signals, state
    out = []
    out.append(("fluxbridge_up", "gauge", "1 while the process answers.", [((), 1.0)]))
    out.append(("fluxbridge_uptime_seconds", "gauge", "Seconds since the process started.", [((), time.time() - STARTED_AT)]))
    out.append(("fluxbridge_info", "gauge", "Version label.", [(_labels(version=config.get_version()), 1.0)]))
    sessions: list[tuple[tuple[tuple[str, str], ...], float]] = []
    trades: list[tuple[tuple[tuple[str, str], ...], float]] = []
    subs: list[tuple[tuple[tuple[str, str], ...], float]] = []
    for aid in list(state._areas):
        st = state._areas.get(aid)
        if st is None:
            continue
        with state._lock:
            sess = [dict(v) for v in st.sessions.values()]
        for s in sess:
            sessions.append((_labels(area=aid, login=str(s.get("name") or ""), broker=str(s.get("broker") or "tradovate")), 1.0 if s.get("connected") else 0.0))
        trades.append((_labels(area=aid), float(sum(len(t.get("accounts") or {}) for t in signals.active_trades_for(aid).values()))))
        subs.append((_labels(area=aid), float(state.subscriber_count(aid))))
    out.append(("fluxbridge_broker_connected", "gauge", "1 when the login is connected.", sessions))
    out.append(("fluxbridge_active_trades", "gauge", "Positions the bridge is managing (per account).", trades))
    out.append(("fluxbridge_stream_subscribers", "gauge", "Open live-feed (SSE) connections.", subs))
    out.append(("fluxbridge_signals_queued", "gauge", "Background signal tasks in flight.", [((), float(len(signals._bg_tasks)))]))
    try:
        users = float(db.user_count())
    except Exception:  # noqa: BLE001
        users = 0.0
    out.append(("fluxbridge_users", "gauge", "Registered users.", [((), users)]))
    return out


def render() -> str:
    lines: list[str] = []
    for name, typ, help_, samples in _gauges():
        lines.append(f"# HELP {name} {help_}")
        lines.append(f"# TYPE {name} {typ}")
        for labels, value in samples:
            lines.append(f"{name}{_fmt_labels(labels)} {_num(value)}")
    with _lock:
        for name in sorted(_counters):
            lines.append(f"# HELP {name} Counter fed by the event bus.")
            lines.append(f"# TYPE {name} counter")
            for labels, value in sorted(_counters[name].items()):
                lines.append(f"{name}{_fmt_labels(labels)} {_num(value)}")
        if _hist_counts:
            name = "fluxbridge_signal_seconds"
            lines.append(f"# HELP {name} Wall time from webhook acceptance to the broker answer.")
            lines.append(f"# TYPE {name} histogram")
            for labels, counts in sorted(_hist_counts.items()):
                for edge, c in zip(LATENCY_BUCKETS, counts):
                    lines.append(f"{name}_bucket{_fmt_labels(labels + (('le', _num(edge)),))} {c}")
                lines.append(f"{name}_bucket{_fmt_labels(labels + (('le', '+Inf'),))} {counts[-1]}")
                lines.append(f"{name}_sum{_fmt_labels(labels)} {_num(_hist_sum[labels])}")
                lines.append(f"{name}_count{_fmt_labels(labels)} {_hist_n[labels]}")
    return "\n".join(lines) + "\n"

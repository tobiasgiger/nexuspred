"""External watchdog: an outbound heartbeat to a URL you monitor elsewhere
(healthchecks.io, Uptime Kuma push monitors, cronitor …). The monitor alerts
*you* when the pings stop — the one failure the bridge cannot report itself
(process gone, host asleep, network down).

Per workspace: ``heartbeat_url`` (empty = off) pinged every
``heartbeat_interval`` seconds with a GET; the last outcome is shown under
Settings → Alerts."""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any, Optional

from . import config, db, http

log = logging.getLogger("nexuspred.watchdog")

LOOP_TICK_S = 15.0
MIN_INTERVAL_S = 30
MAX_INTERVAL_S = 3600
_last: dict[int, dict[str, Any]] = {}       # area → {"at", "ok", "error", "url"}
_next_due: dict[int, float] = {}


def reset() -> None:
    _last.clear()
    _next_due.clear()


def status(area_id: int) -> dict[str, Any]:
    return dict(_last.get(area_id) or {"at": None, "ok": None, "error": "", "url": ""})


def normalize_interval(v: Any) -> int:
    try:
        n = int(float(v))
    except (TypeError, ValueError):
        n = 60
    return max(MIN_INTERVAL_S, min(MAX_INTERVAL_S, n))


async def ping(area_id: int, url: str) -> bool:
    """One heartbeat; records the outcome. Never raises."""
    ok, error = False, ""
    try:
        r = await http.client("outbound").get(url, headers={"User-Agent": "Fluxbridge/heartbeat"}, timeout=10.0)
        ok = r.status_code < 400
        if not ok:
            error = f"HTTP {r.status_code}"
    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {exc}"[:160]
    prev = _last.get(area_id) or {}
    if not ok and prev.get("ok") is not False:
        log.warning("heartbeat for area %s failed: %s", area_id, error)
    _last[area_id] = {"at": datetime.now(timezone.utc).isoformat(), "ok": ok, "error": error, "url": url}
    return ok


async def tick_area(area_id: int, settings: Optional[dict[str, Any]] = None) -> Optional[float]:
    s = settings if settings is not None else config.load_settings(area_id=area_id)
    url = str(s.get("heartbeat_url") or "").strip()
    if not url:
        _next_due.pop(area_id, None)
        _last.pop(area_id, None)
        return None
    interval = normalize_interval(s.get("heartbeat_interval", 60))
    now = time.monotonic()
    if now < _next_due.get(area_id, 0.0):
        return _next_due[area_id] - now
    _next_due[area_id] = now + interval
    await ping(area_id, url)
    return float(interval)


async def heartbeat_loop() -> None:
    while True:
        try:
            ids = db.all_area_ids()
            results = await asyncio.gather(*(tick_area(aid) for aid in ids), return_exceptions=True)   # one slow target never delays the others
            for aid, r in zip(ids, results):
                if isinstance(r, asyncio.CancelledError):
                    raise r
                if isinstance(r, Exception):
                    log.warning("heartbeat tick failed for area %s: %s", aid, r)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - the loop must survive anything
            log.warning("heartbeat loop: %s", exc)
        await asyncio.sleep(LOOP_TICK_S)

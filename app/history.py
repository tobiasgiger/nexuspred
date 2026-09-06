"""Durable history of signals and orders.

:mod:`app.state` keeps a 200-entry ring buffer per area for the live UI; every
entry is *also* handed to this module, which writes it to SQLite
(``signal_log`` / ``order_log``) so a deploy or restart no longer wipes the
record. On startup the ring buffers are re-filled from the tables.

Writes go through a single background thread (batched, one connection) so a
burst of alerts never blocks the event loop on disk I/O. When the writer is not
running — tests, one-off scripts — writes happen inline instead, so the data is
always durable either way.

Retention: rows older than ``NEXUSPRED_HISTORY_DAYS`` (default 90) are pruned
at startup and once a day.
"""
from __future__ import annotations

import os
import queue
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from . import db

RETENTION_DAYS = int(os.environ.get("NEXUSPRED_HISTORY_DAYS") or 90)
HYDRATE_ROWS = 200  # matches state._MAX

_q: "queue.Queue[Optional[tuple[str, int, dict[str, Any]]]]" = queue.Queue()
_thread: Optional[threading.Thread] = None
_running = False
_idle = threading.Event()
_idle.set()


# ------------------------------------------------------------------ writer
def _write(kind: str, area_id: int, entry: dict[str, Any]) -> None:
    if kind == "signal":
        db.insert_signal(area_id, entry)
    else:
        db.insert_order(area_id, entry)


def _worker() -> None:
    while True:
        item = _q.get()
        if item is None:
            _idle.set()
            return
        _idle.clear()
        try:
            _write(*item)
        except Exception:  # noqa: BLE001 - history must never kill the writer
            pass
        finally:
            if _q.empty():
                _idle.set()


def start() -> None:
    """Start the background writer (idempotent)."""
    global _thread, _running
    if _running:
        return
    _running = True
    _thread = threading.Thread(target=_worker, name="history-writer", daemon=True)
    _thread.start()


def stop(timeout: float = 5.0) -> None:
    """Drain the queue and stop the writer."""
    global _thread, _running
    if not _running:
        return
    _running = False
    _q.put(None)
    if _thread is not None:
        _thread.join(timeout)
        _thread = None


def flush(timeout: float = 5.0) -> None:
    """Block until every queued write has landed (tests / shutdown)."""
    if _running:
        _idle.wait(timeout)


def _submit(kind: str, area_id: int, entry: dict[str, Any]) -> None:
    if _running:
        _idle.clear()
        _q.put((kind, area_id, dict(entry)))
    else:
        try:
            _write(kind, area_id, entry)
        except Exception:  # noqa: BLE001
            pass


def record_signal(area_id: int, entry: dict[str, Any]) -> None:
    _submit("signal", area_id, entry)


def record_order(area_id: int, entry: dict[str, Any]) -> None:
    _submit("order", area_id, entry)


# --------------------------------------------------------- startup helpers
def hydrate(area_ids: list[int]) -> int:
    """Refill every area's live ring buffers from the tables. Returns rows loaded."""
    from . import state
    loaded = 0
    for aid in area_ids:
        signals = db.list_signals(aid, limit=HYDRATE_ROWS)["items"]
        orders = db.list_orders(aid, limit=HYDRATE_ROWS)["items"]
        state.hydrate(aid, signals=signals, orders=orders)
        loaded += len(signals) + len(orders)
    return loaded


def prune(days: Optional[int] = None) -> int:
    """Delete rows older than the retention window. Returns rows removed."""
    days = RETENTION_DAYS if days is None else days
    if days <= 0:
        return 0
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    return db.prune_history(cutoff)

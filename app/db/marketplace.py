"""Marketplace subscriptions (webhook + copy-group sharing)."""
from __future__ import annotations
import json
import sqlite3
from typing import Any, Optional
from .core import _active_subs, _connect, _now, _sub_counts, init


def _subs_changed() -> None:
    _active_subs.clear()
    _sub_counts.clear()


def _row_to_sub(r: sqlite3.Row) -> dict[str, Any]:
    try:
        accounts = json.loads(r["accounts"] or "[]")
    except json.JSONDecodeError:
        accounts = []
    try:
        controls = json.loads(r["controls"] or "{}") if "controls" in r.keys() else {}
    except json.JSONDecodeError:
        controls = {}
    return {"id": r["id"], "area_id": r["area_id"], "publisher_area_id": r["publisher_area_id"],
            "webhook_id": r["webhook_id"], "enabled": bool(r["enabled"]), "accounts": accounts,
            "status": (r["status"] if "status" in r.keys() else "active") or "active", "controls": controls if isinstance(controls, dict) else {},
            "created_at": r["created_at"], "updated_at": r["updated_at"]}


SUB_STATUSES = ("active", "pending", "paused")


def upsert_subscription(area_id: int, publisher_area_id: int, webhook_id: str,
                        accounts: list[dict[str, Any]], enabled: bool = True, *,
                        controls: Optional[dict[str, Any]] = None, status: Optional[str] = None) -> dict[str, Any]:
    """Create or update the subscriber area's subscription to a published webhook.
    ``status`` applies to a *new* row only (an existing row keeps what the
    publisher set); ``controls`` replaces the subscriber's controls when given."""
    init()
    now = _now()
    with _connect() as c:
        row = c.execute("SELECT * FROM subscriptions WHERE area_id=? AND publisher_area_id=? AND webhook_id=?",
                        (area_id, publisher_area_id, webhook_id)).fetchone()
        if row is None:
            c.execute(
                "INSERT INTO subscriptions(area_id,publisher_area_id,webhook_id,enabled,accounts,created_at,updated_at,status,controls) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (area_id, publisher_area_id, webhook_id, 1 if enabled else 0, json.dumps(accounts), now, now,
                 status if status in SUB_STATUSES else "active", json.dumps(controls or {})))
        else:
            c.execute("UPDATE subscriptions SET enabled=?, accounts=?, updated_at=?, controls=? WHERE id=?",
                      (1 if enabled else 0, json.dumps(accounts), now,
                       json.dumps(controls) if controls is not None else (row["controls"] if "controls" in row.keys() else "{}"), row["id"]))
        row = c.execute("SELECT * FROM subscriptions WHERE area_id=? AND publisher_area_id=? AND webhook_id=?",
                        (area_id, publisher_area_id, webhook_id)).fetchone()
    _subs_changed()
    return _row_to_sub(row)


def set_subscription_status(sub_id: int, publisher_area_id: int, status: str) -> Optional[dict[str, Any]]:
    """The publisher approves / pauses / resumes one subscriber."""
    if status not in SUB_STATUSES:
        raise ValueError(f"status must be one of {', '.join(SUB_STATUSES)}")
    init()
    with _connect() as c:
        row = c.execute("SELECT * FROM subscriptions WHERE id=?", (sub_id,)).fetchone()
        if not row or row["publisher_area_id"] != publisher_area_id:
            return None
        c.execute("UPDATE subscriptions SET status=?, updated_at=? WHERE id=?", (status, _now(), sub_id))
        row = c.execute("SELECT * FROM subscriptions WHERE id=?", (sub_id,)).fetchone()
    _subs_changed()
    return _row_to_sub(row)


def get_subscription(sub_id: int, area_id: Optional[int] = None) -> Optional[dict[str, Any]]:
    init()
    with _connect() as c:
        row = c.execute("SELECT * FROM subscriptions WHERE id=?", (sub_id,)).fetchone()
    if not row or (area_id is not None and row["area_id"] != area_id):
        return None
    return _row_to_sub(row)


def update_subscription(sub_id: int, area_id: int, *, enabled: Optional[bool] = None,
                        accounts: Optional[list[dict[str, Any]]] = None,
                        controls: Optional[dict[str, Any]] = None) -> Optional[dict[str, Any]]:
    """Update a subscriber's own subscription (enabled flag, routed accounts, controls)."""
    cur = get_subscription(sub_id, area_id)
    if not cur:
        return None
    init()
    with _connect() as c:
        c.execute("UPDATE subscriptions SET enabled=?, accounts=?, controls=?, updated_at=? WHERE id=?",
                  (1 if (cur["enabled"] if enabled is None else enabled) else 0,
                   json.dumps(cur["accounts"] if accounts is None else accounts),
                   json.dumps(cur["controls"] if controls is None else controls), _now(), sub_id))
    _subs_changed()
    return get_subscription(sub_id, area_id)


def delete_subscription(sub_id: int, *, area_id: Optional[int] = None,
                        publisher_area_id: Optional[int] = None) -> Optional[dict[str, Any]]:
    """Remove a subscription — by its subscriber (``area_id``) or by the publisher
    (``publisher_area_id``, "kick"). Returns the removed row or None."""
    init()
    with _connect() as c:
        row = c.execute("SELECT * FROM subscriptions WHERE id=?", (sub_id,)).fetchone()
        if not row:
            return None
        if area_id is not None and row["area_id"] != area_id:
            return None
        if publisher_area_id is not None and row["publisher_area_id"] != publisher_area_id:
            return None
        c.execute("DELETE FROM subscriptions WHERE id=?", (sub_id,))
    _subs_changed()
    return _row_to_sub(row)


def delete_subscriptions_for_webhook(publisher_area_id: int, webhook_id: str) -> int:
    init()
    with _connect() as c:
        cur = c.execute("DELETE FROM subscriptions WHERE publisher_area_id=? AND webhook_id=?",
                        (publisher_area_id, webhook_id))
        n = cur.rowcount
    _subs_changed()
    return n


def list_subscriptions(area_id: int) -> list[dict[str, Any]]:
    """The subscriptions an area holds (as a subscriber)."""
    init()
    with _connect() as c:
        rows = c.execute("SELECT * FROM subscriptions WHERE area_id=? ORDER BY id", (area_id,)).fetchall()
    return [_row_to_sub(r) for r in rows]


def list_subscribers(publisher_area_id: int, webhook_id: str) -> list[dict[str, Any]]:
    """Everyone subscribed to one published webhook, with the subscriber's email."""
    init()
    with _connect() as c:
        rows = c.execute(
            "SELECT s.*, u.email AS email FROM subscriptions s "
            "JOIN areas a ON a.id = s.area_id JOIN users u ON u.id = a.owner_user_id "
            "WHERE s.publisher_area_id=? AND s.webhook_id=? ORDER BY s.id",
            (publisher_area_id, webhook_id)).fetchall()
    return [{**_row_to_sub(r), "email": r["email"]} for r in rows]


def subscriber_counts(publisher_area_id: int) -> dict[str, int]:
    """webhook_id → number of subscriptions (enabled or not) for a publisher area.
    Cached (the webhook list asks on every load) until a subscription changes."""
    cached = _sub_counts.get(publisher_area_id)
    if cached is not None:
        return dict(cached)
    init()
    with _connect() as c:
        rows = c.execute("SELECT webhook_id, COUNT(*) n FROM subscriptions WHERE publisher_area_id=? GROUP BY webhook_id",
                         (publisher_area_id,)).fetchall()
    counts = {r["webhook_id"]: r["n"] for r in rows}
    _sub_counts[publisher_area_id] = counts
    return dict(counts)


def active_subscriptions(publisher_area_id: int, webhook_id: str) -> list[dict[str, Any]]:
    """Enabled subscriptions to a published webhook — the hot path of the signal
    fan-out, cached until any subscription changes."""
    key = (publisher_area_id, webhook_id)
    cached = _active_subs.get(key)
    if cached is not None:
        return [dict(s) for s in cached]
    init()
    with _connect() as c:
        rows = c.execute("SELECT * FROM subscriptions WHERE publisher_area_id=? AND webhook_id=? AND enabled=1 AND status='active' ORDER BY id",
                         key).fetchall()
    subs = [_row_to_sub(r) for r in rows]
    _active_subs[key] = subs
    return [dict(s) for s in subs]

"""Paid marketplace subscriptions: one row per (subscriber area, listing)."""
from __future__ import annotations
import sqlite3
from typing import Any, Optional
from .core import _connect, _now, init

STATUSES = ("pending", "trialing", "active", "past_due", "canceled", "unpaid")
PAID = frozenset({"trialing", "active"})


def _row(r: sqlite3.Row) -> dict[str, Any]:
    return dict(r)


def upsert_payment(area_id: int, publisher_area_id: int, key: str, **fields: Any) -> dict[str, Any]:
    """Create or update the subscriber's payment record for one listing."""
    init()
    now = _now()
    cols = ("stripe_customer", "stripe_subscription", "checkout_session", "status", "price_cents", "currency", "current_period_end", "trial_end")
    with _connect() as c:
        row = c.execute("SELECT * FROM payments WHERE area_id=? AND publisher_area_id=? AND webhook_id=?", (area_id, publisher_area_id, key)).fetchone()
        if row is None:
            vals = {k: fields.get(k) for k in cols}
            c.execute("INSERT INTO payments(area_id,publisher_area_id,webhook_id,stripe_customer,stripe_subscription,checkout_session,status,price_cents,currency,"
                      "current_period_end,trial_end,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (area_id, publisher_area_id, key, vals["stripe_customer"] or "", vals["stripe_subscription"] or "", vals["checkout_session"] or "",
                       vals["status"] or "pending", int(vals["price_cents"] or 0), vals["currency"] or "usd", vals["current_period_end"] or "", vals["trial_end"] or "", now, now))
        else:
            sets = {k: v for k, v in fields.items() if k in cols}
            if sets:
                c.execute(f"UPDATE payments SET {', '.join(f'{k}=?' for k in sets)}, updated_at=? WHERE id=?", (*sets.values(), now, row["id"]))
        row = c.execute("SELECT * FROM payments WHERE area_id=? AND publisher_area_id=? AND webhook_id=?", (area_id, publisher_area_id, key)).fetchone()
    return _row(row)


def get_payment(area_id: int, publisher_area_id: int, key: str) -> Optional[dict[str, Any]]:
    init()
    with _connect() as c:
        row = c.execute("SELECT * FROM payments WHERE area_id=? AND publisher_area_id=? AND webhook_id=?", (area_id, publisher_area_id, key)).fetchone()
    return _row(row) if row else None


def payment_by(field: str, value: str) -> Optional[dict[str, Any]]:
    if field not in ("stripe_subscription", "checkout_session", "stripe_customer"):
        raise ValueError(field)
    init()
    with _connect() as c:
        row = c.execute(f"SELECT * FROM payments WHERE {field}=? ORDER BY id DESC LIMIT 1", (value,)).fetchone()
    return _row(row) if row else None


def update_payment(payment_id: int, **fields: Any) -> Optional[dict[str, Any]]:
    init()
    cols = ("stripe_customer", "stripe_subscription", "checkout_session", "status", "price_cents", "currency", "current_period_end", "trial_end")
    sets = {k: v for k, v in fields.items() if k in cols}
    with _connect() as c:
        if sets:
            c.execute(f"UPDATE payments SET {', '.join(f'{k}=?' for k in sets)}, updated_at=? WHERE id=?", (*sets.values(), _now(), payment_id))
        row = c.execute("SELECT * FROM payments WHERE id=?", (payment_id,)).fetchone()
    return _row(row) if row else None


def list_payments(area_id: Optional[int] = None, *, publisher_area_id: Optional[int] = None, limit: int = 500) -> list[dict[str, Any]]:
    """A subscriber's payments (``area_id``), a publisher's (``publisher_area_id``) or all (admin), with the subscriber's email."""
    init()
    where, params = [], []
    if area_id is not None:
        where.append("p.area_id=?"); params.append(area_id)
    if publisher_area_id is not None:
        where.append("p.publisher_area_id=?"); params.append(publisher_area_id)
    sql = ("SELECT p.*, u.email AS email FROM payments p JOIN areas a ON a.id = p.area_id JOIN users u ON u.id = a.owner_user_id"
           + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY p.id DESC LIMIT ?")
    with _connect() as c:
        rows = c.execute(sql, (*params, max(1, min(int(limit), 5000)))).fetchall()
    return [_row(r) for r in rows]

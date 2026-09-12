"""Web-push device subscriptions."""
from __future__ import annotations
import sqlite3
from typing import Any
from .core import _connect, _now, init


def _row_to_push(r: sqlite3.Row, public: bool = False) -> dict[str, Any]:
    d = {"id": r["id"], "area_id": r["area_id"], "user_id": r["user_id"], "endpoint": r["endpoint"],
         "device": r["device"], "created_at": r["created_at"], "last_used_at": r["last_used_at"],
         "failures": r["failures"], "last_error": r["last_error"]}
    if public:
        # The endpoint is a capability URL (anyone holding it can push to the
        # device); the browser only needs enough to recognise "this device".
        d["endpoint_host"] = r["endpoint"].split("//", 1)[-1].split("/", 1)[0]
        d.pop("endpoint")
    else:
        d["p256dh"] = r["p256dh"]
        d["auth"] = r["auth"]
    return d


def upsert_push_subscription(area_id: int, user_id: int, endpoint: str, p256dh: str, auth: str, *,
                             device: str = "") -> dict[str, Any]:
    """Register (or refresh) a device push subscription. Endpoints are globally unique."""
    init()
    with _connect() as c:
        row = c.execute("SELECT * FROM push_subscriptions WHERE endpoint=?", (endpoint,)).fetchone()
        if row and row["area_id"] != area_id:
            raise ValueError("this push endpoint is registered to another workspace")
        if row:
            c.execute("UPDATE push_subscriptions SET area_id=?, user_id=?, p256dh=?, auth=?, device=?, "
                      "failures=0, last_error='' WHERE id=?",
                      (area_id, user_id, p256dh, auth, device or row["device"], row["id"]))
            sub_id = row["id"]
        else:
            cur = c.execute("INSERT INTO push_subscriptions (area_id, user_id, endpoint, p256dh, auth, device, created_at) "
                            "VALUES (?,?,?,?,?,?,?)", (area_id, user_id, endpoint, p256dh, auth, device, _now()))
            sub_id = cur.lastrowid
        r = c.execute("SELECT * FROM push_subscriptions WHERE id=?", (sub_id,)).fetchone()
    return _row_to_push(r)


def list_push_subscriptions(area_id: int, *, public: bool = False) -> list[dict[str, Any]]:
    init()
    with _connect() as c:
        rows = c.execute("SELECT * FROM push_subscriptions WHERE area_id=? ORDER BY id", (area_id,)).fetchall()
    return [_row_to_push(r, public=public) for r in rows]


def delete_push_subscription(area_id: int, sub_id: int) -> bool:
    init()
    with _connect() as c:
        cur = c.execute("DELETE FROM push_subscriptions WHERE area_id=? AND id=?", (area_id, sub_id))
    return bool(cur.rowcount)


def delete_push_subscription_by_endpoint(area_id: int, endpoint: str) -> bool:
    if not endpoint:
        return False
    init()
    with _connect() as c:
        cur = c.execute("DELETE FROM push_subscriptions WHERE area_id=? AND endpoint=?", (area_id, endpoint))
    return bool(cur.rowcount)


def touch_push_subscription(sub_id: int, *, ok: bool, error: str = "") -> None:
    init()
    with _connect() as c:
        if ok:
            c.execute("UPDATE push_subscriptions SET last_used_at=?, failures=0, last_error='' WHERE id=?", (_now(), sub_id))
        else:
            c.execute("UPDATE push_subscriptions SET failures=failures+1, last_error=? WHERE id=?", (error[:200], sub_id))

"""Copy-trading events, twins and mirrored state."""
from __future__ import annotations
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from .core import _connect, _now, init


def insert_copy_event(area_id: int, rec: dict[str, Any]) -> int:
    """Append one copy-trading event (mirror, reject, drift, feed up/lost …)."""
    init()
    with _connect() as c:
        cur = c.execute(
            "INSERT INTO copy_events(area_id, group_id, ts, kind, leader, follower, symbol, detail, latency_ms) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (area_id, str(rec.get("group_id") or ""), rec.get("ts") or _now(), str(rec.get("kind") or ""),
             str(rec.get("leader") or ""), str(rec.get("follower") or ""), str(rec.get("symbol") or ""),
             str(rec.get("detail") or "")[:400], rec.get("latency_ms")))
        return int(cur.lastrowid or 0)


def list_copy_events(area_id: int, group_id: str = "", limit: int = 100,
                     followers: Optional[list[str]] = None) -> list[dict[str, Any]]:
    init()
    limit = max(1, min(int(limit), 1000))
    with _connect() as c:
        if followers is not None:
            if not followers:
                return []
            marks = ",".join("?" * len(followers))
            rows = c.execute(f"SELECT * FROM copy_events WHERE area_id=? AND group_id=? AND follower IN ({marks}) ORDER BY id DESC LIMIT ?",
                             (area_id, group_id, *followers, limit)).fetchall()
        elif group_id:
            rows = c.execute("SELECT * FROM copy_events WHERE area_id=? AND group_id=? ORDER BY id DESC LIMIT ?",
                             (area_id, group_id, limit)).fetchall()
        else:
            rows = c.execute("SELECT * FROM copy_events WHERE area_id=? ORDER BY id DESC LIMIT ?",
                             (area_id, limit)).fetchall()
    return [dict(r) for r in rows]


_TWIN_COLS = ("contract_id", "symbol", "action", "qty", "order_type", "price", "stop_price", "version_id", "oco_with")


def save_copy_twin(area_id: int, group_id: str, spec: str, leader_order_id: int, follower_order_id: int, **fields: Any) -> None:
    init()
    now = _now()
    vals = {k: fields.get(k) for k in _TWIN_COLS if k in fields}
    with _connect() as c:
        c.execute(
            "INSERT INTO copy_twins(area_id, group_id, spec, leader_order_id, follower_order_id, contract_id, symbol, action, qty, "
            "order_type, price, stop_price, version_id, oco_with, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(area_id, group_id, spec, leader_order_id) DO UPDATE SET follower_order_id=excluded.follower_order_id, "
            "contract_id=excluded.contract_id, symbol=excluded.symbol, action=excluded.action, qty=excluded.qty, order_type=excluded.order_type, "
            "price=excluded.price, stop_price=excluded.stop_price, version_id=excluded.version_id, oco_with=excluded.oco_with, updated_at=excluded.updated_at",
            (area_id, group_id, spec, int(leader_order_id), int(follower_order_id), int(vals.get("contract_id") or 0),
             str(vals.get("symbol") or ""), str(vals.get("action") or ""), int(vals.get("qty") or 0), str(vals.get("order_type") or ""),
             vals.get("price"), vals.get("stop_price"), int(vals.get("version_id") or 0), int(vals.get("oco_with") or 0), now, now))


def delete_copy_twin(area_id: int, group_id: str, spec: str, leader_order_id: int) -> None:
    init()
    with _connect() as c:
        c.execute("DELETE FROM copy_twins WHERE area_id=? AND group_id=? AND spec=? AND leader_order_id=?",
                  (area_id, group_id, spec, int(leader_order_id)))


def delete_copy_twins(area_id: int, group_id: str) -> None:
    init()
    with _connect() as c:
        c.execute("DELETE FROM copy_twins WHERE area_id=? AND group_id=?", (area_id, group_id))


def list_copy_twins(area_id: int, group_id: str) -> list[dict[str, Any]]:
    init()
    with _connect() as c:
        return [dict(r) for r in c.execute("SELECT * FROM copy_twins WHERE area_id=? AND group_id=? ORDER BY id",
                                           (area_id, group_id)).fetchall()]


def save_copy_state(area_id: int, group_id: str, contract_id: int, symbol: str, leader_net: int, unit: int) -> None:
    """Remember a mirrored contract's leader position (survives a restart)."""
    init()
    with _connect() as c:
        c.execute("INSERT INTO copy_state(area_id, group_id, contract_id, symbol, leader_net, unit, updated_at) VALUES(?,?,?,?,?,?,?) "
                  "ON CONFLICT(area_id, group_id, contract_id) DO UPDATE SET symbol=excluded.symbol, leader_net=excluded.leader_net, "
                  "unit=excluded.unit, updated_at=excluded.updated_at",
                  (area_id, group_id, int(contract_id), str(symbol or ""), int(leader_net), int(unit), _now()))


def delete_copy_state(area_id: int, group_id: str, contract_id: int | None = None) -> None:
    init()
    with _connect() as c:
        if contract_id is None:
            c.execute("DELETE FROM copy_state WHERE area_id=? AND group_id=?", (area_id, group_id))
        else:
            c.execute("DELETE FROM copy_state WHERE area_id=? AND group_id=? AND contract_id=?", (area_id, group_id, int(contract_id)))


def list_copy_state(area_id: int, group_id: str) -> list[dict[str, Any]]:
    init()
    with _connect() as c:
        return [dict(r) for r in c.execute("SELECT * FROM copy_state WHERE area_id=? AND group_id=?", (area_id, group_id)).fetchall()]


def prune_copy_events(days: int = 7) -> int:
    init()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with _connect() as c:
        return int(c.execute("DELETE FROM copy_events WHERE ts<?", (cutoff,)).rowcount or 0)

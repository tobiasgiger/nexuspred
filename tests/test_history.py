"""Durable signal/order history (app/history.py + db tables + /api/history/*)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app import context, db, history, state


def _signals(n, area=1, result="received"):
    with context.use_area(area):
        for i in range(n):
            state.log_signal({"action": "buy", "symbol": f"S{i}"}, result=result, webhook="Alpha")


async def test_signals_and_orders_are_persisted_and_paginated(admin):
    _signals(5)
    with context.use_area(1):
        state.log_order({"action": "Buy", "symbol": "MNQU6", "account": "A", "qty": 2,
                         "order_type": "Market", "order_id": 7, "status": "submitted"})
    page = db.list_signals(1, limit=3)
    assert [s["payload"]["symbol"] for s in page["items"]] == ["S4", "S3", "S2"]
    assert page["items"][0]["webhook"] == "Alpha" and page["next_before"] == page["items"][-1]["id"]
    rest = db.list_signals(1, limit=3, before=page["next_before"])
    assert [s["payload"]["symbol"] for s in rest["items"]] == ["S1", "S0"] and rest["next_before"] is None
    orders = db.list_orders(1)["items"]
    assert orders[0]["order_id"] == 7 and orders[0]["symbol"] == "MNQU6" and orders[0]["qty"] == 2
    assert db.list_signals(2)["items"] == []  # other areas see nothing


async def test_filters(admin):
    _signals(2, result="ok")
    _signals(1, result="error: boom")
    assert len(db.list_signals(1, result="error")["items"]) == 1
    assert len(db.list_signals(1, q="S0")["items"]) == 2  # S0 from both batches
    assert len(db.list_signals(1, q="Alpha")["items"]) == 3
    with context.use_area(1):
        state.log_order({"action": "Sell", "symbol": "ESZ6", "account": "B", "status": "rejected"})
        state.log_order({"action": "Buy", "symbol": "MNQU6", "account": "A", "status": "submitted"})
    assert [o["symbol"] for o in db.list_orders(1, symbol="ES")["items"]] == ["ESZ6"]
    assert [o["account"] for o in db.list_orders(1, account="A")["items"]] == ["A"]


async def test_hydrate_refills_ring_buffers_after_restart(admin):
    _signals(3)
    with context.use_area(1):
        state.log_order({"action": "Buy", "symbol": "X", "account": "A", "status": "submitted"})
    state._areas.clear()  # "restart": in-memory state gone
    with context.use_area(1):
        assert state.recent_signals() == []
    assert history.hydrate([1]) == 4
    with context.use_area(1):
        assert [s["payload"]["symbol"] for s in state.recent_signals()] == ["S2", "S1", "S0"]
        assert state.recent_orders()[0]["symbol"] == "X"


async def test_background_writer_flushes(admin):
    history.start()
    try:
        _signals(20)
        history.flush()
        assert len(db.list_signals(1, limit=100)["items"]) == 20
    finally:
        history.stop()
    assert not history._running


async def test_prune_and_stats(admin):
    _signals(2, result="ok")
    _signals(1, result="error: x")
    _signals(1, result="received")
    old = (datetime.now(timezone.utc) - timedelta(days=100)).isoformat()
    db.insert_signal(1, {"ts": old, "result": "ok", "payload": {"old": True}})
    db.insert_order(1, {"ts": old, "action": "Buy", "symbol": "OLD", "account": "A", "status": "submitted"})
    assert history.prune(90) == 2
    assert history.prune(90) == 0
    since = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    stats = db.history_stats(1, since)
    assert stats["totals"] == {"received": 1, "executed": 2, "errors": 1, "skipped": 0, "orders": 0, "rejected": 0}
    assert len(stats["days"]) == 1


async def test_history_api(client):
    _signals(3, result="ok")
    with context.use_area(1):
        state.log_order({"action": "Buy", "symbol": "MNQU6", "account": "A", "status": "submitted"})
    r = await client.get("/api/history/signals?limit=2")
    assert r.status_code == 200 and len(r.json()["items"]) == 2 and r.json()["next_before"]
    r = await client.get(f"/api/history/signals?limit=2&before={r.json()['next_before']}")
    assert len(r.json()["items"]) == 1
    assert (await client.get("/api/history/orders")).json()["items"][0]["symbol"] == "MNQU6"
    st = (await client.get("/api/history/stats?days=7")).json()
    assert st["totals"]["executed"] == 3 and st["totals"]["orders"] == 1 and st["days_window"] == 7

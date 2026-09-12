"""alpha.77 — verified track record of published signals / copy groups and the
subscriber's journal of a subscription."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app import config, context, db, signals, state, track_record
from tests.test_marketplace import two_areas, sub_client, execs  # noqa: F401

ZONE = ZoneInfo("Europe/Zurich")


def _trade(i: int, pnl: float, *, spec: str = "P1", days_ago: int = 0, source: str = "fifo", root: str = "MNQ", now=None) -> dict:
    exit_at = (now or datetime.now(timezone.utc)) - timedelta(days=days_ago, minutes=i)
    return {"pair_id": f"{source}:{spec}:{i}", "source": source, "account_id": 11, "account_spec": spec, "account_name": spec, "environment": "demo",
            "contract_id": 5, "symbol": f"{root}Z6", "root": root, "side": "long", "qty": 1, "entry_price": 100.0, "exit_price": 100.0 + pnl,
            "entry_ts": (exit_at - timedelta(minutes=10)).isoformat(), "exit_ts": exit_at.isoformat(), "entry_fill_id": i * 2, "exit_fill_id": i * 2 + 1,
            "points": pnl, "value_per_point": 1.0, "gross_pnl": pnl, "fees": 0.0, "net_pnl": pnl}


def test_summarize_trades_figures_and_verification():
    now = datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc)
    trades = [_trade(1, 50, now=now), _trade(2, -20, now=now, days_ago=5), _trade(3, 30, now=now, days_ago=40), _trade(4, -10, now=now, days_ago=100, source="csv")]
    for i, t in enumerate(trades):
        t["id"] = i + 1
    s = track_record.summarize_trades(trades, ZONE, now=now)
    assert s["trades"] == 4 and s["wins"] == 2 and s["losses"] == 2 and s["net_pnl"] == 50.0 and s["win_rate"] == 0.5
    assert s["profit_factor"] == round(80 / 30, 2) and s["net_30d"] == 30.0 and s["trades_30d"] == 2 and s["net_90d"] == 60.0
    assert s["verified"] is False and s["verified_share"] == 0.75                   # one CSV-uploaded trade
    assert s["first_trade_at"] < s["last_trade_at"] and s["max_drawdown"] <= 0
    assert [m["trades"] for m in s["monthly"]] and s["monthly"][-1]["cumulative"] == 50.0 and len(s["equity"]) == 4
    empty = track_record.summarize_trades([], ZONE, now=now)
    assert empty["trades"] == 0 and empty["profit_factor"] is None and empty["verified"] is False and empty["monthly"] == []


def test_equity_curve_is_compressed():
    curve = [{"ts": str(i), "equity": float(i), "trade_id": i} for i in range(1000)]
    out = track_record._compress(curve, 120)
    assert len(out) <= 121 and out[0]["equity"] == 0.0 and out[-1]["equity"] == 999.0 and "trade_id" not in out[0]


def test_webhook_record_covers_routed_accounts_only_and_is_cached(two_areas):
    wh = two_areas["wh"]
    db.upsert_journal_trade(1, _trade(1, 100.0, spec="P1"))
    db.upsert_journal_trade(1, _trade(2, -40.0, spec="P1"))
    db.upsert_journal_trade(1, _trade(3, 999.0, spec="OTHER"))                        # not routed by this webhook
    with context.use_area(1):
        state.log_signal({"action": "buy", "symbol": "MNQ1!"}, result="ok", webhook="Alpha", webhook_id=wh["id"])
        state.log_signal({"action": "buy", "symbol": "MNQ1!"}, result="error: boom", webhook="Alpha", webhook_id=wh["id"])
        state.log_signal({"action": "buy", "symbol": "MNQ1!"}, result="ok", webhook="Other", webhook_id="wh_other")
    rec = track_record.webhook_record(1, wh, detail=True)
    assert rec["basis"] == "accounts" and rec["accounts_n"] == 1 and rec["trades"] == 2 and rec["net_pnl"] == 60.0 and rec["verified"] is True
    assert rec["signals"] == {**rec["signals"], "executed": 1, "errors": 1} and rec["signals"]["last_at"]
    assert "P1" not in str(rec) and "OTHER" not in str(rec)                             # account names never leave the publisher
    compact = track_record.webhook_record(1, wh)
    assert "monthly" not in compact and compact["trades"] == 2 and set(compact) == set(track_record.COMPACT_KEYS)
    db.upsert_journal_trade(1, _trade(4, 5.0, spec="P1"))
    assert track_record.webhook_record(1, wh)["trades"] == 2                           # cached for CACHE_TTL_S
    track_record.reset()
    assert track_record.webhook_record(1, wh)["trades"] == 3
    assert track_record.webhook_record(1, {"id": "wh_none", "accounts": []})["basis"] == "none"


def test_copy_record_is_the_leader_account(admin):
    from app import copy as cp
    db.upsert_journal_trade(1, _trade(1, 25.0, spec="LEAD"))
    db.upsert_journal_trade(1, _trade(2, 1.0, spec="FOLLOW"))
    g = cp.new_group("G"); g["leader"] = {"token_idx": 0, "spec": "LEAD", "account_id": 1}
    rec = track_record.copy_record(1, g, detail=True)
    assert rec["basis"] == "leader" and rec["trades"] == 1 and rec["net_pnl"] == 25.0 and rec["signals"] is None
    assert track_record.copy_record(1, {"id": "cg_x", "leader": {}})["basis"] == "none"


async def test_marketplace_listing_carries_the_record_and_detail_endpoints_check_visibility(two_areas, sub_client, client):
    wh = two_areas["wh"]
    db.upsert_journal_trade(1, _trade(1, 80.0, spec="P1"))
    items = (await sub_client.get("/api/marketplace")).json()
    assert items[0]["record"]["trades"] == 1 and items[0]["record"]["net_pnl"] == 80.0 and items[0]["record"]["verified"] is True
    r = await sub_client.get(f"/api/marketplace/1/{wh['id']}/record")
    assert r.status_code == 200 and r.json()["title"] == "Alpha Scalper" and r.json()["monthly"] and r.json()["equity"]
    assert (await sub_client.get("/api/marketplace/1/wh_nope/record")).status_code == 404
    # invite-only for someone else → hidden
    config.update(lambda s: s["webhooks"][0]["sharing"].update({"visibility": "selected", "allowed_user_ids": [999]}), area_id=1)
    assert (await sub_client.get(f"/api/marketplace/1/{wh['id']}/record")).status_code == 404
    # the publisher's own view works whether published or not
    r = await client.get(f"/api/webhooks/{wh['id']}/record")
    assert r.status_code == 200 and r.json()["trades"] == 1
    assert (await client.get("/api/webhooks/wh_nope/record")).status_code == 404
    assert (await client.get("/api/copy/groups/cg_nope/record")).status_code == 404


async def test_subscription_journal_lists_signals_and_pnl_since_subscribing(two_areas, sub_client, execs):
    from tests.helpers import settle
    wh, a2 = two_areas["wh"], two_areas["a2"]
    old = _trade(1, 500.0, spec="S1", days_ago=3)                                       # before the subscription: not counted
    db.upsert_journal_trade(a2, old)
    r = await sub_client.post(f"/api/marketplace/1/{wh['id']}/subscribe", json={"accounts": [{"token_idx": 0, "spec": "S1", "enabled": True}], "enabled": True})
    sub = r.json()
    with context.use_area(1):
        signals.accept({"action": "buy", "symbol": "MNQ1!", "qty": 1}, wh)
    await settle(20)
    db.upsert_journal_trade(a2, _trade(2, 40.0, spec="S1", days_ago=-1))                # closed after subscribing
    db.upsert_journal_trade(a2, _trade(3, -10.0, spec="S1", days_ago=-1))
    db.upsert_journal_trade(a2, _trade(4, 7.0, spec="S2", days_ago=-1))                 # not routed
    r = await sub_client.get(f"/api/subscriptions/{sub['id']}/journal")
    assert r.status_code == 200
    j = r.json()
    assert j["kind"] == "webhook" and j["accounts_n"] == 1 and j["since"] == sub["created_at"]
    assert j["pnl"]["trades"] == 2 and j["pnl"]["net_pnl"] == 30.0
    assert j["signals"]["received"] == 1 and j["signals"]["executed"] == 1 and j["recent"][0]["action"] == "buy" and j["recent"][0]["symbol"] == "MNQ1!"
    assert j["subscription"]["webhook"]["title"] == "Alpha Scalper"
    assert (await sub_client.get("/api/subscriptions/999/journal")).status_code == 404
    # the signal rows in the subscriber's area carry the subscription's webhook id
    rows = db.list_signals(a2, webhook_id=f"sub1_{wh['id']}")["items"]
    assert len(rows) == 2 and {x["result"] for x in rows} == {"received", "ok"}


async def test_subscription_journal_for_a_copy_follow(two_areas, sub_client):
    a2 = two_areas["a2"]
    sub = db.upsert_subscription(a2, 1, "copy:cg_lead", [{"token_idx": 0, "spec": "S1", "enabled": True}], True)
    db.insert_copy_event(1, {"group_id": "cg_lead", "kind": "mirror", "leader": "L", "follower": "S1", "symbol": "MNQZ6", "detail": "bought 1", "latency_ms": 120})
    db.insert_copy_event(1, {"group_id": "cg_lead", "kind": "mirror", "leader": "L", "follower": "S9", "symbol": "MNQZ6", "detail": "someone else"})
    j = track_record.subscription_journal(a2, sub)
    assert j["kind"] == "copy" and len(j["copy_events"]) == 1 and j["copy_events"][0]["latency_ms"] == 120 and j["signals"] is None


def test_signal_stats_and_filters(admin):
    with context.use_area(1):
        state.log_signal({"action": "buy"}, result="received", webhook="W", webhook_id="wh_a")
        state.log_signal({"action": "buy"}, result="ok", webhook="W", webhook_id="wh_a")
        state.log_signal({"action": "sell"}, result="skipped", webhook="W", webhook_id="wh_a")
        state.log_signal({"action": "sell"}, result="ok", webhook="X", webhook_id="wh_b")
    s = db.signal_stats(1, "wh_a")
    assert s == {**s, "received": 1, "executed": 1, "skipped": 1, "errors": 0} and s["first_at"] <= s["last_at"]
    assert db.signal_stats(1, "wh_a", since_ts="2999-01-01")["executed"] == 0
    assert [r["webhook_id"] for r in db.list_signals(1, webhook_id="wh_b")["items"]] == ["wh_b"]
    assert db.list_journal_trades(1, accounts=[]) == []

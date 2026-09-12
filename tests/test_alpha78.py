"""alpha.78 — subscriber controls, publisher controls, discovery fields, latency fairness."""
from __future__ import annotations

import pytest

from app import alerts, config, context, db, events, marketplace, signals, state, track_record
from tests.helpers import FakeExecutor, settle
from tests.test_marketplace import PAYLOAD, S1, execs, sub_client, two_areas  # noqa: F401


# ------------------------------------------------------------ normalisation
def test_sharing_publisher_controls_and_tags():
    sh = marketplace.normalize_sharing({"enabled": True, "max_subscribers": "25", "approval": 1, "paused": "yes", "tags": " NQ, scalping,nq ,, x"})
    assert sh["max_subscribers"] == 25 and sh["approval"] is True and sh["paused"] is True and sh["tags"] == ["nq", "scalping", "x"] and sh["published_at"]
    kept = marketplace.normalize_sharing({"title": "T"}, sh)
    assert kept["published_at"] == sh["published_at"] and kept["tags"] == sh["tags"]        # a later edit keeps the publish date
    assert marketplace.normalize_sharing({"max_subscribers": -3})["max_subscribers"] == 0
    assert marketplace.normalize_sharing({"tags": ["a" * 40]})["tags"] == ["a" * marketplace.TAG_LEN]
    assert marketplace.normalize_sharing({"enabled": False})["published_at"] == ""


def test_controls_normalisation():
    c = marketplace.normalize_controls({"symbols": "mnqz6, es", "max_qty": "3", "max_signals_per_day": 10, "pause_after_errors": "2",
                                        "trade_window": {"enabled": True, "from": "09:00", "to": "16:00", "days": ["mon"], "tz": ""}})
    assert c["symbols"] == ["MNQ", "ES"] and c["max_qty"] == 3 and c["max_signals_per_day"] == 10 and c["pause_after_errors"] == 2
    assert c["trade_window"]["enabled"] is True and c["trade_window"]["days"] == ["mon"]
    assert marketplace.normalize_controls(None) == marketplace.DEFAULT_CONTROLS
    assert marketplace.normalize_controls({"trade_window": {"enabled": False}})["trade_window"] is None
    for bad in ("x", {"max_qty": "many"}, {"max_qty": 5000}, {"symbols": [f"R{i}" for i in range(21)]}, {"trade_window": {"enabled": True, "from": "25:00"}}):
        with pytest.raises(ValueError):
            marketplace.normalize_controls(bad)
    assert marketplace.controls_of({"controls": "broken"}) == marketplace.DEFAULT_CONTROLS


def test_subscription_view_applies_the_qty_cap_and_window():
    wh = {"id": "wh_1", "name": "W", "strategy": "simple", "sharing": {"enabled": True, "title": "T"}}
    sub = {"id": 7, "accounts": [{"token_idx": 0, "spec": "S1", "enabled": True, "sizing": {"mode": "multiplier", "multiplier": 2.0, "fixed": 1, "max_contracts": 5}},
                                {"token_idx": 0, "spec": "S2", "enabled": True}],
           "controls": {"max_qty": 2, "trade_window": {"enabled": True, "from": "09:00", "to": "16:00", "days": ["mon"], "tz": ""}}}
    view = marketplace.subscription_view(wh, sub, 1)
    assert view["accounts"][0]["sizing"]["max_contracts"] == 2 and view["accounts"][1]["sizing"]["max_contracts"] == 2
    assert view["trade_window"]["from"] == "09:00" and view["controls"]["max_qty"] == 2 and view["id"] == "sub1_wh_1"
    plain = marketplace.subscription_view(wh, {"id": 8, "accounts": [{"token_idx": 0, "spec": "S1", "enabled": True}]}, 1)
    assert "sizing" not in plain["accounts"][0] and plain["trade_window"] is None


def test_subscription_gate_symbols_and_daily_cap(admin):
    view = {"id": "sub1_wh", "controls": {"symbols": ["MNQ"], "max_signals_per_day": 1}}
    assert marketplace.subscription_gate(view, "MNQ", "buy", area_id=1)[0] is True
    ok, why, detail = marketplace.subscription_gate(view, "ES", "close_all", area_id=1)
    assert not ok and why == "subscription_symbols" and "ES" in detail
    with context.use_area(1):
        state.log_signal({"action": "buy"}, result="ok", webhook="T", webhook_id="sub1_wh")
    ok, why, _ = marketplace.subscription_gate(view, "MNQ", "sell", area_id=1)
    assert not ok and why == "subscription_daily_cap"
    assert marketplace.subscription_gate(view, "MNQ", "close_all", area_id=1)[0] is True   # the cap is for entries only


# ------------------------------------------------------------- fan-out path
@pytest.fixture
def sized_execs(monkeypatch):
    """Like ``execs`` but the fakes carry the per-account sizing (as AccountExecutor does)."""
    made: list[tuple[int, str, FakeExecutor]] = []

    def fake(wh):
        out = []
        for a in wh.get("accounts") or []:
            if not a.get("enabled"):
                continue
            e = FakeExecutor(a["spec"], qty_multiplier=a.get("qty_multiplier", 1))
            if a.get("sizing"):
                e.sizing = a["sizing"]
            out.append(e)
        made.extend((context.get_area(), wh["id"], e) for e in out)
        return out
    monkeypatch.setattr(signals, "_webhook_executors", fake)

    async def no_alert(*a, **k):
        pass
    monkeypatch.setattr(alerts, "trade_executed", no_alert)
    return made


async def test_controls_are_enforced_in_the_subscriber_area(two_areas, sub_client, sized_execs):
    execs = sized_execs
    wh, a2 = two_areas["wh"], two_areas["a2"]
    r = await sub_client.post(f"/api/marketplace/1/{wh['id']}/subscribe",
                              json={"accounts": [S1], "controls": {"symbols": ["ES"], "max_qty": 1}})
    assert r.status_code == 200 and r.json()["controls"]["symbols"] == ["ES"] and r.json()["status"] == "active"
    with context.use_area(1):
        signals.accept(PAYLOAD, wh)                                   # MNQ → not in the subscriber's symbols
    await settle(20)
    mine = [e for area, wid, e in execs if area == a2]
    assert mine == []                                                 # no executor was even built
    rows = db.list_signals(a2, webhook_id=f"sub1_{wh['id']}")["items"]
    assert rows[0]["result"] == "skipped" and rows[0]["latency_ms"] is not None
    r = await sub_client.put(f"/api/subscriptions/{r.json()['id']}", json={"controls": {"symbols": [], "max_qty": 1}})
    assert r.status_code == 200
    with context.use_area(1):
        signals.accept({**PAYLOAD, "qty": 3}, wh)
    await settle(20)
    mine = [e for area, wid, e in execs if area == a2]
    assert mine and mine[-1].of("place")[-1]["qty"] == 1                # S1 has ×2 sizing: 6 contracts capped to 1
    assert (await sub_client.put(f"/api/subscriptions/{r.json()['id']}", json={"controls": {"max_qty": "x"}})).status_code == 400


async def test_pause_after_consecutive_errors_switches_the_subscription_off(two_areas, sub_client, monkeypatch):
    wh, a2 = two_areas["wh"], two_areas["a2"]
    paused: list[tuple[str, str]] = []

    async def fake_paused(title, reason):
        paused.append((title, reason))
    monkeypatch.setattr(alerts, "subscription_paused", fake_paused)
    monkeypatch.setattr(signals, "_webhook_executors", lambda w: [FakeExecutor("S1", fail_place=True)])

    async def no_alert(*a, **k):
        pass
    monkeypatch.setattr(alerts, "webhook_failed", no_alert)
    r = await sub_client.post(f"/api/marketplace/1/{wh['id']}/subscribe", json={"accounts": [S1], "controls": {"pause_after_errors": 2}})
    sid = r.json()["id"]
    for _ in range(2):
        with context.use_area(1):
            signals.accept(PAYLOAD, wh)
        await settle(30)
    sub = db.get_subscription(sid, a2)
    assert sub["enabled"] is False and paused and "2 consecutive signal errors" in paused[0][1]
    assert not db.active_subscriptions(1, wh["id"])
    assert [e for e in events.recent(kind="subscription.paused")][-1]["subscription_id"] == sid


async def test_publisher_pause_approval_cap_and_subscriber_status(two_areas, sub_client, client, execs):
    wh, a2 = two_areas["wh"], two_areas["a2"]
    # approval required → new subscription is pending and receives nothing
    r = await client.put(f"/api/webhooks/{wh['id']}/sharing", json={"approval": True, "max_subscribers": 1, "tags": ["nq", "scalp"]})
    assert r.status_code == 200 and r.json()["sharing"]["approval"] is True and r.json()["sharing"]["tags"] == ["nq", "scalp"]
    r = await sub_client.post(f"/api/marketplace/1/{wh['id']}/subscribe", json={"accounts": [S1]})
    assert r.status_code == 200 and r.json()["status"] == "pending" and r.json()["active"] is False
    sid = r.json()["id"]
    assert db.active_subscriptions(1, wh["id"]) == []
    items = (await sub_client.get("/api/marketplace")).json()
    assert items[0]["tags"] == ["nq", "scalp"] and items[0]["approval"] is True and items[0]["max_subscribers"] == 1 and items[0]["published_at"]
    # the publisher sees the pending subscriber and approves
    subs = (await client.get(f"/api/webhooks/{wh['id']}/subscribers")).json()
    assert subs[0]["status"] == "pending"
    assert (await client.put(f"/api/webhooks/{wh['id']}/subscribers/{sid}", json={"status": "bogus"})).status_code == 400
    r = await client.put(f"/api/webhooks/{wh['id']}/subscribers/{sid}", json={"status": "active"})
    assert r.status_code == 200 and r.json()["status"] == "active" and len(db.active_subscriptions(1, wh["id"])) == 1
    # the cap: a third area cannot subscribe any more
    u3 = db.create_user("third@example.com", "password123")
    from app import auth
    from tests.conftest import _make_client
    async with _make_client(auth.make_session(u3["id"])) as c3:
        r = await c3.post(f"/api/marketplace/1/{wh['id']}/subscribe", json={"accounts": []})
        assert r.status_code == 409
    # re-subscribing (editing) keeps the publisher's status
    r = await sub_client.post(f"/api/marketplace/1/{wh['id']}/subscribe", json={"accounts": [S1], "enabled": True})
    assert r.json()["status"] == "active"
    # publisher pauses one subscriber → no forwarding; then the whole listing
    await client.put(f"/api/webhooks/{wh['id']}/subscribers/{sid}", json={"status": "paused"})
    assert db.active_subscriptions(1, wh["id"]) == []
    assert (await sub_client.get("/api/subscriptions")).json()[0]["active"] is False
    await client.put(f"/api/webhooks/{wh['id']}/subscribers/{sid}", json={"status": "active"})
    await client.put(f"/api/webhooks/{wh['id']}/sharing", json={"paused": True})
    with context.use_area(1):
        assert signals.forward_to_subscribers(PAYLOAD, config.load_settings(area_id=1)["webhooks"][0]) == 0
    items = (await sub_client.get("/api/marketplace")).json()
    assert items[0]["paused"] is True and items[0]["webhook_enabled"] is False and items[0]["subscription"]["status"] == "active"
    assert (await client.put(f"/api/webhooks/{wh['id']}/subscribers/999", json={"status": "active"})).status_code == 404


async def test_copy_group_subscriber_status_endpoint(two_areas, client, monkeypatch):
    from app import copy as cp
    g = cp.new_group("Lead"); g["leader"] = {"token_idx": 0, "spec": "L1", "account_id": 1}
    g["sharing"] = {"enabled": True, "title": "Lead", "visibility": "all"}
    config.save_settings({"copy_groups": [g]}, area_id=1)
    sub = db.upsert_subscription(two_areas["a2"], 1, f"copy:{g['id']}", [{"token_idx": 0, "spec": "S1", "enabled": True, "mode": "multiplier", "multiplier": 1}], True)
    released: list = []

    async def fake_release(area, gid, specs):
        released.append(sorted(specs)); return 0

    async def fake_sync(area):
        return None
    monkeypatch.setattr(cp, "release_followers", fake_release)
    monkeypatch.setattr(cp, "sync_area", fake_sync)
    r = await client.put(f"/api/copy/groups/{g['id']}/subscribers/{sub['id']}", json={"status": "paused"})
    assert r.status_code == 200 and r.json()["status"] == "paused" and released == [["S1"]]
    assert db.active_subscriptions(1, f"copy:{g['id']}") == []
    assert (await client.get(f"/api/copy/groups/{g['id']}/subscribers")).json()[0]["status"] == "paused"


# ------------------------------------------------------------------ latency
def test_latency_summary_and_signal_latencies(admin):
    assert track_record.latency_summary([]) is None
    assert track_record.latency_summary([5, 1, 9, 3]) == {"n": 4, "p50": 3, "p95": 9, "max": 9}
    with context.use_area(1):
        for ms in (12, 40, 7):
            state.log_signal({"action": "buy"}, result="ok", webhook="W", webhook_id="wh_l", latency_ms=ms)
        state.log_signal({"action": "buy"}, result="received", webhook="W", webhook_id="wh_l")
    assert db.signal_latencies(1, "wh_l") == [7, 40, 12]
    assert db.count_signal_outcomes(1, "wh_l", "2000-01-01") == 3


def test_fan_out_order_is_shuffled(two_areas, monkeypatch):
    wh = two_areas["wh"]
    subs = [{"id": i, "area_id": 100 + i, "publisher_area_id": 1, "webhook_id": wh["id"], "enabled": True, "accounts": [], "status": "active", "controls": {},
             "created_at": "", "updated_at": ""} for i in range(6)]
    monkeypatch.setattr(db, "active_subscriptions", lambda pa, wid: [dict(s) for s in subs])
    orders: list[list[int]] = []
    monkeypatch.setattr(signals, "_spawn", lambda coro: (coro.close(), None)[1])

    def fake_log(payload, result="", webhook="", webhook_id="", latency_ms=None):
        orders[-1].append(int(webhook_id.split("_")[0][3:]) if False else context.get_area())
        return {}
    monkeypatch.setattr(state, "log_signal", fake_log)
    seen = set()
    for _ in range(12):
        orders.append([])
        with context.use_area(1):
            signals.forward_to_subscribers(PAYLOAD, wh)
        seen.add(tuple(orders[-1]))
    assert all(sorted(o) == [100, 101, 102, 103, 104, 105] for o in orders) and len(seen) > 1

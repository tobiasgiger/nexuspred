"""Position / agent / daily-summary watcher (app/watch.py) → alert triggers."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app import alerts, config, context, db, relay, state, watch
from tests.helpers import BrokerFeed


class Sess(BrokerFeed):
    """A login whose /position/list answer and cash snapshots the test controls."""
    def __init__(self, name="L1", accounts=None, positions=None, realized=None, fail=False):
        self.name, self.environment, self.enabled = name, "demo", True
        self.accounts = accounts or [{"id": 11, "spec": "DEMO11"}]
        self.positions = positions if positions is not None else []
        self.realized = realized or {}
        self.fail = fail

    def has_token(self):
        return True

    async def _request(self, method, path, **kw):
        if self.fail:
            raise RuntimeError("offline")
        if path == "/position/list":
            return [dict(p) for p in self.positions]
        if path == "/contract/item":
            return {"name": {901: "MNQZ6", 902: "ESZ6"}.get(kw["params"]["id"], "?")}
        raise AssertionError(path)


def _snap(acc, realized):
    return {"account_id": acc, "spec": f"DEMO{acc}", "realized": realized}


@pytest.fixture
def sent(monkeypatch):
    calls: list[tuple] = []

    def rec(kind):
        async def f(*a, **k):
            calls.append((kind, a, k))
        return f
    for name in ("trade_opened", "position_added", "trade_closed", "agent_lost", "agent_restored", "daily_summary"):
        monkeypatch.setattr(alerts, name, rec(name))
    return calls


async def test_open_close_reverse_and_partial_with_realized_pnl(admin, sent):
    sess = Sess()
    # tick 1: nothing open → baseline only
    assert await watch.observe_area(1, [sess], [_snap(11, 0.0)]) == []
    # tick 2: a long appears → opened
    sess.positions = [{"accountId": 11, "contractId": 901, "netPos": 2, "netPrice": 21050.25}]
    ev = await watch.observe_area(1, [sess], [_snap(11, 0.0)])
    assert [e["kind"] for e in ev] == ["opened"]
    assert sent[-1] == ("trade_opened", ("DEMO11", "MNQZ6", "LONG", 2.0, 21050.25), {})
    # tick 3: add one contract
    sess.positions[0]["netPos"] = 3
    await watch.observe_area(1, [sess], [_snap(11, 0.0)])
    assert sent[-1][0] == "position_added" and sent[-1][1] == ("DEMO11", "MNQZ6", "LONG", 1.0, 3.0)
    # tick 4: partial close, realised moved +40
    sess.positions[0]["netPos"] = 1
    await watch.observe_area(1, [sess], [_snap(11, 40.0)])
    kind, args, kw = sent[-1]
    assert kind == "trade_closed" and args[:4] == ("DEMO11", "MNQZ6", "LONG", 2.0) and args[4] == 40.0 and kw == {"remaining": 1.0}
    # tick 5: fully closed (position row disappears), realised now +25 → this close made −15
    sess.positions = []
    ev = await watch.observe_area(1, [sess], [_snap(11, 25.0)])
    assert [e["kind"] for e in ev] == ["closed"] and ev[0]["pnl"] == -15.0
    kind, args, kw = sent[-1]
    assert kind == "trade_closed" and args[:5] == ("DEMO11", "MNQZ6", "LONG", 1.0, -15.0) and kw == {}
    assert args[5].endswith(" s")   # duration since we saw it open
    # tick 6: reversal in one tick → closed + opened
    sess.positions = [{"accountId": 11, "contractId": 902, "netPos": 1, "netPrice": 5900.0}]
    await watch.observe_area(1, [sess], [_snap(11, 25.0)])
    sess.positions = [{"accountId": 11, "contractId": 902, "netPos": -2, "netPrice": 5901.0}]
    ev = await watch.observe_area(1, [sess], [_snap(11, 75.0)])
    assert [e["kind"] for e in ev] == ["closed", "opened"] and ev[0]["pnl"] == 50.0
    assert sent[-2][0] == "trade_closed" and sent[-1] == ("trade_opened", ("DEMO11", "ESZ6", "SHORT", 2.0, 5901.0), {})
    assert watch._closed_today[1][-1] == {"account": "DEMO11", "symbol": "ESZ6", "pnl": 50.0}


async def test_unreachable_login_never_looks_like_a_close(admin, sent):
    sess = Sess(positions=[{"accountId": 11, "contractId": 901, "netPos": 1}])
    await watch.observe_area(1, [sess], [_snap(11, 0.0)])          # baseline with an open position
    sess.fail = True
    assert await watch.observe_area(1, [sess], []) == []           # poll failed → no events, state kept
    sess.fail = False
    assert await watch.observe_area(1, [sess], [_snap(11, 0.0)]) == []
    sess.positions = []
    ev = await watch.observe_area(1, [sess], [_snap(11, 12.5)])
    assert [e["kind"] for e in ev] == ["closed"] and ev[0]["pnl"] == 12.5 and sent[-1][0] == "trade_closed"


async def test_pre_existing_positions_do_not_alert_and_switches_are_honoured(admin, sent):
    sess = Sess(positions=[{"accountId": 11, "contractId": 901, "netPos": -1}])
    await watch.observe_area(1, [sess], [_snap(11, 0.0)])
    assert sent == []                                              # baseline: nothing sent
    with context.use_area(1):
        config.save_settings({"alert_on_trade_closed": False})
    sess.positions = []
    ev = await watch.observe_area(1, [sess], [_snap(11, 5.0)])
    assert [e["kind"] for e in ev] == ["closed"] and sent == []   # detected, recorded, not alerted
    with context.use_area(1):
        config.save_settings({"alert_on_trade_opened": False})
    assert await watch.observe_area(1, [sess], [_snap(11, 5.0)]) == []   # both off → not even polled
    assert not watch.trade_alerts_enabled(config.load_settings(area_id=1))


async def test_agent_transitions(admin, sent):
    token, agent = db.create_agent(1, "VPS 1")
    await watch.observe_agents(1)                                  # baseline: offline, no alert
    relay.touch(agent["id"])
    await watch.observe_agents(1)
    assert sent[-1][0] == "agent_restored" and sent[-1][1] == ("VPS 1",)
    relay._last_seen[agent["id"]] = -1e9
    await watch.observe_agents(1)
    assert sent[-1][0] == "agent_lost" and sent[-1][1][0] == "VPS 1"
    await watch.observe_agents(1)
    assert len(sent) == 2                                          # no repeat while the state is unchanged


async def test_daily_summary_fires_once_after_the_configured_time(admin, sent, monkeypatch):
    from zoneinfo import ZoneInfo
    zone = ZoneInfo("Europe/Zurich")
    now_local = datetime.now(zone)
    later = (now_local + timedelta(minutes=5)).strftime("%H:%M")
    earlier = (now_local - timedelta(minutes=5)).strftime("%H:%M")
    if now_local.hour == 23 and now_local.minute >= 55 or now_local.hour == 0 and now_local.minute < 5:
        pytest.skip("too close to midnight for a same-day window")
    with context.use_area(1):
        config.save_settings({"daily_summary_time": later})
    state.set_pnl({"accounts": [_snap(11, 120.0) | {"open": 0}], "realized": 120.0, "open": 0.0}, 1)
    watch._seeded.add(1)
    watch._closed_today[1] = [{"account": "DEMO11", "symbol": "MNQZ6", "pnl": 120.0}]
    assert await watch.maybe_daily_summary(1) is False              # not yet time
    with context.use_area(1):
        config.save_settings({"daily_summary_time": earlier})
    assert await watch.maybe_daily_summary(1) is True
    kind, args, _ = sent[-1]
    assert kind == "daily_summary" and args[0]["realized"] == 120.0 and args[1][0]["pnl"] == 120.0 and args[2] == now_local.date().isoformat()
    assert await watch.maybe_daily_summary(1) is False              # once per day
    assert watch._closed_today.get(1) is None                       # counters reset for tomorrow


async def test_alert_texts(admin, monkeypatch):
    pushed: list[tuple] = []
    disc: list[str] = []

    async def fake_push(title, message, *, url="/"):
        pushed.append((title, message, url))

    async def fake_discord(message):
        disc.append(message)

    async def quiet(*a, **k):
        return None
    monkeypatch.setattr(alerts, "_send_push", fake_push)
    monkeypatch.setattr(alerts, "_send_discord", fake_discord)
    monkeypatch.setattr(alerts, "_send_email", quiet)
    await alerts.trade_opened("DEMO11", "MNQZ6", "LONG", 2, 21050.25)
    assert pushed[-1] == ("Opened LONG MNQZ6 · DEMO11", "2 contracts @ 21050.25", "/#/")
    assert disc[-1] == "🟢 **Opened** LONG 2 × MNQZ6 @ 21050.25 · `DEMO11`"
    await alerts.trade_closed("DEMO11", "MNQZ6", "LONG", 2, -80.5, "12 min")
    assert pushed[-1][0] == "Closed LONG MNQZ6 · DEMO11" and pushed[-1][1] == "−$80.50 (12 min) · 2 contracts" and pushed[-1][2] == "/#/journal"
    assert disc[-1].startswith("❌ **Closed** LONG 2 × MNQZ6 · `DEMO11` · **−$80.50** (12 min)")
    await alerts.trade_closed("DEMO11", "MNQZ6", "SHORT", 1, 125.0, "", remaining=2)
    assert disc[-1].startswith("🟡 **Reduced** SHORT MNQZ6 by 1 → 2 left") and "+$125.00" in disc[-1]
    await alerts.trade_closed("DEMO11", "MNQZ6", "LONG", 1, None, "")
    assert "P&L n/a" in disc[-1] and disc[-1].startswith("⚪")
    await alerts.agent_lost("VPS 1", "1.2.3.4")
    assert pushed[-1][0] == "Agent offline: VPS 1" and pushed[-1][2] == "/#/settings/agents" and "1.2.3.4" in disc[-1]
    await alerts.daily_summary({"accounts": [_snap(11, 120.0), _snap(12, -20.0)], "realized": 100.0, "open": 0.0},
                               [{"pnl": 150.0}, {"pnl": -50.0}, {"pnl": None}], "2026-09-08")
    assert pushed[-1][0] == "Daily P&L +$100.00" and pushed[-1][1] == "3 trades closed (1 win, 1 loss) · DEMO11 +$120.00, DEMO12 −$20.00"
    with context.use_area(1):
        config.save_settings({"alert_on_trade_opened": False, "alert_daily_summary": False})
    n = len(pushed)
    await alerts.trade_opened("DEMO11", "MNQZ6", "LONG", 1)
    await alerts.daily_summary({}, [], "d")
    assert len(pushed) == n


async def test_daily_summary_time_is_validated(client):
    r = await client.post("/api/settings", json={"daily_summary_time": "25:00"})
    assert r.status_code == 400
    r = await client.post("/api/settings", json={"daily_summary_time": "7:5"})
    assert r.status_code == 200 and r.json()["daily_summary_time"] == "07:05"


async def test_alert_accounts_filter(admin, sent):
    """Only ticked accounts raise account-level alerts; others are tracked silently."""
    with context.use_area(1):
        config.save_settings({"alert_accounts": ["DEMO11"]})
    sess = Sess(accounts=[{"id": 11, "spec": "DEMO11"}, {"id": 12, "spec": "DEMO12"}])
    await watch.observe_area(1, [sess], [_snap(11, 0.0), _snap(12, 0.0)])
    sess.positions = [{"accountId": 11, "contractId": 901, "netPos": 1}, {"accountId": 12, "contractId": 901, "netPos": 1}]
    ev = await watch.observe_area(1, [sess], [_snap(11, 0.0), _snap(12, 0.0)])
    assert [e["account"] for e in ev] == ["DEMO11", "DEMO12"]           # both detected…
    assert [c[1][0] for c in sent] == ["DEMO11"]                        # …one alerted
    sess.positions = []
    await watch.observe_area(1, [sess], [_snap(11, 30.0), _snap(12, -5.0)])
    assert [c[1][0] for c in sent if c[0] == "trade_closed"] == ["DEMO11"]


async def test_alert_accounts_filter_texts(admin, monkeypatch):
    pushed = []

    async def fake_push(title, message, *, url="/", settings=None):
        pushed.append((title, message))

    async def quiet(*a, **k):
        return None
    monkeypatch.setattr(alerts, "_send_push", fake_push)
    monkeypatch.setattr(alerts, "_send_discord", quiet)
    monkeypatch.setattr(alerts, "_send_email", quiet)
    with context.use_area(1):
        config.save_settings({"alert_accounts": ["DEMO11"]})
        # the daily summary covers the selected accounts only
        await alerts.daily_summary({"accounts": [_snap(11, 30.0) | {"open": 0}, _snap(12, -5.0) | {"open": 0}], "realized": 25.0, "open": 0.0},
                                   [{"account": "DEMO11", "pnl": 30.0}, {"account": "DEMO12", "pnl": -5.0}], "2026-09-08")
        assert pushed[-1] == ("Daily P&L +$30.00", "1 trade closed (1 win, 0 loss) · DEMO11 +$30.00")
        # signal-executed alerts list only the selected accounts, and skip entirely when none match
        await alerts.trade_executed("Breakout", "buy", "MNQZ6", ["DEMO12", "DEMO11"])
        assert pushed[-1][1].endswith("MNQZ6 on DEMO11")
        n = len(pushed)
        await alerts.trade_executed("Breakout", "buy", "MNQZ6", ["DEMO12"])
        assert len(pushed) == n
        config.save_settings({"alert_accounts": []})                     # empty → everyone again
        await alerts.trade_executed("Breakout", "buy", "MNQZ6", ["DEMO12"])
        assert len(pushed) == n + 1


async def test_alert_accounts_are_validated_and_normalised(client):
    r = await client.post("/api/settings", json={"alert_accounts": "DEMO11"})
    assert r.status_code == 400
    r = await client.post("/api/settings", json={"alert_accounts": [" DEMO12 ", "DEMO11", "DEMO11", ""]})
    assert r.status_code == 200 and r.json()["alert_accounts"] == ["DEMO11", "DEMO12"]
    r = await client.post("/api/settings", json={"alert_accounts": None})
    assert r.status_code == 200 and r.json()["alert_accounts"] == []

"""Fourth review pass: validation before the first broker call, bounded queues
and locks, unknown order outcomes, copy-engine serialisation, login validation,
journal pairing over the stored history, the scheduler tick."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app import config, context, copy as cp, db, http, journal, signals, tradovate
from app.engine import common
from app.routers import webhooks as wh_router
from tests.helpers import FakeExecutor
from tests.test_strategies import ENTRY, live, wh  # noqa: F401


# ------------------------------------------------ validation before any order
async def test_bracket_rejects_malformed_prices_before_the_entry(live):
    a = FakeExecutor("A")
    live.use(a)
    for bad in ({**ENTRY, "tp2": "abc"}, {**ENTRY, "sl": "nope"}, {**ENTRY, "entry": "x"}, {**ENTRY, "tp1": float("inf")}):
        with pytest.raises(signals.SignalError):
            await signals.process(bad, wh("bracket", id="wh_val"))
    assert a.of("place") == [] and "wh_val:MNQ" not in signals.active_trades()      # nothing reached the broker


async def test_ts_hunter_rejects_malformed_blocks_before_the_entry(live):
    from tests.test_strategies import ts
    a = FakeExecutor("A")
    live.use(a)
    base = {"event": "signal", "side": "buy", "symbol": "MNQ", "trade_id": "V1", "risk": {"value": 2}}
    for bad in ({**base, "sl": {"value": "abc"}}, {**base, "tv": {"entry_price": "x"}}, {**base, "risk": 5}, {**base, "sl": "90"},
                {**base, "risk": {"value": "1e400"}}, {**base, "risk": {"value": 0}}):
        with pytest.raises(signals.SignalError):
            await signals.process(bad, ts())
    assert a.of("place") == []


async def test_set_sl_tp_rejects_a_bad_target_before_touching_the_stop(live):
    a = FakeExecutor("A", positions=[{"symbol": "MNQU6", "netPos": 2}])
    live.use(a)
    w = wh("simple", id="wh_sltp")
    with pytest.raises(signals.SignalError):
        await signals.process({"action": "set_sl_tp", "symbol": "MNQ1!", "stop_price": 95, "target_price": "abc"}, w)
    assert a.of("place") == []                                                     # the good stop was not placed either


def test_signal_qty_bounds():
    assert common._signal_qty("1e15", 3, strict=False) == 3.0                      # bracket: back to the webhook default
    assert common._signal_qty("nan", 3, strict=False) == 3.0
    assert common._signal_qty(2.7, 3, strict=False) == 2.7
    with pytest.raises(signals.SignalError):
        common._signal_qty("1e15", 1, strict=True)                                 # simple / TS-Hunter: refused
    with pytest.raises(signals.SignalError):
        common._signal_qty(None, 5000, strict=False)                               # a broken default is refused too


# --------------------------------------------------------- locks and ingress
async def test_ghost_management_signals_leave_no_lock_behind(live):
    from tests.test_strategies import ts
    live.use(FakeExecutor("A"))
    before = set(signals._trade_locks)
    for i in range(20):
        await signals.process({"event": "management", "action": "partial_close_percent", "symbol": "MNQ", "percent": 25, "trade_id": f"ghost-{i}"}, ts())
    assert set(signals._trade_locks) == before
    w = wh("bracket", id="wh_lockfree")
    await signals.process({"action": "move_sl", "symbol": "MNQ1!", "new_sl": 100.0}, w)   # nothing tracked → skipped, no lock kept
    assert not any(k.endswith(":wh_lockfree:MNQ") for k in signals._trade_locks)


async def test_ingress_is_rate_limited_and_bounded(client, admin, webhook_factory, monkeypatch):
    w = webhook_factory("Flood")
    monkeypatch.setattr(wh_router, "_INGRESS_LIMIT", wh_router.security.RateLimiter(3, 60.0))
    codes = [(await client.post(f"/webhook/{w['token']}", json={"action": "close_all", "symbol": "MNQ1!"})).status_code for _ in range(5)]
    assert codes == [202, 202, 202, 429, 429]
    monkeypatch.setattr(wh_router, "_INGRESS_LIMIT", wh_router.security.RateLimiter(100, 60.0))
    monkeypatch.setattr(wh_router, "MAX_QUEUED_SIGNALS", 0)
    r = await client.post(f"/webhook/{w['token']}", json={"action": "close_all", "symbol": "MNQ1!"})
    assert r.status_code == 503 and "Retry-After" in r.headers


# ------------------------------------------------- unknown outcomes (Tradovate)
class _Client:
    def __init__(self, exc):
        self.exc = exc

    async def request(self, method, url, **kw):
        raise self.exc


async def test_lost_answer_on_an_order_is_outcome_unknown(admin, monkeypatch):
    sess = tradovate.TradovateSession(0, {"name": "L", "environment": "demo", "enabled": True, "access_token": "t",
                                          "accounts": [{"spec": "A1", "id": 1, "enabled": True}]}, area_id=1)
    monkeypatch.setattr(sess, "_get_token", _tok)
    monkeypatch.setattr(tradovate, "REQUEST_SPACING_S", 0.0)
    monkeypatch.setattr(tradovate, "PRIORITY_SPACING_S", 0.0)
    monkeypatch.setattr(http, "client", lambda name="outbound": _Client(httpx.ReadTimeout("no answer")))
    with pytest.raises(tradovate.OrderOutcomeUnknown):
        await sess._request("POST", "/order/placeorder", json={})
    with pytest.raises(tradovate.TradovateError) as exc:                          # a poll: plain error, no alert
        await sess._request("GET", "/position/list")
    assert not isinstance(exc.value, tradovate.OrderOutcomeUnknown)
    monkeypatch.setattr(http, "client", lambda name="outbound": _Client(httpx.ConnectError("refused")))
    with pytest.raises(tradovate.TradovateError) as exc:                          # nothing was sent: never "unknown"
        await sess._request("POST", "/order/placeorder", json={})
    assert not isinstance(exc.value, tradovate.OrderOutcomeUnknown)


async def _tok(*a, **k):
    return "t"


# ------------------------------------------------------------ copy engine
async def test_concurrent_syncs_start_one_runner(admin, monkeypatch):
    started, stopped = [], []

    def start(self):
        started.append(self)

    async def stop(self):
        stopped.append(self)
        await asyncio.sleep(0.02)                                                  # the window the second sync used to slip into
    monkeypatch.setattr(cp.GroupRunner, "start", start)
    monkeypatch.setattr(cp.GroupRunner, "stop", stop)
    g = cp.new_group("G")
    g.update({"enabled": True, "leader": {"token_idx": 0, "spec": "LEAD", "account_id": 1}, "followers": []})
    with context.use_area(1):
        cp.save_groups([g])
        await cp.sync_area(1)
        assert len(started) == 1
        g2 = {**g, "name": "renamed"}
        cp.save_groups([g2])
        await asyncio.gather(cp.sync_area(1), cp.sync_area(1), cp.sync_area(1))
        assert len(stopped) == 1 and len(started) == 2                             # one restart, not three
        assert cp.runner(1, g["id"]) is started[-1]
    cp.reset()


# ------------------------------------------------------------ login rows
async def test_login_rows_are_validated(client, admin):
    assert (await client.post("/api/token-accounts", json=["x"])).status_code == 400
    assert (await client.post("/api/token-accounts", json={"a": 1})).status_code == 400
    r = await client.post("/api/token-accounts", json=[{"name": "P", "broker": "projectx", "px_user": "u", "px_api_key": "k", "px_firm": "http://10.0.0.1/"}])
    assert r.status_code == 400 and "https" in r.json()["detail"]
    r = await client.post("/api/token-accounts", json=[{"name": "R", "broker": "rithmic", "rithmic_user": "u", "rithmic_password": "p", "rithmic_gateway": "wss://evil.example.com:443"}])
    assert r.status_code == 400 and "rithmic.com" in r.json()["detail"]
    r = await client.post("/api/token-accounts", json=[{"name": "R", "broker": "rithmic", "rithmic_user": "u", "rithmic_password": "p", "rithmic_gateway": "europe"},
                                                       {"name": "T", "access_token": 12345, "md_token": None}])
    assert r.status_code == 200, r.text
    saved = config.load_settings()["token_accounts"]
    assert saved[0]["rithmic_gateway"] == "europe" and saved[1]["access_token"] == "12345"


# ------------------------------------------------------------ journal
class _Sess:
    name, environment = "PX", "demo"
    accounts = [{"id": 101, "spec": "ACC", "enabled": True}]

    async def cash_snapshot(self, aid):
        return {}


async def test_fifo_pairs_over_the_stored_history_not_the_window(admin):
    s = _Sess()
    acct = journal._accounts_of(s)
    info = {5: ("MNQU6", 2.0)}
    def fill(i, action, ts, price):
        return {"id": i, "orderId": i, "contractId": 5, "timestamp": ts, "action": action, "qty": 1, "price": price, "_accountId": 101}
    out = await journal._import_fills(1, s, acct, [fill(1, "Buy", "2026-09-01T10:00:00Z", 100.0)], {}, info, {})
    assert out["trades_new"] == 0                                                  # an open long
    # the next window holds only the exit: without history this lone sell would open a phantom short
    out = await journal._import_fills(1, s, acct, [fill(2, "Sell", "2026-09-03T10:00:00Z", 110.0)], {}, info, {})
    assert out["trades_new"] == 1
    out = await journal._import_fills(1, s, acct, [fill(3, "Buy", "2026-09-05T10:00:00Z", 120.0)], {}, info, {})
    assert out["trades_new"] == 0                                                  # a new open long, no phantom pairing with fill 2
    trades = db.list_journal_trades(1)
    assert len(trades) == 1 and trades[0]["side"] == "long" and trades[0]["entry_fill_id"] == 1 and trades[0]["exit_fill_id"] == 2
    # the overlap re-delivery of fill 2 changes nothing
    out = await journal._import_fills(1, s, acct, [fill(2, "Sell", "2026-09-03T10:00:00Z", 110.0)], {}, info, {})
    assert out["fills_new"] == 0 and out["trades_new"] == 0


def test_fill_keys_are_namespaced_and_stable():
    assert journal.fill_key("projectx", 101, 9) == journal.fill_key("projectx", 101, "9")
    assert journal.fill_key("projectx", 101, 9) != journal.fill_key("rithmic", 101, 9) != journal.fill_key("projectx", 102, 9)
    assert 0 < journal.fill_key("rithmic", 1, "f1") < 2 ** 63 and journal._fid("ab12") > 0 and journal._fid("77") == 77


async def test_scheduler_runs_each_area_once_per_local_day(admin, monkeypatch):
    runs: list[int] = []

    async def fake_import(aid, *, trigger):
        runs.append(aid)
        return {"status": "ok"}
    monkeypatch.setattr(journal, "import_area", fake_import)
    journal._ran_on.clear()
    config.save_settings({"journal_import_time": "23:30", "journal_timezone": "Europe/Zurich"}, area_id=1)
    zh = journal.ZoneInfo("Europe/Zurich")
    before = datetime(2026, 9, 14, 23, 0, tzinfo=zh)
    assert await journal.scheduler_tick(before) == []
    late = datetime(2026, 9, 14, 23, 41, tzinfo=zh)                                # 11 minutes past: still due today
    assert await journal.scheduler_tick(late) == [1]
    assert await journal.scheduler_tick(late + timedelta(minutes=5)) == []        # once per day
    assert await journal.scheduler_tick(late + timedelta(days=1)) == [1]
    assert runs == [1, 1]

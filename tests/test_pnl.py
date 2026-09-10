"""Live account P&L (app/pnl.py): snapshot polling, change detection, stream push, API."""
from __future__ import annotations

import asyncio
from datetime import timezone

from app import config, context, db, pnl, state, tradovate


class Sess:
    def __init__(self, name="L", accounts=None, *, connected=True, fail=False, snap=None):
        self.name, self.environment, self.enabled = name, "demo", True
        self.accounts = accounts or [{"id": 11, "spec": "DEMO11"}, {"id": 12, "spec": "DEMO12"}]
        self.fail, self.snap = fail, snap or {}
        self.calls = 0
        state.set_session_status(name, connected=connected)

    def has_token(self):
        return True

    async def _request(self, method, path, **kw):
        if path == "/position/list":
            return []
        if path == "/userAccountAutoLiq/list":
            return getattr(self, "risk", [])
        if path == "/contract/item":
            return {"name": "MNQZ6"}
        self.calls += 1
        assert path == "/cashBalance/getcashbalancesnapshot"
        aid = kw["json"]["accountId"]
        if self.fail:
            raise tradovate.TradovateError("boom")
        return self.snap.get(aid, {"totalCashValue": 50000 + aid, "realizedPnL": 10.5 * (aid - 10), "openPnL": -3.25,
                                   "weekRealizedPnL": 40.0, "totalPnL": 10.5 * (aid - 10) - 3.25})


def _install(monkeypatch, *sessions):
    monkeypatch.setattr(tradovate.manager_for(1), "all", lambda: list(sessions))


async def test_refresh_area_sums_accounts_and_pushes_once(admin, monkeypatch):
    with context.use_area(1):
        sess = Sess()
    _install(monkeypatch, sess)
    with context.use_area(1):
        sub = state.subscribe(1)
    try:
        s = await pnl.refresh_area(1)
        assert s["realized"] == 31.5 and s["open"] == -6.5 and s["total"] == 25.0 and s["week"] == 80.0
        assert [a["spec"] for a in s["accounts"]] == ["DEMO11", "DEMO12"] and s["error"] == ""
        assert s["accounts"][0]["cash"] == 50011.0 and s["accounts"][0]["total"] == 7.25
        await asyncio.sleep(0)  # the broadcast is scheduled on the loop
        msg = sub.queue.get_nowait()
        assert msg["kind"] == "pnl" and msg["data"]["total"] == 25.0
        # unchanged figures → stored but not re-broadcast
        await pnl.refresh_area(1)
        await asyncio.sleep(0)
        assert sub.queue.empty()
        assert state.pnl(1)["total"] == 25.0
    finally:
        state.unsubscribe(sub, 1)
    assert state.subscriber_count(1) == 0


async def test_disconnected_or_failing_accounts(admin, monkeypatch):
    with context.use_area(1):
        bad = Sess(name="Bad", fail=True)
        off = Sess(name="Off", connected=False)
    _install(monkeypatch, bad, off)
    s = await pnl.refresh_area(1)
    assert s["accounts"] == [] and "DEMO11: boom" in s["error"] and off.calls == 0


async def test_api_and_status(client, monkeypatch):
    with context.use_area(1):
        sess = Sess()
    _install(monkeypatch, sess)
    assert (await client.get("/api/pnl")).json() == {}
    r = await client.get("/api/pnl?refresh=1")
    assert r.json()["total"] == 25.0
    assert (await client.get("/api/status")).json()["pnl"]["total"] == 25.0
    assert (await client.get("/api/pnl")).json()["accounts"][1]["spec"] == "DEMO12"


async def test_loop_respects_off_and_watchers(admin, monkeypatch):
    with context.use_area(1):
        sess = Sess()
    _install(monkeypatch, sess)
    with context.use_area(1):
        config.save_settings({"pnl_poll_seconds": 0})
    sleeps = []

    async def fake_sleep(d):
        sleeps.append(d)
        raise asyncio.CancelledError  # one iteration only
    monkeypatch.setattr(pnl.asyncio, "sleep", fake_sleep)
    try:
        await pnl.pnl_loop()
    except asyncio.CancelledError:
        pass
    assert sess.calls == 0 and sleeps == [pnl.IDLE_INTERVAL_S]   # off → no request
    with context.use_area(1):
        config.save_settings({"pnl_poll_seconds": 5})
    sub = state.subscribe(1)
    try:
        sleeps.clear()
        try:
            await pnl.pnl_loop()
        except asyncio.CancelledError:
            pass
        assert sess.calls == 2 and sleeps == [5.0]                  # watched → fast interval
    finally:
        state.unsubscribe(sub, 1)
    sleeps.clear()
    try:
        await pnl.pnl_loop()
    except asyncio.CancelledError:
        pass
    assert sleeps == [5.0]                                          # nobody watching, but trade alerts need positions
    with context.use_area(1):
        config.save_settings({"alert_on_trade_opened": False, "alert_on_trade_closed": False})
    sleeps.clear()
    try:
        await pnl.pnl_loop()
    except asyncio.CancelledError:
        pass
    assert sleeps == [pnl.IDLE_INTERVAL_S]                          # nobody watching, no trade alerts → idle cadence


async def test_intraday_drawdown_tracks_the_equity_peak(admin, monkeypatch):
    monkeypatch.setattr(pnl, "IDLE_SNAPSHOT_EVERY", 1)   # every tick refreshes flat accounts here
    """Intraday: threshold = highest equity seen (incl. open P&L) − size, capped."""
    from app import drawdown
    with context.use_area(1):
        sess = Sess(accounts=[{"id": 11, "spec": "APEX11"}],
                    snap={11: {"totalCashValue": 50000.0, "realizedPnL": 0, "openPnL": 0, "weekRealizedPnL": 0}})
    sess.risk = [{"id": 11, "trailingMaxDrawdown": 2500, "trailingMaxDrawdownLimit": 52600, "trailingMaxDrawdownMode": "RealTime"}]
    _install(monkeypatch, sess)
    a = (await pnl.refresh_area(1))["accounts"][0]
    assert a["dd_mode"] == "Intraday" and a["dd_size"] == 2500.0 and a["dd_cap"] == 52600.0
    assert a["dd_peak"] == 50000.0 and a["dd_level"] == 47500.0 and a["dd_room"] == 2500.0 and a["dd_seeded"] is False
    # equity rises with an open position → peak and threshold ratchet up
    sess.snap[11] = {"totalCashValue": 50000.0, "realizedPnL": 0, "openPnL": 800.0, "weekRealizedPnL": 0}
    a = (await pnl.refresh_area(1))["accounts"][0]
    assert a["dd_peak"] == 50800.0 and a["dd_level"] == 48300.0 and a["dd_room"] == 2500.0
    # equity falls back → peak stays, room shrinks
    sess.snap[11] = {"totalCashValue": 50000.0, "realizedPnL": 0, "openPnL": -900.0, "weekRealizedPnL": 0}
    a = (await pnl.refresh_area(1))["accounts"][0]
    assert a["dd_peak"] == 50800.0 and a["dd_level"] == 48300.0 and a["dd_room"] == 800.0
    # the cap: beyond 52 600 the threshold no longer trails
    sess.snap[11] = {"totalCashValue": 54000.0, "realizedPnL": 0, "openPnL": 0, "weekRealizedPnL": 0}
    a = (await pnl.refresh_area(1))["accounts"][0]
    assert a["dd_peak"] == 54000.0 and a["dd_level"] == 50100.0 and a["dd_room"] == 3900.0
    # persisted: survives a process restart (state lives in the area settings)
    st = config.load_settings(area_id=1)["dd_state"]["11"]
    assert st["peak"] == 54000.0 and st["since"]


async def test_eod_drawdown_uses_session_closes_and_journal_history(admin, monkeypatch):
    monkeypatch.setattr(pnl, "IDLE_SNAPSHOT_EVERY", 1)   # every tick refreshes flat accounts here
    from datetime import datetime, timezone
    from app import drawdown
    with context.use_area(1):
        sess = Sess(accounts=[{"id": 11, "spec": "APEX11"}],
                    snap={11: {"totalCashValue": 50400.0, "realizedPnL": 400.0, "openPnL": -300.0, "weekRealizedPnL": 0}})
    sess.risk = [{"id": 11, "trailingMaxDrawdown": 3000, "trailingMaxDrawdownMode": "EOD"}]
    _install(monkeypatch, sess)
    # journal knows a higher close from last week → that is the peak, not today's balance
    db.upsert_journal_snapshot(1, {"account_id": 11, "account_spec": "APEX11", "day": "2026-09-02", "total_cash": 51000.0,
                                   "realized_pnl": 0, "open_pnl": 0, "week_realized_pnl": 0, "total_pnl": 0})
    a = (await pnl.refresh_area(1))["accounts"][0]
    assert a["dd_mode"] == "EOD" and a["dd_peak"] == 51000.0 and a["dd_level"] == 48000.0
    assert a["dd_room"] == 2100.0                     # equity 50 100 − 48 000
    # intraday equity highs do NOT move an EOD threshold …
    sess.snap[11] = {"totalCashValue": 50400.0, "realizedPnL": 400.0, "openPnL": 2000.0, "weekRealizedPnL": 0}
    a = (await pnl.refresh_area(1))["accounts"][0]
    assert a["dd_peak"] == 51000.0
    # … but the balance at the session close does, once the session has rolled (17:00 New York)
    sess.snap[11] = {"totalCashValue": 52000.0, "realizedPnL": 2000.0, "openPnL": 0, "weekRealizedPnL": 0}
    await pnl.refresh_area(1)                          # today's close candidate: 52 000
    later = datetime.now(timezone.utc) + __import__("datetime").timedelta(days=1)
    real_apply = drawdown.apply
    monkeypatch.setattr(drawdown, "apply", lambda area, snap, rec, now=None: real_apply(area, snap, rec, now=later))
    sess.snap[11] = {"totalCashValue": 51500.0, "realizedPnL": -500.0, "openPnL": 0, "weekRealizedPnL": 0}
    a = (await pnl.refresh_area(1))["accounts"][0]
    assert a["dd_peak"] == 52000.0 and a["dd_level"] == 49000.0 and a["dd_room"] == 2500.0


async def test_pinning_the_prop_firm_threshold(client, admin, monkeypatch):
    monkeypatch.setattr(pnl, "IDLE_SNAPSHOT_EVERY", 1)   # every tick refreshes flat accounts here
    with context.use_area(1):
        sess = Sess(accounts=[{"id": 11, "spec": "APEX11"}],
                    snap={11: {"totalCashValue": 50000.0, "realizedPnL": 0, "openPnL": 0, "weekRealizedPnL": 0}})
    sess.risk = [{"id": 11, "trailingMaxDrawdown": 2500, "trailingMaxDrawdownMode": "RealTime"}]
    _install(monkeypatch, sess)
    await pnl.refresh_area(1)
    # the firm shows a higher threshold (the account peaked before the bridge watched it)
    r = await client.post("/api/pnl/drawdown", json={"account_id": 11, "level": 48900})
    assert r.status_code == 200
    a = r.json()["accounts"][0]
    assert a["dd_seeded"] is True and a["dd_peak"] == 51400.0 and a["dd_level"] == 48900.0 and a["dd_room"] == 1100.0
    # a pinned threshold still ratchets up with new peaks, never down
    sess.snap[11] = {"totalCashValue": 50000.0, "realizedPnL": 0, "openPnL": 2000.0, "weekRealizedPnL": 0}
    a = (await pnl.refresh_area(1))["accounts"][0]
    assert a["dd_peak"] == 52000.0 and a["dd_level"] == 49500.0
    # reset → back to tracking from what is observed now
    r = await client.post("/api/pnl/drawdown", json={"account_id": 11, "level": None})
    a = r.json()["accounts"][0]
    assert a["dd_seeded"] is False and a["dd_peak"] == 52000.0
    # validation
    assert (await client.post("/api/pnl/drawdown", json={"account_id": 11, "level": "abc"})).status_code == 400
    assert (await client.post("/api/pnl/drawdown", json={"account_id": 999, "level": 1})).status_code == 404
    assert (await client.post("/api/pnl/drawdown", json={"level": 1})).status_code == 400


def test_session_day_rolls_at_17_new_york():
    from datetime import datetime
    from app import drawdown
    assert drawdown.session_day(datetime(2026, 9, 9, 16, 59, tzinfo=drawdown.ET)) == "2026-09-09"
    assert drawdown.session_day(datetime(2026, 9, 9, 21, 0, tzinfo=timezone.utc)) == "2026-09-10"   # 17:00 New York
    assert drawdown.normalize_mode("RealTime") == "Intraday" and drawdown.normalize_mode("eod") == "EOD"


async def test_risk_lookup_failure_never_breaks_pnl(admin, monkeypatch):
    with context.use_area(1):
        sess = Sess()
    async def boom(method, path, **kw):
        if path == "/userAccountAutoLiq/list":
            raise tradovate.TradovateError("403")
        return await Sess._request(sess, method, path, **kw)
    monkeypatch.setattr(sess, "_request", boom)
    _install(monkeypatch, sess)
    s = await pnl.refresh_area(1)
    assert len(s["accounts"]) == 2 and s["accounts"][0]["dd_room"] is None and s["error"] == ""


async def test_flat_accounts_are_snapshotted_only_every_nth_tick(admin, monkeypatch):
    """Accounts without a position cost one cash snapshot every n-th tick; the
    risk record is fetched once per cache window; positions once per login."""
    with context.use_area(1):
        sess = Sess(snap={aid: {"totalCashValue": 50000, "realizedPnL": 10.0, "openPnL": 0.0, "weekRealizedPnL": 0.0} for aid in (11, 12)})
    sess.risk_calls = 0
    orig = sess._request

    async def counting(method, path, **kw):
        if path == "/userAccountAutoLiq/list":
            sess.risk_calls += 1
        return await orig(method, path, **kw)
    sess._request = counting
    _install(monkeypatch, sess)
    monkeypatch.setattr(pnl, "IDLE_SNAPSHOT_EVERY", 3)
    await pnl.refresh_area(1)                       # tick 1: everything fresh
    assert sess.calls == 2 and sess.risk_calls == 1
    s2 = await pnl.refresh_area(1)                  # tick 2: flat → reused
    assert sess.calls == 2 and sess.risk_calls == 1 and len(s2["accounts"]) == 2
    await pnl.refresh_area(1)                       # tick 3: n-th tick → refreshed
    assert sess.calls == 4
    # an account with an open position is refreshed every tick
    sess._request_positions = [{"accountId": 11, "contractId": 901, "netPos": 1}]

    async def with_pos(method, path, **kw):
        if path == "/position/list":
            return list(sess._request_positions)
        return await counting(method, path, **kw)
    sess._request = with_pos
    await pnl.refresh_area(1)                       # tick 4: only DEMO11 (open) is fetched
    assert sess.calls == 5

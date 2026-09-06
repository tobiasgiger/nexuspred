"""Live account P&L (app/pnl.py): snapshot polling, change detection, stream push, API."""
from __future__ import annotations

import asyncio

from app import config, context, pnl, state, tradovate


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
    assert sleeps == [pnl.IDLE_INTERVAL_S]                          # nobody watching → idle cadence

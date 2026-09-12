"""alpha.71: per-webhook trading window, the shared leader feed of the copy
engine, journal imports for ProjectX and Rithmic logins."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS
from zoneinfo import ZoneInfo

import pytest

from app import config, context, copy as cp, journal, leader_feed, projectx, rithmic, signals, trade_window as tw, tradovate

ZH = ZoneInfo("Europe/Zurich")


# ------------------------------------------------------------ trading window
def test_window_normalize():
    w = tw.normalize({"enabled": True, "from": "8:5", "to": "17:00", "days": ["Mon", "TUE", "fri"], "tz": "Europe/Zurich"})
    assert w == {"enabled": True, "from": "08:05", "to": "17:00", "tz": "Europe/Zurich", "days": ["mon", "tue", "fri"]}
    assert tw.normalize(None)["enabled"] is False and tw.normalize({})["days"] == ["mon", "tue", "wed", "thu", "fri"]
    for bad in ({"enabled": True, "from": "25:00"}, {"enabled": True, "days": ["funday"]}, {"enabled": True, "tz": "Mars/Olympus"},
                {"enabled": True, "days": []}, {"enabled": True, "from": "09:00", "to": "09:00"}, "09:00-17:00", {"days": "mon"}):
        with pytest.raises(ValueError):
            tw.normalize(bad)


def test_window_is_open_day_and_overnight():
    w = tw.normalize({"enabled": True, "from": "08:00", "to": "17:00"})
    assert tw.is_open(w, now=datetime(2026, 9, 14, 9, 0, tzinfo=ZH)) == (True, "")                # Monday 09:00
    closed, why = tw.is_open(w, now=datetime(2026, 9, 12, 9, 0, tzinfo=ZH))                       # Saturday
    assert not closed and "sat" in why and "08:00–17:00 Europe/Zurich" in why
    assert tw.is_open(w, now=datetime(2026, 9, 14, 17, 0, tzinfo=ZH))[0] is False                  # end is exclusive
    assert tw.is_open(w, now=datetime(2026, 9, 14, 7, 59, tzinfo=ZH))[0] is False
    # the window's own timezone wins over the fallback; UTC 07:30 = 09:30 Zurich
    assert tw.is_open(w, now=datetime(2026, 9, 14, 7, 30, tzinfo=timezone.utc), default_tz="Europe/Zurich")[0] is True
    assert tw.is_open(w, now=datetime(2026, 9, 14, 7, 30, tzinfo=timezone.utc), default_tz="UTC")[0] is False
    ny = tw.normalize({"enabled": True, "from": "22:00", "to": "06:00", "days": ["fri"], "tz": "America/New_York"})
    et = ZoneInfo("America/New_York")
    assert tw.is_open(ny, now=datetime(2026, 9, 11, 23, 0, tzinfo=et))[0] is True                 # Friday night
    assert tw.is_open(ny, now=datetime(2026, 9, 12, 3, 0, tzinfo=et))[0] is True                  # Saturday 03:00, opened Friday
    assert tw.is_open(ny, now=datetime(2026, 9, 12, 7, 0, tzinfo=et))[0] is False
    assert tw.is_open(ny, now=datetime(2026, 9, 10, 23, 0, tzinfo=et))[0] is False                # Thursday night
    assert tw.is_open(None) == (True, "") and tw.is_open({"enabled": False}) == (True, "")
    assert tw.is_open({"enabled": True, "from": "zz"}) == (True, "")                              # malformed never blocks


async def test_window_saved_through_the_api(client, admin, webhook_factory):
    wh = webhook_factory("W")
    r = await client.put(f"/api/webhooks/{wh['id']}", json={"trade_window": {"enabled": True, "from": "9:00", "to": "16:30", "days": ["mon", "wed"], "tz": ""}})
    assert r.status_code == 200, r.text
    assert r.json()["trade_window"] == {"enabled": True, "from": "09:00", "to": "16:30", "tz": "", "days": ["mon", "wed"]}
    r = await client.put(f"/api/webhooks/{wh['id']}", json={"trade_window": {"enabled": True, "from": "9:00", "to": "9:00"}})
    assert r.status_code == 400 and "differ" in r.json()["detail"]
    assert config.load_settings()["webhooks"][0]["trade_window"]["to"] == "16:30"                # the bad edit changed nothing
    # travels with a settings export / import
    doc = (await client.get("/api/settings/export")).json()
    assert doc["settings"]["webhooks"][0]["trade_window"]["days"] == ["mon", "wed"]
    config.save_settings({"webhooks": []})
    assert (await client.post("/api/settings/import", json=doc)).status_code == 200
    assert config.load_settings()["webhooks"][0]["trade_window"]["from"] == "09:00"


async def test_window_blocks_entries_only(admin):
    with context.use_area(1):
        config.save_settings({"trading_enabled": True, "symbol_map": {"MNQ1!": "MNQZ6"}, "journal_timezone": "UTC"})
        now = datetime.now(timezone.utc)
        other_day = tw.DAYS[(now + timedelta(days=2)).weekday()]
        closed = tw.normalize({"enabled": True, "from": "00:00", "to": "23:59", "days": [other_day], "tz": "UTC"})
        wh = {"id": "w1", "name": "n", "strategy": "bracket", "accounts": [], "enabled": True, "default_qty": 1, "tp_qty": 1, "trade_window": closed}
        r = await signals.process({"action": "buy", "symbol": "MNQ1!"}, wh, trusted=True)
        assert r["status"] == "skipped" and r["reason"] == "trade_window" and "outside the trading window" in r["detail"]
        r = await signals.process({"action": "close_all", "symbol": "MNQ1!"}, wh, trusted=True)
        assert r["reason"] != "trade_window"                                                     # closes always run
        r = await signals.process({"action": "move_sl", "symbol": "MNQ1!"}, wh, trusted=True)
        assert r["reason"] != "trade_window"
        th = {**wh, "id": "w2", "strategy": "ts_hunter"}
        r = await signals.process({"event": "signal", "trade_id": "t1", "symbol": "MNQ1!", "side": "buy"}, th, trusted=True)
        assert r["reason"] == "trade_window"
        r = await signals.process({"event": "management", "trade_id": "t1", "symbol": "MNQ1!", "action": "full_close"}, th, trusted=True)
        assert r.get("reason") != "trade_window"
        r = await signals.process({"event": "signal", "trade_id": "t1", "symbol": "MNQ1!", "side": "buy", "risk": {"value": 1}}, th, simulate=True)
        assert r.get("reason") != "trade_window"                                                 # the simulator ignores the window
        opened = tw.normalize({"enabled": True, "from": "00:00", "to": "23:59", "days": list(tw.DAYS), "tz": "UTC"})
        r = await signals.process({"action": "buy", "symbol": "MNQ1!"}, {**wh, "trade_window": opened}, trusted=True)
        assert r["reason"] == "no_enabled_accounts"                                              # past the window, no accounts routed


# ---------------------------------------------------------- shared leader feed
class _Feed:
    def __init__(self, name="L", lid="lid-1"):
        self.name, self.lid = name, lid
        self.calls = {"positions": 0, "orders": 0}
        self.rows = [{"accountId": 1, "contractId": 901, "netPos": 2}]
        self.fail = False
        self.delay = 0.0

    async def positions_snapshot(self):
        self.calls["positions"] += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail:
            raise RuntimeError("offline")
        return [dict(r) for r in self.rows]

    async def orders_snapshot(self):
        self.calls["orders"] += 1
        return [{"id": 5, "accountId": 1, "ordStatus": "Working"}, "junk"]


async def test_feed_single_flight_ttl_and_isolation(monkeypatch):
    monkeypatch.setattr(leader_feed, "TTL_S", 0.8)
    s = _Feed()
    s.delay = 0.02
    (a, sa), (b, sb), (c, sc) = await asyncio.gather(*(leader_feed.snapshot(1, s, "positions") for _ in range(3)))
    assert s.calls["positions"] == 1 and [sa, sb, sc] == [False, True, True] and a == b == c
    a[0]["netPos"] = 99                                                                           # a copy: never leaks
    rows, shared = await leader_feed.snapshot(1, s, "positions")
    assert shared and rows[0]["netPos"] == 2
    assert leader_feed.stats(1, s) == {"hits": 3, "fetches": 1}
    monkeypatch.setattr(leader_feed, "TTL_S", 0.0)
    rows, shared = await leader_feed.snapshot(1, s, "positions")
    assert not shared and s.calls["positions"] == 2                                              # stale → fetched again
    orders, _ = await leader_feed.snapshot(1, s, "orders")
    assert orders == [{"id": 5, "accountId": 1, "ordStatus": "Working"}] and s.calls["orders"] == 1  # non-dict rows dropped
    # another area or login never shares
    monkeypatch.setattr(leader_feed, "TTL_S", 60.0)
    await leader_feed.snapshot(1, s, "positions")
    assert (await leader_feed.snapshot(2, s, "positions"))[1] is False
    assert (await leader_feed.snapshot(1, _Feed(lid="lid-2"), "positions"))[1] is False
    s.fail = True
    leader_feed.reset()
    with pytest.raises(RuntimeError):
        await leader_feed.snapshot(1, s, "positions")
    with pytest.raises(ValueError):
        await leader_feed.snapshot(1, s, "fills")


async def test_two_groups_on_one_login_share_the_poll(admin, monkeypatch):
    monkeypatch.setattr(leader_feed, "TTL_S", 0.8)
    s = _Feed()
    s.rows = []
    g1, g2 = cp.new_group("A"), cp.new_group("B")
    for g, spec in ((g1, "LEAD1"), (g2, "LEAD2")):
        g.update({"enabled": True, "leader": {"token_idx": 0, "spec": spec, "account_id": 1, "lid": "lid-1"}, "followers": []})
    with context.use_area(1):
        r1, r2 = cp.GroupRunner(1, g1), cp.GroupRunner(1, g2)
        await asyncio.gather(r1._poll_once(s, 1), r2._poll_once(s, 1))
        assert s.calls["positions"] == 1
        assert sorted((r1.diag.get("feed_shared", 0), r2.diag.get("feed_shared", 0))) == [0, 1]
        monkeypatch.setattr(leader_feed, "TTL_S", 0.0)
        await r1._poll_once(s, 1)
        await r2._poll_once(s, 1)
        assert s.calls["positions"] == 3                                                         # beyond the TTL every poll fetches


# ------------------------------------------------- journal: ProjectX + Rithmic
def _px_session(monkeypatch):
    import httpx
    from tests.test_projectx import Gateway
    gw = Gateway()
    client = httpx.AsyncClient(transport=httpx.MockTransport(gw.handle))
    monkeypatch.setattr(projectx.ProjectXSession, "_client", lambda self: client)
    monkeypatch.setattr(projectx, "REQUEST_SPACING_S", 0.0)
    entry = {"name": "Topstep", "broker": "projectx", "environment": "demo", "enabled": True, "px_user": "trader", "px_api_key": "k1",
             "px_firm": "topstep", "lid": "lg_px1", "accounts": []}
    config.save_settings({"token_accounts": [entry], "trading_enabled": True, "journal_history_days": 120}, area_id=1)
    gw.trades = {101: [
        {"id": 1, "accountId": 101, "contractId": "CON.F.US.MNQ.Z25", "creationTimestamp": "2026-09-10T13:30:00+00:00", "price": 21000.0, "profitAndLoss": None, "fees": 1.1, "side": 0, "size": 2, "voided": False, "orderId": 11},
        {"id": 2, "accountId": 101, "contractId": "CON.F.US.MNQ.Z25", "creationTimestamp": "2026-09-10T13:45:00Z", "price": 21010.0, "profitAndLoss": 40.0, "fees": 1.1, "side": 1, "size": 2, "voided": False, "orderId": 12},
        {"id": 3, "accountId": 101, "contractId": "CON.F.US.MNQ.Z25", "creationTimestamp": "2026-09-10T14:00:00Z", "price": 1.0, "fees": 0, "side": 1, "size": 1, "voided": True, "orderId": 13},
        {"id": 4, "accountId": 101, "contractId": "CON.F.US.MNQ.Z25", "creationTimestamp": "2026-09-11T09:00:00Z", "price": 21100.0, "fees": 0, "side": 1, "size": 1, "voided": False, "orderId": 14},   # still open
    ], 102: []}
    return gw, projectx.ProjectXSession(0, entry, area_id=1)


async def test_projectx_import_pairs_trades_fifo(admin, monkeypatch):
    gw, s = _px_session(monkeypatch)
    with context.use_area(1):
        await s.connect()
        out = await journal.import_projectx(1, s)
        assert out["login"] == "Topstep" and out["accounts"] == 2 and out["fills"] == 3 and out["fills_new"] == 3
        assert out["trades"] == 1 and out["trades_new"] == 1 and out["snapshots"] == 2 and out["history_error"] == ""
        assert out["diag"]["Trade/search PRAC-V2-1"]["count"] == 4
        since = out["diag"]["Trade/search PRAC-V2-1"]["since"]
        assert (datetime.now(timezone.utc).date() - datetime.fromisoformat(since).date()).days == 120   # first run: history days
        from app import db
        t = db.list_journal_trades(1)[0]
        assert t["account_spec"] == "PRAC-V2-1" and t["root"] == "MNQ" and t["side"] == "long" and t["qty"] == 2
        assert t["points"] == 10.0 and t["value_per_point"] == 2.0 and t["gross_pnl"] == 40.0 and t["fees"] == 2.2 and t["net_pnl"] == 37.8
        assert t["source"] == "fifo" and t["environment"] == "demo"
        # second run: overlap window only, nothing new
        out = await journal.import_projectx(1, s)
        assert out["fills_new"] == 0 and out["trades_new"] == 0
        assert (datetime.now(timezone.utc).date() - datetime.fromisoformat(out["diag"]["Trade/search PRAC-V2-1"]["since"]).date()).days == journal.OTHER_OVERLAP_DAYS
        # import_area dispatches by broker kind
        mgr = tradovate.manager_for(1)
        monkeypatch.setattr(mgr, "all", lambda: [s])
        rec = await journal.import_area(1)
        assert rec["status"] == "ok" and rec["logins"] == 1 and rec["trades_new"] == 0 and rec["fills"] == 3


async def test_rithmic_import_uses_fill_history_and_side_fees(admin, monkeypatch):
    from tests.test_rithmic import FakeRithmicClient

    class Client(FakeRithmicClient):
        async def get_fill_history(self, start, end, **kw):
            self.calls.append(("fills", {"account_id": kw.get("account_id"), "days": (end - start).days}))
            if kw.get("account_id") != "APEX-123":
                return []
            return [NS(symbol="MNQZ6", exchange="CME", transaction_type="BUY", fill_size=1, fill_price=21000.0, ssboe=1757500000, usecs=250000, fill_id="f1", basket_id="9001"),
                    NS(symbol="MNQZ6", exchange="CME", transaction_type="SELL", fill_size=1, fill_price=21005.0, ssboe=1757500300, usecs=0, fill_id="f2", basket_id="9002"),
                    NS(symbol="", exchange="CME", transaction_type="BUY", fill_size=1, fill_price=1.0, ssboe=1757500400, usecs=0, fill_id="f3", basket_id="9003"),
                    NS(symbol="MNQZ6", exchange="CME", transaction_type="BUY", fill_size=0, fill_price=1.0, ssboe=1757500500, usecs=0, fill_id="f4", basket_id="9004")]
    made = []

    def factory(self):
        c = Client(); made.append(c); return c
    monkeypatch.setattr(rithmic.RithmicSession, "_make_client", factory)
    entry = {"name": "Apex", "broker": "rithmic", "environment": "live", "enabled": True, "rithmic_user": "u", "rithmic_password": "p",
             "rithmic_system": "Apex", "rithmic_gateway": "chicago", "lid": "lg_r1", "accounts": []}
    with context.use_area(1):
        config.save_settings({"token_accounts": [entry], "trading_enabled": True, "journal_fee_per_side": 1.0, "journal_history_days": 30})
        s = rithmic.RithmicSession(0, entry, area_id=1)
        await s.connect()
        out = await journal.import_rithmic(1, s)
        assert out["accounts"] == 2 and out["fills"] == 2 and out["trades_new"] == 1 and out["history_error"] == ""
        assert [c for c in made[0].calls if c[0] == "fills"] == [("fills", {"account_id": "APEX-123", "days": 30}), ("fills", {"account_id": "APEX-456", "days": 30})]
        from app import db
        t = db.list_journal_trades(1)[0]
        assert t["account_spec"] == "APEX-123" and t["symbol"] == "MNQZ6" and t["root"] == "MNQ" and t["side"] == "long"
        assert t["entry_ts"].startswith("2025-09-10T10:26:40.250000") and t["points"] == 5.0 and t["gross_pnl"] == 10.0 and t["fees"] == 2.0 and t["net_pnl"] == 8.0
        assert out["diag"]["fill history APEX-123"]["count"] == 4

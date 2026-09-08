"""Trading journal: Tradovate import (pairs + FIFO fallback), reporting, API, schedule."""
from __future__ import annotations

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from app import config, context, db, journal, tradovate

ZH = ZoneInfo("Europe/Zurich")


# ------------------------------------------------------------ fake broker
class FakeSession:
    """Mimics TradovateSession for the importer: ``_request`` serves canned
    Tradovate entity lists, shaped like the real API responses."""

    def __init__(self, name="Login A", accounts=None, *, pairs=True, fail=(), history=False):
        self.name = name
        self.history = history  # serve the cash-balance log (past sessions) or an empty one
        self.environment = "demo"
        self.enabled = True
        self.accounts = accounts or [{"id": 11, "spec": "DEMO11", "enabled": True}]
        self.pairs = pairs
        self.fail = set(fail)
        self.calls = []
        self.data = {
            "/fill/list": [
                {"id": 1, "orderId": 100, "contractId": 5, "timestamp": "2026-09-01T13:30:00Z", "action": "Buy", "qty": 2, "price": 20000.0},
                {"id": 2, "orderId": 101, "contractId": 5, "timestamp": "2026-09-01T13:45:00Z", "action": "Sell", "qty": 2, "price": 20010.0},
                {"id": 3, "orderId": 102, "contractId": 5, "timestamp": "2026-09-02T14:00:00Z", "action": "Sell", "qty": 1, "price": 20100.0},
                {"id": 4, "orderId": 103, "contractId": 5, "timestamp": "2026-09-02T14:20:00Z", "action": "Buy", "qty": 1, "price": 20130.0},
                {"id": 9, "orderId": 109, "contractId": 5, "timestamp": "2026-09-02T15:00:00Z", "action": "Buy", "qty": 1, "price": 1.0},  # other account
            ],
            "/order/list": [
                {"id": 100, "accountId": 11, "contractId": 5}, {"id": 101, "accountId": 11, "contractId": 5},
                {"id": 102, "accountId": 11, "contractId": 5}, {"id": 103, "accountId": 11, "contractId": 5},
                {"id": 109, "accountId": 99, "contractId": 5},
            ],
            "/fillFee/list": [
                {"id": 1, "commission": 1.0, "exchangeFee": 0.7, "clearingFee": 0.1, "nfaFee": 0.04},
                {"id": 2, "commission": 1.0, "exchangeFee": 0.7, "clearingFee": 0.1, "nfaFee": 0.04},
                {"id": 3, "commission": 0.5, "exchangeFee": 0.35}, {"id": 4, "commission": 0.5, "exchangeFee": 0.35},
            ],
            "/fillPair/list": [
                {"id": 501, "positionId": 7, "buyFillId": 1, "sellFillId": 2, "qty": 2, "buyPrice": 20000.0, "sellPrice": 20010.0},
                {"id": 502, "positionId": 8, "buyFillId": 4, "sellFillId": 3, "qty": 1, "buyPrice": 20130.0, "sellPrice": 20100.0},
            ],
            "/position/list": [{"id": 7, "accountId": 11, "contractId": 5}, {"id": 8, "accountId": 11, "contractId": 5}],
            "/contract/items": [{"id": 5, "name": "MNQU6", "contractMaturityId": 50}, {"id": 6, "name": "MESU6", "contractMaturityId": 60}],
            "/contractMaturity/items": [{"id": 50, "productId": 500}, {"id": 60, "productId": 600}],
            "/product/items": [{"id": 500, "name": "MNQ", "valuePerPoint": 2.0}, {"id": 600, "name": "MES", "valuePerPoint": 5.0}],
            # --- the account's book: today's pair 501 plus two pairs from August
            "/cashBalanceLog/list": [
                {"id": 9001, "accountId": 11, "timestamp": "2026-08-20T14:00:00Z", "tradeDate": {"year": 2026, "month": 8, "day": 20},
                 "cashChangeType": "Commission", "fillId": 601, "delta": -1.32, "amount": 49998.68},
                {"id": 9002, "accountId": 11, "timestamp": "2026-08-20T14:30:00Z", "tradeDate": {"year": 2026, "month": 8, "day": 20},
                 "cashChangeType": "Commission", "fillId": 602, "delta": -1.32, "amount": 49997.36},
                {"id": 9003, "accountId": 11, "timestamp": "2026-08-20T14:30:00Z", "tradeDate": {"year": 2026, "month": 8, "day": 20},
                 "cashChangeType": "FillPair", "fillPairId": 701, "delta": 25.0, "amount": 50022.36},
                {"id": 9004, "accountId": 11, "timestamp": "2026-08-21T15:00:00Z", "tradeDate": {"year": 2026, "month": 8, "day": 21},
                 "cashChangeType": "FillPair", "fillPairId": 702, "delta": -12.5, "amount": 50009.86},
                {"id": 9005, "accountId": 11, "timestamp": "2026-09-01T13:45:00Z", "tradeDate": {"year": 2026, "month": 9, "day": 1},
                 "cashChangeType": "FillPair", "fillPairId": 501, "delta": 40.0, "amount": 50049.86},
                {"id": 9006, "accountId": 99, "timestamp": "2026-08-21T15:00:00Z", "tradeDate": {"year": 2026, "month": 8, "day": 21},
                 "cashChangeType": "FillPair", "fillPairId": 799, "delta": 5.0, "amount": 1.0},  # other account
            ],
            "/fillPair/items": [
                {"id": 501, "buyFillId": 1, "sellFillId": 2, "qty": 2, "buyPrice": 20000.0, "sellPrice": 20010.0},
                {"id": 701, "buyFillId": 601, "sellFillId": 602, "qty": 1, "buyPrice": 5600.0, "sellPrice": 5605.0},
                {"id": 702, "buyFillId": 604, "sellFillId": 603, "qty": 1, "buyPrice": 5602.5, "sellPrice": 5600.0},
            ],
            "/fill/items": [
                {"id": 1, "orderId": 100, "contractId": 5, "timestamp": "2026-09-01T13:30:00Z", "action": "Buy", "qty": 2, "price": 20000.0},
                {"id": 2, "orderId": 101, "contractId": 5, "timestamp": "2026-09-01T13:45:00Z", "action": "Sell", "qty": 2, "price": 20010.0},
                {"id": 601, "orderId": 800, "timestamp": "2026-08-20T14:00:00Z", "action": "Buy", "qty": 1, "price": 5600.0},
                {"id": 602, "orderId": 801, "timestamp": "2026-08-20T14:30:00Z", "action": "Sell", "qty": 1, "price": 5605.0},
                {"id": 603, "orderId": 802, "contractId": 6, "timestamp": "2026-08-21T14:40:00Z", "action": "Sell", "qty": 1, "price": 5600.0},
                {"id": 604, "orderId": 803, "contractId": 6, "timestamp": "2026-08-21T15:00:00Z", "action": "Buy", "qty": 1, "price": 5602.5},
            ],
            "/order/items": [{"id": 800, "accountId": 11, "contractId": 6}, {"id": 801, "accountId": 11, "contractId": 6}],
        }

    def has_token(self):
        return True

    async def _request(self, method, path, **kw):
        self.calls.append(path)
        if path in self.fail:
            raise tradovate.TradovateError(f"boom {path}")
        if path == "/fillPair/list" and not self.pairs:
            return []
        if path == "/cashBalanceLog/list" and not self.history:
            return []
        if path == "/cashBalance/getcashbalancesnapshot":
            return {"totalCashValue": 50123.45, "realizedPnL": 12.5, "openPnL": 0.0, "weekRealizedPnL": 40.0, "totalPnL": 123.0}
        return self.data.get(path, [])


async def _no_report(session, name, params, timezone_minutes=0):
    return {"data": ""}


def _install(monkeypatch, *sessions):
    mgr = tradovate.manager_for(1)
    monkeypatch.setattr(mgr, "all", lambda: list(sessions))
    if journal._request_report.__module__ == journal.__name__:  # not stubbed by the test itself
        monkeypatch.setattr(journal, "_request_report", _no_report)
    monkeypatch.setattr(journal, "_report_definitions", _no_defs)


async def _no_defs(session):
    return [{"name": "Performance", "params": [{"name": "startDate"}, {"name": "endDate"}, {"name": "account"}]}]


# ------------------------------------------------------------ unit pieces
def test_fifo_pairs_splits_partial_fills():
    fills = [
        {"id": 1, "timestamp": "2026-09-01T10:00:00Z", "action": "Buy", "qty": 3, "price": 100.0},
        {"id": 2, "timestamp": "2026-09-01T10:05:00Z", "action": "Sell", "qty": 1, "price": 101.0},
        {"id": 3, "timestamp": "2026-09-01T10:10:00Z", "action": "Sell", "qty": 2, "price": 102.0},
        {"id": 4, "timestamp": "2026-09-01T11:00:00Z", "action": "Sell", "qty": 1, "price": 90.0},   # opens a short
        {"id": 5, "timestamp": "2026-09-01T11:30:00Z", "action": "Buy", "qty": 1, "price": 85.0},
        {"id": 6, "timestamp": "2026-09-01T12:00:00Z", "action": "Buy", "qty": 1, "price": 80.0},    # stays open
    ]
    out = journal.fifo_pairs(fills)
    assert [(m["buy"]["id"], m["sell"]["id"], m["qty"]) for m in out] == [(1, 2, 1), (1, 3, 2), (5, 4, 1)]


def test_build_trade_short_and_fees():
    fees = {3: {"commission": 0.5, "exchangeFee": 0.35}, 4: {"commission": 0.5, "exchangeFee": 0.35}}
    buy = {"id": 4, "contractId": 5, "timestamp": "2026-09-02T14:20:00Z", "qty": 1}
    sell = {"id": 3, "contractId": 5, "timestamp": "2026-09-02T14:00:00Z", "qty": 1}
    t = journal.build_trade(pair_id="x", buy=buy, sell=sell, qty=1, buy_price=20130.0, sell_price=20100.0,
                            account={"id": 11, "spec": "DEMO11"}, symbol="MNQU6", value_per_point=2.0, fees=fees, source="t")
    assert t["side"] == "short" and t["entry_price"] == 20100.0 and t["exit_price"] == 20130.0
    assert t["points"] == -30.0 and t["gross_pnl"] == -60.0 and t["fees"] == 1.7 and t["net_pnl"] == -61.7
    assert t["root"] == "MNQ" and t["entry_ts"] < t["exit_ts"]


def test_next_run_local_time_and_dst():
    zone = ZH
    now = datetime(2026, 9, 6, 20, 0, tzinfo=timezone.utc)  # 22:00 CEST
    nxt = journal.next_run(now, "23:30", zone)
    assert nxt.astimezone(zone).strftime("%Y-%m-%d %H:%M") == "2026-09-06 23:30"
    assert nxt == datetime(2026, 9, 6, 21, 30, tzinfo=timezone.utc)
    now = datetime(2026, 9, 6, 22, 0, tzinfo=timezone.utc)  # 00:00 next day CEST → tomorrow
    assert journal.next_run(now, "23:30", zone).astimezone(zone).strftime("%m-%d %H:%M") == "09-07 23:30"
    now = datetime(2026, 12, 1, 12, 0, tzinfo=timezone.utc)  # winter: CET = UTC+1
    assert journal.next_run(now, "23:30", zone) == datetime(2026, 12, 1, 22, 30, tzinfo=timezone.utc)
    assert journal.next_run(now, "garbage", zone).astimezone(zone).strftime("%H:%M") == "23:30"


# --------------------------------------------------------------- import
async def test_import_uses_fill_pairs(admin, monkeypatch):
    sess = FakeSession()
    _install(monkeypatch, sess)
    rec = await journal.import_area(1, trigger="manual", user_email="admin@example.com")
    assert rec["status"] == "ok" and rec["trades_new"] == 2 and rec["fills_new"] == 4 and rec["snapshots"] == 1
    trades = db.list_journal_trades(1)
    assert [(t["side"], t["qty"], t["net_pnl"], t["source"]) for t in trades] == [
        ("long", 2, 40.0 - 3.68, "fillpair"), ("short", 1, -61.7, "fillpair")]
    assert trades[0]["symbol"] == "MNQU6" and trades[0]["account_spec"] == "DEMO11"
    # idempotent
    rec2 = await journal.import_area(1)
    assert rec2["trades_new"] == 0 and rec2["fills_new"] == 0 and len(db.list_journal_trades(1)) == 2
    assert db.list_journal_snapshots(1)[0]["total_cash"] == 50123.45
    imports = db.list_journal_imports(1)
    assert len(imports) == 2 and imports[1]["trigger"] == "manual" and imports[1]["by"] == "admin@example.com"
    with context.use_area(1):
        assert config.load_settings()["journal_last_import"]


async def test_import_falls_back_to_fifo(admin, monkeypatch):
    _install(monkeypatch, FakeSession(pairs=False))
    rec = await journal.import_area(1)
    assert rec["status"] == "ok" and rec["trades_new"] == 2
    trades = db.list_journal_trades(1)
    assert {t["source"] for t in trades} == {"fifo"}
    assert [(t["side"], t["net_pnl"]) for t in trades] == [("long", 36.32), ("short", -61.7)]


async def test_import_isolates_failing_login(admin, monkeypatch):
    bad = FakeSession(name="Broken", fail={"/fill/list"})
    _install(monkeypatch, bad, FakeSession(name="Good"))
    rec = await journal.import_area(1)
    assert rec["status"] == "partial" and "Broken" in rec["error"] and rec["trades_new"] == 2


async def test_import_without_logins(admin, monkeypatch):
    _install(monkeypatch)
    rec = await journal.import_area(1)
    assert rec["status"] == "error" and "no enabled" in rec["error"]


# ------------------------------------------------------------ reporting
def _trade(i, exit_ts, net, *, root="MNQ", account="A", side="long", qty=1):
    return {"id": i, "exit_ts": exit_ts, "entry_ts": exit_ts, "net_pnl": net, "gross_pnl": net + 1, "fees": 1.0,
            "qty": qty, "root": root, "symbol": root + "U6", "account_name": account, "account_spec": account,
            "account_id": 1, "side": side}


SAMPLE = [
    _trade(1, "2026-09-01T14:00:00+00:00", 100.0),
    _trade(2, "2026-09-01T18:00:00+00:00", -40.0),
    _trade(3, "2026-09-03T14:00:00+00:00", 60.0, root="MES"),
    _trade(4, "2026-09-08T14:00:00+00:00", -20.0, account="B"),
    _trade(5, "2026-09-30T22:30:00+00:00", 10.0),   # 00:30 Oct 1 in Zurich → October bucket
]


def test_summary_buckets_in_journal_timezone():
    days = journal.summary(SAMPLE, "day", ZH)
    assert [(d["bucket"], d["net_pnl"], d["cumulative"]) for d in days] == [
        ("2026-09-01", 60.0, 60.0), ("2026-09-03", 60.0, 120.0), ("2026-09-08", -20.0, 100.0), ("2026-10-01", 10.0, 110.0)]
    weeks = journal.summary(SAMPLE, "week", ZH)
    assert [w["bucket"] for w in weeks] == ["2026-W36", "2026-W37", "2026-W40"] and weeks[0]["start"] == "2026-08-31"
    months = journal.summary(SAMPLE, "month", ZH)
    assert [(m["bucket"], m["trades"], m["net_pnl"]) for m in months] == [("2026-09", 4, 100.0), ("2026-10", 1, 10.0)]
    assert days[0]["win_rate"] == 0.5 and days[0]["profit_factor"] == 2.5


def test_stats_breakdowns_and_streaks():
    st = journal.stats(SAMPLE, ZH)
    assert st["trades"] == 5 and st["net_pnl"] == 110.0 and st["wins"] == 3 and st["losses"] == 2
    assert st["max_drawdown"] == -40.0 and st["longest_win_streak"] == 1 and st["longest_loss_streak"] == 1
    assert st["trading_days"] == 4 and st["avg_per_day"] == 27.5
    assert {b["key"]: b["net_pnl"] for b in st["by_symbol"]} == {"MNQ": 50.0, "MES": 60.0}
    assert {b["key"]: b["trades"] for b in st["by_account"]} == {"A": 4, "B": 1}
    assert [e["equity"] for e in st["equity"]] == [100.0, 60.0, 120.0, 100.0, 110.0]
    assert st["expectancy"] == 22.0 and st["largest_loss"] == -40.0


def test_calendar_month():
    cal = journal.calendar(SAMPLE, 2026, 9, ZH)
    assert len(cal["days"]) == 30 and cal["net_pnl"] == 100.0 and cal["trades"] == 4
    by = {d["day"]: d for d in cal["days"]}
    assert by["2026-09-01"]["net_pnl"] == 60.0 and by["2026-09-01"]["trades"] == 2 and by["2026-09-02"]["trades"] == 0
    assert journal.calendar(SAMPLE, 2026, 10, ZH)["net_pnl"] == 10.0


def test_range_bounds():
    f, t = journal.range_bounds("week", ZH, today=date(2026, 9, 9))  # Wednesday
    assert f.startswith("2026-09-06T22:00:00") and t.startswith("2026-09-09T22:00:00")  # Mon 00:00 CEST … Thu 00:00 CEST
    assert journal.range_bounds("all", ZH) == ("", "")
    f, t = journal.range_bounds("ytd", ZH, today=date(2026, 9, 9))
    assert f.startswith("2025-12-31T23:00:00")  # Jan 1 00:00 CET


# ------------------------------------------------------------------ API
async def test_journal_api(client, monkeypatch):
    _install(monkeypatch, FakeSession())
    r = await client.post("/api/journal/import")
    assert r.status_code == 200 and r.json()["trades_new"] == 2
    ov = (await client.get("/api/journal/overview?frm=2026-09-01&to=2026-09-30&period=day")).json()
    assert ov["stats"]["trades"] == 2 and ov["stats"]["net_pnl"] == round(36.32 - 61.7, 2)
    assert [b["bucket"] for b in ov["summary"]] == ["2026-09-01", "2026-09-02"]
    assert ov["accounts"][0]["account_spec"] == "DEMO11" and ov["symbols"] == ["MNQ"] and ov["schedule"]["time"] == "23:30"
    assert (await client.get("/api/journal/overview?frm=2026-09-01&to=2026-09-30&account=DEMO11&symbol=mnq")).json()["stats"]["trades"] == 2
    assert (await client.get("/api/journal/overview?frm=2026-09-01&to=2026-09-30&side=short")).json()["stats"]["trades"] == 1
    cal = (await client.get("/api/journal/calendar?month=2026-09")).json()
    assert cal["trades"] == 2 and len(cal["days"]) == 30
    tr = (await client.get("/api/journal/trades?range=all&limit=1")).json()
    assert len(tr["items"]) == 1 and tr["next_before"]
    tid = tr["items"][0]["id"]
    r = await client.put(f"/api/journal/trades/{tid}", json={"note": "revenge trade", "tags": ["FOMO", " late "]})
    assert r.json()["note"] == "revenge trade" and r.json()["tags"] == ["fomo", "late"]
    assert (await client.put("/api/journal/trades/999", json={"note": "x"})).status_code == 404
    assert (await client.get("/api/journal/overview?frm=bad")).status_code == 400
    csv_text = (await client.get("/api/journal/export.csv?range=all")).text
    assert csv_text.startswith("id,exit_ts") and "revenge trade" in csv_text and csv_text.count("\n") == 3
    assert len((await client.get("/api/journal/imports")).json()) == 1
    assert (await client.get("/api/journal/snapshots")).json()[0]["realized_pnl"] == 12.5
    # settings: schedule keys editable, last-import stamp protected
    await client.post("/api/settings", json={"journal_import_time": "22:45", "journal_last_import": "hack"})
    with context.use_area(1):
        s = config.load_settings()
    assert s["journal_import_time"] == "22:45" and s["journal_last_import"] != "hack"


async def test_settings_validation(client):
    assert (await client.post("/api/settings", json={"journal_import_time": "25:00"})).status_code == 400
    assert (await client.post("/api/settings", json={"journal_timezone": "Mars/Olympus"})).status_code == 400
    r = await client.post("/api/settings", json={"journal_import_time": "7:5", "journal_timezone": "America/New_York"})
    assert r.status_code == 200 and r.json()["journal_import_time"] == "07:05" and r.json()["journal_timezone"] == "America/New_York"


# ------------------------------------------------------------- history
async def test_history_from_cash_balance_log(admin, monkeypatch):
    sess = FakeSession(history=True)
    _install(monkeypatch, sess)
    rec = await journal.import_area(1)
    assert rec["status"] == "ok", rec
    assert rec["trades_new"] == 2 and rec["history_pairs"] == 3 and rec["history_new"] == 2  # 501 is today's, already stored
    trades = db.list_journal_trades(1)
    assert [(t["exit_ts"][:10], t["symbol"], t["side"], t["source"]) for t in trades] == [
        ("2026-08-20", "MESU6", "long", "history"), ("2026-08-21", "MESU6", "short", "history"),
        ("2026-09-01", "MNQU6", "long", "fillpair"), ("2026-09-02", "MNQU6", "short", "fillpair")]
    h1, h2 = trades[0], trades[1]
    assert h1["gross_pnl"] == 25.0 and h1["fees"] == 2.64 and h1["net_pnl"] == 22.36   # book's P&L + per-fill fees from the log
    assert h1["value_per_point"] == 5.0 and h1["account_spec"] == "DEMO11"
    assert h2["gross_pnl"] == -12.5 and h2["fees"] == 0.0 and h2["entry_price"] == 5600.0 and h2["exit_price"] == 5602.5
    snaps = {(s["day"]): s for s in db.list_journal_snapshots(1, days=400)}
    assert snaps["2026-08-20"]["total_cash"] == 50022.36 and snaps["2026-08-20"]["realized_pnl"] == 25.0
    assert snaps["2026-08-21"]["realized_pnl"] == -12.5 and rec["history_snapshots"] == 3
    assert "/cashBalanceLog/list" in sess.calls and "/fillPair/items" in sess.calls
    # incremental: nothing left to do on the next run
    rec2 = await journal.import_area(1)
    assert rec2["history_pairs"] == 0 and rec2["history_new"] == 0 and len(db.list_journal_trades(1)) == 4
    assert db.list_journal_imports(1)[0]["history_new"] == 0 and db.list_journal_imports(1)[1]["history_new"] == 2


async def test_history_failure_does_not_break_session_import(admin, monkeypatch):
    _install(monkeypatch, FakeSession(history=True, fail={"/cashBalanceLog/list"}))
    rec = await journal.import_area(1)
    assert rec["trades_new"] == 2 and rec["history_new"] == 0 and rec["status"] == "partial" and "history" in rec["error"]


async def test_pairs_with_fills_outside_the_session_list(admin, monkeypatch):
    """A fill-pair list that reaches further back than the fill list: fills are fetched by id."""
    sess = FakeSession()
    sess.data["/fillPair/list"] = sess.data["/fillPair/list"] + [
        {"id": 701, "positionId": 9, "buyFillId": 601, "sellFillId": 602, "qty": 1, "buyPrice": 5600.0, "sellPrice": 5605.0}]
    _install(monkeypatch, sess)
    rec = await journal.import_area(1)
    assert rec["trades_new"] == 3 and "/fill/items" in sess.calls
    assert [t["symbol"] for t in db.list_journal_trades(1)][0] == "MESU6"


async def test_import_records_diagnostics_and_resolves_account_ids(admin, monkeypatch):
    sess = FakeSession(history=True, accounts=[{"spec": "DEMO11", "enabled": True}])  # saved without ids
    sess.data["/account/list"] = [{"id": 11, "name": "DEMO11", "active": True}]
    _install(monkeypatch, sess)
    rec = await journal.import_area(1)
    assert rec["trades_new"] == 2 and rec["history_new"] == 2 and "/account/list" in sess.calls
    import json
    diag = json.loads(db.list_journal_imports(1)[0]["detail"])["Login A"]
    assert diag["accounts"] == [{"id": 11, "spec": "DEMO11"}]
    assert diag["/fill/list"]["count"] == 5 and "price" in diag["/fill/list"]["columns"]
    assert diag["cash_log"]["entries"] == 6 and diag["cash_log"]["entries_for_my_accounts"] == 5
    assert diag["cash_log"]["change_types"]["FillPair"] == 4 and diag["cash_log"]["pairs_in_book"] == 3
    assert diag["cash_log"]["first_trade_date"] == "2026-08-20"
    r = await journal.import_area(1)
    assert r["status"] == "ok"


async def test_cash_log_with_tradeId_instead_of_fillPairId(admin, monkeypatch):
    sess = FakeSession(history=True)
    for e in sess.data["/cashBalanceLog/list"]:
        if "fillPairId" in e:
            e["tradeId"] = e.pop("fillPairId")
    _install(monkeypatch, sess)
    rec = await journal.import_area(1)
    assert rec["history_new"] == 2


# ------------------------------------------------- reporting-service history
PERF_CSV = ("symbol,_priceFormat,_priceFormatType,_tickSize,buyFillId,sellFillId,qty,buyPrice,sellPrice,pnl,boughtTimestamp,soldTimestamp,duration\r\n"
            "MNQU6,-2,0,0.25,7001,7002,1,20000.00,20010.00,$20.00,{d1} 15:30:00,{d1} 15:45:00,15min\r\n"
            "MESU6,-2,0,0.25,7004,7003,2,5610.00,5600.00,$(100.00),{d2} 16:20:00,{d2} 16:00:00,20min\r\n")


class ReportStub:
    """Stands in for journal._request_report: serves a Performance CSV whose
    rows fall inside the requested window, and records every request."""

    def __init__(self, rows_at=("2026-07-10", "2026-08-15"), fail=None):
        self.rows_at = rows_at
        self.fail = fail
        self.requests = []

    async def __call__(self, session, name, params, timezone_minutes=0):
        p = dict(params)
        self.requests.append((name, p["account"], p["startDate"], p["endDate"]))
        if self.fail:
            return {"errorText": self.fail}
        from datetime import datetime as _dt
        start = _dt.strptime(p["startDate"], "%m/%d/%Y").date()
        end = _dt.strptime(p["endDate"], "%m/%d/%Y").date()
        d1, d2 = self.rows_at
        inside = [d for d in (d1, d2) if start <= date.fromisoformat(d) <= end]
        if not inside:
            return {"data": PERF_CSV.split("\r\n")[0] + "\r\n"}  # header only
        text = PERF_CSV.split("\r\n")[0] + "\r\n"
        lines = PERF_CSV.split("\r\n")[1:3]
        for d, line in zip((d1, d2), lines):
            if d in inside:
                text += line.replace("{d1}", d.replace("-", "/")[5:] + "/" + d[:4]).replace("{d2}", d.replace("-", "/")[5:] + "/" + d[:4]) + "\r\n"
        return {"data": text}


async def test_history_from_performance_report(admin, monkeypatch):
    stub = ReportStub()
    monkeypatch.setattr(journal, "_request_report", stub)
    sess = FakeSession()
    _install(monkeypatch, sess)
    with context.use_area(1):
        config.save_settings({"journal_history_days": 200, "journal_fee_per_side": 1.0})
    rec = await journal.import_area(1)
    assert rec["status"] == "ok", rec
    # 200 days back from today → 7 windows of 30 days for the one account
    from datetime import datetime as _dt, timedelta as _td
    today = _dt.now(ZH).date()
    assert [r[1] for r in stub.requests] == ["DEMO11"] * 7 and stub.requests[0][0] == "Performance"
    assert stub.requests[0][2] == (today - _td(days=200)).strftime("%m/%d/%Y")
    assert stub.requests[0][3] == (today - _td(days=171)).strftime("%m/%d/%Y") and stub.requests[-1][3] == today.strftime("%m/%d/%Y")
    assert rec["history_new"] == 2 and rec["trades_new"] == 2
    hist = [t for t in db.list_journal_trades(1) if t["source"] == "report"]
    assert [(t["exit_ts"][:10], t["symbol"], t["side"], t["gross_pnl"], t["fees"], t["net_pnl"]) for t in hist] == [
        ("2026-07-10", "MNQU6", "long", 20.0, 2.0, 18.0), ("2026-08-15", "MESU6", "short", -100.0, 4.0, -104.0)]
    assert hist[0]["pair_id"] == "rpt:7001:7002" and hist[0]["value_per_point"] == 2.0
    with context.use_area(1):
        assert config.load_settings()["journal_report_cursor"] == {"DEMO11": today.isoformat()}
    import json as _json
    diag = _json.loads(rec["detail"])["Login A"]["reports"]["DEMO11"]
    assert diag["windows"] == 7 and diag["rows"] == 2 and diag["new"] == 2 and diag["columns"].startswith("symbol,")
    # incremental: one window from the cursor minus the overlap, nothing new
    stub.requests.clear()
    rec2 = await journal.import_area(1)
    assert len(stub.requests) == 1 and stub.requests[0][2] == (today - _td(days=3)).strftime("%m/%d/%Y") and rec2["history_new"] == 0
    assert len([t for t in db.list_journal_trades(1) if t["source"] == "report"]) == 2


async def test_report_error_is_reported_not_fatal(admin, monkeypatch):
    stub = ReportStub(fail="Report 'Performance' is not available")
    monkeypatch.setattr(journal, "_request_report", stub)
    _install(monkeypatch, FakeSession())
    rec = await journal.import_area(1)
    assert rec["trades_new"] == 2 and rec["history_new"] == 0 and rec["status"] == "partial"
    assert "not available" in rec["error"] and len(stub.requests) == 1  # stops after the first failing window
    with context.use_area(1):
        assert config.load_settings()["journal_report_cursor"] == {}  # nothing covered → cursor untouched


async def test_report_trades_dedup_against_api_pairs(admin, monkeypatch):
    """The same round trip arriving from the entity list (today) and the report is stored once."""
    stub = ReportStub(rows_at=("2026-09-01", "2026-09-02"))
    monkeypatch.setattr(journal, "_request_report", stub)
    sess = FakeSession()
    # make the report's first row the same fills as today's pair 501 (fill ids 1/2)
    global PERF_CSV
    orig = PERF_CSV
    PERF_CSV = PERF_CSV.replace("7001,7002,1,20000.00,20010.00,$20.00", "1,2,2,20000.00,20010.00,$40.00")
    try:
        _install(monkeypatch, sess)
        rec = await journal.import_area(1)
    finally:
        PERF_CSV = orig
    assert rec["trades_new"] == 2 and rec["history_new"] == 1  # only the MES row is new
    assert len(db.list_journal_trades(1)) == 3


async def test_report_window_shrinks_on_too_long_range(admin, monkeypatch):
    """The service caps the span per request: halve until accepted, remember the size."""
    inner = ReportStub(rows_at=("2026-08-30", "2026-09-01"))
    seen = []

    async def picky(session, name, params, timezone_minutes=0):
        p = dict(params)
        from datetime import datetime as _dt
        span = (_dt.strptime(p["endDate"], "%m/%d/%Y") - _dt.strptime(p["startDate"], "%m/%d/%Y")).days + 1
        seen.append(span)
        if span > 7:
            return {"errorText": "Too long range"}
        return await inner(session, name, params)
    monkeypatch.setattr(journal, "_request_report", picky)
    _install(monkeypatch, FakeSession())
    with context.use_area(1):
        config.save_settings({"journal_history_days": 20})
    rec = await journal.import_area(1)
    assert rec["status"] == "ok", rec["error"]
    assert seen[:3] == [21, 15, 7] and max(seen[3:]) <= 7          # 30-day span clipped to the 21-day range → 15 → 7 accepted
    assert rec["history_new"] == 2
    with context.use_area(1):
        s = config.load_settings()
    from datetime import datetime as _dt
    assert s["journal_report_window"] == 7 and s["journal_report_cursor"] == {"DEMO11": _dt.now(ZH).date().isoformat()}
    # next run starts with the remembered span, no probing
    seen.clear()
    await journal.import_area(1)
    assert seen and max(seen) <= 7


# ------------------------------------------------ duplicates across import sources
def _dup_trade(pair_id, source, *, note="", price=(20000.0, 20010.0), exit_ts="2026-09-08T10:38:51+00:00"):
    return {"pair_id": pair_id, "source": source, "account_id": 7, "account_spec": "APEX1", "account_name": "L1",
            "environment": "demo", "contract_id": 1, "symbol": "MGCZ6", "root": "MGC", "side": "short", "qty": 4,
            "entry_price": price[0], "exit_price": price[1], "entry_ts": "2026-09-08T10:30:00+00:00", "exit_ts": exit_ts,
            "entry_fill_id": 0, "exit_fill_id": 0, "points": 3.0, "value_per_point": 10.0, "gross_pnl": 120.0,
            "fees": 5.36, "net_pnl": 114.64}


def test_report_and_csv_trades_use_their_own_key_family(admin):
    from app import journal_csv
    from zoneinfo import ZoneInfo
    acct = {"id": 7, "spec": "APEX1", "name": "L1", "environment": "demo"}
    csv_text = "symbol,qty,buyPrice,sellPrice,boughtTimestamp,soldTimestamp,buyFillId,sellFillId,pnl\nMGCZ6,4,4442.6,4439.6,09/08/2026 12:30:00,09/08/2026 12:38:51,111,222,$120.00\n"
    rpt = journal_csv.parse(csv_text, zone=ZoneInfo("UTC"), account=acct, source="report")["trades"][0]
    csv = journal_csv.parse(csv_text, zone=ZoneInfo("UTC"), account=acct, source="csv")["trades"][0]
    assert rpt["pair_id"] == "rpt:111:222" and csv["pair_id"] == "csv:111:222"
    # the same round trip from the live fill-pair import is recognised as the same trade
    assert db.upsert_journal_trade(1, rpt) == 1
    live = {**rpt, "pair_id": "pair:111:222", "source": "history"}
    assert db.find_similar_journal_trade(1, live) is not None


def test_dedupe_collapses_cross_source_duplicates_and_keeps_notes(admin):
    # the pair the user saw twice: fill-pair import + Performance report
    assert db.upsert_journal_trade(1, _dup_trade("pair:1:2", "history")) == 1
    assert db.upsert_journal_trade(1, _dup_trade("rpt:900:901", "report")) == 1
    rid = [t for t in db.list_journal_trades(1) if t["source"] == "report"][0]["id"]
    db.update_journal_trade_note(1, rid, "scalp after CPI", ["news"])
    # a legit split fill: same source + family, different key → must survive
    assert db.upsert_journal_trade(1, _dup_trade("pair:3:4", "history")) == 1
    # a different trade one minute later → untouched
    assert db.upsert_journal_trade(1, _dup_trade("rpt:905:906", "report", exit_ts="2026-09-08T10:39:51+00:00")) == 1
    # same broker fills reported with a 2 h clock offset by the report → still the same trade
    assert db.upsert_journal_trade(1, {**_dup_trade("pair:50:51", "history", exit_ts="2026-09-08T12:00:00+00:00"), "entry_fill_id": 50, "exit_fill_id": 51}) == 1
    assert db.upsert_journal_trade(1, {**_dup_trade("rpt:50:51", "report", exit_ts="2026-09-08T14:00:00+00:00"), "entry_fill_id": 50, "exit_fill_id": 51}) == 1
    removed = db.dedupe_journal_trades(1)
    assert removed == 2
    left = db.list_journal_trades(1)
    assert len(left) == 4 and not any(t["pair_id"] == "rpt:50:51" for t in left)
    keep = [t for t in left if t["exit_ts"] == "2026-09-08T10:38:51+00:00"]
    assert sorted(t["pair_id"] for t in keep) == ["pair:1:2", "pair:3:4"]      # history survives, split fill kept
    survivor = [t for t in keep if t["pair_id"] == "pair:1:2"][0]
    assert survivor["note"] == "scalp after CPI" and survivor["tags"] == ["news"]  # note carried over
    assert db.dedupe_journal_trades(1) == 0                                   # idempotent


async def test_dedupe_endpoint_and_startup_one_shot(client, admin, monkeypatch):
    db.upsert_journal_trade(1, _dup_trade("pair:1:2", "history"))
    db.upsert_journal_trade(1, _dup_trade("rpt:9:8", "report"))
    r = await client.post("/api/journal/dedupe")
    assert r.status_code == 200 and r.json() == {"removed": 1}
    assert (await client.post("/api/journal/dedupe")).json() == {"removed": 0}

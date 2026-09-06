"""Back-fill from Tradovate CSV exports (app/journal_csv.py + /api/journal/import-csv)."""
from __future__ import annotations

from zoneinfo import ZoneInfo

import pytest

from app import db, journal, journal_csv
from tests.test_journal import FakeSession, _install

ZH = ZoneInfo("Europe/Zurich")
ACC = {"id": 11, "spec": "DEMO11", "name": "DEMO11", "environment": "demo"}

PERFORMANCE = """symbol,_priceFormat,_priceFormatType,_tickSize,buyFillId,sellFillId,qty,buyPrice,sellPrice,pnl,boughtTimestamp,soldTimestamp,duration
MNQU6,-2,0,0.25,1,2,2,20000.00,20010.00,$40.00,09/01/2026 15:30:00,09/01/2026 15:45:00,15min
MNQU6,-2,0,0.25,4,3,1,20130.00,20100.00,$(60.00),09/02/2026 16:20:00,09/02/2026 16:00:00,20min
MGCZ6,-2,0,0.1,7,8,1,2400.5,2401.0,$5.00,09/03/2026 10:00:00,09/03/2026 10:30:00,30min
BADROW,,,,,,,,,,,,
"""

ORDERS = """orderId,Account,Order ID,B/S,Contract,Product,avgPrice,filledQty,Fill Time,lastCommandId,Status,Text,Type,Limit Price,Stop Price,decimalLimit,decimalStop,Filled Qty,Avg Fill Price,Time in Force
100,DEMO11,100,Buy,MESU6,MES,5600.25,2,09/01/2026 15:30:00,1,Filled,,Market,,,,,2,5600.25,Day
101,DEMO11,101,Sell,MESU6,MES,5602.25,2,09/01/2026 15:40:00,2,Filled,,Limit,5602.25,,,,2,5602.25,Day
102,DEMO11,102,Sell,MESU6,MES,,0,,3,Canceled,,Limit,5610,,,,0,,Day
"""

FILLS = "Fill ID;Timestamp;Contract;B/S;Qty;Price\n501;2026-09-04 14:00:00;MNQU6;Sell;1;20100.00\n502;2026-09-04 14:10:00;MNQU6;Buy;1;20090.00\n"


def test_money_and_timestamps():
    assert journal_csv._money("$40.00") == 40.0 and journal_csv._money("$(60.00)") == -60.0
    assert journal_csv._money("-$1,234.50") == -1234.5 and journal_csv._money("") is None
    assert journal_csv.parse_ts("09/01/2026 15:30:00", ZH) == "2026-09-01T13:30:00+00:00"   # CEST → UTC
    assert journal_csv.parse_ts("2026-09-01T13:30:00Z", ZH) == "2026-09-01T13:30:00+00:00"
    assert journal_csv.parse_ts("nonsense", ZH) == ""


def test_parse_performance_export():
    out = journal_csv.parse(PERFORMANCE, zone=ZH, account=ACC, fee_per_side=0.92)
    assert out["format"] == "performance" and out["rows"] == 4 and len(out["skipped"]) == 1
    t1, t2, t3 = out["trades"]
    assert (t1["side"], t1["qty"], t1["gross_pnl"], t1["fees"], t1["net_pnl"]) == ("long", 2, 40.0, 3.68, 36.32)
    assert t1["pair_id"] == "pair:1:2" and t1["exit_ts"] == "2026-09-01T13:45:00+00:00" and t1["source"] == "csv"
    assert t1["value_per_point"] == 2.0  # derived from the export's pnl
    assert (t2["side"], t2["gross_pnl"], t2["entry_price"], t2["exit_price"]) == ("short", -60.0, 20100.0, 20130.0)
    assert t3["root"] == "MGC" and t3["value_per_point"] == 10.0 and t3["account_spec"] == "DEMO11"


def test_parse_orders_export_fifo():
    out = journal_csv.parse(ORDERS, zone=ZH, account=ACC)
    assert out["format"] == "orders" and len(out["trades"]) == 1
    t = out["trades"][0]
    assert (t["symbol"], t["side"], t["qty"], t["points"], t["gross_pnl"]) == ("MESU6", "long", 2, 2.0, 20.0)
    assert t["pair_id"] == "ord:100:101:2" and t["value_per_point"] == 5.0


def test_parse_fills_export_semicolon():
    out = journal_csv.parse(FILLS, zone=ZH, account=ACC)
    assert out["format"] == "fills" and out["trades"][0]["side"] == "short" and out["trades"][0]["gross_pnl"] == 20.0


def test_unrecognised_csv():
    with pytest.raises(journal_csv.CsvError):
        journal_csv.parse("a,b,c\n1,2,3\n", zone=ZH, account=ACC)


async def test_csv_import_dedups_against_api_import(admin, monkeypatch):
    _install(monkeypatch, FakeSession())
    await journal.import_area(1)                       # API: pairs keyed pair:1:2 and pair:4:3
    assert len(db.list_journal_trades(1)) == 2
    rec = journal_csv.import_csv(1, PERFORMANCE, account=ACC, tz_name="Europe/Zurich")
    assert rec["trades"] == 3 and rec["trades_new"] == 1 and rec["duplicates"] == 2   # only MGC is new
    assert len(db.list_journal_trades(1)) == 3
    # the same Orders file twice → exact-key duplicates; the same MES round trip
    # arriving as a Performance row (different key family) → fuzzy duplicate
    assert journal_csv.import_csv(1, ORDERS, account=ACC)["trades_new"] == 1
    assert journal_csv.import_csv(1, ORDERS, account=ACC)["duplicates"] == 1
    perf_mes = ("symbol,buyFillId,sellFillId,qty,buyPrice,sellPrice,pnl,boughtTimestamp,soldTimestamp\n"
                "MESU6,55,56,2,5600.25,5602.25,$20.00,09/01/2026 15:30:00,09/01/2026 15:40:00\n")
    rec = journal_csv.import_csv(1, perf_mes, account=ACC)
    assert rec["trades_new"] == 0 and rec["duplicates"] == 1
    # two genuinely identical 1-lot split fills stay two trades (same family, distinct ids)
    twins = ("symbol,buyFillId,sellFillId,qty,buyPrice,sellPrice,pnl,boughtTimestamp,soldTimestamp\n"
             "MNQU6,70,71,1,20000,20005,$10.00,09/05/2026 15:30:00,09/05/2026 15:31:00\n"
             "MNQU6,72,73,1,20000,20005,$10.00,09/05/2026 15:30:00,09/05/2026 15:31:00\n")
    assert journal_csv.import_csv(1, twins, account=ACC)["trades_new"] == 2
    assert db.list_journal_imports(1)[0]["trigger"] == "csv"


async def test_csv_upload_endpoint(client):
    files = {"file": ("Performance.csv", PERFORMANCE.encode(), "text/csv")}
    r = await client.post("/api/journal/import-csv", files=files, data={"account": "Apex 50k", "fee_per_side": "1.0"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["format"] == "performance" and body["trades_new"] == 3 and body["skipped"] == 1
    trades = db.list_journal_trades(1)
    assert trades[0]["account_name"] == "Apex 50k" and trades[0]["account_id"] < 0 and trades[0]["environment"] == "csv"
    assert trades[0]["fees"] == 4.0  # 2 contracts × 2 sides × $1
    ov = (await client.get("/api/journal/overview?range=all")).json()
    assert ov["accounts"][0]["account_name"] == "Apex 50k" and ov["stats"]["trades"] == 3
    # validation
    assert (await client.post("/api/journal/import-csv", files=files, data={"account": ""})).status_code == 400
    assert (await client.post("/api/journal/import-csv", files={"file": ("x.csv", b"a,b\n1,2\n", "text/csv")}, data={"account": "A"})).status_code == 400
    assert (await client.post("/api/journal/import-csv", data={"account": "A"})).status_code == 400
    # bigger than the default body cap but under the upload cap
    big = PERFORMANCE + "".join(f"MNQU6,-2,0,0.25,{i},{i + 1},1,20000,20001,$2.00,09/01/2026 15:30:00,09/01/2026 15:31:00,1min\n" for i in range(1000, 8000, 2))
    assert len(big) > 256 * 1024
    r = await client.post("/api/journal/import-csv", files={"file": ("big.csv", big.encode(), "text/csv")}, data={"account": "A"})
    assert r.status_code == 200 and r.json()["trades_new"] == 3500


async def test_csv_account_resolves_configured_trade_account(client):
    from app import config, context
    with context.use_area(1):
        config.save_settings({"token_accounts": [{"name": "L", "environment": "live", "enabled": True,
                                                  "accounts": [{"spec": "LIVE77", "id": 77, "enabled": True}]}]})
    r = await client.post("/api/journal/import-csv", files={"file": ("p.csv", PERFORMANCE.encode(), "text/csv")}, data={"account": "LIVE77"})
    t = db.list_journal_trades(1)[0]
    assert r.status_code == 200 and t["account_id"] == 77 and t["environment"] == "live"

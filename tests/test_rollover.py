"""Contract-rollover warnings (app/rollover.py)."""
from __future__ import annotations

from datetime import date

import pytest

from app import alerts, config, context, db, rollover, state


@pytest.mark.parametrize("name, expect", [
    ("MNQU6", ("MNQ", 9, 2026)), ("mnqu6", ("MNQ", 9, 2026)), ("ESZ26", ("ES", 12, 2026)),
    ("GCM6", ("GC", 6, 2026)), ("6EH7", ("6E", 3, 2027)), ("M2KU6", ("M2K", 9, 2026)),
    ("MNQ", None), ("MNQ1!", None), ("ES", None), ("", None), ("U6", None),
])
def test_parse_contract(name, expect):
    assert rollover.parse_contract(name, today=date(2026, 9, 6)) == expect


def test_single_digit_year_wraps_decade():
    assert rollover.parse_contract("ESH1", today=date(2029, 12, 1)) == ("ES", 3, 2031)
    assert rollover.parse_contract("ESZ9", today=date(2030, 1, 5)) == ("ES", 12, 2029)  # just expired, not 2039
    assert rollover.parse_contract("ESH0", today=date(2026, 9, 6)) == ("ES", 3, 2030)


@pytest.mark.parametrize("root, month, year, expect, kind", [
    ("MNQ", 9, 2026, date(2026, 9, 18), "expiry (3rd Friday)"),   # 3rd Friday Sep 2026
    ("ES", 12, 2026, date(2026, 12, 18), "expiry (3rd Friday)"),
    ("GC", 6, 2026, date(2026, 5, 29), "first notice"),           # last business day of May 2026
    ("CL", 7, 2026, date(2026, 6, 22), "last trade"),             # 3 business days before 25 Jun (Thu) → Mon 22
    ("6E", 9, 2026, date(2026, 9, 14), "last trade"),             # 3rd Wed 16 Sep − 2 bd
    ("BTC", 9, 2026, date(2026, 9, 25), "expiry (last Friday)"),
    ("ZN", 12, 2026, date(2026, 11, 30), "first notice"),
    ("XYZ", 9, 2026, date(2026, 9, 1), "contract month"),
])
def test_roll_dates(root, month, year, expect, kind):
    assert rollover.roll_date(root, month, year) == (expect, kind)


def test_next_contract():
    assert rollover.next_contract("MNQ", 9, 2026) == "MNQZ6"
    assert rollover.next_contract("ES", 12, 2026) == "ESH7"
    assert rollover.next_contract("GC", 6, 2026) == "GCN6"
    assert rollover.next_contract("CL", 12, 2026) == "CLF7"


def test_evaluate_thresholds():
    m = {"MNQ1!": "MNQU6", "ES1!": "ESZ6", "GC1!": "GCM6", "NQ1!": "NQ"}
    out = rollover.evaluate(m, warn_days=10, today=date(2026, 9, 10))
    assert [(w["tv_symbol"], w["stage"], w["days_left"]) for w in out] == [
        ("GC1!", "expired", -104), ("MNQ1!", "upcoming", 8)]
    assert out[1]["next"] == "MNQZ6" and out[1]["source"] == "estimate"
    # exact broker date wins over the estimate
    out = rollover.evaluate({"MNQ1!": "MNQU6"}, 10, date(2026, 9, 10), exact={"MNQU6": date(2026, 9, 30)})
    assert out == []
    assert rollover.evaluate(m, warn_days=0, today=date(2026, 8, 1)) == [
        {**rollover.evaluate(m, 0, date(2026, 8, 1))[0]}]  # only the expired GC


async def test_check_area_alerts_once_per_stage(admin, monkeypatch):
    aid = db.user_primary_area(admin["id"])
    sent: list[str] = []

    async def fake_alert(msg):
        sent.append(msg)
    monkeypatch.setattr(alerts, "contract_rollover", fake_alert)
    monkeypatch.setattr(rollover, "_exact_dates", lambda *a, **k: _empty())
    with context.use_area(aid):
        config.save_settings({"symbol_map": {"MNQ1!": "MNQU6", "ES1!": "ESZ6"}})

    w = await rollover.check_area(aid, today=date(2026, 9, 10))
    assert [x["contract"] for x in w] == ["MNQU6"] and len(sent) == 1
    assert "MNQU6" in sent[0] and "MNQZ6" in sent[0]
    assert state.rollover_warnings(aid)[0]["stage"] == "upcoming"
    with context.use_area(aid):
        assert any("Rollover: MNQ1!" in e["message"] for e in state.recent_events())

    # same day → no-op; next day, same stage → no second alert
    await rollover.check_area(aid, today=date(2026, 9, 10))
    await rollover.check_area(aid, today=date(2026, 9, 11))
    assert len(sent) == 1
    # expired → new stage → one more alert
    w = await rollover.check_area(aid, today=date(2026, 9, 20))
    assert w[0]["stage"] == "expired" and len(sent) == 2
    await rollover.check_area(aid, today=date(2026, 9, 21))
    assert len(sent) == 2
    # re-mapped → warning gone, memory cleared
    with context.use_area(aid):
        config.save_settings({"symbol_map": {"MNQ1!": "MNQZ6", "ES1!": "ESZ6"}})
    assert await rollover.check_area(aid, force=True, today=date(2026, 9, 21)) == []
    with context.use_area(aid):
        assert config.load_settings()["rollover_notified"] == {}


async def _empty():
    return {}


async def test_status_and_manual_check(client, monkeypatch):
    monkeypatch.setattr(rollover, "_exact_dates", lambda *a, **k: _empty())
    with context.use_area(1):
        config.save_settings({"symbol_map": {"MNQ1!": "MNQH24"}, "alert_on_rollover": False})  # March 2024 → long expired
    r = await client.post("/api/rollover/check")
    assert r.json()["rollover"][0]["stage"] == "expired"
    assert (await client.get("/api/status")).json()["rollover"][0]["contract"] == "MNQH24"
    # the generic settings form cannot poke the internal memory
    await client.post("/api/settings", json={"rollover_notified": {"X": "y"}, "rollover_warn_days": 5})
    with context.use_area(1):
        s = config.load_settings()
    assert s["rollover_warn_days"] == 5 and "X" not in s["rollover_notified"]


class _Sess:
    """A connected login whose contract listing the test controls."""
    def __init__(self, listing):
        self.name, self.listing = "L1", listing

    async def _request(self, method, path, **kw):
        if path == "/contract/suggest":
            return [c for c in self.listing if str(c["name"]).startswith(kw["params"]["t"])]
        if path == "/contractMaturity/item":
            return {"id": kw["params"]["id"], "expirationDate": "2026-12-18T14:30:00Z"}
        raise AssertionError(path)


async def test_broker_listing_picks_the_next_contract(admin, monkeypatch):
    from app import tradovate
    sess = _Sess([{"name": "MNQU6", "contractMaturityId": 1}, {"name": "MNQZ6", "contractMaturityId": 2},
                  {"name": "MNQH7", "contractMaturityId": 3}, {"name": "MNQZ5", "contractMaturityId": 0}])
    monkeypatch.setattr(tradovate.manager_for(1), "all", lambda: [sess])
    monkeypatch.setattr(rollover, "_exact_dates", lambda *a, **k: _empty())
    state.set_session_status("L1", connected=True)
    with context.use_area(1):
        config.save_settings({"symbol_map": {"MNQ1!": "MNQU6", "GC1!": "GCV6"}, "alert_on_rollover": False})
    w = await rollover.check_area(1, force=True, today=date(2026, 9, 22))
    by = {x["tv_symbol"]: x for x in w}
    assert by["MNQ1!"]["next"] == "MNQZ6" and by["MNQ1!"]["next_source"] == "broker" and by["MNQ1!"]["next_expiry"] == "2026-12-18"
    assert by["GC1!"]["next"] == "GCX6" and by["GC1!"]["next_source"] == "estimate"   # not in the listing → estimate


async def test_apply_rollover_updates_the_map_and_clears_the_warning(client, monkeypatch):
    monkeypatch.setattr(rollover, "_exact_dates", lambda *a, **k: _empty())
    with context.use_area(1):
        config.save_settings({"symbol_map": {"MNQ1!": "MNQH24", "ES1!": "ESH24"}, "alert_on_rollover": False})
    r = await client.get("/api/rollover?refresh=1")
    assert {w["tv_symbol"] for w in r.json()["rollover"]} == {"MNQ1!", "ES1!"}
    # a mapping that expired long ago proposes a contract that is still ahead, not the next dead month
    from datetime import datetime, timezone
    for w in r.json()["rollover"]:
        p = rollover.parse_contract(w["next"])
        assert p and rollover.roll_date(*p)[0] >= datetime.now(timezone.utc).date(), w
    # validation: unknown symbol, not a contract, different product
    for bad in ([{"tv_symbol": "NQ1!", "contract": "NQZ6"}], [{"tv_symbol": "MNQ1!", "contract": "MNQ"}], [{"tv_symbol": "MNQ1!", "contract": "ESZ6"}]):
        assert (await client.post("/api/rollover/apply", json={"items": bad})).status_code == 400, bad
    assert (await client.post("/api/rollover/apply", json={"items": []})).status_code == 400
    r = await client.post("/api/rollover/apply", json={"items": [{"tv_symbol": "MNQ1!", "contract": "mnqz6"}]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["changes"] == [{"tv_symbol": "MNQ1!", "from": "MNQH24", "to": "MNQZ6"}]
    assert body["symbol_map"] == {"MNQ1!": "MNQZ6", "ES1!": "ESH24"}
    assert [w["tv_symbol"] for w in body["rollover"]] == ["ES1!"]        # only the un-rolled one remains
    with context.use_area(1):
        s = config.load_settings()
        assert s["symbol_map"]["MNQ1!"] == "MNQZ6" and "MNQH24" not in s["rollover_notified"]
        assert any("Rollover applied" in e["message"] for e in state.recent_events())

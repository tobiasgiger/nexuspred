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

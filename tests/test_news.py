"""Economic calendar + news lock (app/news.py, /api/news)."""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import pytest

from app import config, context, news, signals


def _feed(now, **over):
    at = (now + timedelta(minutes=over.pop("in_min", 2))).isoformat()
    return {"title": "CPI m/m", "country": "USD", "impact": "High", "date": at, "forecast": "0.3%", "previous": "0.2%", **over}


def _prime(events):
    news._events = news._normalize_feed(events)
    news._fetched_at = time.monotonic()


def test_normalize_settings_and_manual_events():
    s = news.normalize({"enabled": True, "currencies": "usd, eur", "impacts": "high,medium", "before": "10", "after": 15,
                        "action": "flatten", "manual": [{"title": "Powell speech", "at": "2026-09-15T14:30:00Z"}]})
    assert s["currencies"] == ["EUR", "USD"] and s["impacts"] == ["High", "Medium"] and (s["before"], s["after"]) == (10, 15)
    assert s["action"] == "flatten" and s["manual"][0]["at"].startswith("2026-09-15T14:30:00")
    with pytest.raises(ValueError):
        news.normalize({"before": 999})
    with pytest.raises(ValueError):
        news.normalize({"manual": [{"title": "x", "at": "yesterday"}]})
    assert news.normalize(None)["enabled"] is False and news.normalize({"impacts": "nonsense"})["impacts"] == ["High"]


def test_windows_and_active_lock(admin):
    now = datetime(2026, 9, 14, 12, 30, tzinfo=timezone.utc)
    _prime([_feed(now, in_min=3), _feed(now, in_min=3, title="Low thing", impact="Low"), _feed(now, in_min=3, title="EUR thing", country="EUR"),
            _feed(now, in_min=60 * 30, title="NFP")])
    config.save_settings({"news_lock": news.normalize({"enabled": True, "before": 5, "after": 10})}, area_id=1)
    ws = news.windows(1, hours=72, now=now)
    assert [w["title"] for w in ws] == ["CPI m/m", "NFP"]                      # Low impact and EUR filtered out
    assert ws[0]["active"] and not ws[1]["active"]
    lock = news.active_lock(1, now=now)
    assert lock and lock["title"] == "CPI m/m" and lock["lock_until"].startswith("2026-09-14T12:43")
    assert news.active_lock(1, now=now + timedelta(minutes=14)) is None        # 3 + 10 min after → open again
    # disabled → never locks; manual events lock without the feed
    config.save_settings({"news_lock": news.normalize({"enabled": False})}, area_id=1)
    assert news.active_lock(1, now=now) is None
    news.reset()
    config.save_settings({"news_lock": news.normalize({"enabled": True, "manual": [{"title": "Powell", "at": (now + timedelta(minutes=1)).isoformat()}]})}, area_id=1)
    assert news.active_lock(1, now=now)["title"] == "Powell"


async def test_entries_are_blocked_closes_are_not(admin):
    now = datetime.now(timezone.utc)
    _prime([_feed(now, in_min=1)])
    with context.use_area(1):
        config.save_settings({"trading_enabled": True, "news_lock": news.normalize({"enabled": True}),
                              "symbol_map": {"MNQ1!": "MNQZ6"}})
        wh = {"id": "w1", "name": "n", "strategy": "bracket", "accounts": [], "enabled": True, "default_qty": 1, "tp_qty": 1}
        r = await signals.process({"action": "buy", "symbol": "MNQ1!"}, wh, trusted=True)
        assert r["status"] == "skipped" and r["reason"] == "news_lock" and r["event"] == "CPI m/m"
        r = await signals.process({"action": "close_all", "symbol": "MNQ1!"}, wh, trusted=True)
        assert r["reason"] != "news_lock"                                        # closes always run (here: no accounts)
        th = {"id": "w2", "name": "ts", "strategy": "ts_hunter", "accounts": [], "enabled": True}
        r = await signals.process({"event": "signal", "trade_id": "t1", "symbol": "MNQ1!", "side": "buy"}, th, trusted=True)
        assert r["reason"] == "news_lock"
        r = await signals.process({"event": "signal", "trade_id": "t1", "symbol": "MNQ1!", "side": "buy", "risk": {"value": 1}}, th, simulate=True)
        assert r.get("reason") != "news_lock"                                    # the simulator is never locked


async def test_api_settings_events_and_refresh(client, admin, monkeypatch):
    now = datetime.now(timezone.utc)

    async def fake_refresh(force=False):
        _prime([_feed(now, in_min=30)])
        return {"events": 1, "error": "", "cached": False}
    monkeypatch.setattr(news, "refresh", fake_refresh)
    r = await client.put("/api/news/settings", json={"enabled": True, "currencies": ["USD"], "before": 15, "after": 15})
    assert r.status_code == 200 and r.json()["settings"]["before"] == 15
    r = await client.get("/api/news?hours=48")
    body = r.json()
    assert body["status"]["enabled"] and body["events"][0]["title"] == "CPI m/m" and not body["events"][0]["active"]
    assert body["status"]["next"]["title"] == "CPI m/m"
    assert (await client.put("/api/news/settings", json={"before": -1})).status_code == 400
    r = await client.post("/api/news/refresh")
    assert r.status_code == 200 and r.json()["events"] == 1
    st = (await client.get("/api/status")).json()["news_lock"]
    assert st["enabled"] and st["active"] is None


async def test_loop_alerts_once_and_flattens_once(admin, monkeypatch):
    now = datetime.now(timezone.utc)
    _prime([_feed(now, in_min=1)])
    with context.use_area(1):
        config.save_settings({"news_lock": news.normalize({"enabled": True, "action": "flatten"})})
    sent, flattened = [], []

    async def fake_alert(title, currency, until, *, flatten=False):
        sent.append((title, flatten))
    monkeypatch.setattr(news.alerts, "news_lock", fake_alert)

    async def fake_flatten():
        flattened.append(1)
        return {"flattened": 0, "cancelled": 0, "accounts": 0, "errors": []}
    monkeypatch.setattr(signals, "flatten_all", fake_flatten)
    await news._tick_area(1)
    await news._tick_area(1)
    assert sent == [("CPI m/m", True)] and flattened == [1]


def test_feed_normalization_dedupes_and_orders():
    a = {"title": "CPI", "country": "usd", "impact": "high", "date": "2026-09-14T08:30:00-04:00"}
    b = {"title": "NFP", "country": "USD", "impact": "High", "date": "2026-09-11T08:30:00-04:00"}
    ev = news._normalize_feed([a, dict(a), b, {"title": "", "date": "x"}])
    assert [e["title"] for e in ev] == ["NFP", "CPI"] and ev[1]["at"] == "2026-09-14T12:30:00+00:00" and ev[1]["currency"] == "USD"

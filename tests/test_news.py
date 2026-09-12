"""Economic calendar + news lock (app/news.py, /api/news)."""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import pytest

from app import alerts, config, context, news, signals


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
    monkeypatch.setattr(alerts, "news_lock", fake_alert)

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


async def test_calendar_page_api_lists_everything_with_filters(client, admin, monkeypatch):
    now = datetime.now(timezone.utc)

    async def fake_refresh(force=False):
        _prime([_feed(now, in_min=60), _feed(now, in_min=120, title="Unemployment Claims", impact="Medium"),
                _feed(now, in_min=180, title="ECB Rate", country="EUR"), _feed(now, in_min=60 * 24 * 10, title="Far away")])
        return {"events": 4, "error": "", "cached": False}
    monkeypatch.setattr(news, "refresh", fake_refresh)
    await client.put("/api/news/settings", json={"enabled": True, "currencies": ["USD"], "impacts": ["High"], "before": 5, "after": 5,
                                                 "manual": [{"title": "Powell", "at": (now + timedelta(hours=5)).isoformat()}]})
    body = (await client.get("/api/news/calendar")).json()                      # default: next 7 days, everything
    assert [e["title"] for e in body["events"]] == ["CPI m/m", "Unemployment Claims", "ECB Rate", "Powell"]
    assert [e["relevant"] for e in body["events"]] == [True, False, False, True]  # USD/High and manual count for the lock
    assert body["events"][0]["lock_from"] and body["events"][1]["lock_from"] is None
    assert body["currencies"] == ["EUR", "USD"] and body["range"]["end"] > body["range"]["start"]
    body = (await client.get("/api/news/calendar?days=14")).json()
    assert "Far away" in [e["title"] for e in body["events"]]
    body = (await client.get("/api/news/calendar?relevant=true")).json()
    assert [e["title"] for e in body["events"]] == ["CPI m/m", "Powell"]
    body = (await client.get("/api/news/calendar?currencies=eur")).json()
    assert [e["title"] for e in body["events"]] == ["ECB Rate", "Powell"]         # manual events always shown
    body = (await client.get("/api/news/calendar?impacts=medium&q=claims")).json()
    assert [e["title"] for e in body["events"]] == ["Unemployment Claims"]              # the text filter applies to manual events too
    assert (await client.get("/api/news/calendar?start=nope")).status_code == 400


def test_feed_weeks_accumulate_and_a_redelivered_week_replaces_itself():
    now = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)
    old = news._normalize_feed([_feed(now, in_min=-7 * 24 * 60, title="Last week NFP"), _feed(now, in_min=60 * 24 * 100, title="ancient")])
    old[1]["at"] = (now - timedelta(days=100)).isoformat()                       # older than KEEP_DAYS
    new = news._normalize_feed([_feed(now, in_min=60, title="CPI"), _feed(now, in_min=120, title="Retail")])
    merged = news._merge(old, new)
    assert [e["title"] for e in merged] == ["Last week NFP", "CPI", "Retail"]
    # the same week again with one event dropped and one moved → replaced, not duplicated
    again = news._normalize_feed([_feed(now, in_min=90, title="CPI")])
    merged2 = news._merge(merged, again)
    assert [e["title"] for e in merged2] == ["Last week NFP", "CPI"] and merged2[1]["at"] == (now + timedelta(minutes=90)).isoformat()


# ------------------------------------------------------------ preview source
async def test_refresh_adds_a_preview_of_the_coming_weeks(admin, monkeypatch):
    import httpx
    from app import http
    now = datetime.now(timezone.utc)
    weekly = [_feed(now, in_min=60, title="CPI m/m"), _feed(now, in_min=120, title="Retail Sales")]
    weekly_to = weekly[-1]["date"]
    tv = {"status": "ok", "result": [
        {"title": "Fed Interest Rate Decision", "country": "US", "currency": "USD", "importance": 1, "date": (now + timedelta(days=5)).isoformat(), "forecast": 4.25, "previous": 4.5},
        {"title": "Inflation Rate YoY", "country": "GB", "currency": "GBP", "importance": 0, "date": (now + timedelta(days=6)).isoformat()},
        {"title": "already covered", "country": "US", "currency": "USD", "importance": 1, "date": (now + timedelta(minutes=90)).isoformat()},   # inside the weekly span
        {"title": "noise", "country": "US", "currency": "USD", "importance": -1, "date": (now + timedelta(days=7)).isoformat()},
    ]}

    def handler(req):
        if "faireconomy" in req.url.host:
            return httpx.Response(200, json=weekly)
        assert req.url.host == "economic-calendar.tradingview.com" and req.headers["origin"] == "https://www.tradingview.com"
        return httpx.Response(200, json=tv)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(http, "client", lambda name: client)
    r = await news.refresh(force=True)
    assert r["error"] == "" and r["events"] == 5
    titles = [(e["title"], e.get("source")) for e in news._events]
    assert ("CPI m/m", None) in titles and ("Fed Interest Rate Decision", "tv") in titles and ("noise", "tv") in titles
    assert not any(t == "already covered" for t, _ in titles)                 # the weekly file owns its own span
    fed = next(e for e in news._events if e["title"] == "Fed Interest Rate Decision")
    assert fed["impact"] == "High" and fed["currency"] == "USD" and fed["forecast"] == "4.25"
    st = news.status(1)
    assert st["preview_events"] == 3 and st["weekly_to"] == news._parse_ts(weekly_to).isoformat()
    rows = news.calendar(1, start=now, end=now + timedelta(days=10))
    assert [r["source"] for r in rows if r["title"] == "Fed Interest Rate Decision"] == ["tv"]
    # the weekly file catching up replaces the preview rows of that span
    weekly[:] = [_feed(now + timedelta(days=5), in_min=0, title="FOMC Statement")]
    r = await news.refresh(force=True)
    assert not any(e["title"] == "Fed Interest Rate Decision" for e in news._events)
    assert any(e["title"] == "FOMC Statement" for e in news._events) and any(e["title"] == "CPI m/m" for e in news._events)


async def test_preview_failure_keeps_the_weekly_rows(admin, monkeypatch):
    import httpx
    from app import http
    now = datetime.now(timezone.utc)

    def handler(req):
        if "faireconomy" in req.url.host:
            return httpx.Response(200, json=[_feed(now, in_min=60)])
        return httpx.Response(503, text="down")
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(http, "client", lambda name: client)
    r = await news.refresh(force=True)
    assert r["events"] == 1 and "preview" in r["error"]

"""Hot-path performance guarantees (see docs/PERFORMANCE.md): the settings
cache hands out cheap, isolated copies, and one webhook signal reads the
area's settings exactly once."""
from __future__ import annotations

import pytest

from app import alerts, config, context, news, signals, state
from tests.helpers import FakeExecutor
from tests.test_strategies import ENTRY, live, wh  # noqa: F401


def test_settings_copies_are_isolated(admin):
    config.save_settings({"symbol_map": {"MNQ1!": "MNQZ6"}})
    a = config.load_settings()
    a["symbol_map"]["MNQ1!"] = "mutated"
    a["webhooks"].append({"id": "x"})
    b = config.load_settings()
    assert b["symbol_map"] == {"MNQ1!": "MNQZ6"} and b["webhooks"] == []
    assert config.DEFAULT_SETTINGS["webhooks"] == []


def test_snapshot_is_refreshed_by_writes_and_invalidation(admin):
    assert config.load_settings()["trading_enabled"] is False
    assert 1 in config._snapshots                       # built lazily on the first read
    config.save_settings({"trading_enabled": True})
    assert config.load_settings()["trading_enabled"] is True
    config.update(lambda s: s.__setitem__("trading_enabled", False))
    assert config.load_settings()["trading_enabled"] is False
    config.invalidate()
    assert not config._snapshots and config.load_settings()["trading_enabled"] is False


def test_snapshot_preserves_types(admin):
    config.save_settings({"symbol_map": {"a": "b"}, "allowed_symbols": ["MNQ"], "trading_enabled": True})
    s = config.load_settings()
    assert isinstance(s["symbol_map"], dict) and isinstance(s["allowed_symbols"], list)
    assert s["trading_enabled"] is True and s.get("news_lock") == config.DEFAULT_SETTINGS["news_lock"]


async def test_one_settings_read_per_signal(live, monkeypatch):
    a = FakeExecutor("A")
    live.use(a)
    calls = []
    real = config.load_settings

    def counting(*args, **kw):
        calls.append(1)
        return real(*args, **kw)
    monkeypatch.setattr(config, "load_settings", counting)

    async def quiet(*args, **kw):
        return None
    monkeypatch.setattr(alerts, "trade_executed", quiet)
    r = await signals.process({**ENTRY, "sl": 90.0, "tp1": 110.0}, wh("bracket"))
    assert r["accounts"]
    assert len(calls) == 1
    calls.clear()
    r = await signals.process({"action": "close_all", "symbol": "MNQ1!"}, wh("bracket"))
    assert r["status"] == "ok" and len(calls) == 1


async def test_trade_alert_uses_the_signal_snapshot(admin, monkeypatch):
    config.save_settings({"alert_on_trade_executed": True, "alert_discord_enabled": False, "alert_push_enabled": False})
    calls = []
    monkeypatch.setattr(config, "load_settings", lambda *a, **k: calls.append(1) or {})
    s = {"alert_on_trade_executed": True, "alert_discord_enabled": False, "alert_push_enabled": False, "alert_accounts": []}
    await alerts.trade_executed("w", "buy", "MNQZ6", ["A"], settings=s)
    assert calls == []


def test_news_lock_check_uses_the_signal_snapshot(admin, monkeypatch):
    monkeypatch.setattr(config, "load_settings", lambda *a, **k: pytest.fail("settings were re-read"))
    assert news.active_lock(1, settings={"news_lock": {"enabled": False}}) is None


# ------------------------------------------------------------ polling cadence
async def test_each_area_keeps_its_own_pnl_cadence(admin, monkeypatch):
    from app import pnl
    calls = []

    async def fake_refresh(aid):
        calls.append(aid)
        return {"error": "", "accounts": []}
    monkeypatch.setattr(pnl, "refresh_area", fake_refresh)
    with context.use_area(1):
        config.save_settings({"pnl_poll_seconds": 5})
    assert await pnl._poll_area(1) == 5.0 and calls == [1]
    left = await pnl._poll_area(1)                       # not due yet: no broker poll, just the wait
    assert calls == [1] and 4.0 < left <= 5.0
    monkeypatch.setattr(pnl.time, "monotonic", lambda: pnl._next_due[1] + 0.01)
    assert await pnl._poll_area(1) == 5.0 and calls == [1, 1]


async def test_health_refreshes_a_session_only_when_due(admin, monkeypatch):
    from app import health

    class Sess:
        area_id, name = 1, "L"
        renews = 0
        def has_token(self): return True
        async def proactive_refresh(self): Sess.renews += 1
        async def health_check(self): pass
        def seconds_until_refresh(self, fallback=60): return 900.0
    sess = Sess()
    monkeypatch.setattr(state, "session_status", lambda name: {"connected": True})
    assert await health._session_due(sess, 60) == 900.0 and Sess.renews == 1
    left = await health._session_due(sess, 60)
    assert Sess.renews == 1 and 890 < left <= 900                  # skipped: not due


async def test_health_backs_off_a_failing_session(admin, monkeypatch):
    from app import health

    class Bad:
        area_id, name = 1, "B"
        def has_token(self): return True
        async def proactive_refresh(self): raise RuntimeError("bad token")
        async def health_check(self): pass
        def seconds_until_refresh(self, fallback=60): return 900.0
    monkeypatch.setattr(state, "session_status", lambda name: {"connected": False})
    delays = []
    for _ in range(5):
        delays.append(await health._refresh_session(Bad(), 60))
    assert delays == [60.0, 120.0, 240.0, 480.0, 600.0]

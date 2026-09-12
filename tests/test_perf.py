"""Hot-path performance guarantees (see docs/PERFORMANCE.md): the settings
cache hands out cheap, isolated copies, and one webhook signal reads the
area's settings exactly once."""
from __future__ import annotations

import pytest

from app import alerts, config, news, signals
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

"""Characterisation of alert triggers and channel behaviour (no network)."""
from __future__ import annotations

import pytest

from app import alerts, config, signals, state
from app.tradovate import TradovateSession
from tests.helpers import settle


@pytest.fixture
def capture(monkeypatch, admin):
    discord: list[str] = []
    email: list[tuple[str, str]] = []

    async def fake_discord(message):
        discord.append(message)

    async def fake_email(subject, body):
        email.append((subject, body))

    monkeypatch.setattr(alerts, "_send_discord", fake_discord)
    monkeypatch.setattr(alerts, "_send_email", fake_email)
    return discord, email


# ------------------------------------------------------- connection transitions
async def test_connection_alerts_fire_only_on_transitions(capture):
    # v5 fires connection alerts as background tasks (a slow SMTP handshake must
    # not stall the health loop), hence the settle() after each transition.
    discord, email = capture
    sess = TradovateSession(0, {"name": "L1", "environment": "demo"})
    await sess._set_connected(True, last_error="")       # first observation: silent
    await settle()
    assert discord == [] and email == []
    assert state.has_session("L1")
    await sess._set_connected(False, last_error="boom")  # lost
    await settle()
    assert len(discord) == 1 and "Connection lost" in discord[0] and "`L1`" in discord[0] and "boom" in discord[0]
    assert email[0][0] == "Fluxbridge: connection lost (L1)"
    await sess._set_connected(False, last_error="boom")  # still down: silent
    await settle()
    assert len(discord) == 1
    await sess._set_connected(True, last_error="")       # restored
    await settle()
    assert len(discord) == 2 and "Connection restored" in discord[1] and email[1][0] == "Fluxbridge: connection restored (L1)"
    await sess._set_connected(True, last_error="")       # unchanged: silent
    await settle()
    assert len(discord) == 2


async def test_first_observation_disconnected_is_silent(capture):
    discord, _ = capture
    await TradovateSession(0, {"name": "L2"})._set_connected(False, last_error="x")
    await settle()
    assert discord == []


# ------------------------------------------------------------- trigger toggles
async def test_trigger_toggles(capture):
    discord, email = capture
    config.save_settings({"alert_on_connection_lost": False, "alert_on_connection_restored": False,
                          "alert_on_trade_executed": False, "alert_on_webhook_failed": False,
                          "alert_on_discord_lost": False, "alert_on_discord_restored": False})
    await alerts.connection_lost("A", "demo", "e")
    await alerts.connection_restored("A", "demo")
    await alerts.trade_executed("W", "buy", "MNQ", ["A"])
    await alerts.webhook_failed("W", "r")
    await alerts.discord_listener_lost("e")
    await alerts.discord_listener_restored("u")
    assert discord == [] and email == []


async def test_channel_routing_per_trigger(capture):
    discord, email = capture
    await alerts.trade_executed("W", "buy", "MNQU6", ["A", "B"])
    assert discord == ["⚡ **Trade executed** — strategy `W`: BUY MNQU6 on A, B"] and email == []
    await alerts.webhook_failed("W", "bad payload")
    assert "Signal not executed" in discord[-1] and email[-1][0] == "Fluxbridge: signal not executed (W)"
    await alerts.discord_listener_lost("gateway")
    await alerts.discord_listener_restored("me#1")
    assert "offline" in discord[-2] and "online" in discord[-1] and len(email) == 3


# ------------------------------------------------------------- channel sends
class _FakeClient:
    """Stands in for the pooled ``app.http`` client (v5 posts via ``http.client()``)."""
    calls: list = []

    async def post(self, url, json=None, **kw):
        _FakeClient.calls.append((url, json))
        if url.endswith("/boom"):
            raise RuntimeError("network down")

        class R:
            status_code = 204
            text = ""
        return R()


async def test_discord_send_respects_toggle_url_and_mention(admin, monkeypatch):
    _FakeClient.calls.clear()
    monkeypatch.setattr(alerts.http, "client", lambda *a, **k: _FakeClient())
    await alerts._send_discord("m")  # disabled by default
    config.save_settings({"alert_discord_enabled": True})
    await alerts._send_discord("m")  # no url
    assert _FakeClient.calls == []
    config.save_settings({"alert_discord_webhook_url": "https://discord/hook"})
    await alerts._send_discord("m")
    assert _FakeClient.calls[-1] == ("https://discord/hook", {"content": "@everyone m"})
    config.save_settings({"alert_discord_mention_everyone": False})
    await alerts._send_discord("m")
    assert _FakeClient.calls[-1][1] == {"content": "m"}


async def test_discord_send_swallows_errors(admin, monkeypatch):
    monkeypatch.setattr(alerts.http, "client", lambda *a, **k: _FakeClient())
    config.save_settings({"alert_discord_enabled": True, "alert_discord_webhook_url": "https://discord/boom"})
    await alerts._send_discord("m")  # must not raise
    assert any("Discord alert failed" in e["message"] for e in state.recent_events())


async def test_email_requires_full_config(admin, monkeypatch):
    def boom(*a, **kw):
        raise AssertionError("SMTP must not be contacted")
    monkeypatch.setattr(alerts.smtplib, "SMTP", boom)
    config.save_settings({"alert_email_enabled": True, "alert_email_to": "x@y"})  # no username/password
    await alerts._send_email("s", "b")
    assert alerts.smtp_configured() is False
    assert await alerts.send_email_to("x@y", "s", "b") is False


async def test_email_failure_is_logged_not_raised(admin, monkeypatch):
    def boom(*a, **kw):
        raise OSError("smtp down")
    monkeypatch.setattr(alerts.smtplib, "SMTP", boom)
    config.save_settings({"alert_email_enabled": True, "alert_email_to": "x@y",
                          "alert_smtp_username": "u", "alert_smtp_password": "p"})
    await alerts._send_email("s", "b")
    assert any("Email alert failed" in e["message"] for e in state.recent_events())
    assert await alerts.send_email_to("x@y", "s", "b") is False


async def test_test_alert_reports_channels(capture):
    discord, email = capture
    assert await alerts.test_alert() == {"discord": False, "email": False, "push": False}
    config.save_settings({"alert_discord_enabled": True, "alert_discord_webhook_url": "u",
                          "alert_email_enabled": True, "alert_email_to": "x@y",
                          "alert_smtp_username": "u", "alert_smtp_password": "p"})
    assert await alerts.test_alert() == {"discord": True, "email": True, "push": False}
    assert len(discord) == 1 and email[0][0] == "Fluxbridge: test alert"


# ----------------------------------------------------- webhook_failed wiring
async def test_background_signal_failure_alerts(admin, monkeypatch):
    failed = []

    async def fake_failed(name, reason):
        failed.append((name, reason))

    monkeypatch.setattr(alerts, "webhook_failed", fake_failed)
    await signals.process_background({"action": "buy"}, {"id": "w", "name": "W", "strategy": "simple"})
    assert failed == [("W", "Payload missing 'action' or 'symbol'")]
    assert state.recent_signals()[0]["result"].startswith("error:")

"""alpha.70: alerts in the workspace language, the external heartbeat watchdog,
settings export / import."""
from __future__ import annotations

import re

import httpx
import pytest

from app import alerts, config, context, db, i18n, watchdog
from app.routers import settings_io


# ------------------------------------------------------------- German alerts
@pytest.fixture
def capture(monkeypatch, admin):
    discord: list[str] = []
    email: list[tuple[str, str]] = []
    push: list[tuple[str, str]] = []

    async def fake_discord(message, **kw):
        discord.append(message)

    async def fake_email(subject, body):
        email.append((subject, body))

    async def fake_push(title, body, **kw):
        push.append((title, body))

    monkeypatch.setattr(alerts, "_send_discord", fake_discord)
    monkeypatch.setattr(alerts, "_send_email", fake_email)
    monkeypatch.setattr(alerts, "_send_push", fake_push)
    return discord, email, push


def test_alert_dictionary_placeholders_match():
    ph = lambda s: sorted(re.findall(r"\{(\w+)\}", s))  # noqa: E731
    for en, de in i18n.ALERT_DE.items():
        assert ph(en) == ph(de), en


def test_alert_language_follows_setting_then_browser():
    assert i18n.alert_language({"ui_language": "auto", "ui_language_seen": ""}) == "en"
    assert i18n.alert_language({"ui_language": "auto", "ui_language_seen": "de"}) == "de"
    assert i18n.alert_language({"ui_language": "en", "ui_language_seen": "de"}) == "en"
    assert i18n.alert_language({"ui_language": "de", "ui_language_seen": "en"}) == "de"
    tr = i18n.alert_translator({"ui_language": "de"})
    assert tr("{n} trade closed", n=1) == "1 Trade geschlossen"
    assert tr("{n} trades closed", n=3) == "3 Trades geschlossen"
    assert tr("not in the dictionary {x}", x=1) == "not in the dictionary 1"


async def test_alerts_render_in_german(capture):
    discord, email, push = capture
    config.save_settings({"ui_language": "de"})
    await alerts.connection_lost("Login-A", "demo", "timeout")
    assert discord[-1] == "🔴 **Verbindung verloren** — Login `Login-A` (demo, Tradovate) — timeout"
    assert email[-1][0] == "Fluxbridge: Verbindung verloren (Login-A)"
    assert push[-1][0] == "Verbindung verloren: Login-A"
    await alerts.trade_executed("W", "buy", "MNQU6", ["A", "B"])
    assert discord[-1] == "⚡ **Trade ausgeführt** — Strategie `W`: BUY MNQU6 auf A, B"
    config.save_settings({"ui_language": "en"})
    await alerts.connection_restored("Login-A", "demo")
    assert discord[-1] == "🟢 **Connection restored** — login `Login-A` (demo, Tradovate)"


async def test_browser_language_is_reported_and_used_for_alerts(client, capture):
    discord, _, _ = capture
    r = await client.post("/api/settings", json={"ui_language_seen": "de"})
    assert r.status_code == 200 and r.json()["ui_language_seen"] == "de"
    assert (await client.post("/api/settings", json={"ui_language_seen": "fr"})).status_code == 400
    await alerts.trade_executed("W", "sell", "MESU6", ["A"])
    assert discord[-1].startswith("⚡ **Trade ausgeführt**")


# ----------------------------------------------------------------- watchdog
class _FakeHttp:
    def __init__(self, status=200, exc=None):
        self.status, self.exc, self.calls = status, exc, []

    async def get(self, url, **kw):
        self.calls.append(url)
        if self.exc:
            raise self.exc
        return httpx.Response(self.status, request=httpx.Request("GET", url))


def test_interval_is_clamped():
    assert watchdog.normalize_interval("abc") == 60
    assert watchdog.normalize_interval(5) == 30
    assert watchdog.normalize_interval(99999) == 3600
    assert watchdog.normalize_interval("120.0") == 120


async def test_tick_pings_on_schedule_and_records_outcome(admin, area, monkeypatch):
    fake = _FakeHttp()
    monkeypatch.setattr(watchdog.http, "client", lambda name="outbound": fake)
    assert await watchdog.tick_area(area) is None and watchdog.status(area)["url"] == ""     # off by default
    config.save_settings({"heartbeat_url": "https://hc-ping.com/abc", "heartbeat_interval": 60})
    assert await watchdog.tick_area(area) == 60.0
    assert fake.calls == ["https://hc-ping.com/abc"]
    st = watchdog.status(area)
    assert st["ok"] is True and st["error"] == "" and st["at"] and st["url"] == "https://hc-ping.com/abc"
    remaining = await watchdog.tick_area(area)
    assert 0 < remaining <= 60 and len(fake.calls) == 1                                       # not due yet
    watchdog.reset()
    fake.status = 503
    await watchdog.tick_area(area)
    assert watchdog.status(area) == {**watchdog.status(area), "ok": False, "error": "HTTP 503"}
    watchdog.reset()
    fake.exc = httpx.ConnectError("boom")
    await watchdog.tick_area(area)
    assert watchdog.status(area)["ok"] is False and "ConnectError" in watchdog.status(area)["error"]
    config.save_settings({"heartbeat_url": ""})
    assert await watchdog.tick_area(area) is None and watchdog.status(area)["ok"] is None     # switched off → forgotten


async def test_heartbeat_settings_validated_and_shown(client, monkeypatch):
    from app import security
    monkeypatch.setattr(security, "check_outbound_url", lambda url: "private address" if "10.0" in url else None)
    r = await client.post("/api/settings", json={"heartbeat_url": "http://10.0.0.5/ping"})
    assert r.status_code == 400 and "rejected" in r.json()["detail"]
    r = await client.post("/api/settings", json={"heartbeat_url": " https://hc-ping.com/x ", "heartbeat_interval": 5})
    assert r.status_code == 200 and r.json()["heartbeat_url"] == "https://hc-ping.com/x" and r.json()["heartbeat_interval"] == 30
    st = (await client.get("/api/status")).json()["heartbeat"]
    assert st == {"at": None, "ok": None, "error": "", "url": ""}                            # nothing pinged yet


# ------------------------------------------------------ settings export / import
def _login(name, lid):
    return {"name": name, "lid": lid, "environment": "demo", "enabled": True, "access_token": "tok", "md_token": "md",
            "accounts": [{"spec": "DEMO1", "id": 1, "enabled": True}]}


async def test_export_excludes_secrets_and_runtime_state(client, admin, webhook_factory):
    config.save_settings({"token_accounts": [_login("L", "lid-1")], "webhook_passphrase": "pp", "heartbeat_url": "https://hc/x",
                          "alert_smtp_password": "smtp-secret", "discord_user_token": "dtok",
                          "symbol_map": {"MNQ1!": "MNQU6"}, "default_qty": 3, "ui_language": "de"})
    wh = webhook_factory("Main", accounts=[{"token_idx": 0, "lid": "lid-1", "spec": "DEMO1", "enabled": True, "qty_multiplier": 2.0}])
    r = await client.get("/api/settings/export")
    assert r.status_code == 200 and "fluxbridge-settings-" in r.headers["content-disposition"]
    doc = r.json()
    assert doc["fluxbridge_settings"] == 1 and doc["version"] == config.get_version()
    s = doc["settings"]
    assert s["default_qty"] == 3 and s["symbol_map"] == {"MNQ1!": "MNQU6"} and s["ui_language"] == "de"
    assert s["webhooks"][0]["token"] == wh["token"] and s["webhooks"][0]["accounts"][0]["lid"] == "lid-1"
    for k in ("token_accounts", "webhook_passphrase", "heartbeat_url", "alert_smtp_password", "discord_user_token",
              "webhook_secret", "copy_groups", "risk_state", "dd_state", "journal_last_import"):
        assert k not in s, k
    assert "smtp-secret" not in r.text and "tok" not in r.text.split('"webhooks"')[0]
    assert db.list_audit(5)[0]["action"] == "settings_export"


async def test_import_round_trip_keeps_tokens_and_known_routing(client, admin, webhook_factory):
    config.save_settings({"token_accounts": [_login("L", "lid-1")], "symbol_map": {"MNQ1!": "MNQU6"}, "default_qty": 3,
                          "alert_email_to": "me@example.com", "news_lock": {"enabled": True, "before": 7}})
    wh = webhook_factory("Main", strategy="bracket", default_qty=2, tp_qty=1,
                         accounts=[{"token_idx": 0, "lid": "lid-1", "spec": "DEMO1", "enabled": True, "qty_multiplier": 2.0},
                                   {"token_idx": 1, "lid": "lid-other", "spec": "X", "enabled": True, "qty_multiplier": 1.0}])
    doc = (await client.get("/api/settings/export")).json()
    # wipe, then import
    config.save_settings({"symbol_map": {}, "default_qty": 1, "webhooks": [], "alert_email_to": "", "news_lock": config.DEFAULT_SETTINGS["news_lock"]})
    r = await client.post("/api/settings/import", json=doc)
    assert r.status_code == 200, r.text
    assert r.json()["webhooks"] == 1 and "symbol_map" in r.json()["keys"]
    s = config.load_settings()
    assert s["symbol_map"] == {"MNQ1!": "MNQU6"} and s["default_qty"] == 3 and s["alert_email_to"] == "me@example.com"
    assert s["news_lock"]["enabled"] is True and s["news_lock"]["before"] == 7
    got = s["webhooks"][0]
    assert got["id"] == wh["id"] and got["token"] == wh["token"] and got["strategy"] == "bracket" and got["default_qty"] == 2
    assert [a["lid"] for a in got["accounts"]] == ["lid-1"] and got["accounts"][0]["qty_multiplier"] == 2.0   # unknown login dropped
    assert s["token_accounts"][0]["access_token"] == "tok"                                                # untouched
    assert db.list_audit(5)[0]["action"] == "settings_import"
    # the public settings view is refreshed (cache invalidated)
    assert (await client.get("/api/settings")).json()["default_qty"] == 3


async def test_import_regenerates_tokens_clashing_with_another_area(client, admin, webhook_factory):
    wh = webhook_factory("Main")
    doc = (await client.get("/api/settings/export")).json()
    u2 = db.create_user("two@example.com", "password123")
    a2 = db.user_primary_area(u2["id"])
    from app import auth
    from tests.conftest import _make_client, enrolled
    enrolled(u2["id"])
    async with _make_client(auth.make_session(u2["id"])) as c2:
        r = await c2.post("/api/settings/import", json=doc)
        assert r.status_code == 200, r.text
    with context.use_area(a2):
        imported = config.load_settings()["webhooks"]
    assert len(imported) == 1 and imported[0]["token"] != wh["token"] and imported[0]["name"] == "Main"
    assert config.find_webhook(wh["token"])[0] == 1                                                     # the original still routes here
    with context.use_area(1):
        assert config.load_settings()["webhooks"][0]["token"] == wh["token"]


async def test_import_rejects_bad_files(client, admin):
    bad = [
        {"settings": {}},
        {"fluxbridge_settings": 2, "settings": {}},
        {"fluxbridge_settings": 1, "settings": {"token_accounts": []}},
        {"fluxbridge_settings": 1, "settings": {"webhook_passphrase": "x"}},
        {"fluxbridge_settings": 1, "settings": {"heartbeat_url": "https://x"}},
        {"fluxbridge_settings": 1, "settings": {"webhooks": {"id": "x"}}},
        {"fluxbridge_settings": 1, "settings": {"webhooks": [{"name": "x", "default_qty": "lots"}]}},
        {"fluxbridge_settings": 1, "settings": {"symbol_map": ["MNQ"]}},
        {"fluxbridge_settings": 1, "settings": {"default_qty": "three"}},
        {"fluxbridge_settings": 1, "settings": {"ui_language": "fr"}},
        {"fluxbridge_settings": 1, "settings": {"journal_timezone": "Mars/Olympus"}},
        {"fluxbridge_settings": 1, "settings": {"alert_email_to": 5}},
        [],
    ]
    for doc in bad:
        r = await client.post("/api/settings/import", json=doc)
        assert r.status_code == 400, doc
    assert config.load_settings()["webhooks"] == []
    # coercion + normalisation on the happy path
    r = await client.post("/api/settings/import", json={"fluxbridge_settings": 1, "settings": {
        "heartbeat_interval": "7", "alert_push_enabled": 0, "daily_summary_time": "7:5",
        "webhooks": [{"name": "A", "strategy": "nope", "token": "short", "enabled": 0}, {"name": "B", "id": "wh_dead", "token": "x" * 20}]}})
    assert r.status_code == 200, r.text
    s = config.load_settings()
    assert s["heartbeat_interval"] == 30 and s["alert_push_enabled"] is False and s["daily_summary_time"] == "07:05"
    a, b = s["webhooks"]
    assert a["strategy"] == "simple" and a["enabled"] is False and len(a["token"]) >= 16 and a["token"] != "short"
    assert b["id"] == "wh_dead" and b["token"] == "x" * 20 and b["enabled"] is True
    assert settings_io._PORTABLE_KEYS and "heartbeat_url" not in settings_io._PORTABLE_KEYS


async def test_import_and_export_need_a_session(anon_client, admin):
    assert (await anon_client.get("/api/settings/export")).status_code in (401, 403)
    assert (await anon_client.post("/api/settings/import", json={})).status_code in (401, 403)

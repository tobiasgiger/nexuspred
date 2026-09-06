"""Characterisation of account routing, migration, webhook resolution and the
emergency flatten-all."""
from __future__ import annotations

import pytest

from app import config, context, db, main, signals, state, tradovate
from tests.helpers import FakeExecutor

TOKEN_ACCOUNTS = [
    {"name": "L1", "environment": "demo", "enabled": True, "access_token": "t1", "qty_multiplier": 1,
     "accounts": [{"spec": "A1", "id": 1, "enabled": True, "qty_multiplier": 1},
                  {"spec": "A2", "id": 2, "enabled": False, "qty_multiplier": 1}]},
    {"name": "L2", "environment": "live", "enabled": False, "access_token": "t2", "qty_multiplier": 1,
     "accounts": [{"spec": "B1", "id": 3, "enabled": True, "qty_multiplier": 1}]},
]


@pytest.fixture
def accounts(admin):
    config.save_settings({"token_accounts": TOKEN_ACCOUNTS})
    tradovate.manager().reload()


# ------------------------------------------------------------ executor_for
def test_executor_for_addresses_login_and_spec(accounts):
    m = tradovate.manager()
    ex = m.executor_for(0, "A1", 3.5)
    assert (ex.name, ex.spec, ex.id, ex.qty_multiplier) == ("A1", "A1", 1, 3.5)
    # The per-account execution toggle is NOT consulted for webhook routing…
    assert m.executor_for(0, "A2", 1).name == "A2"
    # …but a disabled login is.
    assert m.executor_for(1, "B1", 1) is None
    assert m.executor_for(5, "A1", 1) is None
    assert m.executor_for(0, "nope", 1) is None


def test_manager_enabled_respects_both_toggles(accounts):
    assert [e.name for e in tradovate.manager().enabled()] == ["A1"]


def test_manager_is_per_area(accounts):
    with context.use_area(2):
        assert tradovate.manager().all() == []
    assert len(tradovate.manager().all()) == 2


def test_webhook_executors_filters_and_warns(accounts):
    wh = {"name": "W", "accounts": [
        {"token_idx": 0, "spec": "A1", "enabled": True, "qty_multiplier": 2},
        {"token_idx": 0, "spec": "A2", "enabled": False, "qty_multiplier": 1},
        {"token_idx": 9, "spec": "GHOST", "enabled": True, "qty_multiplier": 1},
    ]}
    execs = signals._webhook_executors(wh)
    assert [(e.name, e.qty_multiplier) for e in execs] == [("A1", 2)]
    assert any("GHOST" in e["message"] for e in state.recent_events())


# --------------------------------------------------------- legacy migration
def test_migrate_legacy_webhook_builds_default_from_enabled_accounts(accounts):
    config.save_settings({"default_qty": 5, "tp_qty": 2})
    config.migrate_legacy_webhook()
    s = config.load_settings()
    assert s["webhooks_migrated"] is True
    (wh,) = s["webhooks"]
    assert wh["name"] == "Default" and wh["strategy"] == "bracket" and wh["enabled"] is True
    assert wh["default_qty"] == 5 and wh["tp_qty"] == 2
    assert wh["token"] != "change-me" and len(wh["token"]) >= 16
    assert wh["accounts"] == [
        {"token_idx": 0, "spec": "A1", "enabled": True, "qty_multiplier": 1.0},
        {"token_idx": 1, "spec": "B1", "enabled": True, "qty_multiplier": 1.0},
    ]
    config.migrate_legacy_webhook()  # idempotent
    assert len(config.load_settings()["webhooks"]) == 1


def test_migrate_reuses_customised_legacy_secret(admin):
    config.save_settings({"webhook_secret": "my-legacy-token"})
    config.migrate_legacy_webhook()
    assert config.load_settings()["webhooks"][0]["token"] == "my-legacy-token"


def test_save_settings_drops_unknown_keys_and_is_per_area(admin):
    config.save_settings({"default_qty": 9, "not_a_key": 1})
    s = config.load_settings()
    assert s["default_qty"] == 9 and "not_a_key" not in s
    assert db.get_area_settings(1)["default_qty"] == 9
    with context.use_area(2):
        assert config.load_settings()["default_qty"] == 3  # defaults only


def test_public_settings_masks_secrets(admin):
    config.save_settings({
        "webhook_passphrase": "pp", "alert_smtp_password": "sp", "discord_user_token": "dt",
        "alert_discord_webhook_url": "https://d",
        "token_accounts": [{"name": "L", "access_token": "a", "md_token": "m", "enabled": True}],
        "discord_channels": [{"id": "1", "label": "c", "enabled": True,
                              "targets": [{"label": "t", "url": "u", "secret": "s", "enabled": True},
                                          {"label": "t2", "url": "u2", "secret": "", "enabled": True}]}],
    })
    p = config.public_settings()
    for k in ("webhook_passphrase", "alert_smtp_password", "discord_user_token", "alert_discord_webhook_url"):
        assert p[k] == "********"
    assert p["token_accounts"][0]["access_token"] == "********" and p["token_accounts"][0]["md_token"] == "********"
    assert [t["secret"] for t in p["discord_channels"][0]["targets"]] == ["********", ""]
    assert p["webhook_secret"] == "change-me"  # legacy field is not masked


async def test_webhook_create_does_not_leak_into_other_areas(client):
    """v4 bug (fixed in v5): load_settings() shallow-copied, so the webhook
    router appended into DEFAULT_SETTINGS['webhooks'] itself."""
    r = await client.post("/api/webhooks", json={"name": "Leaky", "strategy": "simple"})
    assert r.status_code == 200
    with context.use_area(2):
        assert config.load_settings()["webhooks"] == []
    assert config.DEFAULT_SETTINGS["webhooks"] == []


# ---------------------------------------------------------- webhook lookup
def test_resolve_webhook_across_areas(admin):
    wh1 = config.new_webhook("one")
    config.save_settings({"webhooks": [wh1]})
    u2 = db.create_user("two@example.com", "password123")
    a2 = db.user_primary_area(u2["id"])
    assert a2 == 2
    wh2 = config.new_webhook("two")
    config.save_settings({"webhooks": [wh2]}, area_id=a2)

    assert main._resolve_webhook(wh1["token"])[0] == 1
    aid, wh = main._resolve_webhook(wh2["token"])
    assert aid == 2 and wh["id"] == wh2["id"]
    assert main._resolve_webhook("nope") == (None, None)


# -------------------------------------------------------------- flatten_all
class _RecordingAE(FakeExecutor):
    instances: list["_RecordingAE"] = []

    def __init__(self, session, account):
        super().__init__(account["spec"], positions=[{"symbol": "MNQU6", "netPos": 1}, {"symbol": "", "netPos": 2}],
                         working=[{"id": 7}, {"id": None}])
        self.session = session
        _RecordingAE.instances.append(self)


async def test_flatten_all_ignores_toggles_and_trading_switch(accounts, monkeypatch):
    _RecordingAE.instances.clear()
    monkeypatch.setattr(signals, "AccountExecutor", _RecordingAE)
    config.save_settings({"trading_enabled": False})
    r = await signals.flatten_all()
    names = sorted(e.name for e in _RecordingAE.instances)
    assert names == ["A1", "A2"]  # per-account toggle ignored, disabled login L2 excluded
    assert r == {"status": "ok", "accounts": 2, "cancelled": 2, "flattened": 2, "errors": []}
    for e in _RecordingAE.instances:
        assert e.of("cancel") == [{"order_id": 7}] and e.of("liquidate") == [{"symbol": "MNQU6"}]
    assert any("SOS flatten-all" in ev["message"] for ev in state.recent_events())


async def test_flatten_all_without_accounts(admin):
    r = await signals.flatten_all()
    assert r == {"status": "ok", "accounts": 0, "cancelled": 0, "flattened": 0, "errors": []}


async def test_flatten_all_collects_errors(accounts, monkeypatch):
    class Failing(FakeExecutor):
        def __init__(self, session, account):
            super().__init__(account["spec"], positions=[{"symbol": "X", "netPos": 1}])
        async def liquidate_position(self, symbol):
            raise tradovate.TradovateError("nope")
    monkeypatch.setattr(signals, "AccountExecutor", Failing)
    r = await signals.flatten_all()
    assert r["flattened"] == 0 and sorted(r["errors"]) == ["A1: flatten X: nope", "A2: flatten X: nope"]


# --------------------------------------------------------- trade accounts
def test_trade_accounts_overview_flattens_logins(accounts):
    rows = main._trade_accounts_overview()
    assert [(r["token_idx"], r["token_name"], r["spec"], r["enabled"], r["token_enabled"]) for r in rows] == [
        (0, "L1", "A1", True, True), (0, "L1", "A2", False, True), (1, "L2", "B1", True, False)]
    assert rows[0]["environment"] == "demo" and rows[2]["environment"] == "live"
    assert all(r["connected"] is False for r in rows)


def test_trade_accounts_overview_legacy_single_account(admin):
    config.save_settings({"token_accounts": [{"name": "L", "enabled": True, "account_spec": "S", "account_id": 5}]})
    (row,) = main._trade_accounts_overview()
    assert row["spec"] == "S" and row["id"] == 5 and row["enabled"] is True

"""alpha.75 — one typed schema for every setting."""
import pytest

from app import config, settings_schema as ss


def test_every_default_setting_has_a_schema_entry():
    missing = sorted(set(config.DEFAULT_SETTINGS) - set(ss.SCHEMA))
    extra = sorted(set(ss.SCHEMA) - set(config.DEFAULT_SETTINGS))
    assert missing == [] and extra == []


def test_schema_flags_match_the_config_constants():
    assert ss.PROTECTED_KEYS == config.SETTINGS_PROTECTED_KEYS
    assert set(config.SECRET_FIELDS) <= ss.SECRET_KEYS
    assert not (set(ss.PORTABLE_KEYS) & ss.SECRET_KEYS)                       # secrets never travel in an export
    assert "webhooks" in ss.PORTABLE_KEYS and "token_accounts" not in ss.PORTABLE_KEYS


def test_defaults_pass_their_own_schema():
    for key, value in config.DEFAULT_SETTINGS.items():
        ss.coerce_one(key, value)                                              # no ValueError


@pytest.mark.parametrize("key,value,expected", [
    ("trading_enabled", "yes", True),
    ("trading_enabled", 0, False),
    ("default_qty", "3", 3),
    ("heartbeat_interval", 5, 30),                                             # clamped, not rejected
    ("heartbeat_interval", 99999, 3600),
    ("daily_summary_time", "7:5", "07:05"),
    ("journal_timezone", "", "Europe/Zurich"),
    ("alert_accounts", None, []),
    ("ui_language", "de", "de"),
])
def test_coerce_one_accepts_and_normalises(key, value, expected):
    assert ss.coerce_one(key, value) == expected


@pytest.mark.parametrize("key,value", [
    ("alert_email_to", 5),                                                     # text fields reject numbers
    ("default_qty", "many"),
    ("default_qty", 0),
    ("default_qty", 10_000),
    ("entry_order_type", "Stop"),
    ("ui_language", "fr"),
    ("daily_summary_time", "25:00"),
    ("journal_timezone", "Mars/Olympus"),
    ("alert_accounts", "DEMO11"),                                              # a list, never a string
    ("symbol_map", ["not", "a", "dict"]),
    ("heartbeat_url", "x" * 501),
])
def test_coerce_one_rejects(key, value):
    with pytest.raises(ValueError):
        ss.coerce_one(key, value)


def test_coerce_rejects_unknown_and_protected_keys():
    with pytest.raises(ValueError, match="unknown setting"):
        ss.coerce({"no_such_key": 1})
    with pytest.raises(ValueError, match="own endpoint"):
        ss.coerce({"webhook_secret": "abc"})
    assert ss.coerce({"webhook_secret": "abc"}, allow_protected=True) == {"webhook_secret": "abc"}
    assert ss.coerce({"default_qty": "2", "trading_enabled": "off"}) == {"default_qty": 2, "trading_enabled": False}


async def test_settings_endpoint_uses_the_schema(client):
    r = await client.post("/api/settings", json={"default_qty": "abc"})
    assert r.status_code == 400 and "default_qty" in r.json()["detail"]
    r = await client.post("/api/settings", json={"default_qty": "4"})
    assert r.status_code == 200 and r.json()["default_qty"] == 4
    r = await client.post("/api/settings", json={"webhook_secret": "abc"})       # protected: silently ignored, never applied
    assert r.status_code == 200 and r.json().get("webhook_secret") != "abc"

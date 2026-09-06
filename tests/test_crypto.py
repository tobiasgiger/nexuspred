"""Secrets at rest (app/crypto.py): encrypted in the areas.settings JSON,
plain for every caller above app.db, legacy plain values migrate on startup."""
from __future__ import annotations

import json

from app import config, context, crypto, db


def _raw(area_id: int) -> dict:
    with db._connect() as c:
        return json.loads(c.execute("SELECT settings FROM areas WHERE id=?", (area_id,)).fetchone()["settings"])


def test_encrypt_roundtrip_and_passthrough():
    token = crypto.encrypt("s3cret")
    assert token.startswith("enc:v1:") and "s3cret" not in token
    assert crypto.decrypt(token) == "s3cret"
    assert crypto.encrypt(token) == token          # never double-encrypts
    assert crypto.encrypt("") == "" and crypto.encrypt(None) is None and crypto.encrypt(5) == 5
    assert crypto.decrypt("plain-legacy-value") == "plain-legacy-value"


def test_wrong_key_yields_empty_not_exception(monkeypatch):
    token = crypto.encrypt("abc")
    crypto.reset()
    monkeypatch.setenv("NEXUSPRED_ENCRYPTION_KEY", "another-key")
    assert crypto.decrypt(token) == "abc"        # still readable: the session secret is a known previous key
    crypto.reset()
    monkeypatch.setenv("SESSION_SECRET", "rotated-too")
    assert crypto.decrypt(token) == ""           # no known key fits → empty, never an exception
    crypto.reset()


def test_secrets_are_encrypted_in_db_and_plain_via_config(admin):
    aid = db.user_primary_area(admin["id"])
    with context.use_area(aid):
        config.save_settings({
            "discord_user_token": "disc-tok", "alert_smtp_password": "smtp-pw",
            "webhook_passphrase": "pass", "alert_discord_webhook_url": "https://discord.com/api/webhooks/1/x",
            "token_accounts": [{"name": "a", "access_token": "acc-tok", "md_token": "md-tok", "enabled": True}],
            "discord_channels": [{"id": "1", "targets": [{"url": "https://h", "secret": "tgt-secret"}]}],
        })
        s = config.load_settings(force=True)
    assert s["discord_user_token"] == "disc-tok" and s["alert_smtp_password"] == "smtp-pw"
    assert s["token_accounts"][0]["access_token"] == "acc-tok"
    assert s["discord_channels"][0]["targets"][0]["secret"] == "tgt-secret"

    raw = json.dumps(_raw(aid))
    for secret in ("disc-tok", "smtp-pw", "acc-tok", "md-tok", "tgt-secret", '"pass"', "webhooks/1/x"):
        assert secret not in raw
    stored = _raw(aid)
    assert stored["discord_user_token"].startswith("enc:v1:")
    assert stored["token_accounts"][0]["access_token"].startswith("enc:v1:")
    assert stored["token_accounts"][0]["name"] == "a"  # non-secret fields untouched
    assert stored["discord_channels"][0]["targets"][0]["secret"].startswith("enc:v1:")


def test_initial_settings_on_user_creation_are_encrypted():
    user = db.create_user("a@example.com", "password123", initial_settings={"discord_user_token": "legacy"})
    aid = db.user_primary_area(user["id"])
    assert _raw(aid)["discord_user_token"].startswith("enc:v1:")
    assert db.get_area_settings(aid)["discord_user_token"] == "legacy"


def test_legacy_plaintext_is_migrated_on_startup(admin):
    aid = db.user_primary_area(admin["id"])
    with db._connect() as c:  # simulate a pre-encryption row
        c.execute("UPDATE areas SET settings=? WHERE id=?",
                  (json.dumps({"discord_user_token": "old-plain",
                               "token_accounts": [{"name": "x", "access_token": "old-acc"}]}), aid))
    assert db.get_area_settings(aid)["discord_user_token"] == "old-plain"  # readable before migration
    assert db.encrypt_existing_settings() == 1
    assert db.encrypt_existing_settings() == 0  # idempotent
    raw = _raw(aid)
    assert raw["discord_user_token"].startswith("enc:v1:") and raw["token_accounts"][0]["access_token"].startswith("enc:v1:")
    assert db.get_area_settings(aid)["token_accounts"][0]["access_token"] == "old-acc"


def test_key_source_prefers_env(monkeypatch):
    crypto.reset()
    monkeypatch.setenv("NEXUSPRED_ENCRYPTION_KEY", "k")
    assert crypto.key_source() == "env:encryption"
    crypto.reset()
    monkeypatch.delenv("NEXUSPRED_ENCRYPTION_KEY")
    assert crypto.key_source() == "env:session"  # conftest sets SESSION_SECRET
    crypto.reset()


async def test_api_still_masks_secrets(client):
    r = await client.post("/api/settings", json={"alert_smtp_password": "pw"})
    assert r.json()["alert_smtp_password"] == "********"
    assert (await client.get("/api/settings")).json()["alert_smtp_password"] == "********"


def test_key_change_falls_back_to_previous_key_and_reencrypts(admin, monkeypatch):
    """Secrets encrypted under SESSION_SECRET stay readable after
    NEXUSPRED_ENCRYPTION_KEY is introduced, and the startup pass re-encrypts them."""
    aid = db.user_primary_area(admin["id"])
    with context.use_area(aid):
        config.save_settings({"discord_user_token": "old-key-token",
                              "token_accounts": [{"name": "a", "access_token": "acc", "enabled": True}]})
    before = _raw(aid)["discord_user_token"]
    crypto.reset()
    monkeypatch.setenv("NEXUSPRED_ENCRYPTION_KEY", "brand-new-key")   # conftest's SESSION_SECRET becomes the legacy key
    config.invalidate()
    assert crypto.key_source() == "env:encryption"
    assert db.get_area_settings(aid)["discord_user_token"] == "old-key-token"       # readable via fallback
    assert db.get_area_settings(aid)["token_accounts"][0]["access_token"] == "acc"
    assert db.encrypt_existing_settings() == 1                                       # re-encrypted with the new key
    assert _raw(aid)["discord_user_token"] != before and crypto.is_current(_raw(aid)["discord_user_token"])
    assert db.encrypt_existing_settings() == 0
    # explicit previous key also works when neither env session secret nor DB secret match
    crypto.reset()
    monkeypatch.setenv("NEXUSPRED_ENCRYPTION_KEY", "third-key")
    monkeypatch.setenv("NEXUSPRED_ENCRYPTION_KEY_PREVIOUS", "brand-new-key")
    assert db.get_area_settings(aid)["discord_user_token"] == "old-key-token"
    crypto.reset()
    monkeypatch.delenv("NEXUSPRED_ENCRYPTION_KEY_PREVIOUS")
    monkeypatch.setenv("NEXUSPRED_ENCRYPTION_KEY", "unrelated-key")
    assert db.get_area_settings(aid)["discord_user_token"] == ""                     # truly lost → empty, no exception
    crypto.reset()

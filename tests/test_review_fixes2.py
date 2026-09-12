"""Code review, part 2: persistence, security and background-loop hardening."""
from __future__ import annotations

import asyncio

import pytest
from cryptography.fernet import Fernet
from starlette.requests import Request as _R

from app import auth, config, context, crypto, db, drawdown, relay, security
from tests.conftest import _make_client


# ----------------------------------------------------------- single-use codes
def test_invite_pairing_and_reset_are_consumed_once(admin):
    code = db.create_invite(admin["id"], "x@example.com")
    assert db.consume_invite(code, admin["id"]) is True
    assert db.consume_invite(code, admin["id"]) is False
    pairing = db.create_agent_pairing(1, "desk")
    assert db.consume_agent_pairing(pairing) == {"area_id": 1, "name": "desk"}
    assert db.consume_agent_pairing(pairing) is None
    tok = db.create_password_reset(admin["id"])
    assert db.consume_password_reset(tok, "brand-new-pw1") == admin["id"]
    assert db.consume_password_reset(tok, "another-pw123") is None
    assert db.authenticate("admin@example.com", "brand-new-pw1")


# ------------------------------------------------------ undecryptable secrets
def _foreign_cipher(text: str) -> str:
    return crypto.PREFIX + Fernet(Fernet.generate_key()).encrypt(text.encode()).decode()


def test_saving_settings_keeps_a_secret_the_key_cannot_read(admin):
    foreign = _foreign_cipher("tok-from-another-key")
    db.save_area_settings(1, {"token_accounts": [{"name": "L", "lid": "lg_keep1", "environment": "demo", "enabled": True,
                                                  "access_token": foreign, "accounts": []}],
                             "alert_smtp_password": foreign, "webhook_passphrase": "plain-pass"})
    config.invalidate()
    s = config.load_settings(area_id=1)
    assert s["token_accounts"][0]["access_token"] == "" and s["alert_smtp_password"] == ""     # unreadable → empty
    config.save_settings({"trading_enabled": True}, area_id=1)
    raw = db.get_area_settings_raw(1)
    assert raw["token_accounts"][0]["access_token"] == foreign and raw["alert_smtp_password"] == foreign
    assert crypto.decrypt(raw["webhook_passphrase"]) == "plain-pass"
    # a secret the key CAN read is cleared when the user clears it
    config.save_settings({"webhook_passphrase": ""}, area_id=1)
    assert db.get_area_settings_raw(1)["webhook_passphrase"] == ""
    # a login the user re-entered keeps its new token
    config.update(lambda st: st["token_accounts"][0].__setitem__("access_token", "fresh"), area_id=1)
    assert crypto.decrypt(db.get_area_settings_raw(1)["token_accounts"][0]["access_token"]) == "fresh"


# ------------------------------------------------------- unreadable settings
async def test_settings_are_never_written_over_an_unreadable_read(client, admin, monkeypatch):
    config.save_settings({"trading_enabled": True, "default_qty": 4}, area_id=1)
    config.invalidate()

    def boom(_aid):
        raise RuntimeError("disk on fire")
    monkeypatch.setattr(db, "get_area_settings", boom)
    s = config.load_settings(area_id=1)
    assert s["default_qty"] == config.DEFAULT_SETTINGS["default_qty"]        # defaults for this read only
    assert 1 not in config._cache                                              # …and not cached
    with pytest.raises(config.SettingsUnavailable):
        config.save_settings({"trading_enabled": False}, area_id=1)
    r = await client.post("/api/settings", json={"trading_enabled": False})
    assert r.status_code == 503 and "could not be read" in r.json()["detail"]
    monkeypatch.undo()
    assert config.load_settings(area_id=1)["default_qty"] == 4                 # nothing was lost
    config.save_settings({"trading_enabled": False}, area_id=1)
    assert config.load_settings(area_id=1)["default_qty"] == 4


def test_corrupt_settings_json_is_an_error_not_an_empty_area(admin):
    with db._connect() as c:
        c.execute("UPDATE areas SET settings='{not json' WHERE id=1")
    with pytest.raises(ValueError):
        db.get_area_settings_raw(1)


# --------------------------------------------------------------- cascade
def test_deleting_a_user_removes_the_workspace_data(admin):
    u2 = db.create_user("second@example.com", "password123")
    aid = db.user_primary_area(u2["id"])
    db.insert_signal(aid, {"result": "ok", "webhook": "w", "payload": {}})
    db.insert_copy_event(aid, {"group_id": "g", "kind": "feed_up"})
    db.create_password_reset(u2["id"])
    db.delete_user(u2["id"])
    assert db.list_signals(aid)["items"] == [] and db.list_copy_events(aid) == []
    with db._connect() as c:
        assert c.execute("SELECT COUNT(*) FROM password_resets WHERE user_id=?", (u2["id"],)).fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM signal_log WHERE area_id=?", (aid,)).fetchone()[0] == 0


# --------------------------------------------------------------- security
def test_forwarded_for_only_counts_behind_our_proxy():
    scope = {"type": "http", "headers": [(b"x-forwarded-for", b"1.2.3.4")], "client": ("8.8.4.4", 1)}
    assert security.client_ip(_R(scope)) == "8.8.4.4"                     # public peer: header ignored
    scope["client"] = ("10.0.0.2", 1)
    assert security.client_ip(_R(scope)) == "1.2.3.4"                         # our proxy appended it


def test_public_ip_check_uses_the_global_scope():
    assert security._is_public_ip("8.8.8.8") and security._is_public_ip("2606:4700::1111")
    for ip in ("100.64.0.1", "10.0.0.1", "127.0.0.1", "169.254.169.254", "224.0.0.1", "0.0.0.0", "192.0.2.1", "::1", "fc00::1"):
        assert not security._is_public_ip(ip), ip


def test_login_brake_is_keyed_by_a_hash_of_the_address(admin):
    assert security._login_key("A@b.com") == security._login_key("  a@B.com ")
    for _ in range(20):
        security.login_failed("victim@example.com")
    assert not security.login_allowed("Victim@Example.com")
    assert "victim@example.com" not in security.LOGIN_FAILS_PER_EMAIL._hits


async def test_logout_get_ignores_cross_site_requests(client, admin):
    r = await client.get("/logout", headers={"sec-fetch-site": "cross-site"})
    assert r.status_code == 302 and r.headers["location"] == "/" and "set-cookie" not in r.headers
    r = await client.get("/logout")
    assert r.headers["location"] == "/login" and "fb_session=" in r.headers.get("set-cookie", "")


async def test_password_change_keeps_this_session_and_drops_the_others(client, admin):
    old_cookie = auth.make_session(admin["id"])
    r = await client.post("/api/account/password", json={"current": "password123", "new": "even-better-pw"})
    assert r.status_code == 200 and "fb_session=" in r.headers.get("set-cookie", "")
    new_cookie = r.headers["set-cookie"].split("fb_session=", 1)[1].split(";", 1)[0]
    async with _make_client(new_cookie) as fresh:
        assert (await fresh.get("/api/me")).status_code == 200
    async with _make_client(old_cookie) as stale:
        assert (await stale.get("/api/me")).status_code in (401, 302, 303)


# ----------------------------------------------------------- webhook edits
async def test_webhook_edits_are_atomic_and_404_on_unknown_ids(client, admin):
    r = await client.post("/api/webhooks", json={"name": "A"})
    wid, token = r.json()["id"], r.json()["token"]
    assert (await client.put("/api/webhooks/nope", json={"name": "x"})).status_code == 404
    assert (await client.post("/api/webhooks/nope/regenerate-token")).status_code == 404
    r = await client.put(f"/api/webhooks/{wid}", json={"name": "B"})
    assert r.status_code == 200 and r.json()["name"] == "B"
    r = await client.post(f"/api/webhooks/{wid}/regenerate-token")
    assert r.json()["token"] != token and r.json()["name"] == "B"
    r = await client.delete(f"/api/webhooks/{wid}")
    assert r.status_code == 200 and (await client.get("/api/webhooks")).json() == []


# ----------------------------------------------------------------- relay
async def test_abandoned_relay_jobs_are_pruned(monkeypatch):
    monkeypatch.setattr(relay, "DISPATCH_TIMEOUT_S", 0.05)
    relay.touch(7)
    for _ in range(3):
        with pytest.raises(relay.AgentOffline):
            await relay.request(7, method="GET", url="https://demo.tradovateapi.com/v1/auth/me", headers={})
    assert relay.pending(7) == 1                      # only the last (already cancelled) job is left…
    assert await relay.next_jobs(7, wait=0.01) == []  # …and the agent never sees it
    assert relay.pending(7) == 0


# -------------------------------------------------------------- drawdown
def test_drawdown_state_writes_are_batched(admin, monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr(drawdown.config, "save_settings", lambda updates, area_id=None: calls.append(updates))
    drawdown._save(1, {"11": {"peak": 1.0}})
    drawdown._save(1, {"11": {"peak": 2.0}})
    assert len(calls) == 1 and drawdown._load(1) == {"11": {"peak": 2.0}}     # memory is current, disk lags
    drawdown.flush()
    assert len(calls) == 2 and calls[-1]["dd_state"] == {"11": {"peak": 2.0}}
    drawdown._save(1, {}, force=True)
    assert len(calls) == 3


# ------------------------------------------------------------ self-hosting
async def test_backup_download_is_a_consistent_sqlite_copy(client, admin):
    import sqlite3, tempfile, os
    r = await client.get("/api/update/backup")
    assert r.status_code == 200 and r.headers["content-disposition"].startswith("attachment") and "fluxbridge-backup-" in r.headers["content-disposition"]
    fd, path = tempfile.mkstemp(suffix=".db"); os.write(fd, r.content); os.close(fd)
    try:
        c = sqlite3.connect(path)
        assert c.execute("SELECT email FROM users").fetchone()[0] == "admin@example.com"
        assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        os.unlink(path)


async def test_backup_download_is_admin_only(anon_client, admin):
    r = await anon_client.get("/api/update/backup")
    assert r.status_code in (401, 302, 303, 403)


def test_restart_under_systemd_is_a_clean_shutdown(monkeypatch):
    from app import updater
    import os, signal
    sent: list = []
    monkeypatch.setenv("INVOCATION_ID", "abc")
    monkeypatch.setattr(os, "kill", lambda pid, sig: sent.append((pid, sig)))
    monkeypatch.setattr(os, "execv", lambda *a: sent.append("execv"))
    updater._restart()
    assert sent == [(os.getpid(), signal.SIGTERM)]

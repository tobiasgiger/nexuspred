"""Tests for the v5 backend internals that have no v4 counterpart: the pooled
HTTP clients, the settings copy semantics / atomic update, and the in-memory
webhook-token index."""
from __future__ import annotations

import asyncio

import pytest

from app import config, context, db, http, main


# ------------------------------------------------------------- http pool
async def test_http_client_is_pooled_per_name_and_loop():
    a = http.client("tradovate")
    assert http.client("tradovate") is a
    assert http.client("outbound") is not a
    assert a.timeout.read == 20.0 and http.client("outbound").timeout.read == 10.0
    http.reset()
    assert http.client("tradovate") is not a


async def test_http_client_replaced_when_loop_changes():
    a = http.client("outbound")
    # Simulate a client created on a previous (now dead) loop.
    http._loops["outbound"] = object()  # type: ignore[assignment]
    b = http.client("outbound")
    assert b is not a and http._loops["outbound"] is asyncio.get_running_loop()


async def test_http_aclose_all_closes_and_forgets():
    a = http.client("outbound")
    await http.aclose_all()
    assert a.is_closed and http._clients == {}
    assert not http.client("outbound").is_closed


# --------------------------------------------------------- settings copies
def test_load_settings_returns_independent_copies(admin):
    config.save_settings({"webhooks": [config.new_webhook("A")]})
    s1 = config.load_settings()
    s1["webhooks"].append({"id": "junk"})
    s1["symbol_map"]["ZZ1!"] = "ZZ"
    s2 = config.load_settings()
    assert [w["id"] for w in s2["webhooks"]] != ["junk"] and len(s2["webhooks"]) == 1
    assert "ZZ1!" not in s2["symbol_map"]
    assert config.DEFAULT_SETTINGS["webhooks"] == [] and "ZZ1!" not in config.DEFAULT_SETTINGS["symbol_map"]


def test_update_is_read_modify_write_and_drops_unknown_keys(admin):
    def mut(s):
        s["default_qty"] = 7
        s["not_a_key"] = True
        s["webhooks"].append(config.new_webhook("U"))

    out = config.update(mut)
    assert out["default_qty"] == 7 and "not_a_key" not in out and out["webhooks"][0]["name"] == "U"
    fresh = config.load_settings(force=True)
    assert fresh["default_qty"] == 7 and len(fresh["webhooks"]) == 1
    assert db.get_area_settings(1)["default_qty"] == 7


def test_update_is_per_area(admin):
    u2 = db.create_user("two@example.com", "password123")
    a2 = db.user_primary_area(u2["id"])
    config.update(lambda s: s.__setitem__("default_qty", 9), area_id=a2)
    assert config.load_settings(area_id=a2)["default_qty"] == 9
    assert config.load_settings(area_id=1)["default_qty"] == 3


# ------------------------------------------------------------ token index
def test_find_webhook_tracks_settings_writes(admin, monkeypatch):
    assert config.find_webhook("nope") == (None, None)
    assert config.find_webhook("") == (None, None)
    wh = config.new_webhook("one")
    config.save_settings({"webhooks": [wh]})
    aid, found = config.find_webhook(wh["token"])
    assert aid == 1 and found["id"] == wh["id"]
    found["name"] = "mutated"  # a private copy, not the index entry
    assert config.find_webhook(wh["token"])[1]["name"] == "one"

    # Once the index is warm, lookups don't touch the database at all.
    def boom(*a, **k):
        raise AssertionError("index must be served from memory")
    monkeypatch.setattr(db, "all_area_ids", boom)
    monkeypatch.setattr(db, "get_area_settings", boom)
    assert config.find_webhook(wh["token"])[0] == 1
    assert config.find_webhook("missing") == (None, None)
    monkeypatch.undo()

    # Regenerating the token / deleting the webhook is reflected immediately.
    config.update(lambda s: s["webhooks"][0].__setitem__("token", "new-token"))
    assert config.find_webhook(wh["token"]) == (None, None)
    assert config.find_webhook("new-token")[1]["id"] == wh["id"]
    config.save_settings({"webhooks": []})
    assert config.find_webhook("new-token") == (None, None)


def test_find_webhook_sees_new_areas_and_prefers_lowest_area(admin):
    wh1 = config.new_webhook("one")
    config.save_settings({"webhooks": [wh1]})
    assert config.find_webhook(wh1["token"])[0] == 1
    u2 = db.create_user("two@example.com", "password123")
    a2 = db.user_primary_area(u2["id"])
    wh2 = config.new_webhook("two")
    dup = {**config.new_webhook("dup"), "token": wh1["token"]}  # same token in a later area
    config.save_settings({"webhooks": [wh2, dup]}, area_id=a2)
    assert config.find_webhook(wh2["token"]) == (a2, wh2)
    assert config.find_webhook(wh1["token"])[0] == 1  # first area wins, as in v4
    assert main._resolve_webhook(wh2["token"])[0] == a2
    db.delete_user(u2["id"])
    assert config.find_webhook(wh2["token"]) == (None, None)


async def test_webhook_ingress_uses_index(client):
    r = await client.post("/api/webhooks", json={"name": "Idx", "strategy": "simple"})
    token = r.json()["token"]
    ok = await client.post(f"/webhook/{token}", json={"action": "buy", "symbol": "MNQ1!"})
    assert ok.status_code == 202
    await client.post(f"/api/webhooks/{r.json()['id']}/regenerate-token")
    assert (await client.post(f"/webhook/{token}", json={"action": "buy", "symbol": "MNQ1!"})).status_code == 403
    with context.use_area(1):
        assert config.load_settings()["webhooks"][0]["token"] != token


# -------------------------------------------------------------- db caches
def test_connection_is_reused_per_thread_and_follows_db_file(admin):
    c1 = db._connect()
    assert db._connect() is c1
    db.DB_FILE = db.DB_FILE.with_name(db.DB_FILE.stem + "-b.db")
    db._initialized = False
    assert db._connect() is not c1


def test_user_caches_invalidate_on_create_and_delete(admin):
    assert db.user_count() == 1
    u2 = db.create_user("two@example.com", "password123")
    assert db.user_count() == 2 and db.get_user(u2["id"])["email"] == "two@example.com"
    assert db.user_primary_area(u2["id"]) == 2
    got = db.get_user(u2["id"])
    got["email"] = "mutated"  # callers get copies, never the cache entry
    assert db.get_user(u2["id"])["email"] == "two@example.com"
    db.delete_user(u2["id"])
    assert db.user_count() == 1 and db.get_user(u2["id"]) is None
    assert db.user_primary_area(u2["id"]) is None


async def test_password_work_runs_off_loop(admin):
    assert (await db.authenticate_async("admin@example.com", "password123"))["id"] == admin["id"]
    assert await db.authenticate_async("admin@example.com", "nope") is None
    await db.set_password_async(admin["id"], "newpassword1")
    assert await db.authenticate_async("admin@example.com", "newpassword1")
    assert await db.authenticate_async("admin@example.com", "password123") is None


async def test_warm_request_path_needs_no_database(client, monkeypatch):
    assert (await client.get("/api/status")).status_code == 200  # warms every cache

    def boom():
        raise AssertionError("request path must not touch SQLite once warm")
    monkeypatch.setattr(db, "_connect", boom)
    r = await client.get("/api/status")
    assert r.status_code == 200 and r.json()["version"] == config.get_version()
    assert (await client.get("/api/settings")).status_code == 200
    assert (await client.get("/api/webhooks")).status_code == 200
    from tests.conftest import _make_client
    async with _make_client() as anon:  # the anonymous path is DB-free as well
        assert (await anon.get("/api/status")).status_code == 401


async def test_setup_is_serialised(anon_client):
    form = {"email": "a@example.com", "password": "password123", "password2": "password123"}
    r1, r2 = await asyncio.gather(anon_client.post("/setup", data=form),
                                  anon_client.post("/setup", data={**form, "email": "b@example.com"}))
    assert sorted([r1.headers["location"], r2.headers["location"]]) == ["/", "/login"]
    assert db.user_count() == 1


def test_version_is_cached_until_forced(monkeypatch):
    assert config.get_version() == "5.0.0-alpha.1"
    monkeypatch.setattr(config, "VERSION_FILE", config.VERSION_FILE.with_name("VERSION.missing"))
    assert config.get_version() == "5.0.0-alpha.1"
    assert config.get_version(force=True) == "0.0.0"
    monkeypatch.undo()
    assert config.get_version(force=True) == "5.0.0-alpha.1"

"""Tests for the v5 backend internals that have no v4 counterpart: the pooled
HTTP clients, the settings copy semantics / atomic update, and the in-memory
webhook-token index."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from app import config, context, db, http, main, signals, state, tradovate
from app.discord_signals import dispatcher, hub, pipeline
from tests.helpers import FakeExecutor, settle


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
    assert config.find_webhook(wh2["token"])[0] == a2
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
    assert (await client.get("/api/webhooks")).status_code == 200  # (incl. subscriber counts)

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
    real = config.VERSION_FILE.read_text(encoding="utf-8").strip()
    assert real.startswith("5.") and config.get_version() == real
    monkeypatch.setattr(config, "VERSION_FILE", config.VERSION_FILE.with_name("VERSION.missing"))
    assert config.get_version() == real
    assert config.get_version(force=True) == "0.0.0"
    monkeypatch.undo()
    assert config.get_version(force=True) == real


# ---------------------------------------------------------- session reload
_TOKENS = [{"name": "L1", "environment": "demo", "enabled": True, "access_token": "t1",
            "md_token": "m1", "qty_multiplier": 1,
            "accounts": [{"spec": "A1", "id": 1, "enabled": True, "qty_multiplier": 1}]}]


def test_reload_keeps_unchanged_sessions_and_adopts_credentials(admin):
    config.save_settings({"token_accounts": _TOKENS})
    m = tradovate.manager()
    m.reload()
    s1 = m.all()[0]
    s1._contract_cache["MNQ"] = ("MNQU6", datetime.now(timezone.utc))
    m.reload()
    assert m.all()[0] is s1 and "MNQ" in s1._contract_cache  # unchanged → same object

    # The session persisting its own renewed token must not evict itself.
    s1._store_token({"accessToken": "renewed", "mdAccessToken": "m2"})
    m.reload()
    assert m.all()[0] is s1 and s1._token == "renewed" and s1._md_token == "m2"

    # A token the user re-pasted in Settings is adopted in place.
    config.update_token_account(0, access_token="pasted", md_token="m3", token_expires="")
    m.reload()
    assert m.all()[0] is s1 and s1._token == "pasted" and s1._md_token == "m3"

    # A config change (toggle / environment / accounts) rebuilds the session.
    config.update_token_account(0, enabled=False)
    m.reload()
    s2 = m.all()[0]
    assert s2 is not s1 and s2.enabled is False and s2._token == "pasted"
    config.update_token_account(0, accounts=[{"spec": "A1", "id": 1, "enabled": False, "qty_multiplier": 2}])
    m.reload()
    assert m.all()[0] is not s2

    # Logins added / removed.
    config.save_settings({"token_accounts": [*_TOKENS, {**_TOKENS[0], "name": "L2"}]})
    m.reload()
    assert [s.name for s in m.all()] == ["L1", "L2"]
    config.save_settings({"token_accounts": []})
    m.reload()
    assert m.all() == []


async def test_connect_persists_accounts_without_evicting_session(admin, monkeypatch):
    config.save_settings({"token_accounts": _TOKENS})
    m = tradovate.manager()
    m.reload()
    s = m.all()[0]

    async def token():
        return "t1"

    async def accounts():
        return [{"name": "A1", "id": 1}, {"name": "A2", "id": 2}]

    async def request(method, path, **kw):
        return {"name": "me"}

    monkeypatch.setattr(s, "_get_token", token)
    monkeypatch.setattr(s, "list_accounts", accounts)
    monkeypatch.setattr(s, "_request", request)
    await s.connect()
    assert [a["spec"] for a in s.accounts] == ["A1", "A2"]
    assert [a["spec"] for a in config.load_settings()["token_accounts"][0]["accounts"]] == ["A1", "A2"]
    m.reload()
    assert m.all()[0] is s  # its own discovery write is not mistaken for a user edit


# -------------------------------------------------------- concurrency paths
async def test_positions_endpoint_gathers_all_accounts(client, monkeypatch):
    class Failing(FakeExecutor):
        async def positions(self):
            raise tradovate.TradovateError("down")

    execs = [FakeExecutor("A", positions=[{"symbol": "MNQU6", "netPos": 1}]),
             Failing("C"),
             FakeExecutor("B", positions=[{"symbol": "ESU6", "netPos": -1}])]
    monkeypatch.setattr(tradovate.SessionManager, "enabled", lambda self: execs)
    r = await client.get("/api/positions")
    assert [p["symbol"] for p in r.json()] == ["MNQU6", "ESU6"]  # order kept, failure skipped


async def test_cancel_working_is_concurrent_and_collects_errors():
    ex = FakeExecutor("A", working=[{"id": 1}, {"id": None}, {"id": 3}])

    async def cancel(order_id):
        ex.calls.append(("cancel", {"order_id": order_id}))
        if order_id == 3:
            raise tradovate.TradovateError("gone")
        await asyncio.sleep(0.01)
        return {}
    ex.cancel_order = cancel
    errors: list[str] = []
    assert await signals._cancel_working(ex, "", errors) == 1
    assert errors == ["cancel 3: gone"]
    assert [c["order_id"] for c in ex.of("cancel")] == [1, 3]

    class Broken(FakeExecutor):
        async def working_orders(self):
            raise tradovate.TradovateError("list failed")
    errs: list[str] = []
    assert await signals._cancel_working(Broken("B"), "", errs) == 0 and errs == ["list orders: list failed"]


@pytest.fixture
def executing(area, monkeypatch):
    config.save_settings({"trading_enabled": True})
    fake = FakeExecutor("A", working=[{"id": 11, "symbol": "MNQU6"}])
    monkeypatch.setattr(signals, "_webhook_executors", lambda wh: [fake])

    async def no_alert(*a, **k):
        pass
    monkeypatch.setattr(signals.alerts, "trade_executed", no_alert)
    return fake


async def test_trade_locks_pruned_after_close_all(executing, webhook_factory):
    wh = webhook_factory("K", strategy="simple")
    await signals.process({"action": "buy", "symbol": "MNQ1!", "qty": 1}, wh)
    key = f"1:live:{wh['id']}:MNQ"
    assert key in signals._trade_locks
    await signals.process({"action": "close_all", "symbol": "MNQ1!"}, wh)
    assert key not in signals._trade_locks
    assert executing.of("cancel") == [{"order_id": 11}] and executing.of("liquidate") == [{"symbol": "MNQU6"}]


async def test_trade_locks_pruned_after_ts_hunter_full_close(executing, webhook_factory):
    wh = webhook_factory("T", strategy="ts_hunter")
    await signals.process({"event": "signal", "trade_id": "t1", "symbol": "MNQ1!", "side": "sell",
                           "risk": {"value": 2}, "sl": {"value": 100}}, wh)
    assert "1:live:ts:t1" in signals._trade_locks
    await signals.process({"event": "management", "action": "partial_close_percent", "percent": 50,
                           "trade_id": "t1", "symbol": "MNQ1!"}, wh)
    assert "1:live:ts:t1" in signals._trade_locks  # still open
    await signals.process({"event": "management", "action": "full_close", "trade_id": "t1",
                           "symbol": "MNQ1!"}, wh)
    assert "1:live:ts:t1" not in signals._trade_locks and signals.active_trades() == {}


async def test_release_trade_lock_keeps_held_or_awaited_locks():
    lk = signals._trade_lock("k")
    await lk.acquire()
    signals._release_trade_lock("k")
    assert signals._trade_locks["k"] is lk  # held → kept

    async def waiter():
        async with lk:
            pass
    t = asyncio.create_task(waiter())
    await asyncio.sleep(0)  # the waiter is now queued on the lock
    lk.release()
    signals._release_trade_lock("k")
    assert signals._trade_locks["k"] is lk  # awaited → kept
    await t
    signals._release_trade_lock("k")
    assert "k" not in signals._trade_locks


# ------------------------------------------------- in-process Discord dispatch
async def test_dispatch_to_bridge_webhook_runs_in_process(area, monkeypatch):
    wh = config.new_webhook("Routed")
    off = {**config.new_webhook("Off"), "enabled": False}
    config.save_settings({"webhooks": [wh, off]})

    def boom(*a, **k):
        raise AssertionError("bridge webhooks must not go over HTTP")
    monkeypatch.setattr(dispatcher.http, "client", boom)

    payload = {"action": "buy", "symbol": "MNQ1!", "qty": 1}
    results = await dispatcher.dispatch([
        {"label": "Routed", "webhook_id": wh["id"], "url": "", "enabled": True},
        {"label": "Off", "webhook_id": off["id"], "url": "", "enabled": True},
        {"label": "Gone", "webhook_id": "wh_missing", "url": "", "enabled": True},
        {"label": "Disabled", "webhook_id": wh["id"], "url": "", "enabled": False},
    ], payload)
    assert [(r["label"], r["ok"], r["status"]) for r in results] == [
        ("Routed", True, 202), ("Off", False, 403), ("Gone", False, 403)]
    assert results[1]["error"] == "HTTP 403" and all("ms" in r for r in results)
    # Same acceptance semantics as the TradingView ingress: logged as received,
    # then executed in the background (trading is off → skipped). Newest first.
    await settle()
    assert [e["result"] for e in state.recent_signals()[:2]] == ["skipped", "received"]
    assert state.recent_signals()[0]["payload"] == payload


async def test_dispatch_custom_url_uses_pooled_client(area, monkeypatch):
    calls = []

    class _Client:
        async def post(self, url, json=None, headers=None, timeout=None):
            calls.append((url, json, headers, timeout))

            class R:
                status_code = 500 if url.endswith("/bad") else 200
            return R()
    monkeypatch.setattr(dispatcher.http, "client", lambda *a, **k: _Client())
    results = await dispatcher.dispatch([
        {"label": "ext", "url": "https://ext/hook", "secret": "s3", "enabled": True},
        {"label": "bad", "url": "https://ext/bad", "enabled": True},
    ], {"x": 1})
    assert calls[0][0] == "https://ext/hook" and calls[0][2]["X-Webhook-Secret"] == "s3" and calls[0][3] == 5.0
    assert "X-Webhook-Secret" not in calls[1][2]
    assert [(r["ok"], r["status"]) for r in results] == [(True, 200), (False, 500)]
    assert results[1]["error"] == "HTTP 500"


async def test_process_embed_end_to_end_in_process(area, monkeypatch):
    wh = config.new_webhook("Routed")
    config.save_settings({"webhooks": [wh], "discord_channels": [{
        "id": "123", "label": "sig", "enabled": True,
        "targets": [{"label": "", "webhook_id": wh["id"], "enabled": True}]}]})
    from app.discord_signals.parser import EmbedField, EmbedLike
    ev = await pipeline.process_embed(
        EmbedLike(title="SELL MNQ", fields=[EmbedField("Contracts", "2")]), "123")
    assert ev["targets"] == [{"label": "Routed", "url": "", "ok": True, "status": 202, "ms": ev["targets"][0]["ms"]}]
    assert state.recent_signals()[0]["payload"]["action"] == "sell"
    assert state.recent_events()[0]["message"].endswith("1/1 webhook targets ok")


# ---------------------------------------------------------- unified stream
async def test_stream_carries_order_session_and_discord_kinds(area):
    sub = state.subscribe(1)
    try:
        state.log_order({"action": "Buy", "symbol": "MNQU6", "account": "A", "qty": 1,
                         "order_type": "Market", "status": "submitted"})
        state.set_session_status("L1", connected=True)
        hub.record({"kind": "signal", "channel_label": "x"})
        await settle()
        kinds = []
        while not sub.queue.empty():
            kinds.append(sub.queue.get_nowait())
        assert [k["kind"] for k in kinds] == ["order", "session", "discord"]
        assert kinds[0]["data"]["symbol"] == "MNQU6" and "ts" in kinds[0]["data"]
        assert kinds[1]["data"] == {"name": "L1", "connected": True}
        assert kinds[2]["data"]["channel_label"] == "x"
    finally:
        state.unsubscribe(sub, 1)


# ---------------------------------------------------------------- lifespan
async def test_lifespan_starts_and_stops_background_loops(admin):
    async with main._lifespan(main.app):
        assert [t.get_name() for t in main._loop_tasks] == ["health-loop", "discord-health-loop", "history-prune-loop", "journal-import-loop", "pnl-loop", "copy-loop"]
        await settle()
        assert all(not t.done() for t in main._loop_tasks)
        with context.use_area(1):
            assert any("Bridge started" in e["message"] for e in state.recent_events())
        http.client("outbound")
    assert main._loop_tasks == [] and http._clients == {}


# ------------------------------------------------------- input hardening
async def test_webhook_crud_rejects_bad_input_with_400(client):
    assert (await client.post("/api/webhooks", json={"name": "X", "default_qty": "abc"})).status_code == 400
    wh = (await client.post("/api/webhooks", json={"name": "X"})).json()
    r = await client.put(f"/api/webhooks/{wh['id']}", json={"tp_qty": "abc"})
    assert r.status_code == 400 and "Invalid webhook payload" in r.json()["detail"]
    r = await client.put(f"/api/webhooks/{wh['id']}", json={"accounts": [{"spec": "A", "token_idx": "x"}]})
    assert r.status_code == 400
    assert (await client.put(f"/api/webhooks/{wh['id']}", json={"tp_qty": 2})).json()["tp_qty"] == 2

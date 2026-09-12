"""alpha.76 — automations per workspace, the order ticket, the exposure view, /metrics."""
from __future__ import annotations

import asyncio

import pytest

from app import alerts, automations, config, context, db, events, exposure, metrics, risk, signals
from app.routers import trading
from tests.helpers import FakeExecutor


# ------------------------------------------------------------ automations
def test_normalize_rules_types_and_bounds():
    r = automations.normalize_rule({"event": "position.closed", "action": "trading_off", "accounts": "DEMO11, DEMO12", "symbols": ["mnqu6", "es"],
                                    "loss_at_least": "-250", "cooldown_s": "999999", "name": "x" * 200})
    assert r["id"].startswith("au_") and r["accounts"] == ["DEMO11", "DEMO12"] and r["symbols"] == ["ES", "MNQ"]
    assert r["loss_at_least"] == 250.0 and r["cooldown_s"] == 86_400 and len(r["name"]) == 80 and r["enabled"] is True
    for bad in ({"event": "nope", "action": "notify"}, {"event": "risk.triggered", "action": "reboot"}, {"event": "risk.triggered", "action": "notify", "accounts": [1]},
                {"event": "risk.triggered", "action": "notify", "loss_at_least": "abc"}, "text", {"event": "risk.triggered", "action": "notify", "accounts": ["a"] * 21}):
        with pytest.raises(ValueError):
            automations.normalize_rule(bad)
    with pytest.raises(ValueError):
        automations.normalize_rules([{"event": "risk.triggered", "action": "notify"}] * 51)
    rules = automations.normalize_rules([{"id": "au_1", "event": "risk.triggered", "action": "notify"}, {"id": "au_1", "event": "news.lock", "action": "notify"}])
    assert rules[0]["id"] != rules[1]["id"]                                          # ids stay unique


def test_matching_filters_and_loss_threshold():
    r = automations.normalize_rule({"event": "position.closed", "action": "notify", "accounts": ["DEMO11"], "symbols": ["MNQ"], "loss_at_least": 100})
    ok = {"account": "DEMO11", "symbol": "MNQZ6", "pnl": -150.0}
    assert automations.matches(r, "position.closed", ok, 1)
    assert not automations.matches(r, "position.closed", {**ok, "pnl": -50.0}, 1)   # not enough of a loss
    assert not automations.matches(r, "position.closed", {**ok, "pnl": "x"}, 1)
    assert not automations.matches(r, "position.closed", {**ok, "account": "DEMO12"}, 1)
    assert not automations.matches(r, "position.closed", {**ok, "symbol": "ESZ6"}, 1)
    assert not automations.matches(r, "position.opened", ok, 1)
    assert not automations.matches({**r, "enabled": False}, "position.closed", ok, 1)
    r2 = automations.normalize_rule({"event": "trade.executed", "action": "notify", "accounts": ["B"]})
    assert automations.matches(r2, "trade.executed", {"webhook": "w", "accounts": ["A", "B"]}, 1)
    assert not automations.matches(r2, "trade.executed", {"webhook": "w", "accounts": ["A"]}, 1)


def test_render_message_placeholders():
    r = {"name": "Loss", "message": "{account} lost {pnl} on {symbol} ({event})"}
    assert automations.render_message(r, "position.closed", {"account": "DEMO11", "pnl": -80.0, "symbol": "MNQZ6"}) == "DEMO11 lost -80.0 on MNQZ6 (position.closed)"
    assert automations.render_message({"name": "N", "message": "{nope}"}, "x", {}) == "{nope}"
    assert automations.render_message({"name": "N", "message": ""}, "risk.triggered", {"spec": "DEMO11", "reason": "loss"}).startswith("N: risk.triggered")


@pytest.fixture
def notified(monkeypatch, admin):
    sent: list[tuple[str, str]] = []

    async def fake(name, message):
        sent.append((name, message))
    monkeypatch.setattr(alerts, "automation", fake)
    return sent


async def test_rule_fires_once_per_cooldown_and_switches_trading_off(notified):
    config.save_settings({"trading_enabled": True, "automations": automations.normalize_rules([
        {"id": "au_off", "name": "Kill", "event": "risk.triggered", "action": "trading_off", "cooldown_s": 3600}])})
    with context.use_area(1):
        assert await events.emit_async("risk.triggered", spec="DEMO11", kind="loss", reason="daily loss limit", pnl=-500.0, errors=[]) >= 1
        await events.emit_async("risk.triggered", spec="DEMO11", kind="loss", reason="again", pnl=-500.0, errors=[])
    assert config.load_settings(area_id=1)["trading_enabled"] is False
    assert len(notified) == 1 and notified[0][0] == "Kill" and "trading switched OFF" in notified[0][1]
    log = automations.recent(1)
    assert len(log) == 1 and log[0]["action"] == "trading_off" and log[0]["result"] == "trading switched off"
    assert [e for e in events.recent(kind="automation.fired")][-1]["rule"] == "au_off"


async def test_rule_without_area_context_or_unknown_kind_is_ignored(notified):
    config.save_settings({"automations": automations.normalize_rules([{"event": "news.lock", "action": "notify"}])}, area_id=1)
    assert automations._on_event("automation.fired", {}) is None
    tok = context.set_area(None)
    try:
        assert automations._on_event("news.lock", {"title": "CPI"}) is None
    finally:
        context.reset_area(tok)
    assert notified == []


async def test_pause_webhook_and_flatten_account_actions(notified, monkeypatch, webhook_factory):
    with context.use_area(1):
        w = webhook_factory(name="Scalper")
        config.save_settings({"automations": automations.normalize_rules([
            {"event": "signal.failed", "action": "pause_webhook", "cooldown_s": 0},
            {"event": "position.closed", "action": "lock_account", "loss_at_least": 100, "cooldown_s": 0}])})
        flattened: list[tuple[str, str]] = []

        async def fake_flatten(area_id, specs, *, lock_reason=""):
            flattened.append((",".join(specs), lock_reason))
            return "DEMO11: 1 cancelled, 1 flattened"
        monkeypatch.setattr(automations, "_flatten_accounts", fake_flatten)
        await events.emit_async("signal.failed", webhook="Scalper", reason="boom")
        assert [x for x in config.load_settings()["webhooks"] if x["id"] == w["id"]][0]["enabled"] is False
        assert "paused: Scalper" in notified[-1][1]
        await events.emit_async("position.closed", account="DEMO11", symbol="MNQZ6", direction="LONG", qty=2, pnl=-300.0, duration="", remaining=0)
        assert flattened == [("DEMO11", "automation 'position.closed → lock_account'")]
    assert len(automations.recent(1)) == 2


async def test_lock_account_flattens_and_locks_for_today(monkeypatch, admin):
    from tests.test_risk import Sess
    sess = Sess()
    monkeypatch.setattr(automations, "_find_account", lambda aid, spec: (sess, sess.accounts[0]) if spec == "DEMO11" else None)
    detail = await automations._flatten_accounts(1, ["DEMO11", "GHOST"], lock_reason="automation 'x'")
    assert "DEMO11: 1 cancelled, 1 flattened — locked for today" in detail and "GHOST: not found" in detail
    assert risk.is_locked(1, "DEMO11") == "automation 'x'" and sess.pos == [] and sess.orders == []


async def test_a_failing_action_is_logged_and_never_raises(notified, monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("smtp down")
    monkeypatch.setattr(alerts, "automation", boom)
    config.save_settings({"automations": automations.normalize_rules([{"name": "N", "event": "news.lock", "action": "notify"}])}, area_id=1)
    with context.use_area(1):
        await events.emit_async("news.lock", title="CPI", currency="USD", until="14:00")
    log = automations.recent(1)
    assert log and log[0]["result"].startswith("failed: RuntimeError")


async def test_automations_api_roundtrip_and_validation(client, admin):
    r = await client.get("/api/automations")
    assert r.status_code == 200 and r.json()["rules"] == [] and "risk.triggered" in [e["id"] for e in r.json()["events"]]
    r = await client.put("/api/automations", json={"rules": [{"name": "Kill", "event": "risk.triggered", "action": "trading_off"}]})
    assert r.status_code == 200 and r.json()["rules"][0]["name"] == "Kill" and r.json()["rules"][0]["id"].startswith("au_")
    assert config.load_settings(area_id=1)["automations"][0]["name"] == "Kill"
    r = await client.put("/api/automations", json={"rules": [{"event": "nope", "action": "notify"}]})
    assert r.status_code == 400 and "unknown event" in r.json()["detail"]
    r = await client.post("/api/settings", json={"automations": []})                 # protected: the generic form cannot clear it
    assert r.status_code == 200 and config.load_settings(area_id=1)["automations"][0]["name"] == "Kill"
    assert any(a["action"] == "automations_saved" for a in db.list_audit())


async def test_settings_export_carries_automations_and_import_validates(client, admin):
    await client.put("/api/automations", json={"rules": [{"name": "Kill", "event": "risk.triggered", "action": "trading_off"}]})
    doc = (await client.get("/api/settings/export")).json()
    assert doc["settings"]["automations"][0]["name"] == "Kill"
    doc["settings"]["automations"] = [{"event": "bogus", "action": "notify"}]
    r = await client.post("/api/settings/import", json=doc)
    assert r.status_code == 400 and "automations" in r.json()["detail"]


# ------------------------------------------------------------ order ticket
@pytest.fixture
def ticket(monkeypatch, admin):
    ex = FakeExecutor("DEMO11", positions=[{"symbol": "MNQZ6", "account": "DEMO11", "netPos": 2, "netPrice": 20000.0}],
                      working=[{"id": 5, "symbol": "MNQZ6"}, {"id": 6, "symbol": "ESZ6"}])
    monkeypatch.setattr(trading, "_executor", lambda body: ex)
    return ex


async def test_manual_order_validates_and_places(client, ticket):
    base = {"lid": "L1", "spec": "DEMO11", "symbol": "MNQ1!", "action": "buy", "qty": 2}
    r = await client.post("/api/orders/manual", json=base)
    assert r.status_code == 409 and "Trading is disabled" in r.json()["detail"]                # the master switch holds
    config.save_settings({"trading_enabled": True, "symbol_map": {"MNQ1!": "MNQZ6"}})
    for bad in ({**base, "qty": 0}, {**base, "qty": 101}, {**base, "action": "hold"}, {**base, "order_type": "Iceberg"},
                {**base, "order_type": "Limit"}, {**base, "order_type": "Stop", "stop_price": "-1"}, {**base, "symbol": ""}):
        r = await client.post("/api/orders/manual", json=bad)
        assert r.status_code == 400, bad
    assert ticket.of("place") == []
    r = await client.post("/api/orders/manual", json={**base, "order_type": "Limit", "price": "19990.5"})
    assert r.status_code == 200 and r.json()["contract"] == "MNQZ6" and r.json()["status"] == "submitted"
    p = ticket.of("place")[-1]
    assert p["symbol"] == "MNQZ6" and p["action"] == "Buy" and p["qty"] == 2 and p["order_type"] == "Limit" and p["price"] == 19990.5 and p["stop_price"] is None
    assert any(a["action"] == "manual_order" for a in db.list_audit())


async def test_manual_order_rejection_and_rate_limit(client, ticket, monkeypatch):
    config.save_settings({"trading_enabled": True})
    ticket.fail_place = True
    r = await client.post("/api/orders/manual", json={"lid": "L1", "spec": "DEMO11", "symbol": "MNQ", "action": "sell", "qty": 1})
    assert r.status_code == 502 and "placeorder failed" in r.json()["detail"]
    monkeypatch.setattr(trading, "_TICKET_LIMIT", type(trading._TICKET_LIMIT)(1, 60))
    await client.post("/api/orders/manual", json={"lid": "L1", "spec": "DEMO11", "symbol": "MNQ", "action": "sell", "qty": 1})
    r = await client.post("/api/orders/manual", json={"lid": "L1", "spec": "DEMO11", "symbol": "MNQ", "action": "sell", "qty": 1})
    assert r.status_code == 429


async def test_close_position_cancels_that_contract_then_liquidates_and_untracks(client, ticket):
    with context.use_area(1):
        signals._map_for(False)["wh_x:MNQ"] = {"contract": "MNQZ6", "accounts": {"DEMO11": {"contract": "MNQZ6", "qty": 2}, "OTHER": {"contract": "MNQZ6", "qty": 1}}}
    r = await client.post("/api/positions/close", json={"lid": "L1", "spec": "DEMO11", "symbol": "MNQZ6"})
    assert r.status_code == 200 and r.json()["cancelled"] == 1 and r.json()["errors"] == []
    assert ticket.of("cancel") == [{"order_id": 5}] and ticket.of("liquidate") == [{"symbol": "MNQZ6"}]   # ESZ6's order untouched
    with context.use_area(1):
        assert signals.active_trades()["wh_x:MNQ"]["accounts"] == {"OTHER": {"contract": "MNQZ6", "qty": 1}}
    r = await client.post("/api/positions/close", json={"lid": "L1", "spec": "DEMO11", "symbol": ""})
    assert r.status_code == 400


def test_executor_lookup_requires_a_login_and_account(admin):
    from fastapi import HTTPException
    with context.use_area(1):
        for body in ({}, {"spec": "DEMO11"}, {"spec": "DEMO11", "token_idx": "x"}):
            with pytest.raises(HTTPException) as exc:
                trading._executor(body)
            assert exc.value.status_code == 400
        with pytest.raises(HTTPException) as exc:
            trading._executor({"spec": "DEMO11", "token_idx": 0})
        assert exc.value.status_code == 404


# ---------------------------------------------------------------- exposure
def test_exposure_summary_flags_hedges_and_concentration():
    rows = [
        {"symbol": "MNQZ6", "account": "A", "netPos": 2, "netPrice": 20000.0},
        {"symbol": "MNQZ6", "account": "B", "netPos": -1, "netPrice": 20010.0},
        {"symbol": "ESZ6", "account": "A", "netPos": 1, "netPrice": 5000.0},
        {"symbol": "ESZ6", "account": "C", "netPos": 0, "netPrice": 5000.0},        # flat rows are ignored
    ]
    x = exposure.summarize(rows)
    mnq = next(s for s in x["symbols"] if s["root"] == "MNQ")
    assert mnq["long"] == 2 and mnq["short"] == 1 and mnq["net"] == 1 and mnq["accounts"] == 2 and mnq["contracts"] == ["MNQZ6"]
    assert mnq["notional"] == round(2 * 20000 * 2 + 1 * 20010 * 2, 2)
    assert x["total_notional"] == round(mnq["notional"] + 5000 * 50, 2) and x["contracts"] == 4
    kinds = {w["kind"]: w for w in x["warnings"]}
    assert kinds["hedged"]["root"] == "MNQ" and "long on A" in kinds["hedged"]["detail"]
    assert kinds["concentration"]["root"] == "ES"                                  # 250 000 of 330 020
    a = next(r for r in x["accounts"] if r["account"] == "A")
    assert a["contracts"] == 3 and a["symbols"] == ["MNQ", "ES"] and 0 < a["share"] <= 1
    assert exposure.summarize([]) == {"symbols": [], "accounts": [], "total_notional": 0.0, "contracts": 0, "warnings": []}


async def test_exposure_endpoint_and_positions_carry_the_login(client, admin, monkeypatch):
    ex = FakeExecutor("DEMO11", positions=[{"symbol": "MNQZ6", "account": "DEMO11", "netPos": 1, "netPrice": 100.0}])
    monkeypatch.setattr(exposure.tradovate, "manager", lambda: type("M", (), {"enabled": staticmethod(lambda: [ex])})())
    r = await client.get("/api/exposure")
    assert r.status_code == 200
    body = r.json()
    assert body["positions"][0]["spec"] == "DEMO11" and "lid" in body["positions"][0] and body["symbols"][0]["root"] == "MNQ"
    r = await client.get("/api/positions")
    assert r.status_code == 200 and r.json()[0]["symbol"] == "MNQZ6"


# ----------------------------------------------------------------- metrics
async def test_metrics_endpoint_is_off_without_a_token_and_bearer_protected(client, anon_client, monkeypatch):
    monkeypatch.delenv("NEXUSPRED_METRICS_TOKEN", raising=False)
    assert (await anon_client.get("/metrics")).status_code == 404
    monkeypatch.setenv("NEXUSPRED_METRICS_TOKEN", "s3cret")
    r = await anon_client.get("/metrics")
    assert r.status_code == 401 and r.headers.get("www-authenticate") == "Bearer"
    assert (await client.get("/metrics")).status_code == 401                        # a session cookie is not a scraper credential
    assert (await anon_client.get("/metrics", headers={"Authorization": "Bearer wrong"})).status_code == 401
    r = await anon_client.get("/metrics", headers={"Authorization": "Bearer s3cret"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain") and r.headers.get("cache-control") == "no-store"
    text = r.text
    assert "fluxbridge_up 1" in text and "# TYPE fluxbridge_uptime_seconds gauge" in text and 'fluxbridge_info{version="' in text


async def test_metrics_count_bus_events_and_signal_latency(client, anon_client, monkeypatch):
    monkeypatch.setenv("NEXUSPRED_METRICS_TOKEN", "s3cret")
    with context.use_area(1):
        events.emit("signal.done", webhook="w", status="ok", reason="", action="buy", seconds=0.02)
        events.emit("signal.done", webhook="w", status="error", reason="x", action="", seconds=3.0)
        events.emit("execution.problem", title="t", message="m")
        events.emit("connection.lost", account="L1", environment="demo", error="", broker="tradovate")
    await asyncio.sleep(0)
    text = (await anon_client.get("/metrics", headers={"Authorization": "Bearer s3cret"})).text
    assert 'fluxbridge_signals_total{status="ok"} 1' in text and 'fluxbridge_signals_total{status="error"} 1' in text
    assert 'fluxbridge_events_total{kind="execution.problem"} 1' in text and 'fluxbridge_connection_changes_total{broker="tradovate",state="lost"} 1' in text
    assert 'fluxbridge_signal_seconds_bucket{status="ok",le="0.025"} 1' in text and 'fluxbridge_signal_seconds_bucket{status="error",le="+Inf"} 1' in text
    assert 'fluxbridge_signal_seconds_count{status="ok"} 1' in text and 'fluxbridge_signal_seconds_sum{status="error"} 3' in text
    assert metrics.counter("fluxbridge_execution_problems_total") == 1.0


async def test_signal_done_carries_the_latency(admin, monkeypatch):
    seen: list[dict] = []
    off = events.subscribe("signal.done", lambda e: seen.append(e))
    try:
        with context.use_area(1):
            await signals.process_background({"action": "buy", "symbol": "MNQ"}, {"name": "w", "strategy": "simple", "accounts": [], "enabled": True})
    finally:
        off()
    assert seen and isinstance(seen[-1]["seconds"], float) and seen[-1]["seconds"] >= 0

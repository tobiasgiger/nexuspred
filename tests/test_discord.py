"""Characterisation of the Discord signal module: parser, payload translation,
target resolution, pipeline (dry-run / dispatch), de-duplication and routes."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app import config, context, db, state
from app.discord_signals import hub, listener, pipeline
from app.discord_signals.parser import EmbedField, EmbedLike, embed_from_dict, parse_embed, to_float
from app.discord_signals.routes import _merge_channels


def emb(title, **fields):
    return EmbedLike(title=title, fields=[EmbedField(k, v) for k, v in fields.items()])


# ---------------------------------------------------------------- parser
def test_parse_entry():
    s = parse_embed(emb("AkSniper 🎯 · SELL MNQ", Contracts="3", Entry="20,100.25", Time="x"), 42)
    assert s.event_type == "entry" and s.side == "short" and s.symbol == "MNQ"
    assert s.contracts == 3 and s.entry_price == 20100.25 and s.source_channel_id == 42
    assert parse_embed(emb("BUY MES"), 1).side == "long"


def test_parse_sl_tp_update():
    s = parse_embed(emb("AkSniper 🎯 · Stop / target moved · MNQ", Stop="20,050 → 20,080", Target="—"), 1)
    assert s.event_type == "sl_tp_update" and s.symbol == "MNQ"
    assert s.stop_price == 20080 and s.target_price is None


def test_parse_close_with_emoji_and_unicode_minus():
    s = parse_embed(emb("🔴 Closed MNQ · −83.00 pts", **{"P&L": "−$166.00", "Exit": "20,000"}), 1)
    assert s.event_type == "close" and s.symbol == "MNQ"
    assert s.pnl_usd == -166.0 and s.pnl_points == -83.0 and s.exit_price == 20000
    s2 = parse_embed(emb("⚪ Closed MNQ", Move="+12.5"), 1)
    assert s2.pnl_points == 12.5


def test_parse_unrecognised_and_tolerance():
    assert parse_embed(emb("Good morning traders"), 1) is None
    assert parse_embed(EmbedLike(title=None), 1) is None
    assert to_float("−1,234.5") == -1234.5 and to_float("—") is None and to_float(None) is None


def test_embed_from_dict_accepts_list_and_mapping():
    a = embed_from_dict({"title": "t", "fields": [{"name": "A", "value": 1}]})
    b = embed_from_dict({"title": "t", "fields": {"A": 1}})
    assert a.fields[0].value == "1" and b.fields[0].name == "A"


# ------------------------------------------------------- payload translation
def test_build_trade_payload_mapping():
    sig = {"event_type": "entry", "side": "long", "symbol": "MNQ", "contracts": 3,
           "entry_price": 100.0, "stop_price": 90.0, "target_price": 110.0}
    p = pipeline.build_trade_payload(sig, received_at="ts", source="message")
    assert p["action"] == "buy" and p["symbol"] == "MNQ" and p["qty"] == 3
    assert p["entry"] == 100.0 and p["sl"] == 90.0 and p["tp1"] == 110.0
    assert p["received_at"] == "ts" and p["source"] == "message" and p["event_type"] == "entry"

    short = pipeline.build_trade_payload({"event_type": "entry", "side": "short", "symbol": "MNQ"},
                                         received_at="t", source="s")
    assert short["action"] == "sell" and "qty" not in short
    close = pipeline.build_trade_payload({"event_type": "close", "symbol": "MNQ"}, received_at="t", source="s")
    assert close["action"] == "close_all"
    upd = pipeline.build_trade_payload({"event_type": "sl_tp_update", "symbol": "MNQ", "stop_price": 1.0},
                                       received_at="t", source="s")
    assert upd["action"] == "set_sl_tp" and upd["stop_price"] == 1.0
    assert "action" not in pipeline.build_trade_payload({"event_type": "weird"}, received_at="t", source="s")


# ------------------------------------------------------------ resolve_target
def test_resolve_target(admin):
    wh = config.new_webhook("W")
    config.save_settings({"webhooks": [wh]})
    r = pipeline.resolve_target({"webhook_id": wh["id"], "label": ""})
    # v5: bridge webhooks are dispatched in-process (v4 looped back over HTTP).
    assert r == {"label": "W", "webhook_id": wh["id"], "url": "", "secret": ""}
    assert pipeline.resolve_target({"webhook_id": "gone"}) is None
    assert pipeline.resolve_target({"url": " https://x ", "secret": "s"}) == {"label": "https://x", "url": "https://x", "secret": "s"}
    assert pipeline.resolve_target({}) is None


def test_channel_lookup(admin):
    config.save_settings({"discord_channels": [
        {"id": "1", "label": "a", "enabled": True, "targets": []},
        {"id": 2, "label": "b", "enabled": False, "targets": []}]})
    assert pipeline.watched_channel_ids() == {"1"}
    assert pipeline.find_channel(2)["label"] == "b" and pipeline.find_channel("9") is None


# --------------------------------------------------------------- pipeline
@pytest.fixture
def channel(admin):
    wh = config.new_webhook("Routed")
    config.save_settings({"webhooks": [wh], "discord_channels": [{
        "id": "123", "label": "cosniper", "enabled": True,
        "targets": [{"label": "", "webhook_id": wh["id"], "enabled": True},
                    {"label": "ext", "url": "https://ext/hook", "secret": "s3", "enabled": True},
                    {"label": "off", "url": "https://off", "enabled": False}]}]})
    return wh


async def test_process_embed_ignores_unwatched_unless_forced(channel):
    assert await pipeline.process_embed(emb("SELL MNQ"), "999") is None
    ev = await pipeline.process_embed(emb("SELL MNQ"), "999", force=True)
    assert ev["kind"] == "signal" and ev["channel_label"] == "channel 999" and ev["targets"] == []


async def test_process_embed_dry_run_records_without_dispatch(channel, monkeypatch):
    config.save_settings({"discord_dry_run": True})

    async def boom(*a, **k):
        raise AssertionError("dispatch must not run in dry-run")

    monkeypatch.setattr(pipeline.dispatcher, "dispatch", boom)
    ev = await pipeline.process_embed(emb("SELL MNQ", Contracts="2"), "123")
    assert ev["dry_run"] is True and ev["kind"] == "signal" and "latency_ms" in ev
    assert [t["skipped"] for t in ev["targets"]] == ["dry_run", "dry_run"]
    assert hub.recent()[0] is ev or hub.recent()[0]["ts"] == ev["ts"]
    assert any("DRY-RUN" in e["message"] for e in state.recent_events())


async def test_process_embed_dispatches_translated_payload(channel, monkeypatch):
    seen = {}

    async def fake_dispatch(targets, payload):
        seen["targets"], seen["payload"] = targets, payload
        return [{"label": t["label"], "url": t["url"], "ok": True, "status": 202, "ms": 1.0} for t in targets]

    monkeypatch.setattr(pipeline.dispatcher, "dispatch", fake_dispatch)
    ev = await pipeline.process_embed(emb("AkSniper 🎯 · SELL MNQ", Contracts="3", Entry="100"), "123")
    p = seen["payload"]
    assert p["action"] == "sell" and p["symbol"] == "MNQ" and p["qty"] == 3 and p["entry"] == 100
    labels = [t["label"] for t in seen["targets"]]
    assert labels == ["Routed", "ext"]  # disabled target excluded
    assert seen["targets"][0]["webhook_id"] == channel["id"] and seen["targets"][0]["secret"] == ""
    assert seen["targets"][1]["secret"] == "s3"
    assert ev["targets"][0]["ok"] is True and ev["kind"] == "signal" and ev["source"] == "message"
    assert state.recent_events()[0]["message"].endswith("2/2 webhook targets ok")


async def test_process_embed_unrecognised_is_surfaced(channel):
    ev = await pipeline.process_embed(emb("hello"), "123")
    assert ev["kind"] == "unrecognized" and ev["raw"]["title"] == "hello" and ev["targets"] == []
    assert state.recent_events()[0]["level"] == "warn"


async def test_hub_is_per_area(admin):
    hub.record({"kind": "x"})
    assert len(hub.recent()) == 1
    with context.use_area(2):
        assert hub.recent() == []


# ----------------------------------------------------------- de-duplication
def test_listener_dedups_message_and_edit(admin):
    m = listener.ListenerManager(area_id=1)
    msg = SimpleNamespace(id=555)
    e = emb("SELL MNQ", Contracts="1")
    assert m._is_duplicate(msg, e) is False
    assert m._is_duplicate(msg, e) is True                       # edit re-render of the same signal
    assert m._is_duplicate(msg, emb("SELL MNQ", Contracts="2")) is False  # genuinely changed
    assert m._is_duplicate(SimpleNamespace(id=None), e) is False


def test_listener_status_shape(admin):
    m = listener.ListenerManager(area_id=1)
    st = m.status()
    for key in ("state", "connected", "enabled", "dry_run", "has_token", "library_available", "watched_channels", "health"):
        assert key in st
    assert st["state"] == "stopped" and st["health"] == "idle"


# ------------------------------------------------------------------ routes
def test_merge_channels_restores_masked_secrets(admin):
    config.save_settings({"discord_channels": [{"id": "1", "label": "c", "enabled": True,
                                                "targets": [{"label": "t", "url": "https://u", "secret": "real", "enabled": True}]}]})
    out = _merge_channels([
        {"id": " 1 ", "label": "", "targets": [
            {"label": "t", "url": "https://u", "secret": "********", "enabled": True},
            {"label": "w", "webhook_id": "wh_x", "url": "ignored", "secret": "ignored"},
            {"label": "empty"}]},
        {"id": "", "targets": []},
    ])
    assert out == [{"id": "1", "label": "channel 1", "enabled": True, "targets": [
        {"label": "t", "webhook_id": "", "url": "https://u", "secret": "real", "enabled": True},
        {"label": "w", "webhook_id": "wh_x", "url": "", "secret": "", "enabled": True}]}]


async def test_discord_config_routes(client):
    db.set_area_feature(1, "discord_signals", True)  # routes are gated on the entitlement
    r = await client.post("/api/discord/config", json={
        "discord_enabled": True, "discord_dry_run": True, "discord_user_token": " tok ",
        "discord_channels": [{"id": "1", "label": "c", "targets": [{"url": "https://u", "secret": "s"}]}]})
    body = r.json()
    assert body["discord_enabled"] is True and body["discord_user_token"] == "********"
    assert body["discord_channels"][0]["targets"][0]["secret"] == "********"
    await client.post("/api/discord/config", json={"discord_user_token": "********"})
    with context.use_area(1):
        assert config.load_settings()["discord_user_token"] == "tok"
    st = (await client.get("/api/discord/status")).json()
    assert st["enabled"] is True and st["dry_run"] is True and st["has_token"] is True
    assert (await client.get("/api/discord/signals")).json() == []


async def test_discord_test_route(client):
    r = await client.post("/api/discord/test", json={"channel_id": "77", "embed": {"title": "SELL MNQ", "fields": {"Contracts": "1"}}})
    ev = r.json()["event"]
    assert ev["kind"] == "signal" and ev["source"] == "test" and ev["signal"]["side"] == "short"
    assert (await client.post("/api/discord/test", json={})).json() == {"error": "channel_id is required"}
    assert (await client.get("/api/discord/signals")).json()[0]["ts"] == ev["ts"]

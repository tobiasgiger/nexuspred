"""Per-account risk guard (app/risk.py): rules, flatten + lock, the order chokepoint, API."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app import alerts, config, context, risk, tradovate


class Sess(tradovate.TradovateSession):
    """A real session object whose Tradovate calls are answered locally: one
    account holding a position and a working order."""
    def __init__(self, positions=None, orders=None):
        super().__init__(0, {"name": "L1", "environment": "demo", "enabled": True, "access_token": "t",
                             "accounts": [{"id": 11, "spec": "DEMO11", "enabled": True,
                                           "risk": {"loss_limit": 500, "profit_limit": 0, "flatten_at": ""}}]}, area_id=1)
        self.pos = positions if positions is not None else [{"accountId": 11, "contractId": 901, "netPos": 2}]
        self.orders = orders if orders is not None else [{"id": 5, "accountId": 11, "ordStatus": "Working", "contractId": 901}]
        self.calls: list[tuple[str, str, dict]] = []

    def has_token(self):
        return True

    async def _request(self, method, path, **kw):
        self.calls.append((method, path, kw))
        if path == "/position/list":
            return [dict(p) for p in self.pos]
        if path == "/order/list":
            return [dict(o) for o in self.orders]
        if path == "/contract/item":
            return {"id": kw["params"]["id"], "name": "MNQZ6"}
        if path == "/contract/find":
            return {"id": 901, "name": "MNQZ6"}
        if path == "/order/cancelorder":
            self.orders = []
            return {}
        if path == "/order/liquidateposition":
            self.pos = []
            return {}
        raise AssertionError(path)


def _snap(realized, open_=0.0):
    return {"account_id": 11, "spec": "DEMO11", "realized": realized, "open": open_}


@pytest.fixture
def sent(monkeypatch):
    calls = []

    async def rec(*a, **k):
        calls.append((a, k))
    monkeypatch.setattr(alerts, "risk_triggered", rec)
    return calls


def test_normalize_and_evaluate():
    r = risk.normalize({"loss_limit": "500", "profit_limit": 0, "flatten_at": "15:55"})
    assert r == {"loss_limit": 500.0, "profit_limit": 0.0, "flatten_at": "15:55"}
    assert risk.normalize({}) == {"loss_limit": 0.0, "profit_limit": 0.0, "flatten_at": ""}
    for bad in ({"loss_limit": -1}, {"flatten_at": "25:00"}, {"flatten_at": "abc"}):
        with pytest.raises(ValueError):
            risk.normalize(bad)
    assert not risk.active(risk.normalize({})) and risk.active(r)
    noon = datetime(2026, 9, 10, 12, 0, tzinfo=ZoneInfo("Europe/Zurich"))
    assert risk.evaluate(r, -499.99, noon) is None
    assert risk.evaluate(r, -500, noon)[0] == "loss"
    assert risk.evaluate({"profit_limit": 1000}, 1000, noon)[0] == "profit"
    assert risk.evaluate(r, 10, noon.replace(hour=15, minute=55))[0] == "time"
    assert risk.evaluate(r, 10, noon.replace(hour=15, minute=54)) is None


async def test_loss_limit_flattens_locks_and_blocks_orders(admin, sent):
    sess = Sess()
    snaps = [_snap(-200, -350)]                       # realised + open = -550 < -500
    fired = await risk.check_area(1, [sess], snaps)
    assert len(fired) == 1 and fired[0]["kind"] == "loss" and fired[0]["cancelled"] == 1 and fired[0]["flattened"] == 1
    assert snaps[0]["risk"]["locked"] and "daily loss limit" in snaps[0]["risk"]["reason"]
    assert risk.is_locked(1, "DEMO11") and sess.pos == [] and sess.orders == []
    assert sent and sent[0][0][0] == "DEMO11" and sent[0][0][1] == "loss"
    # a second tick does not fire again
    assert await risk.check_area(1, [sess], [_snap(-550)]) == []
    # the chokepoint refuses bridge orders for the locked account, the bypass lets flatten through
    real = tradovate.TradovateSession(0, {"name": "L1", "environment": "demo", "enabled": True, "access_token": "t",
                                          "accounts": [{"spec": "DEMO11", "id": 11, "enabled": True}]}, area_id=1)
    seen = []

    async def fake_request(method, path, **kw):
        seen.append(path)
        return {"orderId": 77}
    real._request = fake_request
    with context.use_area(1):
        with pytest.raises(tradovate.TradovateError, match="risk guard"):
            await real.place_order(symbol="MNQZ6", action="Buy", qty=1, order_type="Market", account_spec="DEMO11", account_id=11)
        assert seen == []
        with risk.bypass():
            r = await real.place_order(symbol="MNQZ6", action="Sell", qty=1, order_type="Market", account_spec="DEMO11", account_id=11)
        assert r["status"] == "submitted" and seen == ["/order/placeorder"]
        # another account of the same login is not affected
        await real.place_order(symbol="MNQZ6", action="Buy", qty=1, order_type="Market", account_spec="DEMO12", account_id=12)


async def test_locked_account_is_flattened_again_and_unlocks_next_day(admin, sent, monkeypatch):
    sess = Sess()
    await risk.check_area(1, [sess], [_snap(-600)])
    assert risk.is_locked(1, "DEMO11")
    # a manual position reappears while locked → closed again (throttled)
    sess.pos = [{"accountId": 11, "contractId": 901, "netPos": 1}]
    await risk.check_area(1, [sess], [_snap(-600, 25)])
    assert sess.pos == []
    sess.pos = [{"accountId": 11, "contractId": 901, "netPos": 1}]
    await risk.check_area(1, [sess], [_snap(-600, 25)])
    assert sess.pos != []                       # within the 30 s re-flatten hold-off
    # next local day: the lock is gone and rules apply afresh
    tomorrow = datetime.now(timezone.utc) + timedelta(days=1)
    monkeypatch.setattr(risk, "local_now", lambda area_id, settings=None: tomorrow.astimezone(ZoneInfo("Europe/Zurich")))
    assert risk.is_locked(1, "DEMO11") is None
    fired = await risk.check_area(1, [sess], [_snap(-10)])
    assert fired == [] and sess.pos != []


async def test_profit_target_and_flatten_time(admin, sent, monkeypatch):
    sess = Sess()
    sess.accounts[0]["risk"] = {"loss_limit": 0, "profit_limit": 300, "flatten_at": ""}
    fired = await risk.check_area(1, [sess], [_snap(310)])
    assert fired[0]["kind"] == "profit"
    risk.unlock(1, "DEMO11")
    sess.accounts[0]["risk"] = {"loss_limit": 0, "profit_limit": 0, "flatten_at": "15:55"}
    fixed = datetime(2026, 9, 10, 15, 54, tzinfo=ZoneInfo("Europe/Zurich"))
    monkeypatch.setattr(risk, "local_now", lambda area_id, settings=None: fixed)
    assert await risk.check_area(1, [sess], [_snap(5)]) == []
    fixed = fixed.replace(minute=55)
    monkeypatch.setattr(risk, "local_now", lambda area_id, settings=None: fixed)
    fired = await risk.check_area(1, [sess], [_snap(5)])
    assert fired[0]["kind"] == "time" and risk.is_locked(1, "DEMO11")


async def test_api_saves_rules_lists_and_unlocks(client, admin):
    with context.use_area(1):
        config.save_settings({"token_accounts": [{"name": "L1", "environment": "demo", "enabled": True, "access_token": "t",
                                                  "accounts": [{"spec": "DEMO11", "id": 11, "enabled": True}]}]})
    r = await client.post("/api/trade-accounts", json=[{"token_idx": 0, "spec": "DEMO11", "enabled": True, "qty_multiplier": 1,
                                                        "risk": {"loss_limit": 400, "profit_limit": 800, "flatten_at": "21:30"}}])
    assert r.status_code == 200 and r.json()[0]["risk"] == {"loss_limit": 400.0, "profit_limit": 800.0, "flatten_at": "21:30"}
    r = await client.post("/api/trade-accounts", json=[{"token_idx": 0, "spec": "DEMO11", "enabled": True, "risk": {"flatten_at": "99:00"}}])
    assert r.status_code == 400
    with context.use_area(1):
        assert risk.config_for(1, "DEMO11")["loss_limit"] == 400.0
        assert risk.any_active(config.load_settings())
        risk._lock(1, "DEMO11", "loss", "daily loss limit hit", -450)
    r = await client.get("/api/risk")
    assert r.json()[0]["locked"] and r.json()[0]["lock"]["kind"] == "loss"
    r = await client.get("/api/trade-accounts")
    assert r.json()[0]["locked"]["reason"] == "daily loss limit hit"
    r = await client.post("/api/risk/unlock", json={"spec": "DEMO11"})
    assert r.status_code == 200
    assert (await client.post("/api/risk/unlock", json={"spec": "DEMO11"})).status_code == 404
    # risk_state cannot be written through the generic settings endpoint
    r = await client.post("/api/settings", json={"risk_state": {"DEMO11": {"day": "2099-01-01"}}})
    assert r.status_code == 200
    with context.use_area(1):
        assert config.load_settings().get("risk_state") == {}

"""ProjectX adapter (app/projectx.py): the bridge's broker surface over a mocked
ProjectX gateway — auth, accounts, contracts, orders, positions, cash, wiring."""
from __future__ import annotations

import json

import httpx
import pytest

from app import config, context, projectx, state, tradovate

CONTRACTS = [
    {"id": "CON.F.US.MNQ.Z25", "name": "MNQZ25", "description": "Micro E-mini Nasdaq-100: December 2025", "tickSize": 0.25, "tickValue": 0.5, "activeContract": True},
    {"id": "CON.F.US.MNQ.H26", "name": "MNQH26", "description": "Micro E-mini Nasdaq-100: March 2026", "tickSize": 0.25, "tickValue": 0.5, "activeContract": False},
    {"id": "CON.F.US.ENQ.Z25", "name": "ENQZ25", "description": "E-mini Nasdaq-100: December 2025", "tickSize": 0.25, "tickValue": 5.0, "activeContract": True},
]


class Gateway:
    """A scripted ProjectX gateway."""
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.token_ok = True
        self.accounts = [{"id": 101, "name": "PRAC-V2-1", "balance": 50250.0, "canTrade": True, "isVisible": True, "simulated": True},
                         {"id": 102, "name": "50K-EXPRESS-2", "balance": 49800.0, "canTrade": True, "isVisible": True, "simulated": False}]
        self.positions = {101: [{"id": 1, "accountId": 101, "contractId": "CON.F.US.MNQ.Z25", "type": 1, "size": 2, "averagePrice": 21000.0}]}
        self.orders = {101: [{"id": 501, "accountId": 101, "contractId": "CON.F.US.MNQ.Z25", "status": 1, "type": 1, "side": 1, "size": 2, "limitPrice": 21100.0, "stopPrice": None, "updateTimestamp": "t1"},
                             {"id": 502, "accountId": 101, "contractId": "CON.F.US.MNQ.Z25", "status": 2, "type": 4, "side": 1, "size": 1, "limitPrice": None, "stopPrice": 20900.0, "updateTimestamp": "t2"}]}
        self.trades = {101: [{"id": 9, "accountId": 101, "contractId": "CON.F.US.MNQ.Z25", "profitAndLoss": 130.0, "fees": 2.5, "voided": False},
                             {"id": 10, "accountId": 101, "contractId": "CON.F.US.MNQ.Z25", "profitAndLoss": -20.0, "fees": 2.5, "voided": True}]}
        self.last_close = 21010.0
        self.reject_place = False
        self.next_id = 900
        self.rate_limit_once = False

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = json.loads(request.content or b"{}")
        self.calls.append((path, body))
        if path == "/api/Auth/loginKey":
            return httpx.Response(200, json={"token": "tok1", "success": self.token_ok, "errorCode": 0, "errorMessage": None if self.token_ok else "bad key"})
        if request.headers.get("authorization") != "Bearer tok1":
            return httpx.Response(401, json={"success": False})
        if self.rate_limit_once:
            self.rate_limit_once = False
            return httpx.Response(429, headers={"Retry-After": "1"}, text="slow down")
        if path == "/api/Auth/validate":
            return httpx.Response(200, json={"success": True, "newToken": "tok1"})
        if path == "/api/Account/search":
            return httpx.Response(200, json={"accounts": self.accounts, "success": True})
        if path == "/api/Contract/search":
            text = body["searchText"].upper()
            return httpx.Response(200, json={"contracts": [c for c in CONTRACTS if c["name"].startswith(text)], "success": True})
        if path == "/api/Contract/searchById":
            return httpx.Response(200, json={"contract": next((c for c in CONTRACTS if c["id"] == body["contractId"]), None), "success": True})
        if path == "/api/Position/searchOpen":
            return httpx.Response(200, json={"positions": self.positions.get(body["accountId"], []), "success": True})
        if path == "/api/Order/search":
            return httpx.Response(200, json={"orders": self.orders.get(body["accountId"], []), "success": True})
        if path == "/api/Order/searchOpen":
            return httpx.Response(200, json={"orders": [o for o in self.orders.get(body["accountId"], []) if o["status"] in (1, 6)], "success": True})
        if path == "/api/Order/place":
            if self.reject_place:
                return httpx.Response(200, json={"orderId": 0, "success": False, "errorCode": 3, "errorMessage": "Insufficient buying power"})
            self.next_id += 1
            self.orders.setdefault(body["accountId"], []).append({"id": self.next_id, "accountId": body["accountId"], "contractId": body["contractId"], "status": 1,
                                                                  "type": body["type"], "side": body["side"], "size": body["size"], "limitPrice": body.get("limitPrice"), "stopPrice": body.get("stopPrice"), "updateTimestamp": "n"})
            return httpx.Response(200, json={"orderId": self.next_id, "success": True, "errorCode": 0, "errorMessage": None})
        if path in ("/api/Order/modify", "/api/Order/cancel", "/api/Position/closeContract"):
            return httpx.Response(200, json={"success": True, "errorCode": 0, "errorMessage": None})
        if path == "/api/Trade/search":
            return httpx.Response(200, json={"trades": self.trades.get(body["accountId"], []), "success": True})
        if path == "/api/History/retrieveBars":
            return httpx.Response(200, json={"bars": [{"t": "x", "o": 21005, "h": 21012, "l": 21000, "c": self.last_close, "v": 10}], "success": True})
        return httpx.Response(404, json={"success": False, "errorMessage": f"no route {path}"})


@pytest.fixture
def px(monkeypatch, admin):
    gw = Gateway()
    client = httpx.AsyncClient(transport=httpx.MockTransport(gw.handle))
    monkeypatch.setattr(projectx.ProjectXSession, "_client", lambda self: client)
    monkeypatch.setattr(projectx, "REQUEST_SPACING_S", 0.0)
    entry = {"name": "Topstep", "broker": "projectx", "environment": "demo", "enabled": True, "px_user": "trader", "px_api_key": "k1",
             "px_firm": "topstep", "lid": "lg_px1", "accounts": []}
    with context.use_area(1):
        config.save_settings({"token_accounts": [entry], "trading_enabled": True})
    return {"gw": gw, "s": projectx.ProjectXSession(0, entry, area_id=1)}


async def test_connect_and_accounts(px):
    s, gw = px["s"], px["gw"]
    st = await s.connect()
    assert st["connected"] and st["broker"] == "projectx" and st["firm"] == "topstep"
    assert [(a["spec"], a["id"], a["simulated"]) for a in s.accounts] == [("PRAC-V2-1", 101, True), ("50K-EXPRESS-2", 102, False)]
    assert s.account_spec == "PRAC-V2-1" and s.base_url == "https://api.topstepx.com"
    assert config.load_settings(area_id=1)["token_accounts"][0]["accounts"][1]["id"] == 102
    assert (await s.health_check())["connected"] and ("/api/Auth/validate", {}) in gw.calls
    gw.token_ok = False
    s2 = projectx.ProjectXSession(0, {"name": "X", "px_user": "u", "px_api_key": "bad", "px_firm": "https://api.custom.projectx.com/"}, area_id=1)
    assert s2.base_url == "https://api.custom.projectx.com"
    with pytest.raises(tradovate.TradovateError, match="bad key"):
        await s2.connect()


async def test_contracts_roots_aliases_and_years(px):
    s, gw = px["s"], px["gw"]
    await s.connect()
    assert await s.resolve_contract("MNQ") == "MNQZ5" and await s.resolve_contract("MNQ1!") == "MNQZ5"     # front month, bridge form
    assert await s.resolve_contract("MNQH6") == "MNQH6" and await s.resolve_contract("MNQH26") == "MNQH6"
    assert await s.resolve_contract("NQ") == "NQZ5"                                                       # alias ENQ → shown as NQ
    cid = await s.contract_id("MNQZ5")
    assert cid == projectx._int_id("CON.F.US.MNQ.Z25") and (await s.contract_info(cid))["name"] == "MNQZ5"
    assert (await s.contract_info(await s.contract_id("NQZ5")))["name"] == "NQZ5"
    assert [c["name"] for c in await s.contract_suggest("MNQ")] == ["MNQZ5", "MNQH6"]
    with pytest.raises(tradovate.TradovateError, match="no contract"):
        await s.resolve_contract("XYZ")
    assert projectx.split_symbol("MNQZ6") == ("MNQ", "Z", "6") and projectx.split_symbol("ENQH26") == ("ENQ", "H", "6") and projectx.split_symbol("ES") == ("ES", "", "")


async def test_feeds_positions_orders_versions_cash(px):
    s, gw = px["s"], px["gw"]
    await s.connect()
    pos = await s.positions_snapshot()
    assert pos[0]["accountId"] == 101 and pos[0]["netPos"] == 2 and pos[0]["symbol"] == "MNQZ25" and pos[0]["netPrice"] == 21000.0
    gw.positions[101][0]["type"] = 2
    assert (await s.positions_snapshot())[0]["netPos"] == -2
    gw.positions[101][0]["type"] = 1
    await s.positions_snapshot()                       # every fresh fetch feeds the 3 s cache the cash snapshot reads
    orders = await s.orders_snapshot()
    assert [(o["id"], o["ordStatus"], o["action"]) for o in orders] == [(501, "Working", "Sell"), (502, "Filled", "Sell")]
    v = await s.order_versions([501, 502])
    assert v[501]["orderQty"] == 2 and v[501]["orderType"] == "Limit" and v[501]["price"] == 21100.0
    assert v[502]["orderType"] == "Stop" and v[502]["stopPrice"] == 20900.0
    assert [o["id"] for o in await s.working_orders(account_spec="PRAC-V2-1")] == [501]
    cash = await s.cash_snapshot(101)
    # realised: 130 − 2.5 fees (voided trade ignored); open: (21010 − 21000) × 2 × (0.5 / 0.25) = 40
    assert cash["totalCashValue"] == 50250.0 and cash["realizedPnL"] == 127.5 and cash["openPnL"] == 40.0
    mine = await s.positions(account_spec="PRAC-V2-1")
    assert mine == [{"symbol": "MNQZ5", "account": "Topstep", "netPos": 2, "netPrice": 21000.0}]


async def test_orders_place_modify_cancel_liquidate_rejects_and_429(px):
    s, gw = px["s"], px["gw"]
    await s.connect()
    ex = tradovate.AccountExecutor(s, {"spec": "PRAC-V2-1", "id": 101, "enabled": True})
    with context.use_area(1):
        r = await ex.place_order(symbol="MNQZ5", action="Buy", qty=2, order_type="Limit", price=21000.0)
    assert r["status"] == "submitted" and r["order_id"] == 901
    placed = [b for p, b in gw.calls if p == "/api/Order/place"][-1]
    assert (placed["accountId"], placed["contractId"], placed["type"], placed["side"], placed["size"], placed["limitPrice"]) == (101, "CON.F.US.MNQ.Z25", 1, 0, 2, 21000.0)
    assert state.recent_orders()[0]["status"] == "submitted"
    with context.use_area(1):
        r = await ex.place_order(symbol="MNQZ5", action="Sell", qty=2, order_type="Stop", stop_price=20900.0)
    placed = [b for p, b in gw.calls if p == "/api/Order/place"][-1]
    assert placed["type"] == 4 and placed["stopPrice"] == 20900.0 and placed["limitPrice"] is None
    await ex.modify_order(r["order_id"], qty=1, order_type="Stop", stop_price=20950.0)
    assert [b for p, b in gw.calls if p == "/api/Order/modify"][-1] == {"accountId": 101, "orderId": 902, "size": 1, "limitPrice": None, "stopPrice": 20950.0, "trailPrice": None}
    await ex.cancel_order(r["order_id"])
    assert [b for p, b in gw.calls if p == "/api/Order/cancel"][-1] == {"accountId": 101, "orderId": 902}
    with context.use_area(1):
        await ex.liquidate_position("MNQZ5")
    assert [b for p, b in gw.calls if p == "/api/Position/closeContract"][-1] == {"accountId": 101, "contractId": "CON.F.US.MNQ.Z25"}
    gw.reject_place = True
    with context.use_area(1), pytest.raises(tradovate.TradovateError, match="Insufficient buying power"):
        await ex.place_order(symbol="MNQZ5", action="Buy", qty=1, order_type="Market")
    assert state.recent_orders()[0]["status"] == "rejected"
    gw.reject_place = False
    with context.use_area(1):
        oco = await ex.place_oco(symbol="MNQZ5", action="Sell", qty=2, order_type="Limit", price=21100.0, stop_price=None,
                                 other={"action": "Sell", "order_type": "Stop", "price": None, "stop_price": 20900.0})
    places = [b for p, b in gw.calls if p == "/api/Order/place"][-2:]
    assert oco["linked"] and places[1]["linkedOrderId"] == oco["order_id"] and oco["oco_id"] == places[1]["linkedOrderId"] + 1
    # a 429 becomes RateLimited with the retry-after (the engines back off)
    gw.rate_limit_once = True
    with pytest.raises(tradovate.RateLimited):
        await s.positions_snapshot()
    assert s.rate_limits == 1
    with pytest.raises(tradovate.TradovateError, match="unknown ProjectX account"):
        await s.place_order(symbol="MNQZ5", action="Buy", qty=1, order_type="Market", account_spec="NOPE")


async def test_risk_lock_and_manager_wiring(px, client, monkeypatch):
    from app import risk
    s = px["s"]
    await s.connect()
    monkeypatch.setattr(risk, "is_locked", lambda area, spec: "daily loss limit" if spec == "PRAC-V2-1" else "")
    with context.use_area(1), pytest.raises(tradovate.TradovateError, match="risk guard"):
        await s.place_order(symbol="MNQZ5", action="Buy", qty=1, order_type="Market", account_spec="PRAC-V2-1", account_id=101)
    assert not any(p == "/api/Order/place" for p, _ in px["gw"].calls)
    mgr = tradovate.manager_for(1)
    mgr.reload()
    ss = mgr.all()
    assert isinstance(ss[0], projectx.ProjectXSession) and ss[0].agent_id == 0
    assert [e.spec for e in mgr.enabled()] == ["PRAC-V2-1", "50K-EXPRESS-2"]
    row = (await client.get("/api/token-accounts")).json()[0]
    assert row["broker"] == "projectx" and row["px_api_key"] == "********" and row["px_firm"] == "topstep"
    r = await client.post("/api/token-accounts", json=[{**row, "px_api_key": "********", "agent_id": 7}])
    assert r.status_code == 200
    saved = config.load_settings(area_id=1)["token_accounts"][0]
    assert saved["px_api_key"] == "k1" and saved["agent_id"] == 0 and saved["broker"] == "projectx"
    mgr.reload(); ss = mgr.all(); mgr.reload()
    assert mgr.all()[0] is ss[0]


async def test_copy_engine_polls_a_projectx_leader(px, monkeypatch):
    from app import copy as cp
    from tests.helpers import FakeExecutor
    s, gw = px["s"], px["gw"]
    await s.connect()
    ex = FakeExecutor("F1")

    class Mgr:
        def all(self): return [s]
        def executor_for(self, idx, spec, mult=1, lid=None): return ex if spec == "F1" else None
    monkeypatch.setattr(tradovate, "manager_for", lambda area_id: Mgr())
    g = cp.new_group("P"); g.update({"enabled": True, "feed": "auto", "leader": {"token_idx": 0, "spec": "PRAC-V2-1", "account_id": 101},
                                     "followers": [cp.normalize_follower({"token_idx": 0, "spec": "F1", "account_id": 0})]})
    r = cp.GroupRunner(1, g)
    await r._seed_leader(s, 101)
    r._mark_feed(True)
    assert r.leader_net and list(r.baseline)                                     # existing MNQ position is baseline
    gw.positions[101] = []
    await r._poll_once(s, 101)
    gw.positions[101] = [{"id": 2, "accountId": 101, "contractId": "CON.F.US.ENQ.Z25", "type": 2, "size": 1, "averagePrice": 21000.0}]
    await r._poll_once(s, 101)
    assert [(c["action"], c["qty"], c["symbol"]) for c in ex.of("place")] == [("Sell", 1, "NQZ5")]                       # the alias shown in the bridge form

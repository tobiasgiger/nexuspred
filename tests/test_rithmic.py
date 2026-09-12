"""Rithmic adapter (app/rithmic.py): the bridge's broker surface over a scripted
Rithmic client — ids, statuses, orders, positions, cash, contracts, wiring."""
from __future__ import annotations

from types import SimpleNamespace as NS

import pytest

from app import config, context, rithmic, state, tradovate


class FakeRithmicClient:
    """What the adapter uses of async_rithmic.RithmicClient, scripted."""
    def __init__(self, **kw):
        self.kw = kw
        self.calls: list[tuple[str, dict]] = []
        self.plants = {"order": NS(is_connected=False)}
        self.accounts_rows = [NS(account_id="APEX-123", account_name="Apex 50k"), NS(account_id="APEX-456", account_name="Apex 100k")]
        self.positions = {"APEX-123": [NS(symbol="MNQZ6", exchange="CME", net_quantity=2, buy_qty=2, sell_qty=0, avg_open_fill_price=21000.5, open_position_pnl=12.5)]}
        self.orders = {"APEX-123": [NS(basket_id="9001", symbol="MNQZ6", exchange="CME", transaction_type="SELL", status="OPEN", quantity=2, total_fill_size=0,
                                        total_unfilled_size=2, price_type="LIMIT", price=21100.0, trigger_price=0, sequence_number=77),
                                     NS(basket_id="9002", symbol="MNQZ6", exchange="CME", transaction_type="SELL", status="COMPLETE", quantity=1, total_fill_size=1,
                                        total_unfilled_size=0, price_type="STOP_MARKET", price=0, trigger_price=20900.0, sequence_number=78)]}
        self.summary = {"APEX-123": [NS(account_id="APEX-123", account_balance=50321.5, day_closed_pnl=121.0, day_open_pnl=12.5, day_pnl=133.5, margin_balance=1000.0)]}
        self.fail_submit = False
        self.next_basket = 5000

    async def connect(self, **kw):
        self.calls.append(("connect", kw)); self.plants["order"].is_connected = True
    async def disconnect(self, timeout=5.0):
        self.plants["order"].is_connected = False
    async def list_accounts(self):
        return list(self.accounts_rows)
    async def list_positions(self, **kw):
        return list(self.positions.get(kw.get("account_id"), []))
    async def list_orders(self, **kw):
        return list(self.orders.get(kw.get("account_id"), []))
    async def list_account_summary(self, **kw):
        return list(self.summary.get(kw.get("account_id"), []))
    async def get_account_rms(self):
        return [NS(account_id="APEX-123", loss_limit=1500.0, min_account_balance=47500.0, auto_liquidate=1)]
    async def get_front_month_contract(self, symbol, exchange):
        self.calls.append(("front", {"symbol": symbol, "exchange": exchange}))
        return f"{symbol}Z6"
    async def search_symbols(self, text, exchange=None, **kw):
        return [NS(symbol="MNQZ6"), NS(symbol="MNQH7"), NS(symbol="MNQ")]
    async def submit_order(self, order_id, symbol, exchange, qty, transaction_type, order_type, **kw):
        self.calls.append(("submit", {"tag": order_id, "symbol": symbol, "exchange": exchange, "qty": qty, "tt": int(transaction_type), "ot": int(order_type), **kw}))
        if self.fail_submit:
            return [NS(basket_id="", rp_code=["7", "insufficient margin"])]
        self.next_basket += 1
        return [NS(basket_id=str(self.next_basket), rp_code=["0"], user_tag=order_id)]
    async def modify_order(self, **kw):
        self.calls.append(("modify", kw)); return [NS(basket_id=kw.get("basket_id"), rp_code=["0"])]
    async def cancel_order(self, **kw):
        self.calls.append(("cancel", kw)); return [NS(basket_id=kw.get("basket_id"), rp_code=["0"])]
    async def exit_position(self, **kw):
        self.calls.append(("exit", kw)); return [NS(rp_code=["0"], symbol=kw.get("symbol"))]


@pytest.fixture
def rsess(monkeypatch, admin):
    made: list[FakeRithmicClient] = []

    def factory(self):
        c = FakeRithmicClient(user=self.user, password=self.password, system=self.system_name, gateway=self.gateway)
        made.append(c)
        return c
    monkeypatch.setattr(rithmic.RithmicSession, "_make_client", factory)
    entry = {"name": "Apex", "broker": "rithmic", "environment": "live", "enabled": True, "rithmic_user": "u", "rithmic_password": "p",
             "rithmic_system": "Apex", "rithmic_gateway": "chicago", "lid": "lg_r1", "accounts": []}
    with context.use_area(1):
        config.save_settings({"token_accounts": [entry], "trading_enabled": True})
    s = rithmic.RithmicSession(0, entry, area_id=1)
    return {"s": s, "made": made}


async def test_connect_discovers_accounts_with_stable_int_ids(rsess):
    s = rsess["s"]
    st = await s.connect()
    assert st["connected"] and st["broker"] == "rithmic" and st["system"] == "Apex"
    assert [a["spec"] for a in s.accounts] == ["APEX-123", "APEX-456"] and all(a["id"] > 0 for a in s.accounts)
    assert s.accounts[0]["id"] == rithmic._int_id("APEX-123") and s.accounts[0]["label"] == "Apex 50k"
    assert rsess["made"][0].kw == {"user": "u", "password": "p", "system": "Apex", "gateway": rithmic.GATEWAYS["chicago"]}
    # persisted for the dashboard, the primary account chosen
    saved = config.load_settings(area_id=1)["token_accounts"][0]
    assert [a["spec"] for a in saved["accounts"]] == ["APEX-123", "APEX-456"] and s.account_spec == "APEX-123"
    hc = await s.health_check()
    assert hc["connected"]


async def test_feeds_map_to_the_bridge_picture(rsess):
    s = rsess["s"]
    await s.connect()
    aid = s.accounts[0]["id"]
    pos = await s.positions_snapshot()
    assert pos == [{"accountId": aid, "contractId": pos[0]["contractId"], "netPos": 2, "netPrice": 21000.5, "openPnL": 12.5, "symbol": "MNQZ6"}]
    assert (await s.contract_info(pos[0]["contractId"])) == {"id": pos[0]["contractId"], "name": "MNQZ6", "exchange": "CME"}
    orders = await s.orders_snapshot()
    assert [(o["id"], o["ordStatus"], o["action"]) for o in orders] == [(9001, "Working", "Sell"), (9002, "Filled", "Sell")]
    v = await s.order_versions([9001, 9002])
    assert v[9001] == {"id": 77, "orderQty": 2, "orderType": "Limit", "price": 21100.0, "stopPrice": None}
    assert v[9002]["orderType"] == "Stop" and v[9002]["stopPrice"] == 20900.0
    assert [o["id"] for o in await s.working_orders(account_spec="APEX-123")] == [9001]
    cash = await s.cash_snapshot(aid)
    assert cash["totalCashValue"] == 50321.5 and cash["realizedPnL"] == 121.0 and cash["openPnL"] == 12.5
    rules = await s.auto_liq_rules()
    assert rules[0]["accountId"] == aid and rules[0]["dailyLossLimit"] == 1500.0
    mine = await s.positions(account_spec="APEX-123")
    assert mine == [{"symbol": "MNQZ6", "account": "Apex", "netPos": 2, "netPrice": 21000.5}]
    assert await s.positions(account_spec="APEX-456") == []


async def test_contracts_front_month_and_exchanges(rsess):
    s = rsess["s"]
    await s.connect()
    assert await s.resolve_contract("MNQ") == "MNQZ6" and await s.resolve_contract("MNQ1!") == "MNQZ6"
    assert await s.resolve_contract("MNQZ6") == "MNQZ6"                          # dated: as is, no lookup
    assert rsess["made"][0].calls.count(("front", {"symbol": "MNQ", "exchange": "CME"})) == 1   # cached
    assert rithmic.exchange_for("MGCZ6") == "COMEX" and rithmic.exchange_for("MCL") == "NYMEX" and rithmic.exchange_for("YM") == "CBOT"
    assert (await s.contract_find("MGCZ6"))["exchange"] == "COMEX"
    assert [c["name"] for c in await s.contract_suggest("MNQ")] == ["MNQZ6", "MNQH7"]
    assert await s.contract_id("MNQZ6") == rithmic._int_id("CME:MNQZ6")


async def test_orders_place_modify_cancel_liquidate_and_rejects(rsess):
    s = rsess["s"]
    await s.connect()
    c = rsess["made"][0]
    ex = tradovate.AccountExecutor(s, {"spec": "APEX-123", "id": s.accounts[0]["id"], "enabled": True})
    with context.use_area(1):
        r = await ex.place_order(symbol="MNQZ6", action="Buy", qty=2, order_type="Limit", price=21000.0)
    assert r["status"] == "submitted" and r["order_id"] == 5001 and r["basket_id"] == "5001"
    sub = c.calls[-1][1]
    assert (sub["symbol"], sub["exchange"], sub["qty"], sub["tt"], sub["account_id"], sub["price"]) == ("MNQZ6", "CME", 2, 1, "APEX-123", 21000.0)
    assert state.recent_orders()[0]["status"] == "submitted" and state.recent_orders()[0]["order_type"] == "Limit"
    with context.use_area(1):
        r = await ex.place_order(symbol="MNQZ6", action="Sell", qty=2, order_type="Stop", stop_price=20900.0)
    assert c.calls[-1][1]["trigger_price"] == 20900.0 and "price" not in c.calls[-1][1]
    await ex.modify_order(r["order_id"], qty=1, order_type="Stop", stop_price=20950.0)
    assert c.calls[-1] == ("modify", {"basket_id": "5002", "account_id": "APEX-123", "qty": 1, "order_type": c.calls[-1][1]["order_type"], "trigger_price": 20950.0})
    await ex.cancel_order(r["order_id"])
    assert c.calls[-1] == ("cancel", {"basket_id": "5002", "account_id": "APEX-123"})
    with context.use_area(1):
        await ex.liquidate_position("MNQZ6")
    assert c.calls[-1] == ("exit", {"account_id": "APEX-123", "symbol": "MNQZ6", "exchange": "CME"})
    # a broker reject is an error with the reason, logged as rejected
    c.fail_submit = True
    with context.use_area(1), pytest.raises(tradovate.TradovateError, match="insufficient margin"):
        await ex.place_order(symbol="MNQZ6", action="Buy", qty=1, order_type="Market")
    assert state.recent_orders()[0]["status"] == "rejected"
    # OCO: two independent orders, warned once
    c.fail_submit = False
    with context.use_area(1):
        oco = await ex.place_oco(symbol="MNQZ6", action="Sell", qty=2, order_type="Limit", price=21100.0, stop_price=None,
                                 other={"action": "Sell", "order_type": "Stop", "price": None, "stop_price": 20900.0})
    assert oco["order_id"] and oco["oco_id"] and oco["linked"] is False
    assert sum(1 for e in state.recent_events() if "independent orders" in e["message"]) == 1
    # an unknown account spec is refused, never the login's primary
    with pytest.raises(tradovate.TradovateError, match="unknown Rithmic account"):
        await s.place_order(symbol="MNQZ6", action="Buy", qty=1, order_type="Market", account_spec="APEX-999")


async def test_risk_lock_blocks_rithmic_orders(rsess, monkeypatch):
    from app import risk
    s = rsess["s"]
    await s.connect()
    monkeypatch.setattr(risk, "is_locked", lambda area, spec: "daily loss limit" if spec == "APEX-123" else "")
    with context.use_area(1), pytest.raises(tradovate.TradovateError, match="risk guard"):
        await s.place_order(symbol="MNQZ6", action="Buy", qty=1, order_type="Market", account_spec="APEX-123", account_id=s.accounts[0]["id"])
    assert not any(k == "submit" for k, _ in rsess["made"][0].calls)


async def test_status_mapping_edge_cases():
    st = rithmic.RithmicSession._status
    assert st(NS(status="Open Pending", completion_reason="", total_unfilled_size=1, quantity=1, total_fill_size=0)) == "Working"
    assert st(NS(status="Cancelled", completion_reason="", total_unfilled_size=1, quantity=1, total_fill_size=0)) == "Canceled"
    assert st(NS(status="", completion_reason="COMPLETE", total_unfilled_size=0, quantity=2, total_fill_size=2)) == "Filled"
    assert st(NS(status="weird", completion_reason="", total_unfilled_size=3, quantity=3, total_fill_size=0)) == "Working"
    assert rithmic._int_id("12345") == 12345 and rithmic._int_id("APEX-1") == rithmic._int_id("APEX-1") != rithmic._int_id("APEX-2")


async def test_manager_builds_rithmic_sessions_and_the_api_masks_the_password(rsess, client):
    mgr = tradovate.manager_for(1)
    mgr.reload()
    ss = mgr.all()
    assert len(ss) == 1 and isinstance(ss[0], rithmic.RithmicSession) and ss[0].agent_id == 0
    execs = mgr.enabled()
    assert execs == []                                             # nothing discovered yet
    r = await client.get("/api/token-accounts")
    row = r.json()[0]
    assert row["broker"] == "rithmic" and row["rithmic_password"] == "********" and row["rithmic_user"] == "u"
    # saving with the mask keeps the password; a live Rithmic login needs a system name
    r = await client.post("/api/token-accounts", json=[{**row, "rithmic_password": "********", "agent_id": 5}])
    assert r.status_code == 200
    assert config.load_settings(area_id=1)["token_accounts"][0]["rithmic_password"] == "p"
    assert config.load_settings(area_id=1)["token_accounts"][0]["agent_id"] == 0
    r = await client.post("/api/token-accounts", json=[{**row, "rithmic_system": ""}])
    assert r.status_code == 400 and "system name" in r.json()["detail"]
    # the same session object survives a reload with unchanged config; new credentials are adopted
    mgr.reload()
    ss = mgr.all()
    mgr.reload()
    assert mgr.all()[0] is ss[0]
    with context.use_area(1):
        config.save_settings({"token_accounts": [{**config.load_settings()["token_accounts"][0], "rithmic_password": "p2"}]})
    mgr.reload()
    assert mgr.all()[0] is ss[0] and ss[0].password == "p2"


async def test_copy_engine_polls_a_rithmic_leader(rsess, monkeypatch):
    """A Rithmic leader: the copy feed is the poll (no user-sync socket), positions
    map through synthetic contract ids and names resolve for the log."""
    from app import copy as cp
    from tests.helpers import FakeExecutor
    s = rsess["s"]
    await s.connect()
    ex = FakeExecutor("F1")

    class Mgr:
        def all(self): return [s]
        def executor_for(self, idx, spec, mult=1, lid=None): return ex if spec == "F1" else None
    monkeypatch.setattr(tradovate, "manager_for", lambda area_id: Mgr())
    g = cp.new_group("R"); g.update({"enabled": True, "feed": "auto", "leader": {"token_idx": 0, "spec": "APEX-123", "account_id": s.accounts[0]["id"]},
                                     "followers": [cp.normalize_follower({"token_idx": 0, "spec": "F1", "account_id": 0})]})
    r = cp.GroupRunner(1, g)
    await r._seed_leader(s, s.accounts[0]["id"])
    r._mark_feed(True)
    assert r.leader_net and r.contract_names[list(r.leader_net)[0]] == "MNQZ6" and list(r.baseline)      # existing position: baseline
    rsess["made"][0].positions["APEX-123"][0].net_quantity = 0
    await r._poll_once(s, s.accounts[0]["id"])
    rsess["made"][0].positions["APEX-123"] = [NS(symbol="MESZ6", exchange="CME", net_quantity=1, buy_qty=1, sell_qty=0, avg_open_fill_price=5600.0, open_position_pnl=0)]
    await r._poll_once(s, s.accounts[0]["id"])
    assert [(c["action"], c["qty"], c["symbol"]) for c in ex.of("place")] == [("Buy", 1, "MESZ6")]

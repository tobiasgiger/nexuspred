"""A call that names a trade account must reach that account — never the
login's primary account (the fallback that could close the wrong position)."""
from __future__ import annotations

import pytest

from app import context, tradovate


def _session():
    sess = tradovate.TradovateSession(0, {"name": "L", "environment": "demo", "enabled": True, "access_token": "t",
                                          "account_spec": "PRIMARY", "account_id": 11,
                                          "accounts": [{"spec": "PRIMARY", "id": 11, "enabled": True},
                                                       {"spec": "SECOND", "id": 12, "enabled": True},
                                                       {"spec": "NOID", "enabled": True}]}, area_id=1)
    sent = []

    async def fake(method, path, **kw):
        sent.append((path, kw.get("json") or kw.get("params")))
        if path == "/contract/find":
            return {"id": 901, "name": "MNQZ6"}
        if path == "/order/list":
            return [{"id": 1, "ordStatus": "Working", "accountId": 11}, {"id": 2, "ordStatus": "Working", "accountId": 12}]
        if path == "/position/list":
            return [{"accountId": 11, "contractId": 901, "netPos": 1}, {"accountId": 12, "contractId": 901, "netPos": -2}]
        if path == "/contract/item":
            return {"name": "MNQZ6"}
        return {"orderId": 5}
    sess._request_raw = fake
    return sess, sent


async def test_executor_targets_its_own_account_even_without_a_stored_id(admin):
    sess, sent = _session()
    with context.use_area(1):
        ex = tradovate.AccountExecutor(sess, {"spec": "SECOND"})          # id missing on the routed entry
        assert ex.id == 12
        await ex.liquidate_position("MNQZ6")
        assert sent[-1][0] == "/order/liquidateposition" and sent[-1][1]["accountId"] == 12
        assert [o["id"] for o in await ex.working_orders()] == [2]
        assert [p["net"] if "net" in p else p.get("netPos") for p in await ex.positions()] == [-2]
        r = await ex.place_order(symbol="MNQZ6", action="Buy", qty=1, order_type="Market")
        assert r["account_id"] == 12 and sent[-1][1]["accountId"] == 12 and sent[-1][1]["accountSpec"] == "SECOND"


async def test_unknown_account_id_is_refused_not_redirected(admin):
    sess, sent = _session()
    with context.use_area(1):
        ex = tradovate.AccountExecutor(sess, {"spec": "NOID"})
        assert ex.id == 0
        for call in (lambda: ex.liquidate_position("MNQZ6"), ex.working_orders, ex.positions,
                     lambda: ex.place_order(symbol="MNQZ6", action="Sell", qty=1, order_type="Market")):
            with pytest.raises(tradovate.TradovateError, match="no Tradovate account id"):
                await call()
        assert not any(p in ("/order/liquidateposition", "/order/placeorder") for p, _ in sent)
        # the legacy single-account path (no spec at all) still uses the primary
        await sess.liquidate_position("MNQZ6")
        assert sent[-1][1]["accountId"] == 11

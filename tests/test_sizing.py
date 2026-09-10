"""Per-account sizing (app/sizing.py): same / multiplier / fixed / max — and its
use by the strategies and the webhook routing API."""
from __future__ import annotations

import pytest

from app import config, context, signals, sizing, tradovate
from tests.helpers import FakeExecutor
from tests.test_strategies import live  # noqa: F401 - fixture


class Ex:
    def __init__(self, **sz):
        self.sizing = sizing.normalize({"sizing": sz})


def test_normalize_and_legacy_multiplier():
    assert sizing.normalize({}) == {"mode": "same", "multiplier": 1.0, "fixed": 1, "max_contracts": 0}
    assert sizing.normalize({"qty_multiplier": 2})["mode"] == "multiplier"
    assert sizing.normalize({"qty_multiplier": 1.0})["mode"] == "same"
    sz = sizing.normalize({"sizing": {"mode": "fixed", "fixed": "3", "max_contracts": ""}})
    assert sz == {"mode": "fixed", "multiplier": 1.0, "fixed": 3, "max_contracts": 0}
    for bad in ({"sizing": {"mode": "weird"}}, {"sizing": {"mode": "multiplier", "multiplier": 0}},
                {"sizing": {"mode": "fixed", "fixed": 0}}, {"sizing": {"max_contracts": -1}}):
        with pytest.raises(ValueError):
            sizing.normalize(bad)
    assert sizing.effective_multiplier(sizing.normalize({"qty_multiplier": 2.5})) == 2.5
    assert sizing.effective_multiplier(sizing.normalize({"sizing": {"mode": "fixed", "fixed": 2}})) == 1.0
    assert sizing.describe(sz) == "fixed 3" and sizing.describe(sizing.normalize({"qty_multiplier": 2})) == "× 2.0"


def test_account_qty_rules():
    assert sizing.account_qty(Ex(mode="same"), 3) == 3
    assert sizing.account_qty(Ex(mode="multiplier", multiplier=0.5), 3) == 2      # 1.5 → 2 (half up)
    assert sizing.account_qty(Ex(mode="multiplier", multiplier=0.1), 1) == 1      # never 0
    assert sizing.account_qty(Ex(mode="fixed", fixed=2), 3) == 2
    assert sizing.account_qty(Ex(mode="fixed", fixed=2), 1, of_entry=3) == 1      # TP slice 1 of 3 → 0.67 → 1
    assert sizing.account_qty(Ex(mode="fixed", fixed=4), 1, of_entry=3) == 1      # 1.33 → 1
    assert sizing.account_qty(Ex(mode="fixed", fixed=4), 2, of_entry=3) == 3      # 2.67 → 3
    assert sizing.account_qty(Ex(mode="multiplier", multiplier=5, max_contracts=4), 2) == 4
    assert sizing.account_qty(Ex(mode="fixed", fixed=9, max_contracts=5), 1) == 5
    # executors without a sizing block still honour qty_multiplier
    assert sizing.account_qty(FakeExecutor("A", qty_multiplier=2), 3) == 6
    assert sizing.account_qty(FakeExecutor("A"), 3) == 3


async def test_fixed_mode_on_a_bracket_scales_the_slices(live):
    """Entry 3 with three TP slices of 1 on a fixed-2 account: entry 2, each TP 1, stop 2."""
    a = FakeExecutor("A")
    a.sizing = sizing.normalize({"sizing": {"mode": "fixed", "fixed": 2}})
    live.use(a)
    from tests.test_strategies import ENTRY, wh
    await signals.process({**ENTRY, "qty": 3}, wh("bracket", default_qty=3, tp_qty=1))
    placed = a.of("place")
    assert placed[0]["qty"] == 2 and placed[0]["order_type"] == "Market"
    assert [p["qty"] for p in placed if p["order_type"] == "Limit"] == [1, 1, 1]
    assert [p["qty"] for p in placed if p["order_type"] == "Stop"] == [2]


async def test_webhook_accounts_carry_a_sizing_rule_to_the_executor(client, admin):
    with context.use_area(1):
        config.save_settings({"token_accounts": [{"name": "L", "environment": "demo", "enabled": True, "access_token": "t",
                                                  "accounts": [{"spec": "A1", "id": 1, "enabled": True}, {"spec": "A2", "id": 2, "enabled": True}]}]})
    r = await client.post("/api/webhooks", json={"name": "W", "strategy": "simple"})
    wid = r.json()["id"]
    r = await client.put(f"/api/webhooks/{wid}", json={"accounts": [
        {"token_idx": 0, "spec": "A1", "enabled": True, "sizing": {"mode": "fixed", "fixed": 2, "max_contracts": 0}},
        {"token_idx": 0, "spec": "A2", "enabled": True, "sizing": {"mode": "same"}}]})
    assert r.status_code == 200, r.text
    accs = r.json()["accounts"]
    assert accs[0]["sizing"]["mode"] == "fixed" and accs[0]["qty_multiplier"] == 1.0 and accs[1]["sizing"]["mode"] == "same"
    r = await client.put(f"/api/webhooks/{wid}", json={"accounts": [{"token_idx": 0, "spec": "A1", "enabled": True, "sizing": {"mode": "fixed", "fixed": 0}}]})
    assert r.status_code == 400
    with context.use_area(1):
        tradovate.manager_for(1).reload()
        exs = signals._webhook_executors(signals.config.find_webhook(r.request.url.path.split("/")[-1]) [1] if False else
                                        next(w for w in config.load_settings()["webhooks"] if w["id"] == wid))
    assert [e.sizing["mode"] for e in exs] == ["fixed", "same"] and exs[0].sizing["fixed"] == 2

"""Stable login ids: routes survive reordering / deleting logins, and saving the
login table never mixes one login's token or accounts into another."""
from __future__ import annotations

from app import config, context, copy as cp, signals, tradovate


def _logins():
    return [{"name": "L1", "environment": "demo", "enabled": True, "access_token": "t1", "accounts": [{"spec": "A1", "id": 1, "enabled": True}]},
            {"name": "L2", "environment": "demo", "enabled": True, "access_token": "t2", "accounts": [{"spec": "B1", "id": 2, "enabled": True}]},
            {"name": "L3", "environment": "demo", "enabled": True, "access_token": "t3", "accounts": [{"spec": "C1", "id": 3, "enabled": True}]}]


def test_ids_are_assigned_and_routes_stamped_on_load(admin):
    with context.use_area(1):
        config.save_settings({"token_accounts": _logins(),
                              "webhooks": [config.new_webhook(name="W") | {"accounts": [{"token_idx": 1, "spec": "B1", "enabled": True, "qty_multiplier": 1}]}],
                              "copy_groups": [{**cp.new_group("G"), "leader": {"token_idx": 0, "spec": "A1", "account_id": 1},
                                               "followers": [cp.normalize_follower({"token_idx": 2, "spec": "C1"})]}]})
        s = config.load_settings()
        lids = [t["lid"] for t in s["token_accounts"]]
        assert all(l.startswith("lg_") for l in lids) and len(set(lids)) == 3
        assert s["webhooks"][0]["accounts"][0]["lid"] == lids[1]
        assert s["copy_groups"][0]["leader"]["lid"] == lids[0] and s["copy_groups"][0]["followers"][0]["lid"] == lids[2]
        assert [t["lid"] for t in config.load_settings(force=True)["token_accounts"]] == lids   # stable


def test_deleting_a_login_keeps_routes_on_the_right_login(admin):
    with context.use_area(1):
        config.save_settings({"token_accounts": _logins(),
                              "webhooks": [config.new_webhook(name="W") | {"accounts": [{"token_idx": 2, "spec": "C1", "enabled": True, "qty_multiplier": 1}]}]})
        s = config.load_settings()
        l3 = s["token_accounts"][2]["lid"]
        config.save_settings({"token_accounts": s["token_accounts"][1:]})     # L1 removed: L3 moves to index 1
        s = config.load_settings()
        route = s["webhooks"][0]["accounts"][0]
        assert route["lid"] == l3 and route["token_idx"] == 1
        tradovate.manager_for(1).reload()
        exs = signals._webhook_executors(s["webhooks"][0])
        assert [e.session.name for e in exs] == ["L3"] and exs[0].id == 3


async def test_saving_logins_matches_by_id_not_position(client, admin):
    with context.use_area(1):
        config.save_settings({"token_accounts": _logins()})
        s = config.load_settings()
    l2, l3 = s["token_accounts"][1]["lid"], s["token_accounts"][2]["lid"]
    body = [{"lid": l2, "name": "L2", "environment": "demo", "enabled": True, "access_token": "********", "md_token": ""},
            {"lid": l3, "name": "L3", "environment": "demo", "enabled": True, "access_token": "********", "md_token": ""}]
    r = await client.post("/api/token-accounts", json=body)
    assert r.status_code == 200
    with context.use_area(1):
        t = config.load_settings()["token_accounts"]
    assert [(x["name"], x["access_token"], x["accounts"][0]["spec"], x["lid"]) for x in t] == \
        [("L2", "t2", "B1", l2), ("L3", "t3", "C1", l3)]
    r = await client.post("/api/token-accounts", json=body + [{"name": "L4", "environment": "demo", "enabled": True, "access_token": "t4"}])
    with context.use_area(1):
        t = config.load_settings()["token_accounts"]
    assert t[2]["name"] == "L4" and t[2]["accounts"] == [] and t[2]["lid"] not in (l2, l3)


def test_token_renewal_writes_into_the_right_login(admin):
    with context.use_area(1):
        config.save_settings({"token_accounts": _logins()})
        s = config.load_settings()
        l3 = s["token_accounts"][2]["lid"]
        config.save_settings({"token_accounts": s["token_accounts"][1:]})
        config.update_token_account(2, area_id=1, lid=l3, access_token="renewed")   # the session still thinks index 2
        t = config.load_settings()["token_accounts"]
        assert t[1]["access_token"] == "renewed" and t[0]["access_token"] == "t2"
        config.update_token_account(0, area_id=1, lid="lg_gone", access_token="stray")  # deleted meanwhile
        assert [x["access_token"] for x in config.load_settings()["token_accounts"]] == ["t2", "renewed"]

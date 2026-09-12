"""Regression coverage for the execution-safety audit batch."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
import pytest

from app import alerts, config, copy as cp, http, projectx, rithmic, tradovate, updater
from app.engine import bracket, manage, ts_hunter
from app.engine.common import SignalError, _flatten_account, _place_stop_with_retry
from tests.helpers import FakeExecutor


WH = {"id": "w", "name": "w", "default_qty": 1, "tp_qty": 1}
SETTINGS = {"entry_order_type": "Market", "tp_order_type": "Limit", "sl_order_type": "Stop"}


async def test_bracket_prices_are_validated_before_any_broker_call(admin):
    ex = FakeExecutor("A")
    with pytest.raises(SignalError, match="Invalid tp1"):
        await bracket.handle_entry(
            {"tp1": "not-a-price", "sl": 90}, "buy", "MNQ", "MNQ", [ex], {}, "", WH,
            settings=SETTINGS,
        )
    assert ex.calls == []


async def test_ts_hunter_metadata_is_validated_before_any_broker_call(admin):
    ex = FakeExecutor("A")
    with pytest.raises(SignalError, match="Invalid sl.value"):
        await ts_hunter.handle_entry(
            {"risk": {"value": 1}, "sl": {"value": "bad"}}, "buy", "MNQ", "MNQ", "T1",
            [ex], {}, "", WH, settings=SETTINGS,
        )
    assert ex.calls == []


async def test_unknown_stop_outcome_is_never_retried(admin):
    class UnknownStop:
        name = "A"
        calls = 0

        async def place_order(self, **kw):
            self.calls += 1
            raise tradovate.OrderOutcomeUnknown("answer lost")

    ex = UnknownStop()
    with pytest.raises(tradovate.OrderOutcomeUnknown):
        await _place_stop_with_retry(
            ex, symbol="MNQ", action="Sell", qty=1, order_type="Stop", stop_price=90, tag=""
        )
    assert ex.calls == 1


async def test_trail_active_retires_zero_quantity_stop(admin):
    ex = FakeExecutor("A")
    active = {"w:MNQ": {"accounts": {"A": {
        "entry_qty": 1, "tp_qty": 1, "qty": 1, "sl_order_id": 77,
        "sl_type": "Stop", "sl_stop": 90,
    }}}}
    r = await bracket.handle_trail_active({"event": "tp1_hit"}, "MNQ", [ex], active, "", WH)
    assert r["status"] == "ok"
    assert ex.of("cancel") == [{"order_id": 77}]
    assert ex.of("modify") == []
    assert active["w:MNQ"]["accounts"]["A"]["sl_order_id"] is None


async def test_trail_active_cancel_failure_stays_tracked_and_is_error(admin):
    ex = FakeExecutor("A")

    async def fail_cancel(order_id):
        raise tradovate.TradovateError("cancel rejected")

    ex.cancel_order = fail_cancel
    active = {"w:MNQ": {"accounts": {"A": {
        "entry_qty": 1, "tp_qty": 1, "qty": 1, "sl_order_id": 77,
        "sl_type": "Stop", "sl_stop": 90,
    }}}}
    r = await bracket.handle_trail_active({"event": "tp1_hit"}, "MNQ", [ex], active, "", WH)
    assert r["status"] == "error" and r["failed"] == ["A"]
    info = active["w:MNQ"]["accounts"]["A"]
    assert info["sl_order_id"] == 77 and info["qty"] == 1


async def test_ts_partial_close_reports_failed_stop_resize(admin):
    ex = FakeExecutor("A")

    async def fail_modify(order_id, **kw):
        raise tradovate.TradovateError("modify rejected")

    ex.modify_order = fail_modify
    active = {"T1": {"side": "buy", "accounts": {"A": {
        "contract": "MNQ", "remaining_qty": 2, "qty": 2,
        "sl_order_id": 88, "sl_type": "Stop", "sl_stop": 90,
    }}}}
    r = await ts_hunter.handle_partial_close({"percent": 50}, "T1", [ex], active, "")
    assert r["status"] == "error" and r["failed"] == ["A"]
    assert ex.of("place")[-1]["qty"] == 1
    info = active["T1"]["accounts"]["A"]
    assert info["remaining_qty"] == 1 and info["sl_order_id"] == 88


async def test_set_sl_tp_position_lookup_failure_is_not_no_position(admin):
    ex = FakeExecutor("A")

    async def fail_positions():
        raise tradovate.TradovateError("feed unavailable")

    ex.positions = fail_positions
    r = await manage.handle_set_sl_tp(
        {"stop_price": 90}, "MNQ", "MNQ", [ex], {}, "", WH
    )
    assert r["status"] == "error"
    assert r["reason"] == "protection_update_failed"
    assert r["failed"] == ["A"]


async def test_uncertain_old_target_cancel_is_reconciled_before_rollback(admin):
    ex = FakeExecutor(
        "A", positions=[{"symbol": "MNQ", "netPos": 1}],
        working=[{"id": 7, "symbol": "MNQ"}], track_working=True,
    )
    real_cancel = ex.cancel_order

    async def uncertain_but_gone(order_id):
        if order_id == 7:
            await real_cancel(order_id)  # broker actually did cancel it
            raise tradovate.OrderOutcomeUnknown("answer lost")
        return await real_cancel(order_id)

    ex.cancel_order = uncertain_but_gone
    active = {"w:MNQ": {"accounts": {"A": {
        "name": "A", "contract": "MNQ", "qty": 1,
        "sl_order_id": None, "tp_order_ids": [7],
    }}}}
    r = await manage.handle_set_sl_tp({"target_price": 120}, "MNQ", "MNQ", [ex], active, "", WH)
    assert r["status"] == "ok"
    new_id = ex.of("place")[-1]["order_id"]
    assert active["w:MNQ"]["accounts"]["A"]["tp_order_ids"] == [new_id]
    assert not any(c["order_id"] == new_id for c in ex.of("cancel"))


async def test_failed_target_cleanup_tracks_every_potentially_live_order(admin):
    ex = FakeExecutor(
        "A", positions=[{"symbol": "MNQ", "netPos": 1}],
        working=[{"id": 7, "symbol": "MNQ"}], track_working=True,
    )

    async def reject_cancel(order_id):
        raise tradovate.TradovateError(f"cancel {order_id} rejected")

    ex.cancel_order = reject_cancel
    active = {"w:MNQ": {"accounts": {"A": {
        "name": "A", "contract": "MNQ", "qty": 1,
        "sl_order_id": None, "tp_order_ids": [7],
    }}}}
    r = await manage.handle_set_sl_tp({"target_price": 120}, "MNQ", "MNQ", [ex], active, "", WH)
    new_id = ex.of("place")[-1]["order_id"]
    assert r["status"] == "error" and r["failed"] == ["A"]
    assert active["w:MNQ"]["accounts"]["A"]["tp_order_ids"] == [new_id, 7]


async def test_account_flatten_reconciles_transient_cancel_failure(admin):
    ex = FakeExecutor(
        "A", positions=[{"symbol": "MNQ", "netPos": 1}],
        working=[{"id": 5, "symbol": "MNQ"}],
    )
    real_cancel = ex.cancel_order
    attempts = 0

    async def flaky_cancel(order_id):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise tradovate.TradovateError("temporary reject")
        return await real_cancel(order_id)

    ex.cancel_order = flaky_cancel
    cancelled, flattened, errors = await _flatten_account(ex)
    assert (cancelled, flattened, errors) == (1, 1, [])
    assert attempts == 2 and ex.working == []


async def test_account_flatten_reports_persistent_order_cleanup(admin):
    ex = FakeExecutor(
        "A", positions=[{"symbol": "MNQ", "netPos": 1}],
        working=[{"id": 5, "symbol": "MNQ"}],
    )

    async def fail_cancel(order_id):
        raise tradovate.TradovateError("still working")

    ex.cancel_order = fail_cancel
    _cancelled, flattened, errors = await _flatten_account(ex)
    assert flattened == 1
    assert any("post-close cancel 5" in e for e in errors)


async def test_tradovate_priority_transport_failure_is_outcome_unknown(admin, monkeypatch):
    class BrokenClient:
        async def request(self, method, url, **kw):
            req = httpx.Request(method, url)
            raise httpx.ReadTimeout("lost answer", request=req)

    monkeypatch.setattr(http, "client", lambda name="outbound": BrokenClient())
    monkeypatch.setattr(tradovate, "_fire", lambda coro: coro.close())
    s = tradovate.TradovateSession(0, {"name": "T", "environment": "demo", "enabled": True}, area_id=1)
    with pytest.raises(tradovate.OrderOutcomeUnknown):
        await s._request_raw("POST", "/order/placeorder", auth=False, json={})


async def test_projectx_oco_does_not_cancel_known_leg_when_second_is_unknown(admin, monkeypatch):
    s = projectx.ProjectXSession(0, {
        "name": "PX", "px_user": "u", "px_api_key": "k", "px_firm": "topstep",
        "accounts": [{"spec": "A", "id": 1, "enabled": True}],
    }, area_id=1)
    calls = 0
    cancelled = []

    async def place_order(**kw):
        nonlocal calls
        calls += 1
        if calls == 1:
            return {"status": "submitted", "order_id": 11, "raw": {}}
        raise tradovate.OrderOutcomeUnknown("second leg answer lost")

    async def cancel_order(order_id, **kw):
        cancelled.append(order_id)
        return {}

    monkeypatch.setattr(s, "place_order", place_order)
    monkeypatch.setattr(s, "cancel_order", cancel_order)
    with pytest.raises(tradovate.OrderOutcomeUnknown):
        await s.place_oco(
            symbol="MNQ", action="Sell", qty=1, order_type="Limit", price=100, stop_price=None,
            other={"action": "Sell", "order_type": "Stop", "price": None, "stop_price": 90},
            account_spec="A", account_id=1,
        )
    assert cancelled == []


async def test_rithmic_cancel_timeout_is_outcome_unknown(admin, monkeypatch):
    s = rithmic.RithmicSession(0, {
        "name": "R", "rithmic_user": "u", "rithmic_password": "p", "rithmic_system": "paper",
        "accounts": [{"spec": "A", "id": 1, "enabled": True}],
    }, area_id=1)

    class Client:
        async def cancel_order(self, **kw):
            raise asyncio.TimeoutError("answer lost")

    async def ensure():
        return Client()

    monkeypatch.setattr(s, "_ensure", ensure)
    monkeypatch.setattr(rithmic, "_fire", lambda coro: coro.close())
    with pytest.raises(tradovate.OrderOutcomeUnknown):
        await s.cancel_order(99, account_spec="A", account_id=1)


class _CopySession:
    def __init__(self, sequence):
        self.sequence = list(sequence)
        self.calls = 0

    async def positions_snapshot(self):
        self.calls += 1
        value = self.sequence[min(self.calls - 1, len(self.sequence) - 1)]
        if value is None:
            raise tradovate.TradovateError("positions unavailable")
        return [] if value == 0 else [{"accountId": 2, "contractId": 5, "netPos": value}]


class _CopyExecutor:
    def __init__(self, sequence):
        self.name = "F"
        self.id = 2
        self.session = _CopySession(sequence)
        self.orders = []

    async def place_order(self, **kw):
        self.orders.append(kw)
        return {"status": "submitted", "order_id": 1}


async def _copy_runner(monkeypatch, sequence):
    group = cp.new_group("g")
    group.update({
        "leader": {"token_idx": 0, "spec": "L", "account_id": 1},
        "followers": [cp.normalize_follower({"token_idx": 0, "spec": "F", "account_id": 2})],
    })
    runner = cp.GroupRunner(1, group)
    ex = _CopyExecutor(sequence)
    monkeypatch.setattr(runner, "_executor", lambda f: ex)

    async def cancel_all(**kw):
        return 0

    monkeypatch.setattr(runner.orders, "cancel_all", cancel_all)
    runner.leader_net[5] = 1
    runner.unit[5] = 1
    runner.contract_names[5] = "MNQ"
    runner.follower_pos[("F", 5)] = 1
    return runner, ex


async def test_copy_flatten_baselines_only_after_broker_confirms_flat(admin, monkeypatch):
    runner, ex = await _copy_runner(monkeypatch, [1, 0])
    n = await runner.flatten_followers(reason="feed lost")
    assert n == 1 and runner.flatten_unresolved == []
    assert 5 in runner.baseline and runner.follower_pos[("F", 5)] == 0
    assert len(ex.orders) == 1


async def test_copy_flatten_keeps_unconfirmed_exposure_out_of_baseline(admin, monkeypatch):
    monkeypatch.setattr(cp, "FLATTEN_VERIFY_DELAY_S", 0)
    runner, ex = await _copy_runner(monkeypatch, [1, 1, 1, 1])
    n = await runner.flatten_followers(reason="feed lost")
    assert n == 1 and runner.flatten_unresolved
    assert 5 not in runner.baseline and runner.follower_pos[("F", 5)] == 1
    assert len(ex.orders) == 1  # never blindly retries the close


async def test_updater_rolls_back_when_dependency_install_fails(admin, monkeypatch, tmp_path):
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(config, "ROOT_DIR", tmp_path)
    monkeypatch.delenv("RENDER", raising=False)
    monkeypatch.delenv("NEXUSPRED_MANAGED_HOST", raising=False)
    monkeypatch.setattr(config, "get_version", lambda force=False: "1.0.0")
    calls = []
    pip_calls = 0

    def fake_run(cmd):
        nonlocal pip_calls
        calls.append(list(cmd))
        if cmd[:3] == ["git", "rev-parse", "HEAD"]:
            return True, "oldsha"
        if cmd[:2] == ["git", "fetch"]:
            return True, "fetched"
        if cmd[:3] == ["git", "reset", "--hard"]:
            return True, "reset"
        if "pip" in cmd and "install" in cmd:
            pip_calls += 1
            return (False, "dependency failed") if pip_calls == 1 else (True, "restored")
        return True, "ok"

    monkeypatch.setattr(updater, "_run", fake_run)
    r = await updater.apply_update()
    assert r["success"] is False and "rolled back" in r["message"]
    assert ["git", "reset", "--hard", "oldsha"] in calls
    assert pip_calls == 2


async def test_updater_refuses_to_start_without_a_rollback_revision(admin, monkeypatch, tmp_path):
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(config, "ROOT_DIR", tmp_path)
    monkeypatch.delenv("RENDER", raising=False)
    monkeypatch.delenv("NEXUSPRED_MANAGED_HOST", raising=False)
    monkeypatch.setattr(config, "get_version", lambda force=False: "1.0.0")
    calls = []

    def fake_run(cmd):
        calls.append(list(cmd))
        return False, "not a revision"

    monkeypatch.setattr(updater, "_run", fake_run)
    r = await updater.apply_update()
    assert r["success"] is False and "current git revision" in r["message"]
    assert calls == [["git", "rev-parse", "HEAD"]]

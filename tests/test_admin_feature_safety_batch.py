"""Regression coverage for alpha.70/71 safety hardening."""
from __future__ import annotations

from app import leader_feed


class _Session:
    def __init__(self, value: int):
        self.lid = "same-login"
        self.value = value
        self.calls = 0

    async def positions_snapshot(self):
        self.calls += 1
        return [{"accountId": 1, "contractId": 10, "netPos": self.value}]

    async def orders_snapshot(self):
        return []


async def test_replacement_session_does_not_reuse_old_shared_snapshot(monkeypatch):
    leader_feed.reset()
    monkeypatch.setattr(leader_feed, "TTL_S", 60.0)
    old = _Session(1)
    replacement = _Session(2)
    first, shared1 = await leader_feed.snapshot(1, old, "positions")
    second, shared2 = await leader_feed.snapshot(1, replacement, "positions")
    assert shared1 is False and first[0]["netPos"] == 1
    assert shared2 is False and second[0]["netPos"] == 2
    assert old.calls == 1 and replacement.calls == 1

"""alpha.75 — the in-process event bus that alerts (and later automations / metrics) listen on."""
import asyncio

import pytest

from app import alerts, events


@pytest.fixture(autouse=True)
def _clean():
    events.reset()
    yield
    events.reset()


def test_subscribe_emit_and_unsubscribe():
    got = []
    off = events.subscribe("x.test", lambda e: got.append(e))
    assert events.emit("x.test", a=1) == 1 and got == [{"a": 1}]
    off()
    assert events.emit("x.test", a=2) == 0 and got == [{"a": 1}]


def test_kind_is_positional_only_so_data_may_carry_a_kind_key():
    got = []
    off = events.subscribe("risk.test", lambda e: got.append(e["kind"]))
    try:
        events.emit("risk.test", kind="loss", spec="DEMO11")
    finally:
        off()
    assert got == ["loss"]


def test_a_failing_handler_never_breaks_the_producer():
    got = []

    def bad(e):
        raise RuntimeError("boom")
    off1 = events.subscribe("y.test", bad)
    off2 = events.subscribe("y.test", lambda e: got.append(1))
    try:
        assert events.emit("y.test") == 2 and got == [1]
    finally:
        off1(); off2()


async def test_emit_async_awaits_every_handler_and_isolates_failures():
    got = []

    async def slow(e):
        await asyncio.sleep(0)
        got.append("slow")

    async def bad(e):
        raise RuntimeError("boom")
    offs = [events.subscribe("z.test", slow), events.subscribe("z.test", bad), events.subscribe("*", lambda k, e: got.append("star" if k == "z.test" else k))]
    try:
        assert await events.emit_async("z.test", n=1) == 2                    # two coroutines, the sync star handler ran inline
    finally:
        for off in offs:
            off()
    assert sorted(got) == ["slow", "star"]


async def test_emit_schedules_coroutines_in_the_background():
    done = asyncio.Event()

    async def handler(e):
        done.set()
    off = events.subscribe("bg.test", handler)
    try:
        events.emit("bg.test")
        await asyncio.wait_for(done.wait(), 1)
    finally:
        off()


def test_recent_keeps_a_bounded_log_without_settings_blobs():
    for i in range(events.RECENT_MAX + 5):
        events.emit("log.test", i=i, settings={"secret": True})
    rows = events.recent(limit=10, kind="log.test")
    assert len(rows) == 10 and rows[-1]["i"] == events.RECENT_MAX + 4 and "settings" not in rows[-1]
    assert len(events.recent(limit=1000)) == events.RECENT_MAX


def test_alerts_listen_on_every_kind_the_producers_emit():
    produced = {"connection.lost", "connection.restored", "trade.executed", "position.opened", "position.added", "position.closed",
                "agent.lost", "agent.restored", "risk.triggered", "execution.problem", "news.lock", "copy.alert", "daily.summary",
                "discord.lost", "discord.restored", "signal.failed", "rollover.due"}
    assert produced <= set(events._handlers)
    assert all(events._handlers[k] for k in produced)


async def test_handlers_see_monkeypatched_alert_functions(monkeypatch):
    got = []

    async def fake(title, message):
        got.append((title, message))
    monkeypatch.setattr(alerts, "execution_problem", fake)
    await events.emit_async("execution.problem", title="T", message="M")
    assert got == [("T", "M")]

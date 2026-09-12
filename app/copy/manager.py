"""Runner registry: start / stop / restart runners per workspace, the copy loop."""
from __future__ import annotations
import asyncio
import json
from typing import Any, Optional
from .. import context, db, state
from . import feed as leader_feed
from .groups import _runners, external_followers, load_groups, masked_status
from .group_runner import GroupRunner




def reset() -> None:
    _runners.clear()
    leader_feed.reset()


async def release_followers(publisher_area_id: int, group_id: str, specs: Any) -> int:
    """Accounts leaving a group (unsubscribe, kick, unpublish, a subscription
    edit) get their mirrored working orders cancelled first: a twin stop or
    limit would otherwise rest at the broker unmanaged and fill later, while
    the new runner no longer knows it. Positions are never touched. Returns
    the cancels sent."""
    r = _runners.get((publisher_area_id, group_id))
    specs = {str(s) for s in specs or () if s}
    if r is None or not specs:
        return 0
    n = 0
    with context.use_area(publisher_area_id):
        for spec in specs:
            try:
                n += await r.orders.cancel_all(reason="follower left the group", spec=spec)
            except Exception as exc:  # noqa: BLE001
                r.error = f"release {spec}: {exc}"[:200]
    return n


def _enabled_specs(accounts: Any) -> set[str]:
    return {str(a.get("spec")) for a in accounts or [] if isinstance(a, dict) and a.get("spec") and a.get("enabled", True)}


_sync_locks: dict[int, asyncio.Lock] = {}


async def sync_area(area_id: int) -> None:
    """Start runners for enabled groups, stop the others, restart changed ones.
    Serialised per area: the copy loop and the routers call this concurrently,
    and two callers seeing the same stale runner must not both start a fresh
    one (every leader change would be mirrored twice). A change of the
    marketplace followers alone is applied in place — a subscriber toggling
    their subscription must not cost the publisher a feed restart."""
    lock = _sync_locks.get(area_id)
    if lock is None:
        lock = _sync_locks[area_id] = asyncio.Lock()
    async with lock:
        groups = {g["id"]: g for g in load_groups(area_id)}
        for key, r in list(_runners.items()):
            if key[0] != area_id:
                continue
            g = groups.get(key[1])
            if g is None or not g.get("enabled") or json.dumps(g, sort_keys=True) != r.fingerprint[0]:
                _runners.pop(key, None)                # gone from the table before the await: no second starter
                await r.stop()
                continue
            external = external_followers(area_id, key[1], group=g)
            if json.dumps(external, sort_keys=True) != r.fingerprint[1]:
                await r.refresh_followers(external)
        for gid, g in groups.items():
            if g.get("enabled") and (area_id, gid) not in _runners:
                r = GroupRunner(area_id, g)
                _runners[(area_id, gid)] = r
                r.start()


def runner(area_id: int, group_id: str) -> Optional[GroupRunner]:
    return _runners.get((area_id, group_id))


def statuses(area_id: int) -> dict[str, dict[str, Any]]:
    return {gid: masked_status(r.status()) for (aid, gid), r in _runners.items() if aid == area_id}


_last_loop_error: dict[str, str] = {}


async def copy_loop() -> None:
    """Keep runners in line with the config and run the feed-loss watchdog."""
    while True:
        try:
            for aid in db.all_area_ids():
                with context.use_area(aid):
                    await sync_area(aid)
            for r in list(_runners.values()):
                try:
                    with context.use_area(r.area_id):
                        await r.watchdog()
                except Exception as exc:  # noqa: BLE001
                    r.error = f"watchdog: {exc}"[:200]
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - the loop must survive anything, but not silently
            if str(exc) != _last_loop_error.get("msg"):
                _last_loop_error["msg"] = str(exc)
                state.log_event("warn", f"copy loop: {exc}")
        await asyncio.sleep(5.0)


async def stop_all() -> None:
    for r in list(_runners.values()):
        await r.stop()
    _runners.clear()

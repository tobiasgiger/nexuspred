"""Fail-closed maintenance for copy-trading login/account bindings.

Copy groups persist a stable login id (``lid``), account ``spec`` and a numeric
broker account id. The id is only a cache: stable login + account spec are the
authority. Broker-account edits or rediscovery refresh cached ids and restart
publisher-side runners so a long-lived poll cannot retain a replaced session.
"""
from __future__ import annotations

from typing import Any

from . import config, copy, db, leader_feed


def _resolve_route(settings: dict[str, Any], route: dict[str, Any]) -> tuple[int | None, int]:
    tokens = settings.get("token_accounts") or []
    lid = str(route.get("lid") or "")
    idx = config.login_index(settings, lid) if lid else None
    if idx is None:
        try:
            candidate = int(route.get("token_idx"))
        except (TypeError, ValueError):
            candidate = -1
        if 0 <= candidate < len(tokens) and (not lid or not tokens[candidate].get("lid")):
            idx = candidate
    if idx is None or not (0 <= idx < len(tokens)):
        return None, 0
    spec = str(route.get("spec") or "")
    account = next((a for a in (tokens[idx].get("accounts") or [])
                    if str(a.get("spec") or a.get("account_spec") or "") == spec), None)
    try:
        aid = int((account or {}).get("id") or (account or {}).get("account_id") or 0)
    except (TypeError, ValueError):
        aid = 0
    return idx, aid


def _refresh_route(settings: dict[str, Any], route: dict[str, Any]) -> bool:
    idx, aid = _resolve_route(settings, route)
    changed = False
    if idx is not None and route.get("token_idx") != idx:
        route["token_idx"] = idx
        changed = True
    if int(route.get("account_id") or 0) != aid:
        route["account_id"] = aid
        changed = True
    return changed


def _refresh_runtime(area_id: int, settings: dict[str, Any]) -> None:
    """Clear stale numeric ids in already-running groups immediately."""
    for (publisher_area, _gid), runner in list(copy._runners.items()):
        if publisher_area == area_id:
            lead = runner.group.get("leader")
            if isinstance(lead, dict):
                _refresh_route(settings, lead)
        for follower in runner.followers:
            farea = int(follower.get("area_id") or publisher_area)
            if farea == area_id:
                _refresh_route(settings, follower)


def repair(area_id: int) -> set[int]:
    """Refresh persisted and live bindings; return affected publisher areas."""
    publishers: set[int] = set()

    def mutate(s: dict[str, Any]) -> None:
        groups = list(s.get("copy_groups") or [])
        changed = False
        for g in groups:
            if not isinstance(g, dict):
                continue
            lead = g.get("leader")
            if isinstance(lead, dict):
                changed |= _refresh_route(s, lead)
            for f in g.get("followers") or []:
                if isinstance(f, dict):
                    changed |= _refresh_route(s, f)
        if changed:
            s["copy_groups"] = groups

    config.update(mutate, area_id=area_id)
    settings = config.load_settings(area_id=area_id)
    _refresh_runtime(area_id, settings)

    for sub in db.list_subscriptions(area_id):
        if not str(sub.get("webhook_id") or "").startswith("copy:"):
            continue
        accounts = [dict(a) for a in (sub.get("accounts") or []) if isinstance(a, dict)]
        changed = False
        for account in accounts:
            changed |= _refresh_route(settings, account)
        if changed:
            db.update_subscription(int(sub["id"]), area_id, accounts=accounts)
            publishers.add(int(sub["publisher_area_id"]))
    return publishers


async def _restart_area(area_id: int) -> None:
    """Restart runners and invalidate shared snapshots from their old sessions."""
    for key, runner in list(copy._runners.items()):
        if key[0] != area_id:
            continue
        try:
            session = runner._leader_session()
            if session is not None:
                leader_feed.drop(area_id, session)
        except Exception:  # noqa: BLE001 - cache invalidation is best effort; stop still proceeds
            pass
        await runner.stop()
        copy._runners.pop(key, None)
    await copy.sync_area(area_id)


async def refresh(area_id: int) -> None:
    """Repair and restart after login/account edits or Connect & Verify."""
    publishers = repair(area_id)
    await _restart_area(area_id)
    for publisher in publishers:
        if publisher != area_id:
            await copy.sync_area(publisher)

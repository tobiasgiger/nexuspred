"""Fail-closed maintenance for copy-trading login/account bindings.

Copy groups persist a stable login id (``lid``), account ``spec`` and a numeric
broker account id.  The id is only a cache: the stable login + account spec are
the authority.  When Broker Accounts are edited or rediscovered we refresh the
cached ids and restart publisher-side runners so no long-lived poll keeps using
a replaced broker session.
"""
from __future__ import annotations

from typing import Any

from . import config, copy, db


def _resolve_route(settings: dict[str, Any], route: dict[str, Any]) -> tuple[int | None, int]:
    """Return (current login index, current account id) for a stored route.

    A missing login/spec deliberately yields account id 0.  We never retain a
    numeric id merely because the intended account can no longer be resolved.
    """
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
    account = next((a for a in (tokens[idx].get("accounts") or []) if str(a.get("spec") or a.get("account_spec") or "") == spec), None)
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
    # A stale broker id is more dangerous than an unknown one.  Zero makes the
    # copy runner resolve by current session/spec or fail closed.
    if int(route.get("account_id") or 0) != aid:
        route["account_id"] = aid
        changed = True
    return changed


def repair(area_id: int) -> set[int]:
    """Refresh persisted bindings for one workspace.

    Returns publisher area ids whose external-follower subscription changed and
    therefore need ``copy.sync_area`` after the caller is ready to touch tasks.
    """
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

    # Marketplace copy followers live in the subscriber's workspace settings but
    # execute inside a publisher-side runner. Refresh their cached account ids too.
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
    """Restart this area's group runners so they re-resolve the current session."""
    for key, runner in list(copy._runners.items()):  # one intentional lifecycle hook; no trade state is rewritten
        if key[0] != area_id:
            continue
        await runner.stop()
        copy._runners.pop(key, None)
    await copy.sync_area(area_id)


async def refresh(area_id: int) -> None:
    """Repair bindings after a login/account edit or broker re-discovery.

    Publisher runners are restarted even when the stable lid/spec did not change:
    the SessionManager may have replaced the underlying session because the broker,
    environment or account set changed.  A running copy feed must not retain that
    old session object.
    """
    publishers = repair(area_id)
    await _restart_area(area_id)
    for publisher in publishers:
        if publisher != area_id:
            await copy.sync_area(publisher)

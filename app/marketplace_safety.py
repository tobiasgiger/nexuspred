"""Execution-time safety for marketplace copy subscriptions.

Publishing ACLs and follower identity are live security/safety constraints.  A
subscription that was valid yesterday must stop participating after the publisher
revokes it, and two follower accounts with the same textual broker spec must not
be allowed into a runner whose legacy runtime state is keyed by that spec.
"""
from __future__ import annotations

from typing import Any

from . import copy, db, marketplace, state


def _enabled_specs(accounts: Any) -> set[str]:
    return {str(a.get("spec") or "") for a in (accounts or [])
            if isinstance(a, dict) and a.get("spec") and a.get("enabled", True)}


def validate_copy_accounts(publisher_area_id: int, group_id: str,
                           accounts: list[dict[str, Any]], *, exclude_sub_id: int | None = None) -> None:
    """Reject runtime follower-spec collisions before a subscription is saved."""
    group, _sharing = copy.find_published(publisher_area_id, group_id)
    if not group:
        raise ValueError("copy group is not published")
    taken = _enabled_specs(group.get("followers"))
    leader_spec = str((group.get("leader") or {}).get("spec") or "")
    if leader_spec:
        taken.add(leader_spec)
    for sub in db.active_subscriptions(publisher_area_id, f"copy:{group_id}"):
        if exclude_sub_id is not None and int(sub.get("id") or 0) == int(exclude_sub_id):
            continue
        taken |= _enabled_specs(sub.get("accounts"))
    clash = sorted(_enabled_specs(accounts) & taken)
    if clash:
        raise ValueError(
            "This copy group already contains a follower with the same broker account spec. "
            "Follower account specs must be unique inside a published group."
        )


async def reconcile_copy_group(publisher_area_id: int, group_id: str, *, sync: bool = True) -> int:
    """Disable subscriptions/accounts no longer safe for one published group.

    ACLs are re-evaluated against the publisher's current sharing configuration.
    Ambiguous duplicate follower specs are disabled rather than letting one
    tenant's state overwrite another tenant's runtime tracking. Mirrored working
    orders are cancelled before a follower is removed; positions are untouched.
    """
    group, _sharing = copy.find_published(publisher_area_id, group_id)
    if not group:
        return 0

    taken = _enabled_specs(group.get("followers"))
    leader_spec = str((group.get("leader") or {}).get("spec") or "")
    if leader_spec:
        taken.add(leader_spec)
    changed = 0

    for sub in db.active_subscriptions(publisher_area_id, f"copy:{group_id}"):
        sub_id = int(sub["id"])
        sub_area = int(sub["area_id"])
        accounts = [dict(a) for a in (sub.get("accounts") or []) if isinstance(a, dict)]
        removed: set[str] = set()

        if not marketplace.subscription_allowed(group, sub):
            removed = _enabled_specs(accounts)
            if sync and removed:
                await copy.release_followers(publisher_area_id, group_id, removed)
            db.update_subscription(sub_id, sub_area, enabled=False)
            changed += 1
            continue

        for account in accounts:
            spec = str(account.get("spec") or "")
            if not spec or not account.get("enabled", True):
                continue
            if spec in taken:
                account["enabled"] = False
                removed.add(spec)
            else:
                taken.add(spec)
        if removed:
            if sync:
                await copy.release_followers(publisher_area_id, group_id, removed)
            db.update_subscription(sub_id, sub_area, accounts=accounts)
            changed += 1

    if changed:
        if sync:
            await copy.sync_area(publisher_area_id)
        state.log_event("warn", f"Copy marketplace safety: {changed} subscription(s) revoked or de-duplicated")
    return changed


async def reconcile_all(*, sync: bool = True) -> int:
    """Repair stale marketplace-copy authorization; optionally before runners start."""
    changed = 0
    for area_id in db.all_area_ids():
        for group in copy.load_groups(area_id):
            if marketplace.sharing_of(group).get("enabled"):
                changed += await reconcile_copy_group(area_id, str(group.get("id") or ""), sync=sync)
    return changed

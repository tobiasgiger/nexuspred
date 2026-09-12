"""Execution-time safety for marketplace and local copy followers.

Copy's legacy runtime state is keyed by broker account ``spec``. Until that
internal state is migrated to a composite identity, a group must never contain
two enabled followers with the same spec, even when they belong to different
logins/workspaces. Marketplace ACLs are also live authorization: revocation must
stop future mirroring rather than merely hiding the listing in the UI.
"""
from __future__ import annotations

from typing import Any

from . import copy, db, marketplace, state

_installed = False
_original_validate_group = None
_original_external_followers = None


def _enabled_specs(accounts: Any) -> set[str]:
    return {str(a.get("spec") or "") for a in (accounts or [])
            if isinstance(a, dict) and a.get("spec") and a.get("enabled", True)}


def validate_group_specs(group: dict[str, Any]) -> None:
    """Reject identities that would collide in GroupRunner's spec-keyed maps."""
    leader = str((group.get("leader") or {}).get("spec") or "")
    seen: set[str] = set()
    for follower in group.get("followers") or []:
        if not isinstance(follower, dict) or not follower.get("enabled", True):
            continue
        spec = str(follower.get("spec") or "")
        if not spec:
            continue
        if spec == leader:
            raise ValueError("The leader and a follower cannot share the same broker account spec")
        if spec in seen:
            raise ValueError(f"Follower {spec} is ambiguous — account specs must be unique inside one copy group")
        seen.add(spec)


def validate_copy_accounts(publisher_area_id: int, group_id: str,
                           accounts: list[dict[str, Any]], *, exclude_sub_id: int | None = None) -> None:
    """Reject a subscription that would collide with any follower already in the group."""
    group, _sharing = copy.find_published(publisher_area_id, group_id)
    if not group:
        raise ValueError("copy group is not published")
    validate_group_specs(group)
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
            f"Follower {clash[0]} is already present in this copy group. "
            "Broker account specs must be unique inside one published group."
        )


def _safe_external_followers(area_id: int, group_id: str, *, group: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Filter the normal external-follower list by the publisher's current ACL."""
    assert _original_external_followers is not None
    followers = _original_external_followers(area_id, group_id, group=group)
    current = group
    if current is None:
        current, _ = copy.find_published(area_id, group_id)
    if not current:
        return []
    allowed = {
        int(sub["id"]) for sub in db.active_subscriptions(area_id, f"copy:{group_id}")
        if marketplace.subscription_allowed(current, sub)
    }
    return [f for f in followers if int(f.get("sub_id") or 0) in allowed]


def install() -> None:
    """Install fail-closed checks at the existing copy chokepoints (idempotent)."""
    global _installed, _original_validate_group, _original_external_followers
    if _installed:
        return
    _original_validate_group = copy.validate_group
    _original_external_followers = copy.external_followers

    def validate(group: dict[str, Any], all_groups: list[dict[str, Any]], accounts: list[dict[str, Any]]) -> None:
        assert _original_validate_group is not None
        _original_validate_group(group, all_groups, accounts)
        validate_group_specs(group)

    copy.validate_group = validate  # type: ignore[assignment]
    copy.external_followers = _safe_external_followers  # type: ignore[assignment]
    _installed = True


def repair_own_groups(area_id: int) -> int:
    """Disable pre-existing ambiguous groups rather than running them unsafely."""
    groups = copy.load_groups(area_id)
    changed = 0
    for group in groups:
        if not group.get("enabled"):
            continue
        try:
            validate_group_specs(group)
        except ValueError as exc:
            group["enabled"] = False
            changed += 1
            state.log_event("error", f"Copy group '{group.get('name', '?')}' disabled: {exc}")
    if changed:
        copy.save_groups(groups, area_id)
    return changed


async def reconcile_copy_group(publisher_area_id: int, group_id: str, *, sync: bool = True) -> int:
    """Disable subscriptions/accounts no longer safe for one published group.

    Mirrored working orders are cancelled before a live follower is removed;
    positions are deliberately not touched.
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
    """Repair old copy config/subscriptions; safe to call before runners start."""
    install()
    changed = 0
    for area_id in db.all_area_ids():
        changed += repair_own_groups(area_id)
        for group in copy.load_groups(area_id):
            if marketplace.sharing_of(group).get("enabled"):
                changed += await reconcile_copy_group(area_id, str(group.get("id") or ""), sync=sync)
    return changed

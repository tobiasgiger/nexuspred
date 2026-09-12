"""Signal marketplace.

An admin can **publish** one of their webhooks. Other users see it on the
Marketplace page (title, strategy, description — never the URL/token or the
publisher's accounts) and can **subscribe**: they pick which of *their own*
trade accounts (with a qty multiplier) the signal should trade and can switch
the subscription on/off. When a TradingView alert hits the published webhook it
is executed in the publisher's area as usual **and** forwarded to every enabled
subscription, each in the subscriber's own area (their trading switch, symbol
map, alerts and logs) — see :func:`app.signals.forward_to_subscribers`.

Sharing config lives on the webhook dict itself (``webhook["sharing"]``), so
the settings schema is untouched; subscriptions live in the ``subscriptions``
table (:mod:`app.db`).
"""
from __future__ import annotations

from typing import Any, Optional

from . import config, db, sizing

VISIBILITIES = ("all", "selected")


def sharing_of(webhook: dict[str, Any]) -> dict[str, Any]:
    """The normalised sharing config of a webhook (defaults: not shared)."""
    s = webhook.get("sharing") or {}
    allowed: list[int] = []
    for x in s.get("allowed_user_ids") or []:
        try:
            allowed.append(int(x))
        except (TypeError, ValueError):
            continue
    return {
        "enabled": bool(s.get("enabled")),
        "title": str(s.get("title") or "").strip(),
        "description": str(s.get("description") or "").strip(),
        "visibility": s.get("visibility") if s.get("visibility") in VISIBILITIES else "all",
        "allowed_user_ids": sorted(set(allowed)),
    }


def normalize_sharing(body: dict[str, Any], current: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Merge a sharing update (from the API) over the current config, coercing types."""
    merged = dict(current or {})
    for key in ("enabled", "title", "description", "visibility", "allowed_user_ids"):
        if key in body:
            merged[key] = body[key]
    out = sharing_of({"sharing": merged})
    out["title"] = out["title"][:80]
    out["description"] = out["description"][:1000]
    return out


def visible_to(sharing: dict[str, Any], user_id: int) -> bool:
    if not sharing.get("enabled"):
        return False
    if sharing.get("visibility") == "selected":
        return int(user_id) in (sharing.get("allowed_user_ids") or [])
    return True


def subscription_allowed(webhook: dict[str, Any], sub: dict[str, Any]) -> bool:
    """Re-check a subscription against the publisher's *current* ACL.

    Subscription creation is not an authorization lease: removing a user from a
    selected publication must stop future executions immediately, without waiting
    for the subscriber to edit or delete the persisted subscription row.
    """
    try:
        owner = db.area_owner(int(sub.get("area_id") or 0))
    except (TypeError, ValueError):
        owner = None
    return bool(owner and visible_to(sharing_of(webhook), int(owner)))


def public_view(webhook: dict[str, Any], publisher_area_id: int,
                publisher_email: Optional[str] = None) -> dict[str, Any]:
    """What a subscriber may see of a published webhook (no token, no accounts)."""
    sh = sharing_of(webhook)
    return {
        "publisher_area_id": publisher_area_id,
        "webhook_id": webhook.get("id", ""),
        "title": sh["title"] or webhook.get("name") or "Signal",
        "description": sh["description"],
        "strategy": webhook.get("strategy", "simple"),
        "default_qty": webhook.get("default_qty", 1),
        "tp_qty": webhook.get("tp_qty", 1),
        "visibility": sh["visibility"],
        "publisher_email": publisher_email if publisher_email is not None else db.area_owner_email(publisher_area_id),
        "webhook_enabled": bool(webhook.get("enabled")),
    }


def published_webhooks(*, user_id: Optional[int] = None,
                       exclude_area: Optional[int] = None) -> list[dict[str, Any]]:
    """Every published webhook (optionally only those visible to ``user_id``),
    across all areas except ``exclude_area`` (a user can't subscribe to their own)."""
    out: list[dict[str, Any]] = []
    for aid in db.all_area_ids():
        if exclude_area is not None and aid == exclude_area:
            continue
        email = None
        for wh in config.load_settings(area_id=aid).get("webhooks") or []:
            sh = sharing_of(wh)
            if not sh["enabled"]:
                continue
            if user_id is not None and not visible_to(sh, user_id):
                continue
            if email is None:
                email = db.area_owner_email(aid) or ""
            out.append(public_view(wh, aid, email))
    return out


def find_published(publisher_area_id: int, webhook_id: str) -> tuple[Optional[dict[str, Any]], dict[str, Any]]:
    """(webhook, sharing) for a published webhook, or (None, {}) if it isn't published."""
    for wh in config.load_settings(area_id=publisher_area_id).get("webhooks") or []:
        if wh.get("id") == webhook_id:
            sh = sharing_of(wh)
            return (wh, sh) if sh["enabled"] else (None, {})
    return None, {}


def clean_accounts(raw: Any) -> list[dict[str, Any]]:
    """Coerce a subscriber's routed-accounts list (same shape as a webhook's)."""
    out: list[dict[str, Any]] = []
    for a in raw or []:
        if not isinstance(a, dict) or not a.get("spec") or a.get("token_idx") is None:
            continue
        try:
            sz = sizing.normalize(a)
            s = config.load_settings()
            idx = int(a["token_idx"])
            lid = str(a.get("lid") or "")
            if lid and config.login_index(s, lid) is not None:
                idx = config.login_index(s, lid)
            elif not lid:
                tokens = s.get("token_accounts") or []
                lid = str(tokens[idx].get("lid") or "") if 0 <= idx < len(tokens) else ""
            out.append({
                "token_idx": idx, "lid": lid,
                "spec": str(a.get("spec", "")),
                "enabled": bool(a.get("enabled", True)),
                "qty_multiplier": sizing.effective_multiplier(sz),
                "sizing": sz,
            })
        except (TypeError, ValueError):
            continue
    return out


def _execution_window(webhook: dict[str, Any], publisher_area_id: int) -> Any:
    """Publisher window for subscriber execution, with its fallback timezone frozen.

    An empty window timezone means "publisher journal timezone". Once execution
    moves into the subscriber workspace that fallback would otherwise become the
    subscriber's timezone and could widen/narrow the entry window by hours.
    """
    raw = webhook.get("trade_window")
    if not isinstance(raw, dict):
        return raw
    out = dict(raw)
    if out.get("enabled") and not str(out.get("tz") or "").strip():
        s = config.load_settings(area_id=publisher_area_id)
        out["tz"] = str(s.get("journal_timezone") or "Europe/Zurich")
    return out


def subscription_view(webhook: dict[str, Any], sub: dict[str, Any], publisher_area_id: int) -> dict[str, Any]:
    """The webhook as seen by the signal engine when executing a subscription:
    the publisher's strategy/qty/window settings with the subscriber's accounts.
    Its id is unique per publisher webhook so tracked trades never collide with
    the subscriber's own webhooks.
    """
    sh = sharing_of(webhook)
    return {
        "id": f"sub{publisher_area_id}_{webhook.get('id', '')}",
        "name": sh["title"] or webhook.get("name") or "Signal",
        "token": "",
        "enabled": True,
        "strategy": webhook.get("strategy", "simple"),
        "default_qty": webhook.get("default_qty", 1),
        "tp_qty": webhook.get("tp_qty", 1),
        "trade_window": _execution_window(webhook, publisher_area_id),
        "accounts": sub.get("accounts") or [],
        "subscription": {"id": sub.get("id"), "publisher_area_id": publisher_area_id,
                         "webhook_id": webhook.get("id", "")},
    }

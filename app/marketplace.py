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

from datetime import datetime, timezone

from . import config, db, sizing, trade_window

VISIBILITIES = ("all", "selected")
MAX_TAGS, TAG_LEN = 5, 20
MAX_MAX_SUBSCRIBERS = 10_000


def sharing_of(webhook: dict[str, Any]) -> dict[str, Any]:
    """The normalised sharing config of a webhook (defaults: not shared)."""
    s = webhook.get("sharing") or {}
    allowed: list[int] = []
    for x in s.get("allowed_user_ids") or []:
        try:
            allowed.append(int(x))
        except (TypeError, ValueError):
            continue
    tags: list[str] = []
    for x in s.get("tags") or []:
        x = str(x or "").strip().lower()[:TAG_LEN]
        if x and x not in tags:
            tags.append(x)
    try:
        max_subs = max(0, min(MAX_MAX_SUBSCRIBERS, int(s.get("max_subscribers") or 0)))
    except (TypeError, ValueError):
        max_subs = 0
    try:
        price = max(0, int(s.get("price_cents") or 0))
        trial = max(0, int(s.get("trial_days") or 0))
    except (TypeError, ValueError):
        price, trial = 0, 0
    return {
        "enabled": bool(s.get("enabled")),
        "title": str(s.get("title") or "").strip(),
        "description": str(s.get("description") or "").strip(),
        "visibility": s.get("visibility") if s.get("visibility") in VISIBILITIES else "all",
        "allowed_user_ids": sorted(set(allowed)),
        # publisher controls (alpha.78)
        "max_subscribers": max_subs,                    # 0 = unlimited
        "approval": bool(s.get("approval")),            # new subscriptions wait for the publisher's OK
        "paused": bool(s.get("paused")),                # forwarding stopped for everyone, listing stays
        "tags": tags[:MAX_TAGS],
        "published_at": str(s.get("published_at") or ""),
        "price_cents": price,                           # monthly, 0 = free (alpha.79)
        "trial_days": trial,
    }


def normalize_sharing(body: dict[str, Any], current: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Merge a sharing update (from the API) over the current config, coercing types."""
    merged = dict(current or {})
    for key in ("enabled", "title", "description", "visibility", "allowed_user_ids", "max_subscribers", "approval", "paused", "tags"):
        if key in body:
            merged[key] = body[key]
    from . import payments
    if "price_cents" in body:
        merged["price_cents"] = payments.normalize_price(body["price_cents"])
    if "trial_days" in body:
        merged["trial_days"] = payments.normalize_trial(body["trial_days"])
    if isinstance(merged.get("tags"), str):
        merged["tags"] = [x for x in merged["tags"].split(",")]
    out = sharing_of({"sharing": merged})
    out["title"] = out["title"][:80]
    out["description"] = out["description"][:1000]
    was = bool((current or {}).get("enabled"))
    if out["enabled"] and (not was or not out["published_at"]):
        out["published_at"] = datetime.now(timezone.utc).isoformat()
    return out


# ------------------------------------------------------- subscriber controls
DEFAULT_CONTROLS: dict[str, Any] = {"symbols": [], "trade_window": None, "max_qty": 0, "max_signals_per_day": 0, "pause_after_errors": 0}


def _root(symbol: str) -> str:
    from .engine.common import _base_root
    return _base_root(str(symbol or "").strip().upper())


def normalize_controls(raw: Any) -> dict[str, Any]:
    """A subscriber's controls typed and bounded (ValueError on bad input)."""
    if raw in (None, ""):
        return dict(DEFAULT_CONTROLS)
    if not isinstance(raw, dict):
        raise ValueError("controls must be an object")
    out = dict(DEFAULT_CONTROLS)
    syms = raw.get("symbols")
    if isinstance(syms, str):
        syms = syms.split(",")
    if syms is not None:
        if not isinstance(syms, list):
            raise ValueError("symbols must be a list")
        roots = []
        for x in syms:
            r = _root(str(x)) if isinstance(x, str) else ""
            if r and r not in roots:
                roots.append(r)
        if len(roots) > 20:
            raise ValueError("at most 20 symbols")
        out["symbols"] = roots
    tw = raw.get("trade_window")
    if tw not in (None, "", False):
        out["trade_window"] = trade_window.normalize(tw)
        if not out["trade_window"].get("enabled"):
            out["trade_window"] = None
    for key, hi in (("max_qty", 1000), ("max_signals_per_day", 500), ("pause_after_errors", 50)):
        v = raw.get(key, 0)
        try:
            n = int(float(v or 0))
        except (TypeError, ValueError):
            raise ValueError(f"{key} must be a whole number")
        if not 0 <= n <= hi:
            raise ValueError(f"{key} must be between 0 and {hi}")
        out[key] = n
    return out


def controls_of(sub: dict[str, Any]) -> dict[str, Any]:
    try:
        return normalize_controls(sub.get("controls"))
    except (TypeError, ValueError):
        return dict(DEFAULT_CONTROLS)


def subscription_gate(view: dict[str, Any], root: str, action: str, *, area_id: int) -> tuple[bool, str, str]:
    """Whether a subscription may run this signal: ``(ok, reason, detail)``.
    Symbols apply to every action; the daily cap to entries only."""
    c = view.get("controls") or {}
    if c.get("symbols") and root.upper() not in c["symbols"]:
        return False, "subscription_symbols", f"{root} is not in the subscription's symbols ({', '.join(c['symbols'])})"
    cap = int(c.get("max_signals_per_day") or 0)
    if cap and action in ("buy", "sell"):
        day_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        n = db.count_signal_outcomes(area_id, str(view.get("id") or ""), day_start)
        if n >= cap:
            return False, "subscription_daily_cap", f"{n} signal(s) already today (cap {cap})"
    return True, "", ""


def visible_to(sharing: dict[str, Any], user_id: int) -> bool:
    if not sharing.get("enabled"):
        return False
    if sharing.get("visibility") == "selected":
        return int(user_id) in (sharing.get("allowed_user_ids") or [])
    return True


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
        "webhook_enabled": bool(webhook.get("enabled")) and not sh["paused"],
        "paused": sh["paused"], "approval": sh["approval"], "max_subscribers": sh["max_subscribers"],
        "tags": sh["tags"], "published_at": sh["published_at"],
        **_price_view(sh),
    }


def _price_view(sh: dict[str, Any]) -> dict[str, Any]:
    from . import payments
    paid = payments.is_paid_listing(sh)
    return {"price_cents": sh["price_cents"] if paid else 0, "trial_days": sh["trial_days"] if paid else 0,
            "currency": payments.get_config()["currency"] if paid else "", "paid": paid}


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


def subscription_view(webhook: dict[str, Any], sub: dict[str, Any], publisher_area_id: int) -> dict[str, Any]:
    """The webhook as seen by the signal engine when executing a subscription:
    the publisher's strategy/qty settings with the subscriber's accounts. Its id
    is unique per publisher webhook so tracked trades never collide with the
    subscriber's own webhooks."""
    sh = sharing_of(webhook)
    c = controls_of(sub)
    accounts = []
    for a in sub.get("accounts") or []:
        if not isinstance(a, dict):
            continue
        a = dict(a)
        if c["max_qty"]:                                  # the subscriber's cap over their per-account sizing
            try:
                sz = dict(a.get("sizing") or sizing.normalize(a))
            except (TypeError, ValueError):
                sz = {"mode": "same", "multiplier": 1.0, "fixed": 1, "max_contracts": 0}
            mx = int(sz.get("max_contracts") or 0)
            sz["max_contracts"] = min(mx, c["max_qty"]) if mx else c["max_qty"]
            a["sizing"] = sz
        accounts.append(a)
    return {
        "id": f"sub{publisher_area_id}_{webhook.get('id', '')}",
        "name": sh["title"] or webhook.get("name") or "Signal",
        "token": "",
        "enabled": True,
        "strategy": webhook.get("strategy", "simple"),
        "default_qty": webhook.get("default_qty", 1),
        "tp_qty": webhook.get("tp_qty", 1),
        "accounts": accounts,
        "trade_window": c["trade_window"],
        "controls": c,
        "subscription": {"id": sub.get("id"), "publisher_area_id": publisher_area_id,
                         "webhook_id": webhook.get("id", "")},
    }

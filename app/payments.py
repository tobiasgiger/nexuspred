"""Paid marketplace subscriptions through Stripe Checkout + webhooks.

The bridge operator (admin) connects *their* Stripe account under Settings →
Payments; a publisher sets a monthly price (and an optional trial) on a
listing. A subscriber who subscribes to a paid listing gets a Checkout link;
the subscription stays ``unpaid`` (nothing is forwarded) until Stripe reports
the payment, and drops back to ``unpaid`` when the Stripe subscription is
cancelled or a payment fails. Stripe is called over plain HTTPS (no SDK); the
webhook signature is verified before anything is trusted. Money lands in the
operator's Stripe account — settling with publishers is outside the bridge.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Optional

from . import crypto, db, state

log = logging.getLogger("payments")

META_KEY = "payments"
API = "https://api.stripe.com/v1"
SIGNATURE_TOLERANCE_S = 300
CURRENCIES = ("usd", "eur", "chf", "gbp")
MAX_PRICE_CENTS = 1_000_000
MAX_TRIAL_DAYS = 90
DEFAULTS: dict[str, Any] = {"enabled": False, "stripe_secret_key": "", "stripe_webhook_secret": "", "currency": "usd", "trial_days_default": 0}
SECRET_KEYS = ("stripe_secret_key", "stripe_webhook_secret")

_cache: Optional[dict[str, Any]] = None


# ------------------------------------------------------------------ config
def get_config() -> dict[str, Any]:
    """The operator's payments config with secrets decrypted (cached)."""
    global _cache
    if _cache is not None:
        return dict(_cache)
    raw = db.meta_get(META_KEY)
    cfg = dict(DEFAULTS)
    if raw:
        try:
            stored = json.loads(raw)
            if isinstance(stored, dict):
                cfg.update({k: stored[k] for k in DEFAULTS if k in stored})
        except ValueError:
            pass
    for k in SECRET_KEYS:
        v = cfg.get(k) or ""
        cfg[k] = str(crypto.decrypt(v) or "") if v else ""
    _cache = cfg
    return dict(cfg)


def save_config(updates: dict[str, Any]) -> dict[str, Any]:
    """Merge validated updates; secrets are encrypted at rest. ValueError on bad input."""
    global _cache
    cfg = get_config()
    if "enabled" in updates:
        cfg["enabled"] = bool(updates["enabled"])
    if "currency" in updates:
        cur = str(updates["currency"] or "usd").lower().strip()
        if cur not in CURRENCIES:
            raise ValueError(f"currency must be one of {', '.join(CURRENCIES)}")
        cfg["currency"] = cur
    if "trial_days_default" in updates:
        try:
            n = int(float(updates["trial_days_default"] or 0))
        except (TypeError, ValueError):
            raise ValueError("trial_days_default must be a whole number")
        if not 0 <= n <= MAX_TRIAL_DAYS:
            raise ValueError(f"trial_days_default must be between 0 and {MAX_TRIAL_DAYS}")
        cfg["trial_days_default"] = n
    for k in SECRET_KEYS:
        if k in updates and updates[k] != "********":
            v = str(updates[k] or "").strip()
            if len(v) > 200 or any(ch.isspace() for ch in v):
                raise ValueError(f"{k} looks wrong")
            cfg[k] = v
    if cfg["stripe_secret_key"] and not cfg["stripe_secret_key"].startswith(("sk_", "rk_")):
        raise ValueError("the Stripe secret key starts with sk_ (or a restricted rk_ key)")
    if cfg["stripe_webhook_secret"] and not cfg["stripe_webhook_secret"].startswith("whsec_"):
        raise ValueError("the webhook signing secret starts with whsec_")
    stored = dict(cfg)
    for k in SECRET_KEYS:
        stored[k] = crypto.encrypt(cfg[k]) if cfg[k] else ""
    db.meta_set(META_KEY, json.dumps(stored))
    _cache = dict(cfg)
    return dict(cfg)


def public_config() -> dict[str, Any]:
    cfg = get_config()
    return {**{k: ("********" if cfg[k] else "") for k in SECRET_KEYS},
            "enabled": cfg["enabled"], "currency": cfg["currency"], "trial_days_default": cfg["trial_days_default"],
            "configured": configured(), "webhook_configured": bool(cfg["stripe_webhook_secret"])}


def configured() -> bool:
    cfg = get_config()
    return bool(cfg["enabled"] and cfg["stripe_secret_key"])


def reset() -> None:
    global _cache
    _cache = None


# ------------------------------------------------------------ listing price
def normalize_price(raw: Any) -> int:
    try:
        n = int(float(raw or 0))
    except (TypeError, ValueError):
        raise ValueError("price_cents must be a whole number of cents")
    if not 0 <= n <= MAX_PRICE_CENTS:
        raise ValueError(f"price_cents must be between 0 and {MAX_PRICE_CENTS}")
    if 0 < n < 100:
        raise ValueError("the minimum price is 1.00")
    return n


def normalize_trial(raw: Any) -> int:
    try:
        n = int(float(raw or 0))
    except (TypeError, ValueError):
        raise ValueError("trial_days must be a whole number")
    if not 0 <= n <= MAX_TRIAL_DAYS:
        raise ValueError(f"trial_days must be between 0 and {MAX_TRIAL_DAYS}")
    return n


def is_paid_listing(sharing: dict[str, Any]) -> bool:
    return configured() and int(sharing.get("price_cents") or 0) > 0


def has_paid(area_id: int, publisher_area_id: int, key: str) -> bool:
    p = db.get_payment(area_id, publisher_area_id, key)
    return bool(p and p["status"] in db.payments.PAID)


# ------------------------------------------------------------------ Stripe
async def _stripe(method: str, path: str, data: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """One Stripe API call (form-encoded, Basic auth). Raises RuntimeError with
    Stripe's message on a non-2xx answer."""
    from . import http
    key = get_config()["stripe_secret_key"]
    if not key:
        raise RuntimeError("Stripe is not configured")
    client = http.client("outbound")
    r = await client.request(method, API + path, data=data or None, auth=(key, ""), timeout=20.0,
                             headers={"User-Agent": "Fluxbridge/payments"})
    try:
        body = r.json()
    except ValueError:
        body = {}
    if r.status_code >= 300:
        msg = ((body or {}).get("error") or {}).get("message") or f"HTTP {r.status_code}"
        raise RuntimeError(f"Stripe: {msg}")
    return body


def _iso(ts: Any) -> str:
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat() if ts else ""
    except (TypeError, ValueError, OSError):
        return ""


async def create_checkout(*, area_id: int, publisher_area_id: int, key: str, title: str, price_cents: int, trial_days: int,
                          email: str, base_url: str) -> str:
    """Start a Stripe Checkout (subscription mode) and remember the pending
    payment. Returns the URL to send the subscriber to."""
    cfg = get_config()
    existing = db.get_payment(area_id, publisher_area_id, key)
    data: dict[str, Any] = {
        "mode": "subscription",
        "success_url": f"{base_url}/#/subscriptions?paid=1",
        "cancel_url": f"{base_url}/#/marketplace",
        "client_reference_id": f"{area_id}:{publisher_area_id}:{key}",
        "line_items[0][quantity]": "1",
        "line_items[0][price_data][currency]": cfg["currency"],
        "line_items[0][price_data][unit_amount]": str(int(price_cents)),
        "line_items[0][price_data][recurring][interval]": "month",
        "line_items[0][price_data][product_data][name]": title[:120],
        "metadata[area_id]": str(area_id), "metadata[publisher_area_id]": str(publisher_area_id), "metadata[key]": key,
        "subscription_data[metadata][area_id]": str(area_id), "subscription_data[metadata][publisher_area_id]": str(publisher_area_id),
        "subscription_data[metadata][key]": key,
    }
    if trial_days:
        data["subscription_data[trial_period_days]"] = str(int(trial_days))
    if existing and existing.get("stripe_customer"):
        data["customer"] = existing["stripe_customer"]
    elif email:
        data["customer_email"] = email
    session = await _stripe("POST", "/checkout/sessions", data)
    db.upsert_payment(area_id, publisher_area_id, key, checkout_session=str(session.get("id") or ""), status="pending",
                      price_cents=int(price_cents), currency=cfg["currency"])
    return str(session.get("url") or "")


async def create_portal(area_id: int, base_url: str) -> str:
    """A Stripe customer-portal link for the subscriber (cancel, invoices, card)."""
    mine = [p for p in db.list_payments(area_id) if p.get("stripe_customer")]
    if not mine:
        raise RuntimeError("No billing account yet")
    session = await _stripe("POST", "/billing_portal/sessions", {"customer": mine[0]["stripe_customer"], "return_url": f"{base_url}/#/subscriptions"})
    return str(session.get("url") or "")


# ----------------------------------------------------------------- webhook
def verify_signature(payload: bytes, header: str, secret: str, *, now: Optional[float] = None) -> bool:
    """Stripe's ``Stripe-Signature`` scheme: ``t=<ts>,v1=<hmac>`` over ``<ts>.<payload>``."""
    if not secret or not header:
        return False
    parts = dict(p.split("=", 1) for p in header.split(",") if "=" in p)
    ts, sig = parts.get("t"), [v for k, v in (p.split("=", 1) for p in header.split(",") if "=" in p) if k == "v1"]
    if not ts or not sig:
        return False
    try:
        if abs((now or time.time()) - int(ts)) > SIGNATURE_TOLERANCE_S:
            return False
    except ValueError:
        return False
    expected = hmac.new(secret.encode(), f"{ts}.".encode() + payload, hashlib.sha256).hexdigest()
    return any(hmac.compare_digest(expected, s.strip()) for s in sig)


def _ref(obj: dict[str, Any]) -> Optional[tuple[int, int, str]]:
    """(area, publisher area, key) from a Checkout session or Stripe subscription."""
    meta = obj.get("metadata") or {}
    try:
        if meta.get("area_id") and meta.get("publisher_area_id") and meta.get("key"):
            return int(meta["area_id"]), int(meta["publisher_area_id"]), str(meta["key"])
        ref = str(obj.get("client_reference_id") or "")
        a, pa, key = ref.split(":", 2)
        return int(a), int(pa), key
    except (TypeError, ValueError, AttributeError):
        return None


async def handle_event(event: dict[str, Any]) -> str:
    """Apply one verified Stripe event. Returns what was done (for the log)."""
    kind = str(event.get("type") or "")
    obj = (event.get("data") or {}).get("object") or {}
    if kind == "checkout.session.completed":
        ref = _ref(obj)
        if not ref:
            return "checkout without reference ignored"
        area, pa, key = ref
        p = db.upsert_payment(area, pa, key, checkout_session=str(obj.get("id") or ""), stripe_customer=str(obj.get("customer") or ""),
                              stripe_subscription=str(obj.get("subscription") or ""), status="active")
        await _apply(p)
        return f"checkout completed for {key}"
    if kind in ("customer.subscription.created", "customer.subscription.updated", "customer.subscription.deleted"):
        sid = str(obj.get("id") or "")
        p = db.payment_by("stripe_subscription", sid) if sid else None
        if p is None:
            ref = _ref(obj)
            if not ref:
                return "subscription without reference ignored"
            p = db.upsert_payment(*ref, stripe_subscription=sid, stripe_customer=str(obj.get("customer") or ""))
        status = "canceled" if kind.endswith("deleted") else str(obj.get("status") or "active")
        if status not in db.payments.STATUSES:
            status = "unpaid" if status in ("incomplete", "incomplete_expired", "paused") else "active"
        p = db.update_payment(p["id"], status=status, stripe_customer=str(obj.get("customer") or p.get("stripe_customer") or ""),
                              current_period_end=_iso(obj.get("current_period_end")), trial_end=_iso(obj.get("trial_end")))
        await _apply(p)
        return f"subscription {sid}: {status}"
    if kind == "invoice.payment_failed":
        sid = str(obj.get("subscription") or "")
        p = db.payment_by("stripe_subscription", sid) if sid else None
        if p is None:
            return "invoice for unknown subscription ignored"
        p = db.update_payment(p["id"], status="past_due")
        await _apply(p)
        return f"payment failed for {sid}"
    return f"{kind} ignored"


async def _apply(p: Optional[dict[str, Any]]) -> None:
    """Mirror the payment state onto the marketplace subscription: paid →
    active (or pending when the publisher approves by hand), else unpaid."""
    if not p:
        return
    from . import context, copy as cp, marketplace
    key = p["webhook_id"]
    sub = next((s for s in db.list_subscriptions(p["area_id"]) if s["publisher_area_id"] == p["publisher_area_id"] and s["webhook_id"] == key), None)
    if not sub:
        return
    paid = p["status"] in db.payments.PAID
    if paid:
        if sub["status"] != "unpaid":
            return
        if key.startswith("copy:"):
            _g, sh = cp.find_published(p["publisher_area_id"], key[5:])
        else:
            _w, sh = marketplace.find_published(p["publisher_area_id"], key)
        target = "pending" if (sh or {}).get("approval") else "active"
    else:
        if sub["status"] == "unpaid":
            return
        target = "unpaid"
    db.set_subscription_status(sub["id"], p["publisher_area_id"], target)
    with context.use_area(p["area_id"]):
        state.log_event("info", f"Subscription {key}: payment {p['status']} → {target}")
    if key.startswith("copy:"):
        try:
            await cp.sync_area(p["publisher_area_id"])
        except Exception as exc:  # noqa: BLE001
            log.warning("copy sync after payment change failed: %s", exc)

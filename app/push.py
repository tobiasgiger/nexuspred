"""Web Push notifications (VAPID) to the installed dashboard app.

Works on desktop browsers and on iPhone/iPad (iOS 16.4+) once the dashboard is
added to the home screen. Each device registers a push subscription from the
Alerts settings page; the bridge signs every push with its own VAPID key pair
(generated once, stored encrypted in the ``meta`` table) and delivers through
the browser vendor's push service. Payloads are encrypted end-to-end by
``pywebpush``; the push service never sees the text.

The push channel plugs into :mod:`app.alerts` like Discord and email: the same
per-trigger switches decide *what* is sent, ``alert_push_enabled`` decides
whether the area's devices get it at all.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
from typing import Any, Optional

from cryptography.hazmat.primitives import serialization

from . import config, context, crypto, db, state

log = logging.getLogger(__name__)

_vapid: Any = None  # py_vapid.Vapid, cached after first use
_public_b64: str = ""


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def available() -> bool:
    try:
        import pywebpush  # noqa: F401
        import py_vapid  # noqa: F401
        return True
    except ImportError:
        return False


def _load() -> Any:
    """The VAPID key pair (created on first call, kept encrypted in ``meta``)."""
    global _vapid, _public_b64
    if _vapid is not None:
        return _vapid
    from py_vapid import Vapid
    stored = db.meta_get("vapid_private_pem")
    if stored:
        pem = crypto.decrypt(stored) or ""
        try:
            _vapid = Vapid.from_pem(pem.encode("utf-8")) if pem else None
        except Exception:  # noqa: BLE001 - corrupt / foreign key → regenerate
            _vapid = None
    if _vapid is None:
        v = Vapid()
        v.generate_keys()
        db.meta_set("vapid_private_pem", crypto.encrypt(v.private_pem().decode("utf-8")))
        _vapid = v
    elif stored and not crypto.is_current(stored):
        # Readable only via a legacy key → re-store under the current key so a
        # future key change (with PREVIOUS set) can't strand every subscription.
        try:
            db.meta_set("vapid_private_pem", crypto.encrypt(_vapid.private_pem().decode("utf-8")))
        except Exception:  # noqa: BLE001
            pass
    pub = _vapid.public_key.public_bytes(serialization.Encoding.X962,
                                        serialization.PublicFormat.UncompressedPoint)
    _public_b64 = _b64url(pub)
    return _vapid


def public_key() -> str:
    """The URL-safe base64 public key the browser needs for ``subscribe()``."""
    _load()
    return _public_b64


def diagnose(area_id: int) -> dict[str, Any]:
    """Admin diagnostics: the server's push identity and a live send to every
    device WITHOUT pruning, so the raw push-service answer is visible."""
    import hashlib
    if not available():
        return {"available": False, "error": "pywebpush is not installed on the server"}
    _load()
    stored = db.meta_get("vapid_private_pem") or ""
    info: dict[str, Any] = {
        "available": True,
        "public_key": _public_b64,
        "public_key_fp": hashlib.sha256(_public_b64.encode()).hexdigest()[:12],
        "vapid_key_encrypted": bool(stored) and crypto.is_encrypted(stored),
        "vapid_key_decrypts": bool(stored) and bool(crypto.decrypt(stored)),
        "vapid_key_current": bool(stored) and crypto.is_current(stored),
        "crypto_source": crypto.key_source(),
        "claims_sub": _claims().get("sub"),
        "devices": [],
    }
    payload = {"title": "Fluxbridge diagnostic", "body": "diagnostic ping", "url": "/#/settings/alerts", "tag": "diag"}
    for sub in db.list_push_subscriptions(area_id):
        ok, status, err = _send_one(sub, payload)
        info["devices"].append({"device": sub.get("device") or sub["id"], "host": sub["endpoint"].split("//", 1)[-1].split("/", 1)[0],
                                "ok": ok, "status": status, "error": err})
    return info


def reset() -> None:
    global _vapid, _public_b64
    _vapid = None
    _public_b64 = ""


def _claims() -> dict[str, Any]:
    """VAPID JWT claims. ``exp`` is set explicitly to 12 h: py_vapid's default is
    exactly 24 h, which is Apple's hard maximum — with any clock skew Apple's push
    service answers 403 BadJwtToken and every iPhone device "fails"."""
    import time
    origin = config.PUBLIC_URL or "https://localhost"
    host = origin.split("//", 1)[-1].split("/", 1)[0] or "localhost"
    return {"sub": f"mailto:admin@{host}", "exp": int(time.time()) + 12 * 3600}


def _send_one(sub: dict[str, Any], payload: dict[str, Any]) -> tuple[bool, int, str]:
    """Deliver one push (blocking). Returns (ok, status, error)."""
    from pywebpush import WebPushException, webpush
    info = {"endpoint": sub["endpoint"], "keys": {"p256dh": sub["p256dh"], "auth": sub["auth"]}}
    try:
        resp = webpush(subscription_info=info, data=json.dumps(payload), vapid_private_key=_load(),
                       vapid_claims=dict(_claims()), ttl=600, timeout=15)
        status = getattr(resp, "status_code", 201)
        return status < 300, status, "" if status < 300 else getattr(resp, "text", "")[:200]
    except WebPushException as exc:
        resp = getattr(exc, "response", None)
        status = getattr(resp, "status_code", 0) or 0
        body = (getattr(resp, "text", "") or "").strip().replace("\n", " ")
        return False, status, f"{status or 'error'}: {body or str(exc)}"[:300]
    except Exception as exc:  # noqa: BLE001
        return False, 0, f"{type(exc).__name__}: {exc}"[:200]


def _deliver_sync(area_id: int, payload: dict[str, Any], only_ids: Optional[list[int]] = None) -> dict[str, Any]:
    subs = db.list_push_subscriptions(area_id)
    if only_ids is not None:
        subs = [s for s in subs if s["id"] in only_ids]
    sent = gone = failed = 0
    for sub in subs:
        ok, status, err = _send_one(sub, payload)
        low = err.lower()
        stale = status in (404, 410) or (status == 403 and ("badjwttoken" in low or "vapidpkhash" in low or "mismatch" in low))
        if ok:
            sent += 1
            db.touch_push_subscription(sub["id"], ok=True)
        elif stale:
            gone += 1
            db.delete_push_subscription(area_id, sub["id"])  # unsubscribed / app removed / bound to a rotated key
            try:
                with context.use_area(area_id):
                    state.log_event("info", f"Push device '{sub.get('device') or sub['id']}' removed (stale subscription) — re-enable it on the device to restore alerts")
            except Exception:  # noqa: BLE001
                pass
        else:
            failed += 1
            db.touch_push_subscription(sub["id"], ok=False, error=err)
            try:
                with context.use_area(area_id):
                    state.log_event("warn", f"Push to '{sub.get('device') or sub['id']}' failed: {err}")
            except Exception:  # noqa: BLE001
                pass
    return {"sent": sent, "gone": gone, "failed": failed, "devices": len(subs)}


async def send(area_id: int, title: str, body: str, *, url: str = "/", tag: str = "",
               only_ids: Optional[list[int]] = None) -> dict[str, Any]:
    """Push ``title`` / ``body`` to every device of an area (in a worker thread)."""
    if not available():
        return {"sent": 0, "gone": 0, "failed": 0, "devices": 0, "error": "pywebpush not installed"}
    payload = {"title": title[:80], "body": body[:400], "url": url, "tag": tag or "fluxbridge"}
    return await asyncio.to_thread(_deliver_sync, area_id, payload, only_ids)


async def send_current_area(title: str, body: str, **kw: Any) -> dict[str, Any]:
    return await send(context.get_area(), title, body, **kw)


def strip_markdown(text: str) -> str:
    """Discord-flavoured alert text → plain notification text."""
    return text.replace("**", "").replace("`", "").strip()

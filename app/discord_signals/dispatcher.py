"""Fan a parsed signal out to a channel's configured webhook targets.

* **Bridge webhooks run in-process.** A target that references one of the
  bridge's own webhooks (``webhook_id``) is handed straight to the signal
  engine, with the same acceptance semantics as a TradingView POST — 403 when
  the webhook is missing or disabled, otherwise the signal is logged as
  "received" and executed in the background (reported as 202) — but without
  v4's loopback HTTP round-trip through ``127.0.0.1:$PORT``.
* **Custom URLs are POSTed** through the shared keep-alive client
  (:mod:`app.http`). All targets go out concurrently; each has its own
  timeout and try/except, so one slow or dead target never delays the others.
  A target's secret, if set, is sent as ``X-Webhook-Secret``.
* **Dry-run.** When dry-run is active the pipeline skips dispatch entirely;
  this module never sends in that case.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

from .. import config, http, signals, state

_TIMEOUT_SECONDS = 5.0


def _ms(started: float) -> float:
    return round((time.monotonic() - started) * 1000, 1)


def _dispatch_local(target: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    """Accept the payload on one of this area's own webhooks (no HTTP)."""
    label = target.get("label") or "webhook"
    started = time.monotonic()
    wid = target.get("webhook_id")
    wh = next((w for w in (config.load_settings().get("webhooks") or []) if w.get("id") == wid), None)
    if not wh or not wh.get("enabled"):
        return {"label": label, "url": "", "ok": False, "status": 403, "error": "HTTP 403", "ms": _ms(started)}
    signals.accept(dict(payload), wh)
    return {"label": label, "url": "", "ok": True, "status": 202, "ms": _ms(started)}


async def _post_one(target: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    """Deliver the payload to a single target; return a per-target result record."""
    if target.get("webhook_id"):
        return _dispatch_local(target, payload)

    label = target.get("label") or target.get("url") or "target"
    url = target.get("url") or ""
    started = time.monotonic()
    if not url:
        return {"label": label, "url": url, "ok": False, "error": "no url", "ms": 0}

    headers = {"Content-Type": "application/json"}
    secret = target.get("secret")
    if secret:
        headers["X-Webhook-Secret"] = secret

    try:
        resp = await http.client("outbound").post(
            url, json=payload, headers=headers, timeout=_TIMEOUT_SECONDS)
        ok = resp.status_code < 400
        result = {"label": label, "url": url, "ok": ok, "status": resp.status_code, "ms": _ms(started)}
        if not ok:
            result["error"] = f"HTTP {resp.status_code}"
        return result
    except Exception as exc:  # noqa: BLE001 - one target failing must not affect others
        return {"label": label, "url": url, "ok": False, "error": str(exc), "ms": _ms(started)}


async def dispatch(targets: list[dict[str, Any]], payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Deliver ``payload`` to every ENABLED target concurrently.

    Disabled targets are skipped entirely (they get no request). Returns a list
    of per-target result records (label, ok, status/error, latency ms).
    """
    active = [t for t in targets if t.get("enabled") and (t.get("url") or t.get("webhook_id"))]
    if not active:
        return []
    results = await asyncio.gather(
        *(_post_one(t, payload) for t in active), return_exceptions=True
    )
    out: list[dict[str, Any]] = []
    for t, r in zip(active, results):
        if isinstance(r, Exception):
            out.append({
                "label": t.get("label") or t.get("url"), "url": t.get("url", ""),
                "ok": False, "error": str(r), "ms": 0,
            })
        else:
            out.append(r)
    return out


def log_dispatch_summary(channel_label: str, results: list[dict[str, Any]]) -> None:
    """Record a concise summary of a fan-out into the bridge's event log."""
    if not results:
        return
    ok = sum(1 for r in results if r.get("ok"))
    failed = [r for r in results if not r.get("ok")]
    if failed:
        detail = ", ".join(f"{r.get('label')}: {r.get('error')}" for r in failed)
        state.log_event(
            "warn",
            f"[discord] {channel_label}: {ok}/{len(results)} webhook targets ok "
            f"— failed: {detail}",
        )
    else:
        state.log_event(
            "info", f"[discord] {channel_label}: {ok}/{len(results)} webhook targets ok"
        )

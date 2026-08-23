"""Fan a parsed signal out to a channel's configured webhook targets.

Design points (from the spec):

* **Own HTTP client.** The dispatcher owns a module-level ``httpx.AsyncClient``
  whose lifecycle is independent of the Discord client. If the Discord gateway
  reconnects or drops, the dispatcher can still POST (e.g. events replayed from
  cache), and vice-versa.
* **Parallel.** All enabled targets for a channel are POSTed concurrently with
  ``asyncio.gather`` — one slow or dead target never delays the others.
* **Per-target isolation.** Each target has its own timeout and its own
  try/except; a failure is recorded against that target only.
* **Secret header.** A target's secret, if set, is sent as ``X-Webhook-Secret``.
* **Dry-run.** When dry-run is active the caller skips dispatch entirely; the
  event is still recorded/displayed. This module never sends in that case.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Optional

import httpx

from .. import state

_TIMEOUT_SECONDS = 5.0

_client: Optional[httpx.AsyncClient] = None
_client_lock = asyncio.Lock()


async def _get_client() -> httpx.AsyncClient:
    """Lazily create the shared client. Independent of the Discord client."""
    global _client
    if _client is None or _client.is_closed:
        async with _client_lock:
            if _client is None or _client.is_closed:
                _client = httpx.AsyncClient(timeout=_TIMEOUT_SECONDS)
    return _client


async def aclose() -> None:
    """Close the shared client (called on app shutdown)."""
    global _client
    if _client is not None and not _client.is_closed:
        try:
            await _client.aclose()
        except Exception:  # noqa: BLE001
            pass
    _client = None


async def _post_one(target: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    """POST the payload to a single target; return a per-target result record."""
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
        client = await _get_client()
        resp = await client.post(url, json=payload, headers=headers, timeout=_TIMEOUT_SECONDS)
        ms = round((time.monotonic() - started) * 1000, 1)
        ok = resp.status_code < 400
        result = {
            "label": label, "url": url, "ok": ok,
            "status": resp.status_code, "ms": ms,
        }
        if not ok:
            result["error"] = f"HTTP {resp.status_code}"
        return result
    except Exception as exc:  # noqa: BLE001 - one target failing must not affect others
        ms = round((time.monotonic() - started) * 1000, 1)
        return {"label": label, "url": url, "ok": False, "error": str(exc), "ms": ms}


async def dispatch(targets: list[dict[str, Any]], payload: dict[str, Any]) -> list[dict[str, Any]]:
    """POST ``payload`` to every ENABLED target concurrently.

    Disabled targets are skipped entirely (they get no request). Returns a list
    of per-target result records (label, ok, status/error, latency ms).
    """
    active = [t for t in targets if t.get("enabled") and t.get("url")]
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

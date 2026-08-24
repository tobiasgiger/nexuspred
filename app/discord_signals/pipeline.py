"""The signal-processing pipeline: embed -> parse -> dispatch -> record.

This is the seam between the Discord listener and the webhook dispatcher. It is
deliberately free of any ``discord`` import so it can be driven both by the live
listener and by the test/simulation endpoint, and so importing it can never fail
just because ``discord.py-self`` isn't installed.

Config is read *live* here (per event) so channel/target/dry-run changes made in
the dashboard take effect without restarting the process.
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from typing import Any, Optional

from .. import config, state
from . import dispatcher, hub
from .parser import EmbedLike, parse_embed


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _find_webhook(webhook_id: str) -> Optional[dict[str, Any]]:
    for wh in config.load_settings().get("webhooks", []):
        if wh.get("id") == webhook_id:
            return wh
    return None


def resolve_target(t: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Resolve a channel target to an effective {label, url, secret} for dispatch.

    A target is either a reference to one of the bridge's own webhooks
    (``webhook_id`` → posted to that webhook's local URL; the path token is the
    auth, so no secret is needed) or a custom external ``url`` (+ optional
    secret). Returns ``None`` if it can't be resolved (e.g. the referenced
    webhook was deleted).
    """
    label = (t.get("label") or "").strip()
    wid = t.get("webhook_id")
    if wid:
        wh = _find_webhook(wid)
        if not wh or not wh.get("token"):
            return None
        port = os.environ.get("PORT", "9000")
        url = f"http://127.0.0.1:{port}/webhook/{wh.get('token')}"
        return {"label": label or wh.get("name") or "webhook", "url": url, "secret": ""}
    url = (t.get("url") or "").strip()
    if url:
        return {"label": label or url, "url": url, "secret": t.get("secret") or ""}
    return None


def _webhook_action(sig: dict[str, Any]) -> Optional[str]:
    """Map a parsed Discord signal to a bridge webhook action, or None."""
    et = sig.get("event_type")
    side = (sig.get("side") or "").lower()
    if et == "entry":
        return "buy" if side == "long" else "sell" if side == "short" else None
    if et == "close":
        return "close_all"
    if et == "sl_tp_update":
        return "move_sl"
    return None


def build_trade_payload(sig: dict[str, Any], *, received_at: str, source: str) -> dict[str, Any]:
    """Translate a parsed Discord signal into a webhook (TradingView-style) payload.

    The bridge's own webhooks expect ``action`` + ``symbol`` (+ qty / entry / sl /
    tp), while a Discord signal is shaped as ``event_type`` / ``side`` / prices.
    This overlays the executable fields so a routed webhook can act on it, while
    keeping the original signal fields for any external/custom target:

      * entry  → ``buy`` / ``sell`` (qty from ``contracts``; entry/sl/tp if present)
      * close  → ``close_all`` (flatten the symbol)
      * SL/TP move → ``move_sl`` (new stop from ``stop_price``; bracket webhooks)
    """
    p: dict[str, Any] = {**sig, "received_at": received_at, "source": source}
    action = _webhook_action(sig)
    if not action:
        return p
    p["action"] = action
    p["symbol"] = sig.get("symbol") or ""
    if action in ("buy", "sell"):
        if sig.get("contracts") is not None:
            p["qty"] = sig["contracts"]
        if sig.get("entry_price") is not None:
            p["entry"] = sig["entry_price"]
        if sig.get("stop_price") is not None:
            p["sl"] = sig["stop_price"]
        if sig.get("target_price") is not None:
            p["tp1"] = sig["target_price"]
    elif action == "move_sl":
        if sig.get("stop_price") is not None:
            p["new_sl"] = sig["stop_price"]
    return p


def find_channel(channel_id: str) -> Optional[dict[str, Any]]:
    """Return the live config for a channel id (string compare), or None."""
    cid = str(channel_id)
    for c in config.load_settings().get("discord_channels") or []:
        if str(c.get("id")) == cid:
            return c
    return None


def watched_channel_ids() -> set[str]:
    """The set of channel ids we currently care about (enabled channels)."""
    return {
        str(c.get("id"))
        for c in (config.load_settings().get("discord_channels") or [])
        if c.get("enabled") and c.get("id")
    }


async def process_embed(
    embed: EmbedLike,
    channel_id: str,
    *,
    source: str = "message",
    received_monotonic: Optional[float] = None,
    force: bool = False,
) -> Optional[dict[str, Any]]:
    """Run one embed through the pipeline.

    ``source`` is a label for the dashboard ("message" | "edit" | "test").
    ``received_monotonic`` is a ``time.monotonic()`` reading taken as close to
    reception as possible, used for the latency measurement. ``force`` bypasses
    the channel enabled-check (used by the test endpoint).

    Returns the recorded event dict, or ``None`` if the channel isn't watched.
    """
    if received_monotonic is None:
        received_monotonic = time.monotonic()

    settings = config.load_settings()
    channel = find_channel(channel_id)
    channel_enabled = bool(channel and channel.get("enabled"))
    if not force and not channel_enabled:
        return None  # not a channel we watch — ignore silently (no noise)

    channel_label = (channel or {}).get("label") or f"channel {channel_id}"
    dry_run = bool(settings.get("discord_dry_run"))

    signal = parse_embed(embed, int(channel_id) if str(channel_id).isdigit() else 0)

    event: dict[str, Any] = {
        "ts": _now_iso(),
        "source": source,
        "channel_id": str(channel_id),
        "channel_label": channel_label,
        "dry_run": dry_run,
    }

    if signal is None:
        # Unrecognised: surface it loudly so provider format changes are noticed.
        event["kind"] = "unrecognized"
        event["raw"] = {
            "title": embed.title,
            "fields": [{"name": f.name, "value": f.value} for f in embed.fields],
        }
        event["targets"] = []
        state.log_event(
            "warn",
            f"[discord] Unrecognised message in {channel_label}: "
            f"title={embed.title!r}",
        )
        return hub.record(event)

    event["kind"] = "signal"
    event["signal"] = signal.to_dict()

    targets = (channel or {}).get("targets") or []
    # Resolve each enabled target (webhook reference -> local URL, or custom URL).
    active_targets = []
    for t in targets:
        if not t.get("enabled"):
            continue
        resolved = resolve_target(t)
        if resolved:
            active_targets.append({**resolved, "enabled": True})

    payload = build_trade_payload(signal.to_dict(), received_at=event["ts"], source=source)

    if dry_run:
        event["targets"] = [
            {"label": t.get("label") or t.get("url"), "url": t.get("url", ""),
             "ok": None, "skipped": "dry_run"}
            for t in active_targets
        ]
        event["latency_ms"] = round((time.monotonic() - received_monotonic) * 1000, 1)
        state.log_event(
            "info",
            f"[discord] DRY-RUN {channel_label}: {signal.event_type} "
            f"{signal.symbol or ''} — would send to {len(active_targets)} target(s)",
        )
        return hub.record(event)

    # Latency = reception -> the moment we fire the webhook POSTs.
    event["latency_ms"] = round((time.monotonic() - received_monotonic) * 1000, 1)
    results = await dispatcher.dispatch(active_targets, payload)
    event["targets"] = results
    dispatcher.log_dispatch_summary(channel_label, results)
    return hub.record(event)

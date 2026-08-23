"""The signal-processing pipeline: embed -> parse -> dispatch -> record.

This is the seam between the Discord listener and the webhook dispatcher. It is
deliberately free of any ``discord`` import so it can be driven both by the live
listener and by the test/simulation endpoint, and so importing it can never fail
just because ``discord.py-self`` isn't installed.

Config is read *live* here (per event) so channel/target/dry-run changes made in
the dashboard take effect without restarting the process.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Optional

from .. import config, state
from . import dispatcher, hub
from .parser import EmbedLike, parse_embed


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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
    active_targets = [t for t in targets if t.get("enabled") and t.get("url")]

    payload = {**signal.to_dict(), "received_at": event["ts"], "source": source}

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

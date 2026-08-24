"""Parse Discord signal embeds into structured :class:`Signal` objects.

The signal provider posts three kinds of message, distinguished by the embed
title:

1. **Entry** — ``"AkSniper 🎯 · SELL MNQ"`` (or ``BUY``)
   Fields: ``Contracts``, ``Entry``, ``Time``

2. **Stop / target update** — ``"AkSniper 🎯 · Stop / target moved · MNQ"``
   Fields: ``Stop``, ``Target`` (format ``"old → new"``), ``Position``

3. **Trade closed** — ``"Closed MNQ · +90.75 pts"``
   Fields: ``P&L``, ``Move``, ``Exit``, ``Held``

The parser is deliberately defensive: a new or renamed field must never raise —
an embed we don't recognise returns ``None`` so the caller can surface it as
"unrecognised" (visible in the log/dashboard) instead of crashing or silently
dropping it. That way a change to the provider's format is noticed immediately.

To stay decoupled from ``discord.py-self`` (and trivially testable), the parser
works on a normalised :class:`EmbedLike` structure — either build one directly
in a test, or convert a real ``discord.Embed`` with :func:`embed_from_discord`.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any, Optional


@dataclass
class EmbedField:
    """A single embed field (name/value pair)."""

    name: str = ""
    value: str = ""


@dataclass
class EmbedLike:
    """A normalised, framework-agnostic view of a Discord embed."""

    title: str = ""
    description: str = ""
    fields: list[EmbedField] = field(default_factory=list)


@dataclass
class Signal:
    """A parsed trade signal. All price/quantity fields are optional so a
    partial or unusual message still produces *something* rather than crashing."""

    event_type: str                         # "entry" | "sl_tp_update" | "close"
    symbol: Optional[str] = None
    side: Optional[str] = None              # "long" | "short"
    contracts: Optional[float] = None
    entry_price: Optional[float] = None
    stop_price: Optional[float] = None
    target_price: Optional[float] = None
    exit_price: Optional[float] = None
    pnl_usd: Optional[float] = None
    pnl_points: Optional[float] = None
    source_channel_id: Optional[int] = None
    raw_title: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# --------------------------------------------------------------------------- #
# Normalisation helpers
# --------------------------------------------------------------------------- #
def embed_from_discord(embed: Any) -> EmbedLike:
    """Convert a ``discord.Embed``-like object into an :class:`EmbedLike`.

    Tolerant of missing attributes/None so a malformed embed can't raise here.
    """
    title = (getattr(embed, "title", "") or "")
    description = (getattr(embed, "description", "") or "")
    fields: list[EmbedField] = []
    for f in (getattr(embed, "fields", None) or []):
        fields.append(
            EmbedField(
                name=str(getattr(f, "name", "") or ""),
                value=str(getattr(f, "value", "") or ""),
            )
        )
    return EmbedLike(title=str(title), description=str(description), fields=fields)


def embed_from_dict(data: dict[str, Any]) -> EmbedLike:
    """Build an :class:`EmbedLike` from a plain dict (used by the test endpoint).

    Accepts ``fields`` as a list of ``{"name", "value"}`` dicts *or* as a flat
    mapping ``{name: value}``.
    """
    raw_fields = data.get("fields") or []
    fields: list[EmbedField] = []
    if isinstance(raw_fields, dict):
        for name, value in raw_fields.items():
            fields.append(EmbedField(name=str(name), value=str(value)))
    else:
        for f in raw_fields:
            if isinstance(f, dict):
                fields.append(
                    EmbedField(name=str(f.get("name", "")), value=str(f.get("value", "")))
                )
    return EmbedLike(
        title=str(data.get("title", "") or ""),
        description=str(data.get("description", "") or ""),
        fields=fields,
    )


def field_map(embed: EmbedLike) -> dict[str, str]:
    """Lower-cased ``{field name: value}`` map, whitespace-trimmed."""
    out: dict[str, str] = {}
    for f in embed.fields:
        try:
            out[f.name.strip().lower()] = f.value.strip()
        except AttributeError:
            continue
    return out


def to_float(value: Optional[str]) -> Optional[float]:
    """Best-effort numeric parse: strip currency/symbols, keep sign + decimal.

    Normalises the Unicode minus variants (−, –, —) that signal providers often
    use to an ASCII '-' first, so a negative P&L keeps its sign.
    """
    if value is None:
        return None
    text = str(value).replace("−", "-").replace("–", "-").replace("—", "-")
    cleaned = re.sub(r"[^\d.\-]", "", text)
    if not cleaned or cleaned in ("-", ".", "-."):
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
# The parser
# --------------------------------------------------------------------------- #
def parse_embed(embed: EmbedLike, channel_id: int) -> Optional[Signal]:
    """Return a :class:`Signal` for a recognised embed, else ``None``.

    Never raises on unexpected content — an unknown shape is a ``None`` return,
    which the caller records as "unrecognised".
    """
    try:
        title = (embed.title or "").strip()
        fields = field_map(embed)
        title_lower = title.lower()
        # Providers often prefix the title with an emoji (🔴/⚪/🎯). Strip leading
        # non-word characters so title-type detection isn't thrown off by them.
        title_core = re.sub(r"^[\W_]+", "", title_lower)

        # --- 1. Entry -----------------------------------------------------
        if "sell" in title_lower or "buy" in title_lower:
            side = "short" if "sell" in title_lower else "long"
            symbol_match = re.search(r"(SELL|BUY)\s+(\S+)", title, re.IGNORECASE)
            return Signal(
                event_type="entry",
                symbol=symbol_match.group(2) if symbol_match else None,
                side=side,
                contracts=to_float(fields.get("contracts")),
                entry_price=to_float(fields.get("entry")),
                source_channel_id=channel_id,
                raw_title=title,
            )

        # --- 2. Stop / target update -------------------------------------
        if "stop / target moved" in title_lower or "stop/target moved" in title_lower:
            symbol_match = re.search(r"moved\s*[·•]\s*(\S+)", title, re.IGNORECASE)
            target_raw = fields.get("target", "")
            target_new = target_raw.split("→")[-1] if "→" in target_raw else target_raw
            stop_raw = fields.get("stop", "")
            stop_new = stop_raw.split("→")[-1] if "→" in stop_raw else stop_raw
            return Signal(
                event_type="sl_tp_update",
                symbol=symbol_match.group(1) if symbol_match else None,
                stop_price=to_float(stop_new),
                target_price=to_float(target_new),
                source_channel_id=channel_id,
                raw_title=title,
            )

        # --- 3. Trade closed ---------------------------------------------
        # Tolerate a leading emoji (🔴/⚪/…): match "closed" on the stripped title.
        if title_core.startswith("closed"):
            symbol_match = re.search(r"closed\s+(\S+)", title, re.IGNORECASE)
            # P&L points may be a field ("Move") or in the title ("· −83.00 pts").
            pnl_points = to_float(fields.get("move"))
            if pnl_points is None:
                m = re.search(r"([-−–]?\s*\d[\d.,]*)\s*pts", title, re.IGNORECASE)
                if m:
                    pnl_points = to_float(m.group(1))
            return Signal(
                event_type="close",
                symbol=symbol_match.group(1) if symbol_match else None,
                exit_price=to_float(fields.get("exit")),
                pnl_usd=to_float(fields.get("p&l") or fields.get("pnl")),
                pnl_points=pnl_points,
                source_channel_id=channel_id,
                raw_title=title,
            )

        return None  # unrecognised -> surfaced in log/dashboard, never dropped
    except Exception:  # noqa: BLE001 - a parser bug must never crash the listener
        return None

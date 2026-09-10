"""Per-account order sizing for routed signals (webhooks, marketplace
subscriptions) — the same three rules as copy trading:

* ``same``        — the contracts the signal carries, 1:1;
* ``multiplier``  — signal quantity × factor, rounded half up, never below 1;
* ``fixed``       — always this many contracts for the entry; partial
  quantities (bracket take-profit slices) are scaled proportionally.

``max_contracts`` caps the result (0 = no cap). ``qty_multiplier`` on an
account entry is the pre-5.0.0-alpha.44 form and is still honoured: it maps
to ``multiplier`` mode (a factor of 1 is ``same``).
"""
from __future__ import annotations

from typing import Any, Optional

MODES = ("same", "multiplier", "fixed")


def _round_half_up(x: float) -> int:
    return int(x + 0.5) if x >= 0 else -int(-x + 0.5)


def normalize(account: dict[str, Any]) -> dict[str, Any]:
    """The sizing block of a routed-account entry (validating; raises ValueError)."""
    raw = account.get("sizing") if isinstance(account.get("sizing"), dict) else {}
    legacy = account.get("qty_multiplier", 1)
    legacy = 1.0 if legacy in (None, "") else float(legacy)
    mode = str(raw.get("mode") or ("multiplier" if legacy != 1.0 else "same"))
    if mode not in MODES:
        raise ValueError(f"sizing mode must be one of {', '.join(MODES)}")
    mult = raw.get("multiplier", legacy)
    mult = legacy if mult in (None, "") else float(mult)
    fixed = raw.get("fixed", 1)
    fixed = 1 if fixed in (None, "") else int(fixed)
    mx = raw.get("max_contracts", 0)
    mx = 0 if mx in (None, "") else int(mx)
    if not (0.01 <= mult <= 100):
        raise ValueError("multiplier must be between 0.01 and 100")
    if not (1 <= fixed <= 1000):
        raise ValueError("fixed contracts must be between 1 and 1000")
    if not (0 <= mx <= 1000):
        raise ValueError("max contracts must be between 0 and 1000")
    return {"mode": mode, "multiplier": round(mult, 4), "fixed": fixed, "max_contracts": mx}


def effective_multiplier(sizing: dict[str, Any]) -> float:
    """The factor older code paths read (``qty_multiplier``)."""
    return float(sizing.get("multiplier") or 1) if sizing.get("mode") == "multiplier" else 1.0


def account_qty(ex: Any, base_qty: int, *, of_entry: Optional[int] = None) -> int:
    """Contracts one account trades for a signal quantity.

    ``of_entry`` marks a partial quantity (a take-profit slice) and names the
    signal's entry size, so ``fixed`` mode scales the slice proportionally
    (fixed 2 for an entry of 3 → a TP slice of 1 becomes 1, of 3 becomes 2).
    """
    sizing = getattr(ex, "sizing", None)
    if not isinstance(sizing, dict):
        mult = float(getattr(ex, "qty_multiplier", 1) or 1)
        sizing = {"mode": "multiplier" if mult != 1 else "same", "multiplier": mult, "fixed": 1, "max_contracts": 0}
    base = max(0, int(base_qty))
    mode = sizing.get("mode", "same")
    if mode == "fixed":
        fixed = int(sizing.get("fixed") or 1)
        qty = fixed if not of_entry else _round_half_up(fixed * base / int(of_entry))
    elif mode == "multiplier":
        qty = _round_half_up(base * float(sizing.get("multiplier") or 1))
    else:
        qty = base
    qty = max(1, qty)
    mx = int(sizing.get("max_contracts") or 0)
    return min(qty, mx) if mx else qty


def describe(sizing: dict[str, Any]) -> str:
    mode = sizing.get("mode", "same")
    core = "1:1" if mode == "same" else f"× {sizing.get('multiplier')}" if mode == "multiplier" else f"fixed {sizing.get('fixed')}"
    return core + (f", max {sizing['max_contracts']}" if sizing.get("max_contracts") else "")

"""Open positions across every enabled account and what they add up to."""
from __future__ import annotations

import asyncio
from typing import Any

from . import tradovate
from .engine.common import _base_root
from .journal import value_per_point
from .tradovate import TradovateError

CONCENTRATION_SHARE = 0.6            # one symbol holding more than this share of the notional is flagged


async def collect_positions() -> list[dict[str, Any]]:
    """Open positions across every enabled account: one ``/position/list`` per
    login (not per account), logins fetched concurrently. Each row carries the
    login (``lid`` / ``token_idx``) so the dashboard can act on it."""
    async def one(ex: Any) -> list[dict[str, Any]]:
        try:
            rows = await ex.positions()
        except TradovateError:
            return []
        return [_tag(r, ex) for r in rows]

    async def per_login(sess: Any, exs: list[Any]) -> list[dict[str, Any]]:
        try:
            raw = await sess.positions_snapshot()
        except Exception:  # noqa: BLE001 - one login down must not hide the others
            return []
        out: list[dict[str, Any]] = []
        for ex in exs:
            rows = await sess.positions_named(sess.positions_from(raw or [], account_id=ex.id, account_name=ex.name))
            out.extend(_tag(r, ex) for r in rows)
        return out

    groups: dict[int, tuple[Any, list[Any]]] = {}
    singles: list[Any] = []
    for ex in tradovate.manager().enabled():
        sess = getattr(ex, "session", None)
        if sess is not None and getattr(sess, "kind", "tradovate") == "tradovate" and hasattr(sess, "positions_from") and getattr(ex, "id", 0):
            groups.setdefault(id(sess), (sess, []))[1].append(ex)
        else:
            singles.append(ex)
    results = await asyncio.gather(*[per_login(s, exs) for s, exs in groups.values()], *(one(ex) for ex in singles))
    return [p for chunk in results for p in chunk]


def _tag(row: dict[str, Any], ex: Any) -> dict[str, Any]:
    sess = getattr(ex, "session", None)
    row["lid"] = getattr(sess, "lid", "") or ""
    row["token_idx"] = getattr(sess, "idx", None)
    row["spec"] = getattr(ex, "spec", "") or row.get("account") or ""
    row["environment"] = getattr(sess, "environment", "") or ""
    return row


def _num(v: Any) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return 0.0
    return f if f == f and f not in (float("inf"), float("-inf")) else 0.0


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Per symbol root and per account: contracts long / short / net and the
    notional at the average entry price (contracts × price × value per point).
    Flags a root that is long on one account and short on another (hedged
    across accounts, usually a mistake) and a root that dominates the book."""
    by_symbol: dict[str, dict[str, Any]] = {}
    by_account: dict[str, dict[str, Any]] = {}
    total = 0.0
    for r in rows:
        symbol = str(r.get("symbol") or "")
        root = _base_root(symbol.upper()) if symbol else "?"
        net = _num(r.get("netPos"))
        if not net:
            continue
        price = _num(r.get("netPrice"))
        vpp = value_per_point(root)
        notional = abs(net) * price * vpp
        total += notional
        acct = str(r.get("account") or r.get("spec") or "")
        s = by_symbol.setdefault(root, {"root": root, "long": 0.0, "short": 0.0, "net": 0.0, "notional": 0.0, "accounts": 0,
                                        "long_accounts": [], "short_accounts": [], "value_per_point": vpp, "contracts": []})
        s["long" if net > 0 else "short"] += abs(net)
        s["net"] += net
        s["notional"] += notional
        s["accounts"] += 1
        (s["long_accounts"] if net > 0 else s["short_accounts"]).append(acct)
        if symbol and symbol not in s["contracts"]:
            s["contracts"].append(symbol)
        a = by_account.setdefault(acct, {"account": acct, "environment": r.get("environment") or "", "contracts": 0.0, "notional": 0.0, "symbols": []})
        a["contracts"] += abs(net)
        a["notional"] += notional
        if root not in a["symbols"]:
            a["symbols"].append(root)
    warnings: list[dict[str, Any]] = []
    for s in by_symbol.values():
        s["share"] = round(s["notional"] / total, 4) if total else 0.0
        s["notional"] = round(s["notional"], 2)
        if s["long_accounts"] and s["short_accounts"]:
            warnings.append({"kind": "hedged", "root": s["root"],
                             "detail": f"long on {', '.join(s['long_accounts'])}, short on {', '.join(s['short_accounts'])}"})
        if len(by_symbol) > 1 and s["share"] > CONCENTRATION_SHARE:
            warnings.append({"kind": "concentration", "root": s["root"], "detail": f"{round(s['share'] * 100)}% of the notional"})
    for a in by_account.values():
        a["share"] = round(a["notional"] / total, 4) if total else 0.0
        a["notional"] = round(a["notional"], 2)
    symbols = sorted(by_symbol.values(), key=lambda x: -x["notional"])
    accounts = sorted(by_account.values(), key=lambda x: -x["notional"])
    return {"symbols": symbols, "accounts": accounts, "total_notional": round(total, 2),
            "contracts": round(sum(abs(_num(r.get("netPos"))) for r in rows), 2), "warnings": warnings}

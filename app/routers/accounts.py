"""Tradovate logins (token accounts) and the per-account execution toggles."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from .. import config, context, db, state, tradovate

router = APIRouter(prefix="/api", tags=["accounts"])


def trade_accounts_overview() -> list[dict[str, Any]]:
    """Flat list of every trade account across all logins, with execution toggle
    and live connection status — powers the Trade Accounts overview."""
    out: list[dict[str, Any]] = []
    for idx, t in enumerate(config.load_settings().get("token_accounts") or []):
        tname = t.get("name") or f"account {idx + 1}"
        env = t.get("environment") or "demo"
        tconn = bool(state.session_status(tname).get("connected"))
        accts = t.get("accounts") or []
        if not accts and (t.get("account_spec") or t.get("account_id")):
            accts = [{"spec": t.get("account_spec", ""), "id": t.get("account_id", 0),
                      "enabled": True, "qty_multiplier": t.get("qty_multiplier", 1)}]
        for a in accts:
            out.append({
                "token_idx": idx, "token_name": tname, "environment": env,
                "token_enabled": bool(t.get("enabled")), "connected": tconn,
                "agent_id": int(t.get("agent_id") or 0),
                "spec": a.get("spec") or a.get("account_spec") or "",
                "id": a.get("id") or a.get("account_id") or 0,
                "enabled": bool(a.get("enabled", True)),
                "qty_multiplier": float(a.get("qty_multiplier", t.get("qty_multiplier", 1)) or 1),
            })
    return out


# =============================================================== Token accounts
@router.get("/token-accounts")
async def api_token_accounts() -> list[dict[str, Any]]:
    return config.public_settings().get("token_accounts", [])


@router.post("/token-accounts")
async def api_save_token_accounts(request: Request) -> list[dict[str, Any]]:
    """Save the per-account token list. Masked tokens ('********') keep the stored
    value, so editing other fields doesn't wipe the tokens."""
    incoming = await request.json()
    existing = config.load_settings().get("token_accounts") or []
    cleaned: list[dict[str, Any]] = []
    for i, a in enumerate(incoming):
        prev = existing[i] if i < len(existing) else {}
        access = a.get("access_token", "")
        md = a.get("md_token", "")
        cleaned.append({
            "name": (a.get("name") or f"account {i + 1}").strip(),
            "environment": "live" if a.get("environment") == "live" else "demo",
            "access_token": prev.get("access_token", "") if access == "********" else access.strip(),
            "md_token": prev.get("md_token", "") if md == "********" else md.strip(),
            "enabled": bool(a.get("enabled")),
            "qty_multiplier": float(a.get("qty_multiplier", 1) or 1),
            "account_spec": a.get("account_spec") or prev.get("account_spec", ""),
            "account_id": a.get("account_id") or prev.get("account_id", 0),
            "token_expires": prev.get("token_expires", ""),
            "agent_id": _own_agent(a.get("agent_id")),
            "accounts": prev.get("accounts") or [],
        })
    config.save_settings({"token_accounts": cleaned})
    tradovate.manager().reload()
    enabled = sum(1 for a in cleaned if a["enabled"])
    state.log_event("info", f"Token accounts updated — {enabled}/{len(cleaned)} enabled")
    return config.public_settings().get("token_accounts", [])


def _own_agent(value: Any) -> int:
    """An execution agent id is only accepted when the agent is paired with *this*
    workspace — otherwise a user could route their orders (and Tradovate tokens)
    through another tenant's VPS."""
    try:
        agent_id = int(value or 0)
    except (TypeError, ValueError):
        agent_id = 0
    if agent_id <= 0:
        return 0
    if not db.get_agent(context.get_area(), agent_id):
        raise HTTPException(status_code=400, detail=f"Execution agent #{agent_id} is not paired with this workspace")
    return agent_id


# =============================================================== Trade accounts
@router.get("/trade-accounts")
async def api_trade_accounts() -> list[dict[str, Any]]:
    """Overview of every trade account under every login, with on/off toggles."""
    return trade_accounts_overview()


@router.post("/trade-accounts")
async def api_save_trade_accounts(request: Request) -> list[dict[str, Any]]:
    """Save per-account execution toggles & qty multipliers (keyed by login + spec)."""
    incoming = await request.json()
    tokens = list(config.load_settings().get("token_accounts") or [])
    by_token: dict[int, dict[str, Any]] = {}
    for item in incoming:
        try:
            idx = int(item.get("token_idx"))
        except (TypeError, ValueError):
            continue
        by_token.setdefault(idx, {})[item.get("spec", "")] = item

    for idx, updates in by_token.items():
        if not (0 <= idx < len(tokens)):
            continue
        t = dict(tokens[idx])
        existing = {(a.get("spec") or a.get("account_spec") or ""): dict(a)
                    for a in (t.get("accounts") or [])}
        for spec, u in updates.items():
            a = existing.get(spec, {"spec": spec, "id": u.get("id", 0)})
            a["spec"] = spec
            a["enabled"] = bool(u.get("enabled"))
            a["qty_multiplier"] = float(u.get("qty_multiplier", 1) or 1)
            if u.get("id"):
                a["id"] = u["id"]
            existing[spec] = a
        t["accounts"] = list(existing.values())
        tokens[idx] = t

    config.save_settings({"token_accounts": tokens})
    tradovate.manager().reload()
    enabled = sum(1 for a in trade_accounts_overview() if a["enabled"])
    state.log_event("info", f"Trade-account toggles updated — {enabled} enabled for execution")
    return trade_accounts_overview()

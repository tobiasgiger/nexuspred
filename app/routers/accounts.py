"""Tradovate logins (token accounts) and the per-account execution toggles."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from .. import config, context, db, risk, state, tradovate

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
                "token_idx": idx, "lid": t.get("lid") or "", "token_name": tname, "environment": env,
                "token_enabled": bool(t.get("enabled")), "connected": tconn,
                "agent_id": int(t.get("agent_id") or 0),
                "spec": a.get("spec") or a.get("account_spec") or "",
                "id": a.get("id") or a.get("account_id") or 0,
                "enabled": bool(a.get("enabled", True)),
                "qty_multiplier": float(a.get("qty_multiplier", t.get("qty_multiplier", 1)) or 1),
                "risk": dict(a.get("risk") or {}),
                "locked": risk.lock_of(context.get_area(), a.get("spec") or a.get("account_spec") or ""),
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
    by_lid = {t.get("lid"): t for t in existing if t.get("lid")}
    claimed = {a.get("lid") for a in incoming if isinstance(a, dict) and a.get("lid")}
    cleaned: list[dict[str, Any]] = []
    for i, a in enumerate(incoming):
        # A row names the login it edits by its stable id; a row without one is
        # new — unless it comes from a client that never sent ids, in which case
        # the old position match applies, but never onto a login another row claims.
        if a.get("lid") and a["lid"] in by_lid:
            prev = by_lid[a["lid"]]
        elif not a.get("lid") and i < len(existing) and existing[i].get("lid") not in claimed:
            prev = existing[i]
        else:
            prev = {}
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
            "lid": prev.get("lid") or config._new_lid(),
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
    current = config.load_settings()
    for item in incoming:
        idx = config.login_index(current, item.get("lid")) if isinstance(item, dict) else None
        if idx is None:
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
            if u.get("id") and not a.get("id"):
                a["id"] = u["id"]                 # only for an account Connect & Verify has not seen yet
            if "risk" in u:
                try:
                    a["risk"] = risk.normalize(u["risk"])
                except (TypeError, ValueError) as exc:
                    raise HTTPException(status_code=400, detail=f"{spec}: {exc}") from exc
            existing[spec] = a
        t["accounts"] = list(existing.values())
        tokens[idx] = t

    config.save_settings({"token_accounts": tokens})
    tradovate.manager().reload()
    enabled = sum(1 for a in trade_accounts_overview() if a["enabled"])
    state.log_event("info", f"Trade-account toggles updated — {enabled} enabled for execution")
    return trade_accounts_overview()


# ================================================================= Risk guard
@router.get("/risk")
async def api_risk() -> list[dict[str, Any]]:
    """Every trade account's risk rules and today's lock."""
    return risk.overview(context.get_area())


@router.post("/risk/unlock")
async def api_risk_unlock(request: Request) -> dict[str, Any]:
    """Clear an account's risk lock for today (the rules stay in place)."""
    body = await request.json()
    spec = str(body.get("spec") or "")
    if not spec:
        raise HTTPException(status_code=400, detail="spec required")
    if not risk.unlock(context.get_area(), spec):
        raise HTTPException(status_code=404, detail="Account is not locked")
    state.log_event("warn", f"🔓 Risk guard: {spec} unlocked by user")
    return {"status": "unlocked", "spec": spec}

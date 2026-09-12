"""Settings export / import: the workspace's configuration as one JSON file —
webhooks with their routing, symbol map, trading rules, alert preferences,
news-lock rules — without any secret (broker tokens, passwords, API keys, the
Discord user token, the webhook passphrase, the heartbeat ping URL). The webhook
tokens *do* travel (they are the URLs TradingView already points at) — the file
is a capability to fire signals and must be stored like one. Meant for
backups of the configuration and for moving a workspace to another bridge; the
database backup (Settings → Updates) is the full copy including secrets."""
from __future__ import annotations

import secrets
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from .. import config, context, db, marketplace, news, sizing, state, trade_window
from .core import validate_settings

router = APIRouter(prefix="/api/settings", tags=["settings-io"])

FORMAT = 1
MAX_WEBHOOKS = 200
# keys that travel; everything else (token_accounts, secrets, runtime state,
# copy groups bound to this bridge's login ids) stays behind
EXPORT_KEYS = (
    "default_qty", "tp_qty", "entry_order_type", "tp_order_type", "sl_order_type", "breakeven_to_entry",
    "allowed_symbols", "symbol_map", "webhooks",
    "health_check_interval", "pnl_poll_seconds",
    "journal_auto_import", "journal_import_time", "journal_timezone", "journal_history_days", "journal_fee_per_side",
    "alert_discord_enabled", "alert_discord_mention_everyone", "alert_email_enabled", "alert_email_to",
    "alert_smtp_host", "alert_smtp_port", "alert_smtp_username", "alert_push_enabled", "alert_accounts",
    "alert_on_connection_lost", "alert_on_connection_restored", "alert_on_trade_executed", "alert_on_trade_opened",
    "alert_on_trade_closed", "alert_on_agent_lost", "alert_on_agent_restored", "alert_on_risk", "alert_on_copy",
    "alert_daily_summary", "daily_summary_time", "alert_on_webhook_failed", "alert_on_discord_lost",
    "alert_on_discord_restored", "alert_on_rollover", "rollover_warn_days", "discord_health_grace",
    "news_lock", "ui_language", "heartbeat_interval", "auto_check_updates",
)
# never exported even if listed above by mistake: a ping URL is a capability
SECRET_KEYS = set(config.SECRET_FIELDS) | {"webhook_secret", "discord_user_token", "heartbeat_url"}
_PORTABLE_KEYS = tuple(k for k in EXPORT_KEYS if k in config.DEFAULT_SETTINGS and k not in SECRET_KEYS)


def export_settings(area_id: int) -> dict[str, Any]:
    s = config.load_settings(area_id=area_id)
    out = {k: s[k] for k in _PORTABLE_KEYS if k in s}
    return {"fluxbridge_settings": FORMAT, "exported_at": datetime.now(timezone.utc).isoformat(),
            "version": config.get_version(), "settings": out}


def _import_webhook(w: dict[str, Any], current: dict[str, Any], area_id: int) -> dict[str, Any]:
    """A webhook from the file, normalised like the create/edit endpoints do.
    Routing survives only for logins that exist here (matched by login id);
    a token already used by another workspace is replaced."""
    try:
        wh = config.new_webhook(name=str(w.get("name") or "Imported webhook")[:80],
                                strategy=str(w.get("strategy") or "simple"),
                                default_qty=w.get("default_qty", 1), tp_qty=w.get("tp_qty", 1))
        wid = str(w.get("id") or "")
        if wid.startswith("wh_") and 4 <= len(wid) <= 32 and wid[3:].isalnum():
            wh["id"] = wid
        token = str(w.get("token") or "")
        owner, _ = config.find_webhook(token) if token else (None, None)
        if 16 <= len(token) <= 64 and token.replace("-", "").replace("_", "").isalnum() and owner in (None, area_id):
            wh["token"] = token
        wh["enabled"] = bool(w.get("enabled", True))
        accounts = []
        for a in w.get("accounts") or []:
            if not isinstance(a, dict) or not a.get("spec"):
                continue
            idx = config.login_index(current, str(a.get("lid") or ""))
            if idx is None:
                continue                                  # that login is not on this bridge
            sz = sizing.normalize(a)
            accounts.append({"token_idx": idx, "lid": str(a["lid"]), "spec": str(a["spec"])[:64],
                             "enabled": bool(a.get("enabled")), "qty_multiplier": sizing.effective_multiplier(sz),
                             "sizing": sz})
        wh["accounts"] = accounts
        if isinstance(w.get("sharing"), dict):
            wh["sharing"] = marketplace.normalize_sharing(w["sharing"])
        if w.get("trade_window"):
            wh["trade_window"] = trade_window.normalize(w["trade_window"])
    except (TypeError, ValueError, AttributeError) as exc:
        raise HTTPException(status_code=400, detail=f"Invalid webhook in file: {exc}") from exc
    return wh


async def _validate(doc: Any, area_id: int) -> dict[str, Any]:
    if not isinstance(doc, dict) or doc.get("fluxbridge_settings") != FORMAT or not isinstance(doc.get("settings"), dict):
        raise HTTPException(status_code=400, detail="Not a Fluxbridge settings export (format 1)")
    incoming = dict(doc["settings"])
    unknown = [k for k in incoming if k not in _PORTABLE_KEYS]
    if unknown:
        raise HTTPException(status_code=400, detail=f"Export carries keys that cannot be imported: {', '.join(sorted(unknown)[:5])}")
    current = config.load_settings(area_id=area_id)
    if "webhooks" in incoming:
        whs = incoming["webhooks"]
        if not isinstance(whs, list) or len(whs) > MAX_WEBHOOKS or not all(isinstance(w, dict) for w in whs):
            raise HTTPException(status_code=400, detail="webhooks must be a list of webhook objects")
        seen: set[str] = set()
        out = []
        for w in whs:
            wh = _import_webhook(w, current, area_id)
            if wh["id"] in seen or wh["token"] in seen:
                wh["id"], wh["token"] = f"wh_{secrets.token_hex(4)}", secrets.token_urlsafe(16)
            seen.update((wh["id"], wh["token"]))
            out.append(wh)
        incoming["webhooks"] = out
    if "news_lock" in incoming:
        try:
            incoming["news_lock"] = news.normalize(incoming["news_lock"])
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=f"news_lock: {exc}") from exc
    if "symbol_map" in incoming:
        sm = incoming["symbol_map"]
        if not isinstance(sm, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in sm.items()):
            raise HTTPException(status_code=400, detail="symbol_map must map symbol names to contracts")
    if "allowed_symbols" in incoming and not isinstance(incoming["allowed_symbols"], (list, str)):
        raise HTTPException(status_code=400, detail="allowed_symbols must be a list")
    for k in ("default_qty", "tp_qty", "health_check_interval", "pnl_poll_seconds", "journal_history_days",
              "rollover_warn_days", "discord_health_grace", "heartbeat_interval", "alert_smtp_port"):
        if k in incoming:
            try:
                incoming[k] = int(incoming[k])
            except (TypeError, ValueError) as exc:
                raise HTTPException(status_code=400, detail=f"{k} must be a number") from exc
    for k in _PORTABLE_KEYS:
        if k in incoming and isinstance(config.DEFAULT_SETTINGS[k], bool):
            incoming[k] = bool(incoming[k])
        elif k in incoming and isinstance(config.DEFAULT_SETTINGS[k], str) and not isinstance(incoming[k], str):
            raise HTTPException(status_code=400, detail=f"{k} must be text")
    await validate_settings(incoming)                     # the same checks the settings form runs
    return incoming


@router.get("/export")
async def api_export(request: Request) -> JSONResponse:
    user = request.state.user
    doc = export_settings(context.get_area())
    db.log_action(user["id"], user["email"], "settings_export", user["email"])
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
    return JSONResponse(doc, headers={"Content-Disposition": f'attachment; filename="fluxbridge-settings-{stamp}.json"'})


@router.post("/import")
async def api_import(request: Request) -> dict[str, Any]:
    """Replace the exported keys with the file's values (keys absent from the
    file stay as they are). Webhook tokens travel with the file so TradingView
    alerts pointing at the old bridge keep working on the new one; routing is
    kept only for logins present here."""
    user = request.state.user
    area = context.get_area()
    incoming = await _validate(await request.json(), area)

    def apply(s: dict[str, Any]) -> None:
        for k, v in incoming.items():
            s[k] = v
    config.update(apply, area_id=area)
    config.invalidate(area)
    db.log_action(user["id"], user["email"], "settings_import", user["email"], ", ".join(sorted(incoming)))
    state.log_event("info", f"Settings imported ({len(incoming)} key(s): {', '.join(sorted(incoming))})")
    return {"status": "ok", "keys": sorted(incoming), "webhooks": len(incoming.get("webhooks") or [])}

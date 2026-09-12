"""Per-workspace automations: "when <event> [matching …] then <action>".

Rules live in the area's settings (``automations``) and listen on the event
bus (:mod:`app.events`). Every producer already announces what happened —
a lost connection, a closed position with its P&L, a risk trigger, a failed
signal — so a rule is a filter over the event's data plus one of a handful
of actions the bridge can take on its own: notify, switch trading off,
flatten one account or all of them, lock an account for the day, pause a
webhook. Each firing is logged, announced as ``automation.fired`` (metrics,
never automatable itself) and rate-limited per rule by its cooldown.
"""
from __future__ import annotations

import secrets
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any, Optional

from . import config, context, events, state

# event kind → which event fields the filters apply to
KINDS: dict[str, dict[str, str]] = {
    "connection.lost":     {"account": "account"},
    "connection.restored": {"account": "account"},
    "position.opened":     {"account": "account", "symbol": "symbol"},
    "position.closed":     {"account": "account", "symbol": "symbol"},
    "trade.executed":      {"webhook": "webhook", "symbol": "contract"},
    "signal.failed":       {"webhook": "webhook"},
    "risk.triggered":      {"account": "spec"},
    "execution.problem":   {},
    "news.lock":           {},
    "agent.lost":          {},
    "copy.alert":          {},
    "discord.lost":        {},
    "daily.summary":       {},
}
ACTIONS = ("notify", "trading_off", "flatten_account", "flatten_all", "lock_account", "pause_webhook")
MAX_RULES = 50
MAX_LIST = 20
LOG_KEEP = 100
MIN_COOLDOWN_S = 2.0          # even "no cooldown" never fires the same rule twice inside one event burst

_last_fired: dict[tuple[int, str], float] = {}
_log: dict[int, deque[dict[str, Any]]] = {}


# ------------------------------------------------------------------ rules
def _root(symbol: str) -> str:
    from .engine.common import _base_root
    return _base_root(str(symbol or "").upper())


def _str_list(v: Any, key: str, *, upper: bool = False) -> list[str]:
    if v is None or v == "":
        return []
    if isinstance(v, str):
        v = [x for x in v.split(",")]
    if not isinstance(v, list):
        raise ValueError(f"{key} must be a list")
    out: list[str] = []
    for x in v:
        if not isinstance(x, str):
            raise ValueError(f"{key} entries must be text")
        x = x.strip()
        if x:
            out.append(x.upper() if upper else x)
    if len(out) > MAX_LIST:
        raise ValueError(f"{key}: at most {MAX_LIST} entries")
    return sorted(set(out))


def normalize_rule(raw: Any) -> dict[str, Any]:
    """One rule typed and bounded, or ValueError."""
    if not isinstance(raw, dict):
        raise ValueError("a rule must be an object")
    event = str(raw.get("event") or "").strip()
    if event not in KINDS:
        raise ValueError(f"unknown event '{event}'")
    action = str(raw.get("action") or "").strip()
    if action not in ACTIONS:
        raise ValueError(f"unknown action '{action}'")
    name = str(raw.get("name") or "").strip()[:80] or f"{event} → {action}"
    rid = str(raw.get("id") or "").strip()
    if not rid or not rid.startswith("au_") or len(rid) > 20:
        rid = f"au_{secrets.token_hex(4)}"
    try:
        cooldown = int(float(raw.get("cooldown_s", 60) or 0))
    except (TypeError, ValueError):
        raise ValueError("cooldown_s must be a number of seconds")
    cooldown = max(0, min(86_400, cooldown))
    loss = raw.get("loss_at_least")
    if loss in (None, ""):
        loss_v: Optional[float] = None
    else:
        try:
            loss_v = abs(float(loss))
        except (TypeError, ValueError):
            raise ValueError("loss_at_least must be a number")
        if loss_v != loss_v or loss_v == float("inf"):
            raise ValueError("loss_at_least must be finite")
    message = str(raw.get("message") or "")[:500]
    return {
        "id": rid, "name": name, "enabled": bool(raw.get("enabled", True)), "event": event, "action": action,
        "accounts": _str_list(raw.get("accounts"), "accounts"),
        "symbols": [_root(x) for x in _str_list(raw.get("symbols"), "symbols", upper=True)],
        "webhooks": _str_list(raw.get("webhooks"), "webhooks"),
        "loss_at_least": loss_v, "cooldown_s": cooldown, "message": message,
    }


def normalize_rules(raw: Any) -> list[dict[str, Any]]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError("automations must be a list of rules")
    if len(raw) > MAX_RULES:
        raise ValueError(f"at most {MAX_RULES} automations")
    out = [normalize_rule(r) for r in raw]
    seen: set[str] = set()
    for r in out:
        while r["id"] in seen:
            r["id"] = f"au_{secrets.token_hex(4)}"
        seen.add(r["id"])
    return out


def rules_for(area_id: int) -> list[dict[str, Any]]:
    raw = config.setting("automations", area_id=area_id)
    return raw if isinstance(raw, list) else []


# --------------------------------------------------------------- matching
def _webhook_ids(area_id: int, name_or_id: str) -> set[str]:
    """A webhook's id and name (events name webhooks, rules may hold either)."""
    out = {name_or_id}
    for w in (config.setting("webhooks", area_id=area_id) or []):
        if name_or_id in (w.get("id"), w.get("name")):
            out.update(x for x in (w.get("id"), w.get("name")) if x)
    return out


def matches(rule: dict[str, Any], kind: str, data: dict[str, Any], area_id: int) -> bool:
    if not rule.get("enabled", True) or rule["event"] != kind:
        return False
    fields = KINDS[kind]
    if rule["accounts"]:
        acc = str(data.get(fields.get("account", ""), "") or "")
        # trade.executed carries the list of accounts it went to
        accs = {acc} | {str(a) for a in (data.get("accounts") or [])}
        if not (accs & set(rule["accounts"])):
            return False
    if rule["symbols"]:
        sym = _root(str(data.get(fields.get("symbol", ""), "") or ""))
        if sym not in rule["symbols"]:
            return False
    if rule["webhooks"]:
        wh = str(data.get(fields.get("webhook", ""), "") or "")
        if not (_webhook_ids(area_id, wh) & set(rule["webhooks"])):
            return False
    if rule.get("loss_at_least") is not None and kind == "position.closed":
        try:
            pnl = float(data.get("pnl"))
        except (TypeError, ValueError):
            return False
        if pnl > -rule["loss_at_least"]:
            return False
    return True


def _cooldown_ok(area_id: int, rule: dict[str, Any], now: float) -> bool:
    key = (area_id, rule["id"])
    last = _last_fired.get(key)
    if last is not None and now - last < max(MIN_COOLDOWN_S, float(rule.get("cooldown_s") or 0)):
        return False
    _last_fired[key] = now
    return True


def _summary(kind: str, data: dict[str, Any]) -> str:
    parts = []
    for k in ("account", "spec", "symbol", "contract", "webhook", "direction", "qty", "pnl", "reason", "title", "error"):
        v = data.get(k)
        if v not in (None, "", [], {}):
            parts.append(f"{k}={v}" if k not in ("title", "reason", "error") else str(v)[:120])
    return f"{kind}" + (f" ({', '.join(parts)})" if parts else "")


def render_message(rule: dict[str, Any], kind: str, data: dict[str, Any]) -> str:
    text = rule.get("message") or ""
    if not text.strip():
        return f"{rule['name']}: {_summary(kind, data)}"
    fields = {"event": kind, "rule": rule["name"], "account": data.get("account") or data.get("spec") or "",
              "symbol": data.get("symbol") or data.get("contract") or "", "webhook": data.get("webhook") or "",
              "pnl": data.get("pnl", ""), "reason": data.get("reason") or data.get("error") or data.get("title") or "",
              "direction": data.get("direction") or "", "qty": data.get("qty", "")}
    try:
        return text.format_map({k: ("" if v is None else v) for k, v in fields.items()})
    except (KeyError, ValueError, IndexError):
        return text


# ---------------------------------------------------------------- actions
def _accounts_of(area_id: int, rule: dict[str, Any], kind: str, data: dict[str, Any]) -> list[str]:
    """Which trade accounts an account-scoped action targets: the event's
    account when it has one, else the rule's account filter."""
    field = KINDS[kind].get("account")
    if field and data.get(field):
        return [str(data[field])]
    if kind == "trade.executed" and data.get("accounts"):
        return [str(a) for a in data["accounts"] if not rule["accounts"] or str(a) in rule["accounts"]]
    return list(rule["accounts"])


def _find_account(area_id: int, spec: str) -> Optional[tuple[Any, dict[str, Any]]]:
    from . import tradovate
    for sess in tradovate.manager_for(area_id).all():
        if not sess.enabled:
            continue
        for a in sess.accounts:
            if str(a.get("spec") or "") == spec:
                return sess, a
    return None


async def _flatten_accounts(area_id: int, specs: list[str], *, lock_reason: str = "") -> str:
    from . import risk
    if not specs:
        return "no account to act on"
    lines = []
    for spec in specs:
        found = _find_account(area_id, spec)
        if not found:
            lines.append(f"{spec}: not found")
            continue
        sess, acc = found
        if lock_reason:
            risk._lock(area_id, spec, "automation", lock_reason, 0.0)
        c, f, errs = await risk.flatten_account(sess, acc)
        lines.append(f"{spec}: {c} cancelled, {f} flattened" + (f", errors: {'; '.join(errs)}" if errs else "") + (" — locked for today" if lock_reason else ""))
    return "; ".join(lines)


async def _run_action(area_id: int, rule: dict[str, Any], kind: str, data: dict[str, Any]) -> str:
    from . import alerts, signals
    action = rule["action"]
    message = render_message(rule, kind, data)
    if action == "notify":
        await alerts.automation(rule["name"], message)
        return "notified"
    if action == "trading_off":
        was = bool(config.load_settings(area_id=area_id).get("trading_enabled"))
        if was:
            config.save_settings({"trading_enabled": False}, area_id=area_id)
        await alerts.automation(rule["name"], message + (" — trading switched OFF" if was else " — trading was already off"))
        return "trading switched off" if was else "trading was already off"
    if action == "flatten_all":
        r = await signals.flatten_all()
        detail = f"{r.get('flattened', 0)} flattened, {r.get('cancelled', 0)} cancelled on {r.get('accounts', 0)} account(s)"
        await alerts.automation(rule["name"], f"{message} — flatten all: {detail}")
        return f"flatten all: {detail}"
    if action in ("flatten_account", "lock_account"):
        specs = _accounts_of(area_id, rule, kind, data)
        detail = await _flatten_accounts(area_id, specs, lock_reason=(f"automation '{rule['name']}'" if action == "lock_account" else ""))
        await alerts.automation(rule["name"], f"{message} — {detail}")
        return detail
    if action == "pause_webhook":
        wanted: set[str] = set()
        field = KINDS[kind].get("webhook")
        if field and data.get(field):
            wanted |= _webhook_ids(area_id, str(data[field]))
        for w in rule["webhooks"]:
            wanted |= _webhook_ids(area_id, w)
        paused: list[str] = []

        def mut(s: dict[str, Any]) -> None:
            for w in s.get("webhooks") or []:
                if (w.get("id") in wanted or w.get("name") in wanted) and w.get("enabled"):
                    w["enabled"] = False
                    paused.append(str(w.get("name") or w.get("id")))
        if wanted:
            config.update(mut, area_id=area_id)
        detail = f"paused: {', '.join(paused)}" if paused else "no enabled webhook matched"
        await alerts.automation(rule["name"], f"{message} — {detail}")
        return detail
    return "unknown action"


async def _run(area_id: int, rules: list[dict[str, Any]], kind: str, data: dict[str, Any]) -> None:
    with context.use_area(area_id):
        for rule in rules:
            try:
                result = await _run_action(area_id, rule, kind, data)
            except Exception as exc:  # noqa: BLE001 - one rule must not stop the next
                result = f"failed: {type(exc).__name__}: {exc}"[:300]
            entry = {"at": datetime.now(timezone.utc).isoformat(), "rule": rule["id"], "name": rule["name"], "event": kind,
                     "action": rule["action"], "result": result, "summary": _summary(kind, data)[:300]}
            _log.setdefault(area_id, deque(maxlen=LOG_KEEP)).appendleft(entry)
            state.log_event("warn" if not result.startswith("failed") else "error", f"⚙️ Automation '{rule['name']}' ({kind} → {rule['action']}): {result}")
            events.emit("automation.fired", rule=rule["id"], name=rule["name"], event=kind, action=rule["action"], result=result)


def _on_event(kind: str, data: dict[str, Any]) -> Any:
    """Bus listener: match the area's rules; returns the coroutine that runs them."""
    if kind not in KINDS:
        return None
    area_id = context.get_area_optional()
    if area_id is None:
        return None
    rules = rules_for(area_id)
    if not rules:
        return None
    now = time.monotonic()
    hit = [r for r in rules if matches(r, kind, data, area_id) and _cooldown_ok(area_id, r, now)]
    if not hit:
        return None
    return _run(area_id, hit, kind, data)


def recent(area_id: int, limit: int = 50) -> list[dict[str, Any]]:
    return list(_log.get(area_id) or [])[:limit]


def reset() -> None:
    _last_fired.clear()
    _log.clear()


_unsubscribe = events.subscribe("*", _on_event)

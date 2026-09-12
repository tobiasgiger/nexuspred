"""Copy groups: records, validation, sizing, marketplace views, follower lists."""
from __future__ import annotations
import json
import secrets
from datetime import datetime, timezone
from typing import Any, Optional
from .. import config, context, db, tradovate


# the live runners per (area, group id) — owned by manager.py, read here for the status views
_runners: dict[tuple[int, str], Any] = {}


def new_group(name: str = "Copy group") -> dict[str, Any]:
    return {
        "id": "cg_" + secrets.token_urlsafe(6), "name": name, "enabled": False,
        "leader": {"token_idx": 0, "spec": "", "account_id": 0},
        "symbols": [],                       # roots (MNQ, ES …); empty = every contract
        "followers": [],
        "feed": "auto",                      # auto | websocket | poll
        "feed_loss_flatten_s": 30,
        "copy_adds": True,                   # fixed mode: scale with the leader's adds
        "copy_orders": True,                 # mirror the leader's working limit / stop orders
        "on_feed_loss": "flatten",           # flatten | pause — what to do after feed_loss_flatten_s
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


def normalize_follower(f: dict[str, Any]) -> dict[str, Any]:
    mode = "fixed" if str(f.get("mode") or "multiplier") == "fixed" else "multiplier"
    def _num(key: str, default: float) -> float:
        v = f.get(key, default)
        return default if v in (None, "") else float(v)
    mult = _num("multiplier", 1)
    fixed = int(_num("fixed", 1))
    mx = int(_num("max_contracts", 0))
    direction = str(f.get("direction") or "both")
    if direction not in ("both", "long", "short"):
        direction = "both"
    if not (0.01 <= mult <= 100):
        raise ValueError("multiplier must be between 0.01 and 100")
    if not (1 <= fixed <= 1000):
        raise ValueError("fixed contracts must be between 1 and 1000")
    if not (0 <= mx <= 1000):
        raise ValueError("max contracts must be between 0 and 1000")
    return {"token_idx": int(f["token_idx"]), "lid": str(f.get("lid") or ""), "spec": str(f["spec"]), "account_id": int(f.get("account_id") or 0),
            "enabled": bool(f.get("enabled", True)), "mode": mode, "multiplier": round(mult, 4),
            "fixed": fixed, "max_contracts": mx, "direction": direction}


def load_groups(area_id: Optional[int] = None) -> list[dict[str, Any]]:
    g = config.load_settings(area_id=area_id).get("copy_groups")
    return [dict(x) for x in g] if isinstance(g, list) else []


def save_groups(groups: list[dict[str, Any]], area_id: Optional[int] = None) -> None:
    config.save_settings({"copy_groups": groups}, area_id=area_id)


def validate_group(g: dict[str, Any], all_groups: list[dict[str, Any]], accounts: list[dict[str, Any]]) -> None:
    """Raise ValueError on an inconsistent group (unknown accounts, leader among
    followers, a chain that would feed a leader from its own followers)."""
    idx_to_lid = {int(a["token_idx"]): str(a.get("lid") or "") for a in accounts if a.get("lid")}

    def key(entry: dict[str, Any]) -> tuple[str, str]:
        """(login id, account) — an entry without an id is resolved by its index."""
        idx = int(entry["token_idx"])
        return (str(entry.get("lid") or idx_to_lid.get(idx) or idx), str(entry["spec"]))
    known = {key(a) for a in accounts}
    brokers = {key(a): str(a.get("broker") or "tradovate") for a in accounts}
    lead = key(g["leader"])
    if lead not in known:
        raise ValueError("Leader account is not a discovered trade account")
    if not g["followers"]:
        raise ValueError("Add at least one follower account")
    seen = set()
    for f in g["followers"]:
        k = key(f)
        if k not in known:
            raise ValueError(f"Follower {f['spec']} is not a discovered trade account")
        if brokers.get(k) != brokers.get(lead):
            # positions are matched by the broker's contract ids, which differ between brokers
            raise ValueError(f"Follower {f['spec']} is on {brokers.get(k)} but the leader is on {brokers.get(lead)} — a copy group stays within one broker")
        if k == lead:
            raise ValueError("The leader cannot be its own follower")
        if k in seen:
            raise ValueError(f"Follower {f['spec']} is listed twice")
        seen.add(k)
    if not (5 <= int(g.get("feed_loss_flatten_s") or 30) <= 600):
        raise ValueError("feed-loss flatten must be between 5 and 600 seconds")
    if str(g.get("on_feed_loss") or "flatten") not in ("flatten", "pause"):
        raise ValueError("on_feed_loss must be flatten or pause")
    # a follower account belongs to one group only: two mirrors on one account fight each other
    for og in all_groups:
        if og.get("id") == g.get("id"):
            continue
        theirs = {key(f) for f in og.get("followers", []) if f.get("enabled", True)}
        clash = [f["spec"] for f in g["followers"] if f.get("enabled", True) and key(f) in theirs]
        if clash:
            raise ValueError(f"Follower {clash[0]} already follows {og.get('leader', {}).get('spec', '?')} in group '{og.get('name', '?')}' — an account can follow one leader only")
    # cycle check across groups: leader → followers edges
    edges: dict[tuple[int, str], set[tuple[int, str]]] = {}
    for og in [*(x for x in all_groups if x.get("id") != g.get("id")), g]:
        ol = key(og["leader"])
        edges.setdefault(ol, set()).update(key(f) for f in og.get("followers", []))
    stack, visited = list(edges.get(lead, ())), set()
    while stack:
        n = stack.pop()
        if n == lead:
            raise ValueError("This would create a loop: a follower of this group is (indirectly) its leader")
        if n in visited:
            continue
        visited.add(n)
        stack.extend(edges.get(n, ()))


def _round_half_up(x: float) -> int:
    return int(x + 0.5) if x >= 0 else -int(-x + 0.5)


def target_qty(f: dict[str, Any], leader_net: float, unit: float, *, copy_adds: bool = True) -> int:
    """The follower's target net position for a leader net position."""
    net = int(leader_net)
    if net == 0:
        return 0
    sign = 1 if net > 0 else -1
    if f.get("direction") == "long" and sign < 0:
        return 0
    if f.get("direction") == "short" and sign > 0:
        return 0
    if f.get("mode") == "fixed":
        fixed = int(f.get("fixed", 1) or 1)
        q = _round_half_up(fixed * abs(net) / unit) if (copy_adds and unit) else fixed
    else:
        q = _round_half_up(abs(net) * float(f.get("multiplier", 1) or 1))
    mx = int(f.get("max_contracts", 0) or 0)
    if mx:
        q = min(q, mx)
    return sign * max(q, 0)


def parse_frames(text: str) -> list[dict[str, Any]]:
    """Tradovate speaks a SockJS-like framing: ``o`` open, ``h`` heartbeat,
    ``a[...]`` a JSON array of messages, ``c[...]`` close. Returns the messages."""
    if not text:
        return []
    kind = text[0]
    if kind in ("o", "h"):
        return [{"e": "open" if kind == "o" else "heartbeat"}]
    if kind == "a":
        try:
            arr = json.loads(text[1:])
        except ValueError:
            return []
        return [m for m in arr if isinstance(m, dict)]
    if kind == "c":
        return [{"e": "close"}]
    return []


def sharing_of(g: dict[str, Any]) -> dict[str, Any]:
    from .. import marketplace
    return marketplace.sharing_of(g)


def public_view(g: dict[str, Any], area_id: int, email: Optional[str] = None) -> dict[str, Any]:
    """What a subscriber may see of a published group: no accounts, no logins."""
    sh = sharing_of(g)
    lead_idx = int((g.get("leader") or {}).get("token_idx") or 0)
    tokens = config.load_settings(area_id=area_id).get("token_accounts") or []
    env = str(tokens[lead_idx].get("environment") or "demo") if 0 <= lead_idx < len(tokens) else "demo"
    r = _runners.get((area_id, g["id"]))
    return {"kind": "copy", "publisher_area_id": area_id, "group_id": g["id"],
            "title": sh["title"] or g.get("name") or "Copy group", "description": sh["description"], "visibility": sh["visibility"],
            "publisher_email": email if email is not None else db.area_owner_email(area_id),
            "symbols": list(g.get("symbols") or []), "environment": env, "copy_orders": bool(g.get("copy_orders")),
            "enabled": bool(g.get("enabled")) and not sh["paused"], "running": bool(r and r.tasks), "feed_ok": bool(r and r.feed_ok), "paused": bool(r and r.paused),
            "publisher_paused": sh["paused"], "approval": sh["approval"], "max_subscribers": sh["max_subscribers"], "tags": sh["tags"], "published_at": sh["published_at"],
            "followers_count": len(g.get("followers") or []) + len(r.external if r else external_followers(area_id, g["id"]))}


def published_groups(*, user_id: Optional[int] = None, exclude_area: Optional[int] = None) -> list[dict[str, Any]]:
    from .. import marketplace
    out = []
    for aid in db.all_area_ids():
        if exclude_area is not None and aid == exclude_area:
            continue
        email = None
        for g in load_groups(aid):
            sh = sharing_of(g)
            if not sh["enabled"] or (user_id is not None and not marketplace.visible_to(sh, user_id)):
                continue
            if email is None:
                email = db.area_owner_email(aid) or ""
            out.append(public_view(g, aid, email))
    return out


def find_published(publisher_area_id: int, group_id: str) -> tuple[Optional[dict[str, Any]], dict[str, Any]]:
    for g in load_groups(publisher_area_id):
        if g.get("id") == group_id:
            sh = sharing_of(g)
            return (g, sh) if sh["enabled"] else (None, {})
    return None, {}


def leader_broker(publisher_area_id: int, group_id: str) -> str:
    """The broker of a published group's leader login (``tradovate`` when unknown)."""
    g, _ = find_published(publisher_area_id, group_id)
    if not g:
        return "tradovate"
    lead = g.get("leader") or {}
    with context.use_area(publisher_area_id):
        mgr = tradovate.manager_for(publisher_area_id)
        if hasattr(mgr, "session_for"):
            sess = mgr.session_for(lead.get("lid") or None, int(lead.get("token_idx") or 0))
        else:
            sessions = mgr.all()
            idx = int(lead.get("token_idx") or 0)
            sess = sessions[idx] if 0 <= idx < len(sessions) else None
    return str(getattr(sess, "kind", "tradovate") or "tradovate") if sess else "tradovate"


def clean_subscriber_accounts(raw: Any, area_id: int, *, exclude_sub_id: Optional[int] = None,
                              broker_kind: Optional[str] = None) -> list[dict[str, Any]]:
    """A subscriber's follower accounts (their own logins, resolved by login id),
    validated: discovered accounts only, on the leader's broker (``broker_kind``)
    when given, no account that already follows a leader through an own group or
    another subscription. Raises ValueError."""
    from ..routers.accounts import trade_accounts_overview
    with context.use_area(area_id):
        known = trade_accounts_overview()
    by_key = {(str(a.get("lid") or ""), str(a["spec"])): a for a in known}
    by_spec = {str(a["spec"]): a for a in known}
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for a in raw or []:
        if not isinstance(a, dict) or not a.get("spec"):
            continue
        lid = str(a.get("lid") or "")
        acct = by_key.get((lid, str(a["spec"]))) if lid else by_spec.get(str(a["spec"]))
        if acct is None:
            raise ValueError(f"{a['spec']} is not one of your discovered trade accounts")
        if broker_kind and str(acct.get("broker") or "tradovate") != broker_kind:
            raise ValueError(f"{a['spec']} is on {acct.get('broker') or 'tradovate'}; this leader trades on {broker_kind} — copy trading stays within one broker")
        f = normalize_follower({**a, "token_idx": acct["token_idx"], "lid": acct.get("lid") or "", "account_id": acct.get("id") or 0})
        if f["spec"] in seen:
            raise ValueError(f"{f['spec']} is listed twice")
        seen.add(f["spec"])
        out.append(f)
    taken_own = {str(f["spec"]) for g in load_groups(area_id) for f in g.get("followers") or [] if f.get("enabled", True)}
    taken_own |= {str((g.get("leader") or {}).get("spec") or "") for g in load_groups(area_id) if g.get("enabled")}
    taken_subs = {str(a["spec"]) for sub in db.list_subscriptions(area_id) if sub["webhook_id"].startswith("copy:") and sub["id"] != exclude_sub_id
                  for a in sub.get("accounts") or [] if isinstance(a, dict) and a.get("enabled", True)}
    for f in out:
        if f["enabled"] and f["spec"] in taken_own:
            raise ValueError(f"{f['spec']} already follows a leader in one of your own copy groups (or is a leader) — an account can follow one leader only")
        if f["enabled"] and f["spec"] in taken_subs:
            raise ValueError(f"{f['spec']} already follows another copy-trading subscription")
    return out


def following_status(area_id: int) -> list[dict[str, Any]]:
    """The subscriber's view: every copy subscription of this workspace with the
    live picture of *their* accounts (never the leader's account details)."""
    out = []
    for sub in db.list_subscriptions(area_id):
        if not sub["webhook_id"].startswith("copy:"):
            continue
        gid = sub["webhook_id"][5:]
        g, sh = find_published(sub["publisher_area_id"], gid)
        r = _runners.get((sub["publisher_area_id"], gid)) if g else None
        st = r.status() if r else None
        mine = {str(a.get("spec")) for a in sub.get("accounts") or [] if isinstance(a, dict)}
        out.append({"sub_id": sub["id"], "publisher_area_id": sub["publisher_area_id"], "group_id": gid,
                    "enabled": sub["enabled"], "accounts": sub.get("accounts") or [], "created_at": sub["created_at"],
                    "published": g is not None, "title": (sh.get("title") or (g or {}).get("name") or "Copy group") if g else "(no longer published)",
                    "publisher_email": db.area_owner_email(sub["publisher_area_id"]) or "", "symbols": list((g or {}).get("symbols") or []),
                    "running": bool(st and st["running"]), "feed_ok": bool(st and st["feed_ok"]), "paused": bool(st and st["paused"]),
                    "pause_reason": (st or {}).get("pause_reason", ""), "latency_ms": (st or {}).get("latency_ms"),
                    "leader_positions": (st or {}).get("leader_positions", []),
                    "followers": [f for f in (st or {}).get("followers", []) if f.get("area_id") == area_id and f["spec"] in mine]})
    return out


def masked_status(st: dict[str, Any]) -> dict[str, Any]:
    """A group's status for the publisher: subscribers' accounts are never shown."""
    followers = []
    for f in st.get("followers", []):
        if f.get("external"):
            alias = f"subscriber #{f.get('sub_id', '?')}"
            masked = {**f, "spec": alias, "orders": [{**o} for o in f.get("orders", [])]}
            for k in ("error", "detail"):
                if isinstance(masked.get(k), str) and f.get("spec"):
                    masked[k] = masked[k].replace(str(f["spec"]), alias)
            followers.append(masked)
        else:
            followers.append(f)
    return {**st, "followers": followers}


def external_followers(area_id: int, group_id: str, *, group: Optional[dict[str, Any]] = None) -> list[dict[str, Any]]:
    """Followers that subscribed to this group on the marketplace: every enabled
    account of every enabled subscription, stamped with the subscriber's area
    (their logins, trading switch, risk locks, logs) and subscription id.
    ``group`` is the already loaded group (saves a settings read)."""
    out: list[dict[str, Any]] = []
    published = sharing_of(group)["enabled"] if group is not None else find_published(area_id, group_id)[0] is not None
    if not published:
        return out                                   # unpublished: subscribers' accounts leave the mirror
    for sub in db.active_subscriptions(area_id, f"copy:{group_id}"):
        for a in sub.get("accounts") or []:
            if not isinstance(a, dict) or not a.get("enabled", True):
                continue
            try:
                f = normalize_follower(a)
            except (TypeError, ValueError, KeyError):
                continue
            out.append({**f, "area_id": int(sub["area_id"]), "sub_id": int(sub["id"]), "external": True})
    return out


def effective_followers(area_id: int, group: dict[str, Any], external: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Own followers plus external ones — an external account that is the leader
    or already an own follower is left out (one mirror per account)."""
    own = [dict(f) for f in group.get("followers") or []]
    taken = {str(f["spec"]) for f in own} | {str((group.get("leader") or {}).get("spec") or "")}
    for f in external:
        if str(f["spec"]) in taken:
            continue
        taken.add(str(f["spec"]))
        own.append(dict(f))
    return own

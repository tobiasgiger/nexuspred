"""Copy trading: mirror a leader account's positions onto follower accounts.

A **copy group** names one leader trade account, an optional symbol filter and
a list of followers with their sizing. The engine is a *position mirror*: on
every leader position change it computes each follower's target net position
and sends one market order for the difference to the follower's current
position. That single rule covers opening, adding, reducing, closing and
reversing, and it is self-healing — a missed event, a partial fill or a
rejected order is corrected on the next event or by the periodic reconcile.

**Feed.** The leader login is watched over Tradovate's WebSocket "user sync"
(events ~100 ms after a fill). A leader login that executes through a paired
agent, or a group set to ``feed: "poll"``, is polled once a second instead —
through the agent, so the login's IP rule is kept. Losing the feed for longer
than ``feed_loss_flatten_s`` flattens the followers' mirrored contracts and
pauses the group until the user resumes it.

**Sizing.** ``multiplier``: target = leader net × factor (rounded). ``fixed``:
``fixed`` contracts for the leader's initial entry, scaled proportionally when
the leader adds or reduces (``copy_adds``), or constant when not. Both are
capped by ``max_contracts`` and filtered by ``direction``.

**Baseline.** A leader position that already exists when the group starts is
not copied; mirroring of that contract begins once the leader is flat again,
or immediately after *Sync now*.
"""
from __future__ import annotations

import asyncio
import json
import secrets
import time
from datetime import datetime, timezone
from typing import Any, Optional

from . import alerts, config, context, db, history, news, risk, state, tradovate
from .copy_orders import OrderMirror
from .engine.common import _base_root
from .tradovate import AccountExecutor, RateLimited, TradovateError

WS_URLS = {"live": "wss://live.tradovateapi.com/v1/websocket",
           "demo": "wss://demo.tradovateapi.com/v1/websocket"}
POLL_INTERVAL_S = 2.0        # REST cadence while the socket is down (positions; orders every 2nd)
POLL_WS_INTERVAL_S = 10.0    # REST cadence while the socket is synced (a safety net only)
RECONCILE_INTERVAL_S = 10.0
HEARTBEAT_S = 2.5
RECONNECT_BACKOFF_S = 2.0   # first retry delay after a feed error (doubles up to 30 s)
FEED_STALE_S = 12.0          # no frame for this long → the WebSocket is considered lost
POLL_ERROR_SLEEP_S = 2.0     # retry delay of the REST poll after an error
POLL_MAX_INTERVAL_S = 30.0   # the poll slows down to this after Tradovate rate-limits us
POLL_RECOVER_S = 60.0        # a clean minute speeds the poll up again by one step
ORDERS_EVERY_N = 2           # the leader's orders are read every n-th position poll
WS_SYNC_TIMEOUT_S = 15.0     # no answer to user/syncrequest → reconnect the socket
WS_RECENT_MAX = 20           # raw socket messages kept for the diagnostics block
REJECT_HOLDOFF_S = 30.0      # reconcile leaves a follower alone this long after a rejected order
ORDER_SETTLE_S = 5.0         # …and this long after a successful one (the fill must reach /position/list)
FOLLOWER_RESEED_S = 60.0     # followers' positions are re-read at most this often while the leader seed fails
MAX_EVENTS_MEMORY = 200


# ----------------------------------------------------------------- config
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


# ----------------------------------------------------------------- sizing
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


# ----------------------------------------------------------- ws framing
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


# ----------------------------------------------------------------- runner
# ------------------------------------------------------------ marketplace
def sharing_of(g: dict[str, Any]) -> dict[str, Any]:
    from . import marketplace
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
            "enabled": bool(g.get("enabled")), "running": bool(r and r.tasks), "feed_ok": bool(r and r.feed_ok), "paused": bool(r and r.paused),
            "followers_count": len(g.get("followers") or []) + len(r.external if r else external_followers(area_id, g["id"]))}


def published_groups(*, user_id: Optional[int] = None, exclude_area: Optional[int] = None) -> list[dict[str, Any]]:
    from . import marketplace
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
    from .routers.accounts import trade_accounts_overview
    with context.use_area(area_id):
        known = trade_accounts_overview()
        s = config.load_settings()
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


class GroupRunner:
    """Live state and tasks of one enabled copy group."""

    def __init__(self, area_id: int, group: dict[str, Any]) -> None:
        self.area_id = area_id
        self.group = group
        self.id = group["id"]
        self.external = external_followers(area_id, self.id, group=group)   # marketplace subscribers' accounts
        self.followers = effective_followers(area_id, group, self.external)
        # what sync_area compares each cycle: serialised once here, not per cycle
        self.fingerprint = (json.dumps(group, sort_keys=True), json.dumps(self.external, sort_keys=True))
        self._area_by_spec = {str(f["spec"]): int(f.get("area_id") or area_id) for f in self.followers}
        self.tasks: list[asyncio.Task] = []
        self.feed_kind = ""
        self.feed_ok = False
        self.poll_interval = POLL_INTERVAL_S    # adapts to Tradovate's rate limit
        self.throttled_until: float = 0.0
        self._poll_n = 0
        self._last_429: float = 0.0
        self.ws_ok = False                     # the WebSocket accelerator is connected and synced
        self.ws_error = ""
        self.ws_last_frame: float = 0.0
        self.feed_since: float = 0.0          # monotonic when the feed last became ok
        self.last_frame: float = 0.0
        self.last_event_ts: Optional[str] = None
        self.last_latency_ms: Optional[int] = None
        self.paused = False
        self.pause_reason = ""
        self.error = ""
        self.leader_net: dict[int, int] = {}          # contract_id → net
        self.unit: dict[int, int] = {}                # contract_id → leader size at open
        self.baseline: set[int] = set()               # contracts ignored until the leader is flat
        self.contract_names: dict[int, str] = {}
        self.follower_pos: dict[tuple[str, int], int] = {}   # (spec, contract_id) → net
        self.follower_err: dict[str, str] = {}
        self.locks: dict[str, asyncio.Lock] = {}
        self._leader_seeded = False
        self._last_error = ""
        self._filtered: set[int] = set()              # contracts already reported as filtered out
        self.follower_err_at: dict[str, float] = {}   # spec → monotonic of the last reject
        self.last_order_at: dict[str, float] = {}     # spec → monotonic of the last successful order
        self._pending_poll: dict[int, int] = {}       # contract → net a poll saw once while the socket is synced
        self._followers_seeded_at: float = 0.0
        self.leader_account_id = 0
        self.diag: dict[str, Any] = {"frames": 0, "props": {}, "backstop_catches": 0, "recent": []}
        self.orders = OrderMirror(self)
        self._stop = asyncio.Event()

    # ---- helpers
    def _leader_session(self) -> Optional[tradovate.TradovateSession]:
        mgr = tradovate.manager_for(self.area_id)
        lead = self.group["leader"]
        sessions = mgr.all()
        s = None
        if lead.get("lid"):
            s = next((x for x in sessions if getattr(x, "lid", "") == lead["lid"]), None)
        if s is None:
            # by position only for routes without an id, or logins that carry none
            # (never onto another login that has its own id)
            idx = int(lead["token_idx"])
            cand = sessions[idx] if 0 <= idx < len(sessions) else None
            if cand is not None and (not lead.get("lid") or not getattr(cand, "lid", "")):
                s = cand
        return s if s is not None and s.enabled else None

    async def _leader_account_id(self, session: Any) -> int:
        """Tradovate's numeric id of the leader account (settings first, then
        the login's account list). 0 = unknown → the feed cannot be filtered."""
        spec = self.group["leader"]["spec"]
        for a in session.accounts:
            if a.get("spec") == spec and a.get("id"):
                return int(a["id"])
        if self.group["leader"].get("account_id"):
            return int(self.group["leader"]["account_id"])
        try:
            for a in await session.account_list():
                if str(a.get("name")) == spec and a.get("id"):
                    return int(a["id"])
        except Exception as exc:  # noqa: BLE001
            self.error = f"account list: {exc}"[:200]
        return 0

    def _area_of(self, f: dict[str, Any]) -> int:
        """The workspace a follower belongs to (marketplace followers: theirs)."""
        return int(f.get("area_id") or self.area_id)

    def _session_key(self, f: dict[str, Any]) -> str:
        return f"lid:{f['lid']}" if f.get("lid") else f"idx:{int(f['token_idx'])}"

    def _session(self, f: dict[str, Any]) -> Optional[Any]:
        """The follower's login — by its stable id first (a subscriber who
        deletes a login must not have their other login read by position)."""
        mgr = tradovate.manager_for(self._area_of(f))
        if hasattr(mgr, "session_for"):
            return mgr.session_for(f.get("lid") or None, int(f["token_idx"]))
        sessions = mgr.all()
        idx = int(f["token_idx"])
        return sessions[idx] if 0 <= idx < len(sessions) else None

    def _executor(self, f: dict[str, Any]) -> Optional[AccountExecutor]:
        mgr = tradovate.manager_for(self._area_of(f))
        if f.get("lid") and hasattr(mgr, "session_for"):
            return mgr.executor_for(int(f["token_idx"]), str(f["spec"]), 1, lid=f["lid"])
        return mgr.executor_for(int(f["token_idx"]), str(f["spec"]), 1)

    def _wanted(self, contract_id: int) -> bool:
        roots = [str(r).upper() for r in (self.group.get("symbols") or [])]
        if not roots:
            return True
        name = self.contract_names.get(contract_id, "")
        return _base_root(name).upper() in roots if name else False

    async def _contract_name(self, session: Any, cid: int) -> str:
        name = self.contract_names.get(cid)
        if name:
            return name
        try:
            item = await session.contract_info(cid)
            name = str((item or {}).get("name") or cid)
        except Exception:  # noqa: BLE001
            name = str(cid)
        self.contract_names[cid] = name
        return name

    def _alert(self, title: str, message: str, *, email: bool = False, area_id: Optional[int] = None) -> None:
        """Alerts never hold a follower lock or the socket loop: fire and forget.
        ``area_id`` routes a follower's problem to the follower's own workspace."""
        async def run() -> None:
            try:
                with context.use_area(area_id if area_id is not None else self.area_id):
                    await alerts.copy_alert(title, message, email=email)
            except Exception:  # noqa: BLE001
                pass
        tradovate._fire(run())

    def _record(self, kind: str, *, follower: str = "", symbol: str = "", detail: str = "",
                latency_ms: Optional[int] = None) -> None:
        rec = {"group_id": self.id, "kind": kind, "leader": self.group["leader"]["spec"],
               "follower": follower, "symbol": symbol, "detail": detail[:300], "latency_ms": latency_ms}
        farea = self._area_by_spec.get(follower, self.area_id) if follower else self.area_id
        if farea != self.area_id:
            # a marketplace follower's event lives in the follower's workspace, with the
            # leader account hidden; the publisher's log keeps it without the account
            # (broker errors start with the account name: masked in the detail too)
            alias = f"subscriber #{next((f.get('sub_id') for f in self.followers if f['spec'] == follower), '?')}"
            history.defer(db.insert_copy_event, farea, {**rec, "leader": "leader"})
            history.defer(db.insert_copy_event, self.area_id, {**rec, "follower": alias, "detail": rec["detail"].replace(follower, alias)})
            return
        history.defer(db.insert_copy_event, self.area_id, rec)        # off the loop: a WAL commit never delays a mirror

    # ---- lifecycle
    async def _in_area(self, fn: Any) -> Any:
        """Run a task body in the group's own workspace: the leader login logs
        and alerts through the implicit context, and the runner may be started
        from a subscriber's request or the startup context. (``fn`` builds the
        coroutine here, so a task cancelled before it ran leaves nothing unawaited.)"""
        with context.use_area(self.area_id):
            return await fn()

    def start(self) -> None:
        self._stop.clear()
        self.tasks = [asyncio.create_task(self._in_area(self._feed_main), name=f"copy-feed-{self.id}"),
                      asyncio.create_task(self._in_area(self._reconcile_loop), name=f"copy-reconcile-{self.id}")]

    async def stop(self) -> None:
        self._stop.set()
        for t in self.tasks:
            t.cancel()
        for t in self.tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self.tasks = []
        self.feed_ok = False
        t = getattr(self.orders, "_apply_task", None)
        if t is not None and not t.done():
            t.cancel()

    def _follower_id(self, session: Any, f: dict[str, Any]) -> int:
        """Tradovate id of a follower account (0 when unknown — never guessed)."""
        return int(f.get("account_id") or 0) or next((int(a["id"]) for a in session.accounts if a.get("spec") == f["spec"] and a.get("id")), 0)

    async def _seed_followers(self, *, force: bool = False) -> None:
        """Followers' current positions from the broker (one call per login).
        Each login's picture is rebuilt from scratch, so a position that is gone
        at the broker is gone here too. Re-seeds are throttled while the leader
        seed keeps failing."""
        # 0.0 = never seeded: on a freshly booted host monotonic() itself can be < FOLLOWER_RESEED_S
        if not force and self._followers_seeded_at and time.monotonic() - self._followers_seeded_at < FOLLOWER_RESEED_S:
            return
        by_session: dict[tuple[int, str], list[dict[str, Any]]] = {}
        for f in self.followers:
            by_session.setdefault((self._area_of(f), self._session_key(f)), []).append(f)
        for (farea, _skey), fs in by_session.items():
            s = self._session(fs[0])
            if s is None:
                for f in fs:
                    self.follower_err[f["spec"]] = "login disabled or gone"
                continue
            ids = {self._follower_id(s, f): f["spec"] for f in fs}
            ids.pop(0, None)
            for f in fs:
                if self._follower_id(s, f) == 0:
                    self.follower_err[f["spec"]] = "no broker account id — run Connect & Verify on the login"
            try:
                raw = await s.positions_snapshot()
            except Exception as exc:  # noqa: BLE001
                for f in fs:
                    self.follower_err[f["spec"]] = f"positions: {exc}"
                continue
            if not isinstance(raw, list):
                continue
            for key in [k for k in self.follower_pos if k[0] in ids.values()]:
                self.follower_pos.pop(key, None)              # rebuild this login's picture from the broker
            for p in raw:
                spec = ids.get(int(p.get("accountId") or 0))
                if spec and int(p.get("netPos") or 0):
                    self.follower_pos[(spec, int(p.get("contractId") or 0))] = int(p.get("netPos") or 0)
        self._followers_seeded_at = time.monotonic()
        try:
            await self.orders.load()
        except Exception as exc:  # noqa: BLE001
            self.orders.error = f"twins: {exc}"[:200]

    def _persist(self, cid: int) -> None:
        """Keep the mirrored contracts' leader picture in the database so a
        restart (every deploy is one) carries on instead of starting a baseline."""
        try:
            net = self.leader_net.get(cid, 0)
            if net and cid not in self.baseline:
                history.defer(db.save_copy_state, self.area_id, self.id, cid, self.contract_names.get(cid, str(cid)), net, self.unit.get(cid) or abs(net))
            else:
                history.defer(db.delete_copy_state, self.area_id, self.id, cid)
        except Exception as exc:  # noqa: BLE001
            self.error = f"state: {exc}"[:200]

    async def _seed_leader(self, session: Any, account_id: int) -> None:
        """Read the leader's positions. At the first start an open position is
        *baseline* (not copied) — unless the database remembers it as mirrored
        from before a restart, in which case the change since then is mirrored
        like after a reconnect. After a feed reconnect the picture is diffed
        against what we knew, so changes made during the outage are mirrored
        (or recorded as skipped while the group is paused)."""
        raw = await session.positions_snapshot()
        seen: dict[int, int] = {}
        for p in raw if isinstance(raw, list) else []:
            if int(p.get("accountId") or 0) != account_id:
                continue
            cid, net = int(p.get("contractId") or 0), int(p.get("netPos") or 0)
            if net:
                seen[cid] = net
        if not self._leader_seeded:
            self._leader_seeded = True
            remembered = {int(r["contract_id"]): r for r in db.list_copy_state(self.area_id, self.id)}
            for cid, r in remembered.items():
                # mirrored before the restart: pick up where we left off
                self.leader_net[cid] = int(r["leader_net"])
                self.unit[cid] = int(r["unit"] or abs(int(r["leader_net"])) or 1)
                if r.get("symbol"):
                    self.contract_names[cid] = str(r["symbol"])
            for cid, net in seen.items():
                if cid in remembered:
                    continue
                await self._contract_name(session, cid)
                self.leader_net[cid] = net
                self.unit[cid] = abs(net)
                self.baseline.add(cid)      # existing position: not copied until flat / sync
            if remembered:
                self._record("resumed", detail=f"restart: {len(remembered)} mirrored contract(s) restored from the database")
                for cid in remembered:
                    await self._on_position(session, cid, seen.get(cid, 0))
            return
        for cid, net in seen.items():
            if cid not in self.leader_net:
                self.leader_net[cid] = 0      # opened during the outage: an entry, not a baseline
            await self._on_position(session, cid, net)
        for cid in [c for c, n in self.leader_net.items() if n and c not in seen]:
            await self._on_position(session, cid, 0)

    # ---- feed
    async def _feed_main(self) -> None:
        """The REST poll of the leader's orders and positions (once a second) is
        the feed: it decides ``feed_ok`` and therefore the feed-loss watchdog.
        Where possible the Tradovate WebSocket runs alongside as an accelerator —
        its events are applied the moment they arrive, but losing the socket
        never counts as losing the feed."""
        while not self._stop.is_set():
            session = self._leader_session()
            if session is None or not session.has_token():
                self.error = "leader login disabled or without token"
                self._mark_feed(False)
                await asyncio.sleep(5)
                continue
            account_id = await self._leader_account_id(session)
            self.leader_account_id = account_id
            if not account_id:
                self.error = f"leader account {self.group['leader']['spec']} has no broker account id — run Connect & Verify on the login"
                self._mark_feed(False)
                await asyncio.sleep(10)
                continue
            kind = self.group.get("feed") or "auto"
            use_ws = getattr(session, "kind", "tradovate") == "tradovate" and (kind == "websocket" or (kind == "auto" and not session.agent_id))
            self.feed_kind = "websocket" if use_ws else "poll"
            try:
                await self._seed_followers(force=not self._leader_seeded and not self._followers_seeded_at)
                await self._seed_leader(session, account_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.error = f"{type(exc).__name__}: {exc}"[:200]
                self._mark_feed(False)
                await asyncio.sleep(POLL_ERROR_SLEEP_S)
                continue
            ws_task = asyncio.create_task(self._ws_accelerator(session, account_id), name=f"copy-ws-{self.id}") if use_ws else None
            try:
                await self._run_poll(session, account_id)
            finally:
                if ws_task is not None:
                    ws_task.cancel()
                    try:
                        await ws_task
                    except (asyncio.CancelledError, Exception):  # noqa: BLE001
                        pass
                self.ws_ok = False

    async def _run_poll(self, session: Any, account_id: int) -> None:
        """Poll until stopped or until the login must be re-resolved. A 429 from
        Tradovate is a throttle, not a lost feed: the poll waits the penalty,
        slows down a step and speeds up again after a clean minute."""
        while not self._stop.is_set():
            if not session.enabled or not session.has_token():
                self.error = "leader login disabled or without token"
                self._mark_feed(False)
                return
            try:
                await self._poll_once(session, account_id)
                self._mark_feed(True)
                base = POLL_WS_INTERVAL_S if self.ws_ok else POLL_INTERVAL_S
                if self.poll_interval > base and time.monotonic() - self._last_429 > POLL_RECOVER_S:
                    self.poll_interval = max(base, self.poll_interval / 2)
                    self._last_429 = time.monotonic()
                elif self.poll_interval < base:
                    self.poll_interval = base
            except asyncio.CancelledError:
                raise
            except RateLimited as exc:
                self._last_429 = time.monotonic()
                self.poll_interval = min(POLL_MAX_INTERVAL_S, self.poll_interval * 2)
                self.throttled_until = time.monotonic() + exc.retry_after
                self.diag["last_429"] = f"{exc.path}: {exc.text[:160]}" if exc.text else f"{exc.path}: (empty body)"
                self.error = f"rate limited by the broker on {exc.path} — waiting {exc.retry_after:.0f} s, poll now every {self.poll_interval:.0f} s"
                self.last_frame = time.monotonic()        # the broker is reachable, just throttling
                if not self.feed_ok:
                    self._mark_feed(True)
                self.diag["rate_limits"] = int(self.diag.get("rate_limits") or 0) + 1
                await asyncio.sleep(exc.retry_after)
                continue
            except Exception as exc:  # noqa: BLE001
                self.error = f"{type(exc).__name__}: {exc}"[:200]
                self._mark_feed(False)
                await asyncio.sleep(POLL_ERROR_SLEEP_S)
                continue
            await self._sleep_poll()

    async def _sleep_poll(self) -> None:
        """Wait for the next poll — cut short the moment the socket drops, so the
        REST safety net takes over without a 10-second hole."""
        was_ok = self.ws_ok
        deadline = time.monotonic() + self.poll_interval
        while not self._stop.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0 or (was_ok and not self.ws_ok):
                return
            await asyncio.sleep(min(0.5, remaining))

    async def _ws_accelerator(self, session: Any, account_id: int) -> None:
        backoff = RECONNECT_BACKOFF_S
        while not self._stop.is_set():
            try:
                await self._run_ws(session, account_id)
                backoff = RECONNECT_BACKOFF_S
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                err = f"{type(exc).__name__}: {exc}"[:200]
                if self.ws_ok or err != self.ws_error:
                    self._record("ws_lost", detail=err)
                self.ws_error = err
                self.ws_ok = False
                await asyncio.sleep(backoff)
                backoff = min(60.0, backoff * 2)

    def _mark_feed(self, ok: bool) -> None:
        if ok and not self.feed_ok:
            self.feed_since = time.monotonic()
            self._record("feed_up", detail=self.feed_kind)
            self.error = ""
            self._last_error = ""
        elif ok and self.feed_ok and self.error.startswith("rate limited") and time.monotonic() >= self.throttled_until:
            self.error = ""
        elif not ok and self.feed_ok:
            self._record("feed_lost", detail=self.error or "connection closed")
            self._last_error = self.error
        elif not ok and self.error and self.error != self._last_error:
            self._record("feed_lost", detail=self.error)      # still down, new reason
            self._last_error = self.error
        self.feed_ok = ok
        if ok:
            self.last_frame = time.monotonic()

    async def _run_ws(self, session: Any, account_id: int) -> None:
        import websockets  # lazy: only groups on the WebSocket feed need it
        token = await session._get_token()
        user_id = await session.user_id()
        self.diag.update({"user_id": user_id, "leader_account_id": account_id, "sync": None})
        url = WS_URLS["live" if session.environment == "live" else "demo"]
        async with websockets.connect(url, ping_interval=None, open_timeout=15, close_timeout=5) as ws:
            first = await asyncio.wait_for(ws.recv(), 15)
            if not str(first).startswith("o"):
                raise TradovateError(f"unexpected opening frame {str(first)[:20]!r}")
            await asyncio.wait_for(ws.send(f"authorize\n1\n\n{token}"), 5)
            self.ws_last_frame = time.monotonic()
            sync_sent_at: Optional[float] = None

            async def heartbeat() -> None:
                # Tradovate drops a socket without "[]" every ~2.5 s — the beat must
                # never wait behind a mirror or an alert in the receive loop
                while True:
                    await asyncio.sleep(HEARTBEAT_S)
                    await asyncio.wait_for(ws.send("[]"), 5)
            beat = asyncio.create_task(heartbeat(), name=f"copy-ws-beat-{self.id}")
            try:
                await self._ws_loop(ws, session, account_id, user_id, sync_sent_at)
            finally:
                beat.cancel()
                try:
                    await beat
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass

    async def _ws_loop(self, ws: Any, session: Any, account_id: int, user_id: int, sync_sent_at: Optional[float]) -> None:
        if True:
            while not self._stop.is_set():
                now = time.monotonic()
                if sync_sent_at is not None and not self.ws_ok and now - sync_sent_at > WS_SYNC_TIMEOUT_S:
                    raise TradovateError("no answer to user/syncrequest")
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
                except asyncio.TimeoutError:
                    if time.monotonic() - self.ws_last_frame > FEED_STALE_S:
                        raise TradovateError("no frames from Tradovate")
                    continue
                self.ws_last_frame = time.monotonic()
                self.diag["frames"] = int(self.diag.get("frames") or 0) + 1
                text = str(raw)
                if text and text[0] not in ("h", "o"):
                    recent = self.diag.setdefault("recent", [])
                    recent.append(f"{datetime.now(timezone.utc).strftime('%H:%M:%S')} {text[:600]}")
                    del recent[:-WS_RECENT_MAX]
                for msg in parse_frames(text):
                    if msg.get("i") == 1 and msg.get("s") == 200 and sync_sent_at is None:
                        # authorised: only now ask for the user sync (requests sent
                        # before the authorize answer are dropped by Tradovate)
                        await asyncio.wait_for(ws.send("user/syncrequest\n2\n\n" + json.dumps({"users": [user_id]} if user_id else {})), 5)
                        sync_sent_at = time.monotonic()
                        continue
                    await self._on_ws_message(session, account_id, msg)

    async def _on_ws_message(self, session: Any, account_id: int, msg: dict[str, Any]) -> None:
        e = msg.get("e")
        if e == "close":
            raise TradovateError("Tradovate closed the socket")
        if e == "shutdown":
            raise TradovateError(f"Tradovate shutdown: {msg.get('d')}")
        if msg.get("i") == 1 and msg.get("s") not in (None, 200):
            raise TradovateError(f"authorize failed: {msg.get('d') or msg.get('s')}")
        if msg.get("i") == 2:
            d = msg.get("d")
            if msg.get("s") not in (None, 200) or not isinstance(d, dict):
                raise TradovateError(f"user sync failed: {msg.get('s')} {str(d)[:120]}")
            # initial snapshot: positions of all the user's accounts
            positions = d.get("positions") or []
            self.diag["sync"] = {"status": msg.get("s"), "keys": sorted(d.keys())[:20], "positions": len(positions),
                                 "accounts": [a.get("id") for a in (d.get("accounts") or []) if isinstance(a, dict)][:20]}
            if not self.ws_ok:
                self.ws_ok, self.ws_error = True, ""
                self._record("ws_up", detail=f"user sync: {len(positions)} position(s)")
            for p in positions:
                if int(p.get("accountId") or 0) == account_id:
                    cid_, net_ = int(p.get("contractId") or 0), int(p.get("netPos") or 0)
                    if cid_ not in self.leader_net and net_:
                        self.leader_net[cid_] = 0          # opened after the REST seed: an entry, mirror it
                    await self._on_position(session, cid_, net_)
            if self.orders.enabled:
                for o in d.get("orders") or []:
                    self.orders.on_entity("order", o)
                for v in d.get("orderVersions") or []:
                    self.orders.on_entity("orderversion", v)
                if d.get("orders") is not None:
                    await self.orders.apply(session, self.orders.entity_snapshot(account_id), self.orders.entity_statuses(account_id))
            return
        if e == "props":
            d = msg.get("d") if isinstance(msg.get("d"), dict) else {}
            etype = str(d.get("entityType") or "").lower()
            props = self.diag.setdefault("props", {})
            props[etype or "?"] = int(props.get(etype or "?") or 0) + 1
            ent = d.get("entity") if isinstance(d.get("entity"), dict) else d
            if etype in ("order", "orderversion"):
                if self.orders.on_entity(etype, ent):
                    self.orders.apply_soon(session, account_id)
                return
            if etype != "position":
                return
            self.diag["last_position_event"] = {k: ent.get(k) for k in ("accountId", "contractId", "netPos", "netPrice", "timestamp") if k in ent}
            if int(ent.get("accountId") or 0) == account_id and "netPos" in ent:
                await self._on_position(session, int(ent.get("contractId") or 0), int(ent.get("netPos") or 0))

    async def _poll_once(self, session: Any, account_id: int, *, source: str = "") -> int:
        """One REST look at the leader's orders and positions; mirrors every
        change found. Orders go first so the twins of a just-filled leader order
        are cancelled before the position mirror sizes the follower. Returns how
        many contracts changed position. While the socket is synced, a change
        the poll finds first is logged as ``ws_miss``."""
        if not source:
            source = "backstop" if self.ws_ok else "poll"
        self._poll_n += 1
        if self._poll_n % ORDERS_EVERY_N == 1 or ORDERS_EVERY_N == 1:
            await self.orders.poll(session, account_id)
        raw = await session.positions_snapshot()
        self.last_frame = time.monotonic()
        seen: set[int] = set()
        changed = 0
        for p in raw if isinstance(raw, list) else []:
            if int(p.get("accountId") or 0) != account_id:
                continue
            cid, net = int(p.get("contractId") or 0), int(p.get("netPos") or 0)
            seen.add(cid)
            if self.leader_net.get(cid, 0) != net:
                if not self._poll_confirmed(cid, net):
                    continue                     # a snapshot older than the last socket event: wait for a second look
                changed += 1
                if source != "poll":
                    self.diag["backstop_catches"] = int(self.diag.get("backstop_catches") or 0) + 1
                    self._record("ws_miss", symbol=await self._contract_name(session, cid),
                                 detail=f"{source}: leader {self.leader_net.get(cid, 0):+d} → {net:+d} not delivered by the socket")
                await self._on_position(session, cid, net)
            else:
                self._pending_poll.pop(cid, None)
        for cid in [c for c, n in self.leader_net.items() if n and c not in seen]:
            if not self._poll_confirmed(cid, 0):
                continue
            changed += 1
            if source != "poll":
                self._record("ws_miss", symbol=self.contract_names.get(cid, str(cid)),
                             detail=f"{source}: leader {self.leader_net.get(cid, 0):+d} → 0 not delivered by the socket")
            await self._on_position(session, cid, 0)
        return changed

    def _poll_confirmed(self, cid: int, net: int) -> bool:
        """While the socket is synced a REST snapshot can predate the socket event
        that was already applied; only a difference seen on two consecutive polls
        is acted on. Without the socket the poll is the feed and acts at once."""
        if not self.ws_ok:
            self._pending_poll.pop(cid, None)
            return True
        if self._pending_poll.get(cid) == net:
            self._pending_poll.pop(cid, None)
            return True
        self._pending_poll[cid] = net
        return False

    # ---- mirror
    async def _on_position(self, session: Any, cid: int, net: int, *, snapshot: bool = False) -> None:
        t0 = time.monotonic()
        prev = self.leader_net.get(cid, 0)
        if snapshot and cid not in self.leader_net and net:
            # first sight through the feed snapshot: same rule as at start
            self.leader_net[cid] = net
            self.unit[cid] = abs(net)
            self.baseline.add(cid)
            await self._contract_name(session, cid)
            return
        if net == prev:
            return
        self.leader_net[cid] = net             # claimed before any await: socket and poll never mirror twice
        if prev == 0 and net != 0:
            self.unit[cid] = abs(net)
        name = await self._contract_name(session, cid)
        self.last_event_ts = datetime.now(timezone.utc).isoformat()
        if cid in self.baseline:
            if net == 0:
                # the leader closed a position we never copied: nothing to mirror —
                # the followers may hold their own position in this contract
                self.baseline.discard(cid)
                self._record("ignored", symbol=name, detail=f"leader {prev:+d} → 0: existing position closed, not copied; mirroring starts with the next entry")
            else:
                self._record("ignored", symbol=name, detail=f"leader {prev:+d} → {net:+d}: existing position, not copied until flat or synced")
            return
        self._persist(cid)
        if not self._wanted(cid):
            if cid not in self._filtered:
                self._filtered.add(cid)
                self._record("filtered", symbol=name, detail=f"leader {prev:+d} → {net:+d}: {_base_root(name)} is not in the group's symbols")
            return
        if self.paused:
            self._record("skipped", symbol=name, detail=f"leader {prev:+d} → {net:+d}: group paused")
            return
        s = config.load_settings(area_id=self.area_id)
        if not s.get("trading_enabled"):
            self._record("skipped", symbol=name, detail=f"leader {prev:+d} → {net:+d}: trading switch is off")
            return
        await self._mirror_contract(cid, name, net, reason=f"leader {prev:+d} → {net:+d}", t0=t0)

    async def _mirror_contract(self, cid: int, name: str, net: int, *, reason: str, t0: Optional[float] = None) -> None:
        unit = self.unit.get(cid) or abs(net) or 1
        copy_adds = bool(self.group.get("copy_adds", True))
        results = await asyncio.gather(*(self._mirror_follower(f, cid, name, net, unit, copy_adds, reason, t0)
                                         for f in self.followers if f.get("enabled", True)), return_exceptions=True)
        for f, r in zip([f for f in self.followers if f.get("enabled", True)], results):
            if isinstance(r, BaseException) and not isinstance(r, asyncio.CancelledError):
                self.follower_err[f["spec"]] = f"{type(r).__name__}: {r}"[:200]
                self._record("reject", follower=f["spec"], symbol=name, detail=f"{reason}: {type(r).__name__}: {r}")

    async def _mirror_follower(self, f: dict[str, Any], cid: int, name: str, net: int, unit: int,
                               copy_adds: bool, reason: str, t0: Optional[float]) -> None:
        spec = f["spec"]
        lock = self.locks.setdefault(spec, asyncio.Lock())
        async with lock:
            target = target_qty(f, net, unit, copy_adds=copy_adds)
            ex = self._executor(f)
            if ex is None:
                self.follower_err[spec] = "login disabled or account gone"
                self._record("reject", follower=spec, symbol=name, detail="login disabled or account gone")
                return
            farea = self._area_of(f)
            if farea != self.area_id and not config.setting("trading_enabled", area_id=farea):
                self._record("skipped", follower=spec, symbol=name, detail=f"{reason}: trading switch is off in the follower's workspace")
                return
            if reason == "drift" and self.leader_net.get(cid, 0) != net:
                return                                  # the leader moved while we waited for the lock: the newer event handles it
            if news.flattened_lock(farea):
                # the follower's workspace is flat for a news event: the reconcile must not re-open it
                self._record("skipped", follower=spec, symbol=name, detail=f"{reason}: news lock (flatten) active in the follower's workspace")
                return
            if self.orders.touched_recently(spec, cid):
                # a twin on this contract may just have filled: cancel what is
                # still working and take the broker's position, not our memory
                await self.orders.cancel_all(reason="leader position changed", spec=spec, cid=cid)
                actual = await self._broker_net(ex, cid)
                if actual is not None:
                    self.follower_pos[(spec, cid)] = actual
            have = self.follower_pos.get((spec, cid), 0)
            delta = target - have
            if delta == 0:
                return
            with context.use_area(farea):
                try:
                    res = await ex.place_order(symbol=name, action="Buy" if delta > 0 else "Sell",
                                               qty=abs(delta), order_type="Market")
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - a follower failure must be visible, never swallowed
                    err = f"{exc}" if isinstance(exc, TradovateError) else f"{type(exc).__name__}: {exc}"
                    self.follower_err[spec] = err[:200]
                    self.follower_err_at[spec] = time.monotonic()
                    self._record("reject", follower=spec, symbol=name, detail=f"{reason}: {err}")
                    self._alert(f"Copy reject: {spec}", f"{name} {reason}: {err}", area_id=farea)
                    return
            if not isinstance(res, dict) or res.get("status") != "submitted":
                raw = res.get("raw") if isinstance(res, dict) else None
                err = str((raw or {}).get("errorText") or (res.get("status") if isinstance(res, dict) else res))
                self.follower_err[spec] = err[:200]
                self.follower_err_at[spec] = time.monotonic()
                self._record("reject", follower=spec, symbol=name, detail=f"{reason}: {err}")
                self._alert(f"Copy reject: {spec}", f"{name} {reason}: {err}", area_id=farea)
                return
            self.follower_pos[(spec, cid)] = target
            self.last_order_at[spec] = time.monotonic()
            self.follower_err.pop(spec, None)
            self.follower_err_at.pop(spec, None)
            latency = int((time.monotonic() - t0) * 1000) if t0 is not None else None
            self.last_latency_ms = latency if latency is not None else self.last_latency_ms
            self._record("mirror", follower=spec, symbol=name, latency_ms=latency,
                         detail=f"{reason} → {'Buy' if delta > 0 else 'Sell'} {abs(delta)} (now {target:+d})")

    async def _broker_net(self, ex: Any, cid: int) -> Optional[int]:
        """The follower's real net position for a contract (None if unreadable)."""
        if not int(getattr(ex, "id", 0) or 0):
            return None
        try:
            raw = await ex.session.positions_snapshot()
        except Exception:  # noqa: BLE001
            return None
        if not isinstance(raw, list):
            return None
        for p in raw:
            if int(p.get("accountId") or 0) == int(ex.id or 0) and int(p.get("contractId") or 0) == cid:
                return int(p.get("netPos") or 0)
        return 0

    # ---- reconcile / watchdog / actions
    async def _reconcile_loop(self) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(RECONCILE_INTERVAL_S)
            try:
                await self.reconcile()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.error = f"reconcile: {exc}"[:200]

    async def reconcile(self) -> int:
        """Compare followers' broker positions with what the mirror expects and
        fix drift with a market order. Returns the number of corrections."""
        if self.paused or not self.feed_ok:
            return 0
        fixes = 0
        session = self._leader_session()
        if session is not None and self.orders.enabled:
            try:
                fixes += await self.orders.reconcile(session, self.leader_account_id)
            except Exception as exc:  # noqa: BLE001
                self.orders.error = f"reconcile: {exc}"[:200]
        by_session: dict[tuple[int, str], list[dict[str, Any]]] = {}
        for f in self.followers:
            if f.get("enabled", True):
                by_session.setdefault((self._area_of(f), self._session_key(f)), []).append(f)
        for (farea, _skey), fs in by_session.items():
            s = self._session(fs[0])
            if s is None:
                continue
            ids = {self._follower_id(s, f): f for f in fs}
            ids.pop(0, None)                                    # an account without id is never "flat"
            fs = [f for f in fs if self._follower_id(s, f)]
            if not fs:
                continue
            try:
                raw = await s.positions_snapshot()
            except Exception:  # noqa: BLE001
                continue
            if not isinstance(raw, list):
                continue
            actual: dict[tuple[str, int], int] = {}
            for p in raw:
                f = ids.get(int(p.get("accountId") or 0))
                if f:
                    actual[(f["spec"], int(p.get("contractId") or 0))] = int(p.get("netPos") or 0)
            read_at = time.monotonic()
            for f in fs:
                spec = f["spec"]
                if time.monotonic() - self.follower_err_at.get(spec, -1e9) < REJECT_HOLDOFF_S:
                    continue                                    # just rejected: don't hammer the broker
                if risk.is_locked(farea, spec) or news.flattened_lock(farea):
                    continue                                    # the risk guard (or a news flatten) closed this account for now
                for cid, net in list(self.leader_net.items()):
                    if cid in self.baseline or not self._wanted(cid):
                        continue
                    if self.orders.twins_for(spec, cid):
                        continue                                # a working twin explains a size gap
                    key = (spec, cid)
                    lock = self.locks.setdefault(spec, asyncio.Lock())
                    async with lock:
                        # an order that went out after this snapshot was read, or just
                        # before it, is not yet in it: leave the follower alone for a moment
                        if self.last_order_at.get(spec, -1e9) > read_at - ORDER_SETTLE_S:
                            break
                        have = actual.get(key, 0)
                        self.follower_pos[key] = have           # trust the broker (under the lock)
                    target = target_qty(f, net, self.unit.get(cid) or abs(net) or 1, copy_adds=bool(self.group.get("copy_adds", True)))
                    if have != target:
                        name = self.contract_names.get(cid, str(cid))
                        self._record("drift", follower=spec, symbol=name, detail=f"broker {have:+d}, expected {target:+d} — correcting")
                        await self._mirror_follower(f, cid, name, net, self.unit.get(cid) or abs(net) or 1,
                                                    bool(self.group.get("copy_adds", True)), "drift", None)
                        fixes += 1
        return fixes

    async def watchdog(self) -> None:
        """Called every few seconds: flatten + pause after a long feed loss."""
        if self.paused or not self.tasks:
            return
        limit = float(self.group.get("feed_loss_flatten_s") or 30)
        if time.monotonic() < self.throttled_until:
            return                                              # a 429 penalty is a throttle, never a lost feed
        lost_for = (time.monotonic() - self.last_frame) if self.last_frame else 0.0
        stale = max(FEED_STALE_S, 3 * POLL_ERROR_SLEEP_S, self.poll_interval + FEED_STALE_S)
        if self.feed_ok and lost_for > stale:
            self.error = self.error or "poll stalled"
            self._mark_feed(False)
        if not self.feed_ok and self.last_frame and lost_for > limit:
            if str(self.group.get("on_feed_loss") or "flatten") == "pause":
                self.paused, self.pause_reason = True, f"feed lost for {int(lost_for)} s — mirroring paused (followers keep their positions); resume when the leader feed is back"
            else:
                n = await self.flatten_followers(reason=f"feed lost for {int(lost_for)} s")
                self.paused, self.pause_reason = True, f"feed lost for {int(lost_for)} s — followers flattened ({n} order(s)); resume when the leader feed is back"
            self._record("paused", detail=self.pause_reason)
            await alerts.copy_alert(f"Copy group paused: {self.group['name']}", self.pause_reason, email=True)

    async def flatten_followers(self, *, reason: str) -> int:
        """Cancel every twin, then close every mirrored contract on every follower
        (market). Returns orders sent."""
        sent = 0
        await self.orders.cancel_all(reason=f"flatten: {reason}")
        # only what the mirror opened: mirrored leader contracts plus contracts a twin
        # touched — never a follower's own, unrelated position
        contracts = {cid for cid, n in self.leader_net.items() if cid not in self.baseline}
        contracts |= {k[1] for k in self.orders.touched if k[1]} | {t["contract_id"] for t in self.orders.twins.values()}
        for f in self.followers:
            if not f.get("enabled", True):
                continue
            ex = self._executor(f)
            if ex is None:
                continue
            for cid in contracts:
                key = (f["spec"], cid)
                lock = self.locks.setdefault(f["spec"], asyncio.Lock())
                async with lock:
                    actual = await self._broker_net(ex, cid)
                    have = self.follower_pos.get(key, 0) if actual is None else actual
                    if not have:
                        self.follower_pos[key] = 0
                        continue
                    name = self.contract_names.get(cid, str(cid))
                    with context.use_area(self._area_of(f)):
                        try:
                            res = await ex.place_order(symbol=name, action="Sell" if have > 0 else "Buy", qty=abs(have), order_type="Market")
                        except asyncio.CancelledError:
                            raise
                        except Exception as exc:  # noqa: BLE001 - keep going with the other followers
                            self._record("reject", follower=f["spec"], symbol=name, detail=f"flatten ({reason}): {exc}")
                            continue
                    if isinstance(res, dict) and res.get("status") == "submitted":
                        self.follower_pos[key] = 0
                        self.last_order_at[f["spec"]] = time.monotonic()
                        sent += 1
                        self._record("flatten", follower=f["spec"], symbol=name, detail=f"{reason}: closed {have:+d}")
                    else:
                        self._record("reject", follower=f["spec"], symbol=name, detail=f"flatten ({reason}): not accepted ({res})")
        # what was flattened is not re-entered by the reconcile: it becomes baseline
        # until the leader is flat or the user syncs
        for cid in contracts:
            if self.leader_net.get(cid):
                self.baseline.add(cid)
                self._persist(cid)
        return sent

    async def sync_now(self) -> int:
        """Copy the leader's current positions right away (drops the baseline)."""
        self.baseline.clear()
        self.paused, self.pause_reason = False, ""
        n = 0
        for cid, net in list(self.leader_net.items()):
            self._persist(cid)
            if not self._wanted(cid) or not net:
                continue
            await self._mirror_contract(cid, self.contract_names.get(cid, str(cid)), net, reason="sync")
            n += 1
        self._record("resumed", detail="synced to the leader's current positions")
        return n

    def status(self) -> dict[str, Any]:
        followers = []
        for f in self.followers:
            rows = []
            for cid, net in self.leader_net.items():
                if not self._wanted(cid):
                    continue
                name = self.contract_names.get(cid, str(cid))
                target = 0 if cid in self.baseline else target_qty(f, net, self.unit.get(cid) or abs(net) or 1, copy_adds=bool(self.group.get("copy_adds", True)))
                have = self.follower_pos.get((f["spec"], cid), 0)
                rows.append({"symbol": name, "leader": net, "target": target, "actual": have, "baseline": cid in self.baseline})
            followers.append({"spec": f["spec"], "enabled": f.get("enabled", True), "error": self.follower_err.get(f["spec"], ""),
                              "positions": rows, "orders": self.orders.status(f["spec"]),
                              "area_id": self._area_of(f), "external": bool(f.get("external")), "sub_id": f.get("sub_id")})
        return {"id": self.id, "running": bool(self.tasks), "feed": self.feed_kind, "feed_ok": self.feed_ok,
                "ws_ok": self.ws_ok, "ws_error": self.ws_error,
                "poll_interval": self.poll_interval, "throttled": time.monotonic() < self.throttled_until,
                "paused": self.paused, "pause_reason": self.pause_reason, "error": self.error,
                "last_event_ts": self.last_event_ts, "latency_ms": self.last_latency_ms,
                "diag": {**self.diag, "leader_account_id": self.leader_account_id, "baseline": sorted(self.contract_names.get(c, str(c)) for c in self.baseline)},
                "orders_enabled": self.orders.enabled, "orders_error": self.orders.error,
                "leader_orders": self.orders.leader_status(),
                "leader_positions": [{"symbol": self.contract_names.get(c, str(c)), "net": n, "baseline": c in self.baseline}
                                     for c, n in self.leader_net.items() if n],
                "followers": followers}


# ---------------------------------------------------------------- manager
_runners: dict[tuple[int, str], GroupRunner] = {}


def reset() -> None:
    _runners.clear()


async def release_followers(publisher_area_id: int, group_id: str, specs: Any) -> int:
    """Accounts leaving a group (unsubscribe, kick, unpublish, a subscription
    edit) get their mirrored working orders cancelled first: a twin stop or
    limit would otherwise rest at the broker unmanaged and fill later, while
    the new runner no longer knows it. Positions are never touched. Returns
    the cancels sent."""
    r = _runners.get((publisher_area_id, group_id))
    specs = {str(s) for s in specs or () if s}
    if r is None or not specs:
        return 0
    n = 0
    with context.use_area(publisher_area_id):
        for spec in specs:
            try:
                n += await r.orders.cancel_all(reason="follower left the group", spec=spec)
            except Exception as exc:  # noqa: BLE001
                r.error = f"release {spec}: {exc}"[:200]
    return n


def _enabled_specs(accounts: Any) -> set[str]:
    return {str(a.get("spec")) for a in accounts or [] if isinstance(a, dict) and a.get("spec") and a.get("enabled", True)}


async def sync_area(area_id: int) -> None:
    """Start runners for enabled groups, stop the others, restart changed ones."""
    groups = {g["id"]: g for g in load_groups(area_id)}
    for key, r in list(_runners.items()):
        if key[0] != area_id:
            continue
        g = groups.get(key[1])
        if g is None or not g.get("enabled") or json.dumps(g, sort_keys=True) != r.fingerprint[0] \
                or json.dumps(external_followers(area_id, key[1], group=g), sort_keys=True) != r.fingerprint[1]:
            await r.stop()
            _runners.pop(key, None)
    for gid, g in groups.items():
        if g.get("enabled") and (area_id, gid) not in _runners:
            r = GroupRunner(area_id, g)
            _runners[(area_id, gid)] = r
            r.start()


def runner(area_id: int, group_id: str) -> Optional[GroupRunner]:
    return _runners.get((area_id, group_id))


def statuses(area_id: int) -> dict[str, dict[str, Any]]:
    return {gid: masked_status(r.status()) for (aid, gid), r in _runners.items() if aid == area_id}


_last_loop_error: dict[str, str] = {}


async def copy_loop() -> None:
    """Keep runners in line with the config and run the feed-loss watchdog."""
    while True:
        try:
            for aid in db.all_area_ids():
                with context.use_area(aid):
                    await sync_area(aid)
            for r in list(_runners.values()):
                try:
                    with context.use_area(r.area_id):
                        await r.watchdog()
                except Exception as exc:  # noqa: BLE001
                    r.error = f"watchdog: {exc}"[:200]
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - the loop must survive anything, but not silently
            if str(exc) != _last_loop_error.get("msg"):
                _last_loop_error["msg"] = str(exc)
                state.log_event("warn", f"copy loop: {exc}")
        await asyncio.sleep(5.0)


async def stop_all() -> None:
    for r in list(_runners.values()):
        await r.stop()
    _runners.clear()

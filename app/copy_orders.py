"""Copy trading, stage 2: mirror the leader's **working orders** onto followers.

Every working limit / stop / stop-limit order on the leader account gets a
*twin* on each follower, sized with the group's rule (multiplier or fixed,
proportional to the leader's position when one exists). Twins follow the
leader's modifications (price, quantity) and are cancelled the moment the
leader's order is no longer working — filled, cancelled, expired or replaced.
Two leader orders that cancel each other (a stop / target pair, ``ocoId``)
become one OCO pair on the follower, so the broker itself guarantees that a
filled follower stop cancels the follower target and vice versa.

The position mirror (:mod:`app.copy`) stays the source of truth for *size*:
when a leader order fills, its twins are cancelled first and the follower's
real broker position is read before the difference is sent as a market order,
so a twin that already filled is never doubled. Twins are persisted
(``copy_twins``) and verified against the broker after a restart, and a
reconcile pass every ten seconds re-creates missing twins, cancels orphans and
drops twins the broker no longer holds.

Not mirrored: market orders (they fill before we could copy them; the position
mirror covers the result), trailing stops and other exotic types (logged as
``order_skip``), orders on contracts outside the group's symbol filter, and
orders on *baseline* contracts (positions the leader held before the group
started — a copied stop without the position could open a trade).
"""
from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any, Optional

from . import config, context, db, leader_feed
from .tradovate import WORKING_STATUSES, RateLimited, TradovateError

if TYPE_CHECKING:  # pragma: no cover
    from .copy import GroupRunner

WORKING = {"Working"}
GONE = {"Filled", "Canceled", "Cancelled", "Rejected", "Expired", "Completed"}   # final: the twin goes
MIRRORED_TYPES = {"Limit", "Stop", "StopLimit"}
TOUCH_WINDOW_S = 30.0       # after a twin event the position mirror reads the broker's position
DONE_HOLD_S = 30.0          # a twin that filled / vanished at the broker is not re-created for this long


def _num(v: Any) -> Optional[float]:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


class OrderMirror:
    def __init__(self, runner: "GroupRunner") -> None:
        self.r = runner
        self.enabled = bool(runner.group.get("copy_orders"))
        self.leader_orders: dict[int, dict[str, Any]] = {}      # leader order id → snapshot
        self.twins: dict[tuple[str, int], dict[str, Any]] = {}  # (spec, leader order id) → twin
        self.touched: dict[tuple[str, int], float] = {}         # (spec, contract) → monotonic
        self.error = ""
        self._seeded = False
        self.ent_orders: dict[int, dict[str, Any]] = {}       # socket: order entities by id
        self.ent_versions: dict[int, dict[str, Any]] = {}     # socket: latest orderVersion by order id
        self._apply_task: Optional[asyncio.Task] = None
        self._rest_missing: set[int] = set()                     # leader orders one REST look already missed (socket synced)
        self._skip_said: dict[tuple[str, int], str] = {}         # (follower, leader order) → last skip reason recorded
        self._apply_lock = asyncio.Lock()                      # poll, socket and reconcile never diff concurrently
        self._dirty = False                                    # socket events arrived while an apply ran
        self._done_at: dict[tuple[str, int], float] = {}       # (spec, leader order id) → twin found done (monotonic)

    # ------------------------------------------------------------ helpers
    def _twin_key(self, spec: str, leader_order_id: int) -> tuple[str, int]:
        return (spec, int(leader_order_id))

    def touched_recently(self, spec: str, cid: int) -> bool:
        return any(k[1] == cid and k[0] == spec for k in self.twins) or \
            time.monotonic() - self.touched.get((spec, cid), -1e9) < TOUCH_WINDOW_S

    def _touch(self, spec: str, cid: int) -> None:
        self.touched[(spec, cid)] = time.monotonic()

    def twins_for(self, spec: str, cid: Optional[int] = None) -> list[dict[str, Any]]:
        return [t for (s, _), t in self.twins.items() if s == spec and (cid is None or t["contract_id"] == cid)]

    def twin_qty(self, f: dict[str, Any], order: dict[str, Any]) -> int:
        """Follower quantity for a leader order: the group's sizing rule applied
        proportionally (leader position when one exists, else the order itself)."""
        from .copy import _round_half_up, target_qty
        qty = int(order.get("qty") or 0)
        if qty <= 0:
            return 0
        cid = int(order.get("contract_id") or 0)
        net = int(self.r.leader_net.get(cid, 0))
        if net == 0:
            sign = 1 if order.get("action") == "Buy" else -1
            return abs(target_qty(f, sign * qty, qty, copy_adds=bool(self.r.group.get("copy_adds", True))))
        unit = self.r.unit.get(cid) or abs(net) or 1
        follower_size = abs(target_qty(f, net, unit, copy_adds=bool(self.r.group.get("copy_adds", True))))
        if follower_size == 0:
            return 0
        return max(1, _round_half_up(qty * follower_size / abs(net)))

    def _record(self, kind: str, **kw: Any) -> None:
        self.r._record(kind, **kw)

    def _skip_once(self, spec: str, o: dict[str, Any], name: str, why: str) -> None:
        """An ``order_skip`` row once per (follower, leader order, reason): the
        reconcile revisits every unmirrored order every few seconds and would
        otherwise write thousands of identical rows a day."""
        key = (spec, int(o["id"]))
        if self._skip_said.get(key) == why:
            return
        self._skip_said[key] = why
        if len(self._skip_said) > 2000:
            self._skip_said = {k: v for k, v in self._skip_said.items() if k[1] in self.leader_orders}
        self._record("order_skip", follower=spec, symbol=name, detail=f"leader {o['action']} {o['qty']} {o['order_type']}: {why}")

    # ------------------------------------------------------------ persist
    async def load(self) -> None:
        """Twins from the database, verified against the followers' working orders."""
        if not self.enabled or self._seeded:
            return
        self._seeded = True
        rows = db.list_copy_twins(self.r.area_id, self.r.id)
        if not rows:
            return
        working = await self._follower_working()
        for row in rows:
            spec = row["spec"]
            alive = working.get(spec)
            if alive is not None and int(row["follower_order_id"]) not in alive:
                db.delete_copy_twin(self.r.area_id, self.r.id, spec, int(row["leader_order_id"]))
                continue
            self.twins[self._twin_key(spec, row["leader_order_id"])] = {
                "leader_order_id": int(row["leader_order_id"]), "follower_order_id": int(row["follower_order_id"]),
                "contract_id": int(row["contract_id"] or 0), "symbol": row["symbol"], "action": row["action"],
                "qty": int(row["qty"] or 0), "order_type": row["order_type"], "price": row["price"],
                "stop_price": row["stop_price"], "version_id": int(row["version_id"] or 0), "oco_with": int(row["oco_with"] or 0)}

    def _save(self, spec: str, t: dict[str, Any]) -> None:
        self.twins[self._twin_key(spec, t["leader_order_id"])] = t
        db.save_copy_twin(self.r.area_id, self.r.id, spec, t["leader_order_id"], t["follower_order_id"],
                          **{k: t.get(k) for k in ("contract_id", "symbol", "action", "qty", "order_type", "price", "stop_price", "version_id", "oco_with")})

    def _drop(self, spec: str, leader_order_id: int) -> None:
        self.twins.pop(self._twin_key(spec, leader_order_id), None)
        db.delete_copy_twin(self.r.area_id, self.r.id, spec, leader_order_id)

    async def _follower_working(self) -> dict[str, set[int]]:
        """spec → ids of the follower's working orders (specs whose login failed
        are absent). Followers on the same Tradovate login share one
        ``/order/list`` — five followers used to cost five identical requests."""
        out: dict[str, set[int]] = {}
        by_session: dict[int, tuple[Any, list[tuple[dict[str, Any], Any]]]] = {}
        for f in self.r.followers:
            ex = self.r._executor(f)
            if ex is None:
                continue
            sess = getattr(ex, "session", None)
            if sess is not None and getattr(sess, "kind", "tradovate") == "tradovate" and hasattr(sess, "orders_snapshot") and getattr(ex, "id", 0):
                by_session.setdefault(id(sess), (sess, []))[1].append((f, ex))
                continue
            try:
                out[f["spec"]] = {int(o["id"]) for o in await ex.working_orders() if o.get("id")}
            except Exception:  # noqa: BLE001
                continue
        for sess, items in by_session.values():
            if len(items) == 1:
                f, ex = items[0]
                try:
                    out[f["spec"]] = {int(o["id"]) for o in await ex.working_orders() if o.get("id")}
                except Exception:  # noqa: BLE001
                    pass
                continue
            try:
                raw = await sess.orders_snapshot()
            except Exception:  # noqa: BLE001
                continue
            for f, ex in items:
                out[f["spec"]] = {int(o["id"]) for o in raw or []
                                  if o.get("id") and o.get("ordStatus") in WORKING_STATUSES and o.get("accountId") == ex.id}
        return out

    # --------------------------------------------------------- leader feed
    async def snapshot(self, session: Any, account_id: int) -> tuple[dict[int, dict[str, Any]], dict[int, str]]:
        """The leader's working orders with their latest version, plus the status
        of every order of the account (working, in transition or final)."""
        raw, _shared = await leader_feed.snapshot(self.r.area_id, session, "orders")
        mine = [o for o in raw if int(o.get("accountId") or 0) == account_id and o.get("id")]
        statuses = {int(o["id"]): str(o.get("ordStatus") or "") for o in mine}
        orders = [o for o in mine if statuses[int(o["id"])] in WORKING]
        if not orders:
            return {}, statuses
        versions = await session.order_versions([int(o["id"]) for o in orders]) if hasattr(session, "order_versions") else {}
        out: dict[int, dict[str, Any]] = {}
        for o in orders:
            oid = int(o["id"])
            v = versions.get(oid) or {}
            out[oid] = {"id": oid, "contract_id": int(o.get("contractId") or 0), "action": str(o.get("action") or ""),
                        "qty": int(v.get("orderQty") or 0), "order_type": str(v.get("orderType") or ""),
                        "price": _num(v.get("price")), "stop_price": _num(v.get("stopPrice")),
                        "version_id": int(v.get("id") or 0), "oco_id": int(o.get("ocoId") or 0)}
        return out, statuses

    async def poll(self, session: Any, account_id: int) -> None:
        """REST look at the leader's working orders, then act on the difference."""
        if not self.enabled:
            return
        try:
            now, statuses = await self.snapshot(session, account_id)
        except RateLimited:
            raise                                   # the poll loop waits the penalty
        except Exception as exc:  # noqa: BLE001
            self.error = f"orders: {exc}"[:200]
            return
        self.error = ""
        if self.r.ws_ok:
            # a REST list can predate an order the socket just delivered (and whose
            # twin is already resting): an order missing from the list entirely is
            # only "gone" when two consecutive looks miss it
            known = set(self.leader_orders) | {key[1] for key in self.twins}
            missing = {i for i in known if i not in now and statuses.get(i) is None}
            unconfirmed = missing - self._rest_missing
            self._rest_missing = missing
            for i in unconfirmed:
                if i in self.leader_orders:
                    now[i] = self.leader_orders[i]      # keep it one more look
                else:
                    statuses[i] = "Working"             # a twin's leader order not yet in our list: not gone either
        else:
            self._rest_missing = set()
        await self.apply(session, now, statuses)

    # ---- socket entities (user sync snapshot + props events)
    def on_entity(self, kind: str, ent: dict[str, Any]) -> bool:
        """Merge an ``order`` / ``orderVersion`` entity from the socket. Returns
        True when it belongs to a tracked order (→ apply soon)."""
        if not self.enabled or not isinstance(ent, dict):
            return False
        if kind == "order":
            oid = int(ent.get("id") or 0)
            if not oid:
                return False
            self.ent_orders[oid] = ent
            if str(ent.get("ordStatus") or "") in GONE:
                # keep the final status for one apply (the twin is cancelled), then forget the order
                self.ent_versions.pop(oid, None)
            return True
        if kind == "orderversion":
            oid = int(ent.get("orderId") or 0)
            if not oid:
                return False
            if str((self.ent_orders.get(oid) or {}).get("ordStatus") or "") in GONE:
                return False                        # a version for an order that is already final
            cur = self.ent_versions.get(oid)
            if cur is None or int(ent.get("id") or 0) >= int(cur.get("id") or 0):
                self.ent_versions[oid] = ent
            return True
        return False

    def prune_entities(self) -> None:
        """Forget final orders that no twin / tracked leader order refers to any more."""
        for oid in [i for i, o in self.ent_orders.items() if str(o.get("ordStatus") or "") in GONE]:
            if oid not in self.leader_orders and not any(k[1] == oid for k in self.twins):
                self.ent_orders.pop(oid, None)
                self.ent_versions.pop(oid, None)
        for oid in [i for i in self.ent_versions if i not in self.ent_orders]:
            self.ent_versions.pop(oid, None)

    def entity_statuses(self, account_id: int) -> dict[int, str]:
        return {oid: str(o.get("ordStatus") or "") for oid, o in self.ent_orders.items()
                if int(o.get("accountId") or 0) == account_id}

    def entity_snapshot(self, account_id: int) -> dict[int, dict[str, Any]]:
        """The leader's working orders as the socket entities describe them."""
        out: dict[int, dict[str, Any]] = {}
        for oid, o in self.ent_orders.items():
            if int(o.get("accountId") or 0) != account_id or str(o.get("ordStatus")) not in WORKING:
                continue
            v = self.ent_versions.get(oid) or {}
            out[oid] = {"id": oid, "contract_id": int(o.get("contractId") or 0), "action": str(o.get("action") or ""),
                        "qty": int(v.get("orderQty") or 0), "order_type": str(v.get("orderType") or ""),
                        "price": _num(v.get("price")), "stop_price": _num(v.get("stopPrice")),
                        "version_id": int(v.get("id") or 0), "oco_id": int(o.get("ocoId") or 0)}
        return out

    def apply_soon(self, session: Any, account_id: int, delay: float = 0.25) -> None:
        """Apply the socket's order state shortly — an order and its version
        arrive as separate events, the delay lets both land first. Events that
        arrive while an apply runs are picked up by one more pass."""
        self._dirty = True
        if self._apply_task is not None and not self._apply_task.done():
            return

        async def run() -> None:
            while self._dirty and not self.r._stop.is_set():
                await asyncio.sleep(delay)
                self._dirty = False
                if self.r._stop.is_set():
                    return
                try:
                    await self.apply(session, self.entity_snapshot(account_id), self.entity_statuses(account_id))
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    self.error = f"orders (socket): {exc}"[:200]
        self._apply_task = asyncio.create_task(run(), name=f"copy-orders-apply-{self.r.id}")

    async def apply(self, session: Any, now: dict[int, dict[str, Any]], statuses: Optional[dict[int, str]] = None) -> None:
        """Diff a fresh picture of the leader's working orders against the last one.

        ``statuses`` (order id → ordStatus for every order of the account) tells
        a leader order that is merely *in transition* (PendingReplace,
        PendingCancel, Suspended …) from one that is final or gone: twins of an
        order in transition are kept, and the order itself is carried over
        until it is working again or final. Only one apply runs at a time."""
        async with self._apply_lock:
            prev = self.leader_orders
            fresh: dict[int, dict[str, Any]] = {}
            for i, o in now.items():
                if o["order_type"]:
                    fresh[i] = o
                elif i in prev:
                    fresh[i] = prev[i]              # version not (yet) known: keep what we know
                # else: a brand-new order whose version has not arrived — wait for it
            transitional: set[int] = set()
            if statuses is not None:
                for i in set(prev) | {key[1] for key in self.twins}:
                    st = statuses.get(i)
                    if i not in fresh and st is not None and st not in WORKING and st not in GONE:
                        transitional.add(i)
                        if i in prev:
                            fresh[i] = prev[i]
            now = fresh
            # every twin whose leader order is no longer working goes — including
            # twins restored from the database after a restart
            gone_ids = sorted(({key[1] for key in self.twins if key[1] not in now} | {i for i in prev if i not in now}) - transitional)
            new = [now[i] for i in now if i not in prev]
            changed = [now[i] for i in now if i in prev and (now[i]["version_id"] != prev[i]["version_id"]
                                                             or (now[i]["qty"], now[i]["price"], now[i]["stop_price"]) != (prev[i]["qty"], prev[i]["price"], prev[i]["stop_price"]))]
            self.leader_orders = now
            for oid in gone_ids:
                await self.cancel_twins(oid, reason="leader order gone")
            self.prune_entities()
            if not prev and not self.r._leader_seeded:
                return
            await self._create(session, new)
            for o in changed:
                await self._modify(o)

    # ------------------------------------------------------------ actions
    def _blocked(self, cid: int) -> Optional[str]:
        if cid in self.r.baseline:
            return "baseline contract"
        if not self.r._wanted(cid):
            return "symbol not in the group"
        if self.r.paused:
            return "group paused"
        if not config.setting("trading_enabled", area_id=self.r.area_id):
            return "trading switch is off"
        return None

    async def _create(self, session: Any, orders: list[dict[str, Any]]) -> int:
        """Twins for new leader orders on every enabled follower. Returns twins placed."""
        if not orders:
            return 0
        created = 0
        # pair OCO legs (a.oco_id == b.id or b.oco_id == a.id)
        by_id = {o["id"]: o for o in orders}
        done: set[int] = set()
        for o in orders:
            if o["id"] in done:
                continue
            partner = by_id.get(o["oco_id"]) if o["oco_id"] and o["oco_id"] in by_id and o["oco_id"] not in done else None
            if partner is None:
                partner = next((p for p in orders if p["id"] not in done and p["id"] != o["id"] and p["oco_id"] == o["id"]), None)
            done.add(o["id"])
            if partner is not None:
                done.add(partner["id"])
            name = await self.r._contract_name(session, o["contract_id"])
            why = self._blocked(o["contract_id"])
            if why:
                self._skip_once("", o, name, why)
                continue
            if o["order_type"] not in MIRRORED_TYPES or (partner is not None and partner["order_type"] not in MIRRORED_TYPES):
                self._skip_once("", o, name, "order type not mirrored")
                continue
            res = await asyncio.gather(*(self._create_for(f, o, partner, name) for f in self.r.followers if f.get("enabled", True)),
                                       return_exceptions=True)
            created += sum(r for r in res if isinstance(r, int))
        return created

    def _held_back(self, spec: str, leader_order_id: int) -> bool:
        """A twin that filled / vanished at the broker moments ago is not re-created
        right away: the position mirror is about to read the broker's position."""
        now = time.monotonic()
        for k in [k for k, t in self._done_at.items() if now - t >= DONE_HOLD_S]:
            self._done_at.pop(k, None)
        return now - self._done_at.get(self._twin_key(spec, leader_order_id), -1e9) < DONE_HOLD_S

    async def _create_for(self, f: dict[str, Any], o: dict[str, Any], partner: Optional[dict[str, Any]], name: str) -> int:
        """One twin (or OCO pair) for one follower. Returns twins placed."""
        spec = f["spec"]
        if self._twin_key(spec, o["id"]) in self.twins or self._held_back(spec, o["id"]):
            return 0
        qty = self.twin_qty(f, o)
        if qty <= 0:
            self._skip_once(spec, o, name, "sizing gives 0 (direction / cap)")
            return 0
        if partner is not None:
            pq = self.twin_qty(f, partner)
            if self._twin_key(spec, partner["id"]) in self.twins or self._held_back(spec, partner["id"]) or pq <= 0:
                if pq <= 0:
                    self._record("order_skip", follower=spec, symbol=name, detail=f"leader {partner['action']} {partner['qty']} {partner['order_type']}: sizing gives 0 (direction / cap)")
                partner = None                      # the partner leg is already there or not wanted: single order
            elif pq != qty:
                # an OCO pair must share one quantity at the broker: legs of different
                # size become two independent twins (the leader's own legs differ too)
                self._record("order_skip", follower=spec, symbol=name,
                             detail=f"leader OCO legs size to {qty} and {pq} contracts: mirrored as two independent orders")
                n = await self._create_for(f, o, None, name)
                return n + await self._create_for(f, partner, None, name)
        farea = self.r._area_of(f)
        if farea != self.r.area_id and not config.setting("trading_enabled", area_id=farea):
            self._skip_once(spec, o, name, "trading switch is off in the follower's workspace")
            return 0
        ex = self.r._executor(f)
        if ex is None:
            self._record("order_reject", follower=spec, symbol=name, detail="login disabled or account gone")
            return 0
        lock = self.r.locks.setdefault(spec, asyncio.Lock())
        async with lock:
            if self._twin_key(spec, o["id"]) in self.twins:
                return 0                            # placed by a concurrent pass while we waited
            # shielded: a runner stop (group edit, restart of the feed) must not cancel
            # the request after the broker accepted it and before the twin is recorded
            return await asyncio.shield(self._place_twin(f, o, partner, name, ex, farea, qty, pq if partner is not None else 0))

    async def _place_twin(self, f: dict[str, Any], o: dict[str, Any], partner: Optional[dict[str, Any]], name: str,
                          ex: Any, farea: int, qty: int, pq: int) -> int:
        spec = f["spec"]
        with context.use_area(farea):
            try:
                if partner is None:
                    res = await ex.place_order(symbol=name, action=o["action"], qty=qty, order_type=o["order_type"],
                                               price=o["price"], stop_price=o["stop_price"])
                    ids = [(o, int(res.get("order_id") or 0))] if res.get("status") == "submitted" else []
                    err = None if ids else str((res.get("raw") or {}).get("errorText") or res.get("status"))
                else:
                    res = await ex.place_oco(symbol=name, action=o["action"], qty=qty, order_type=o["order_type"],
                                             price=o["price"], stop_price=o["stop_price"],
                                             other={"action": partner["action"], "order_type": partner["order_type"],
                                                    "price": partner["price"], "stop_price": partner["stop_price"]})
                    ok = res.get("status") == "submitted"
                    ids = [(o, int(res.get("order_id") or 0)), (partner, int(res.get("oco_id") or 0))] if ok else []
                    err = None if ok else str((res.get("raw") or {}).get("errorText") or res.get("status"))
                    qty_by = {o["id"]: qty, partner["id"]: pq}
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                err, ids = (f"{exc}" if isinstance(exc, TradovateError) else f"{type(exc).__name__}: {exc}"), []
        if err is not None:
            self.r.follower_err[spec] = err[:200]
            self.r.follower_err_at[spec] = time.monotonic()
            self._record("order_reject", follower=spec, symbol=name, detail=f"leader {o['action']} {o['qty']} {o['order_type']}: {err}")
            return 0
        for lo, fid in ids:
            t = {"leader_order_id": lo["id"], "follower_order_id": fid, "contract_id": lo["contract_id"], "symbol": name,
                 "action": lo["action"], "qty": qty if partner is None else qty_by[lo["id"]], "order_type": lo["order_type"],
                 "price": lo["price"], "stop_price": lo["stop_price"], "version_id": lo["version_id"],
                 "oco_with": (partner["id"] if lo is o else o["id"]) if partner is not None else 0}
            self._save(spec, t)
            self._touch(spec, lo["contract_id"])
            px = f" @ {lo['price']}" if lo["price"] is not None else ""
            sp = f" stop {lo['stop_price']}" if lo["stop_price"] is not None else ""
            self._record("order_mirror", follower=spec, symbol=name,
                         detail=f"{lo['action']} {t['qty']} {lo['order_type']}{px}{sp} (leader {lo['qty']}{', OCO' if partner is not None else ''})")
        return len(ids)

    async def _modify(self, o: dict[str, Any]) -> None:
        name = self.r.contract_names.get(o["contract_id"], str(o["contract_id"]))
        for f in self.r.followers:
            spec = f["spec"]
            t = self.twins.get(self._twin_key(spec, o["id"]))
            if t is None:
                continue
            qty = self.twin_qty(f, o) or t["qty"]
            if (qty, o["price"], o["stop_price"]) == (t["qty"], t["price"], t["stop_price"]):
                t["version_id"] = o["version_id"]
                continue
            ex = self.r._executor(f)
            if ex is None:
                continue
            with context.use_area(self.r._area_of(f)):
                try:
                    await ex.modify_order(t["follower_order_id"], qty=qty, order_type=o["order_type"], price=o["price"], stop_price=o["stop_price"])
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    self._record("order_reject", follower=spec, symbol=name, detail=f"modify {t['action']} {o['order_type']}: {exc}")
                    continue
            t.update({"qty": qty, "price": o["price"], "stop_price": o["stop_price"], "version_id": o["version_id"]})
            self._save(spec, t)
            self._touch(spec, o["contract_id"])
            px = f" @ {o['price']}" if o["price"] is not None else ""
            sp = f" stop {o['stop_price']}" if o["stop_price"] is not None else ""
            self._record("order_modify", follower=spec, symbol=name, detail=f"{t['action']} {qty} {o['order_type']}{px}{sp}")

    async def cancel_twins(self, leader_order_id: int, *, reason: str, spec_only: Optional[str] = None) -> int:
        """Cancel every follower twin of one leader order. Returns cancels sent."""
        n = 0
        for f in self.r.followers:
            spec = f["spec"]
            if spec_only and spec != spec_only:
                continue
            t = self.twins.get(self._twin_key(spec, leader_order_id))
            if t is None:
                continue
            ex = self.r._executor(f)
            if ex is None:
                self._record("order_reject", follower=spec, symbol=t["symbol"],
                             detail=f"{t['action']} {t['qty']} {t['order_type']}: login disabled — the twin stays at the broker until the login is back")
                continue
            with context.use_area(self.r._area_of(f)):
                try:
                    await ex.cancel_order(t["follower_order_id"])
                    n += 1
                    self._record("order_cancel", follower=spec, symbol=t["symbol"], detail=f"{t['action']} {t['qty']} {t['order_type']}: {reason}")
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    # already filled / cancelled at the broker is fine — but a twin that is
                    # still working must not be forgotten (a stray order would sit at the broker)
                    still = await self._still_working(ex, t["follower_order_id"])
                    if still is not False:
                        self.r.follower_err[spec] = f"cancel failed: {exc}"[:200]
                        self.r.follower_err_at[spec] = time.monotonic()
                        self._record("order_reject", follower=spec, symbol=t["symbol"],
                                     detail=f"{t['action']} {t['qty']} {t['order_type']}: cancel failed ({exc}) — order still working, retrying")
                        continue
                    self._record("order_cancel", follower=spec, symbol=t["symbol"], detail=f"{t['action']} {t['qty']} {t['order_type']}: {reason} (broker: {exc})")
            self._drop(spec, leader_order_id)
            self._touch(spec, t["contract_id"])
        return n

    @staticmethod
    async def _still_working(ex: Any, order_id: int) -> Optional[bool]:
        """True / False when the broker answered, None when it did not."""
        try:
            return any(int(o.get("id") or 0) == order_id for o in await ex.working_orders())
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            return None

    async def cancel_all(self, *, reason: str, spec: Optional[str] = None, cid: Optional[int] = None) -> int:
        n = 0
        for key, t in list(self.twins.items()):
            if (spec and key[0] != spec) or (cid is not None and t["contract_id"] != cid):
                continue
            n += await self.cancel_twins(key[1], reason=reason, spec_only=key[0])
        return n

    # ---------------------------------------------------------- reconcile
    async def reconcile(self, session: Any, account_id: int) -> int:
        """Twins the broker no longer holds are dropped; twins whose leader order
        is gone are cancelled; missing twins are created. Returns actions."""
        if not self.enabled or (not self.twins and not self.leader_orders):
            return 0                                # nothing to compare: no broker round-trip
        actions = 0
        working = await self._follower_working()
        for key, t in list(self.twins.items()):
            spec = key[0]
            alive = working.get(spec)
            if alive is None:
                continue
            if t["follower_order_id"] not in alive:
                self._record("order_done", follower=spec, symbol=t["symbol"], detail=f"{t['action']} {t['qty']} {t['order_type']}: no longer working at the broker (filled or cancelled)")
                self._drop(spec, key[1])
                self._touch(spec, t["contract_id"])
                if key[1] in self.leader_orders:
                    self._done_at[key] = time.monotonic()      # not re-created for DONE_HOLD_S
                actions += 1
            elif key[1] not in self.leader_orders:
                actions += await self.cancel_twins(key[1], reason="orphan: leader order gone", spec_only=spec)
        missing = [o for o in self.leader_orders.values()
                   if any(self._twin_key(f["spec"], o["id"]) not in self.twins and not self._held_back(f["spec"], o["id"])
                          for f in self.r.followers if f.get("enabled", True))]
        if missing:
            actions += await self._create(session, missing)
        return actions

    def status(self, spec: Optional[str] = None) -> list[dict[str, Any]]:
        return [{"symbol": t["symbol"], "action": t["action"], "qty": t["qty"], "type": t["order_type"], "price": t["price"],
                 "stop": t["stop_price"], "leader_order_id": t["leader_order_id"], "follower_order_id": t["follower_order_id"],
                 "oco": bool(t["oco_with"])}
                for (s, _), t in self.twins.items() if spec is None or s == spec]

    def leader_status(self) -> list[dict[str, Any]]:
        return [{"id": o["id"], "symbol": self.r.contract_names.get(o["contract_id"], str(o["contract_id"])), "action": o["action"],
                 "qty": o["qty"], "type": o["order_type"], "price": o["price"], "stop": o["stop_price"], "oco": bool(o["oco_id"])}
                for o in self.leader_orders.values()]

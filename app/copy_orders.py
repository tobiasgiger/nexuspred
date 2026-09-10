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

from . import context, db
from .tradovate import RateLimited, TradovateError

if TYPE_CHECKING:  # pragma: no cover
    from .copy import GroupRunner

WORKING = {"Working"}
MIRRORED_TYPES = {"Limit", "Stop", "StopLimit"}
TOUCH_WINDOW_S = 30.0       # after a twin event the position mirror reads the broker's position


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
        """spec → ids of the follower's working orders (specs whose login failed are absent)."""
        out: dict[str, set[int]] = {}
        for f in self.r.group["followers"]:
            ex = self.r._executor(f)
            if ex is None:
                continue
            try:
                out[f["spec"]] = {int(o["id"]) for o in await ex.working_orders() if o.get("id")}
            except Exception:  # noqa: BLE001
                continue
        return out

    # --------------------------------------------------------- leader feed
    async def snapshot(self, session: Any, account_id: int) -> dict[int, dict[str, Any]]:
        """The leader's working orders with their latest version."""
        raw = await session._request("GET", "/order/list") or []
        orders = [o for o in raw if isinstance(o, dict) and int(o.get("accountId") or 0) == account_id
                  and str(o.get("ordStatus")) in WORKING]
        if not orders:
            return {}
        versions = await session.order_versions([int(o["id"]) for o in orders]) if hasattr(session, "order_versions") else {}
        out: dict[int, dict[str, Any]] = {}
        for o in orders:
            oid = int(o["id"])
            v = versions.get(oid) or {}
            out[oid] = {"id": oid, "contract_id": int(o.get("contractId") or 0), "action": str(o.get("action") or ""),
                        "qty": int(v.get("orderQty") or 0), "order_type": str(v.get("orderType") or ""),
                        "price": _num(v.get("price")), "stop_price": _num(v.get("stopPrice")),
                        "version_id": int(v.get("id") or 0), "oco_id": int(o.get("ocoId") or 0)}
        return out

    async def poll(self, session: Any, account_id: int) -> None:
        """Diff the leader's working orders against the last look and act."""
        if not self.enabled:
            return
        try:
            now = await self.snapshot(session, account_id)
        except RateLimited:
            raise                                   # the poll loop waits the penalty
        except Exception as exc:  # noqa: BLE001
            self.error = f"orders: {exc}"[:200]
            return
        self.error = ""
        prev = self.leader_orders
        # every twin whose leader order is no longer working goes — including
        # twins restored from the database after a restart
        gone_ids = sorted({key[1] for key in self.twins if key[1] not in now} | {i for i in prev if i not in now})
        new = [now[i] for i in now if i not in prev]
        changed = [now[i] for i in now if i in prev and (now[i]["version_id"] != prev[i]["version_id"]
                                                         or (now[i]["qty"], now[i]["price"], now[i]["stop_price"]) != (prev[i]["qty"], prev[i]["price"], prev[i]["stop_price"]))]
        self.leader_orders = now
        for oid in gone_ids:
            await self.cancel_twins(oid, reason="leader order gone")
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
        from . import config
        if not config.load_settings(area_id=self.r.area_id).get("trading_enabled"):
            return "trading switch is off"
        return None

    async def _create(self, session: Any, orders: list[dict[str, Any]]) -> None:
        if not orders:
            return
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
                self._record("order_skip", symbol=name, detail=f"leader {o['action']} {o['qty']} {o['order_type']}: {why}")
                continue
            if o["order_type"] not in MIRRORED_TYPES or (partner is not None and partner["order_type"] not in MIRRORED_TYPES):
                self._record("order_skip", symbol=name, detail=f"leader {o['action']} {o['qty']} {o['order_type']}: order type not mirrored")
                continue
            await asyncio.gather(*(self._create_for(f, o, partner, name) for f in self.r.group["followers"] if f.get("enabled", True)),
                                 return_exceptions=True)

    async def _create_for(self, f: dict[str, Any], o: dict[str, Any], partner: Optional[dict[str, Any]], name: str) -> None:
        spec = f["spec"]
        if self._twin_key(spec, o["id"]) in self.twins:
            return
        qty = self.twin_qty(f, o)
        if qty <= 0:
            self._record("order_skip", follower=spec, symbol=name, detail=f"leader {o['action']} {o['qty']} {o['order_type']}: sizing gives 0 (direction / cap)")
            return
        ex = self.r._executor(f)
        if ex is None:
            self._record("order_reject", follower=spec, symbol=name, detail="login disabled or account gone")
            return
        lock = self.r.locks.setdefault(spec, asyncio.Lock())
        async with lock:
            with context.use_area(self.r.area_id):
                try:
                    if partner is None:
                        res = await ex.place_order(symbol=name, action=o["action"], qty=qty, order_type=o["order_type"],
                                                   price=o["price"], stop_price=o["stop_price"])
                        ids = [(o, int(res.get("order_id") or 0))] if res.get("status") == "submitted" else []
                        err = None if ids else str((res.get("raw") or {}).get("errorText") or res.get("status"))
                    else:
                        pq = self.twin_qty(f, partner) or qty
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
                return
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

    async def _modify(self, o: dict[str, Any]) -> None:
        name = self.r.contract_names.get(o["contract_id"], str(o["contract_id"]))
        for f in self.r.group["followers"]:
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
            with context.use_area(self.r.area_id):
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
        for f in self.r.group["followers"]:
            spec = f["spec"]
            if spec_only and spec != spec_only:
                continue
            t = self.twins.get(self._twin_key(spec, leader_order_id))
            if t is None:
                continue
            ex = self.r._executor(f)
            if ex is not None:
                with context.use_area(self.r.area_id):
                    try:
                        await ex.cancel_order(t["follower_order_id"])
                        n += 1
                        self._record("order_cancel", follower=spec, symbol=t["symbol"], detail=f"{t['action']} {t['qty']} {t['order_type']}: {reason}")
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:  # noqa: BLE001 - already filled / cancelled at the broker is fine
                        self._record("order_cancel", follower=spec, symbol=t["symbol"], detail=f"{t['action']} {t['qty']} {t['order_type']}: {reason} (broker: {exc})")
            self._drop(spec, leader_order_id)
            self._touch(spec, t["contract_id"])
        return n

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
        if not self.enabled:
            return 0
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
                actions += 1
            elif key[1] not in self.leader_orders:
                actions += await self.cancel_twins(key[1], reason="orphan: leader order gone", spec_only=spec)
        missing = [o for o in self.leader_orders.values()
                   if any(self._twin_key(f["spec"], o["id"]) not in self.twins for f in self.r.group["followers"] if f.get("enabled", True))]
        if missing:
            await self._create(session, missing)
            actions += len(missing)
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

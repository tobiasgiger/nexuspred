"""Rithmic broker adapter — :class:`app.broker.BrokerSession` over the Rithmic
Protocol Buffer API (R | Protocol), through the ``async_rithmic`` library.

One :class:`RithmicSession` per login (``token_accounts[i]`` with
``broker: "rithmic"``): a persistent WebSocket per plant (order, P&L, ticker),
kept alive and reconnected by the library. The bridge sees the same picture it
gets from Tradovate — positions, working orders with versions, cash snapshot,
contracts — so strategies, copy trading, the risk guard and P&L work unchanged.

Identifiers: Rithmic keys accounts and contracts by *strings* (``"APEX-12345"``,
``("MNQZ6", "CME")``). The bridge's engines use integers (``accountId``,
``contractId``, order ``id``), so this adapter derives stable integer ids from
the strings (CRC32) and keeps the reverse maps; ``contract_info`` resolves them
back to symbols. Order ids are Rithmic *basket ids* (numeric strings).

Limits of this first version (verify on the paper system before live use):
* no broker-side OCO (``place_oco`` places two independent orders and says so),
* no execution-agent routing (Rithmic logins trade from the bridge's IP),
* no journal import (``raw_get`` raises — the journal skips Rithmic logins),
* order statuses are mapped from Rithmic's status text; unknown texts count as
  working while unfilled quantity remains.
"""
from __future__ import annotations

import asyncio
import logging
import os
import secrets
import zlib
from datetime import datetime, timezone
from typing import Any, Optional

from . import alerts, config, context, risk, state
from .tradovate import OrderOutcomeUnknown, TradovateError, _fire

log = logging.getLogger(__name__)

# Gateways (Rithmic "R | Protocol" servers). The paper-trading / test system and
# the live systems of the prop firms run on different hosts; the login's
# ``rithmic_gateway`` may override these.
GATEWAYS: dict[str, str] = {
    "test": "wss://rituz00100.rithmic.com:443",          # "Rithmic Test" system
    "paper": "wss://rprotocol.rithmic.com:443",          # "Rithmic Paper Trading"
    "chicago": "wss://rprotocol.rithmic.com:443",        # live, Chicago (Apex, Topstep, MFFU …)
    "europe": "wss://rprotocol-de.rithmic.com:443",      # live, Frankfurt
}
DEFAULT_SYSTEM = {"demo": "Rithmic Paper Trading", "live": ""}
APP_NAME = os.environ.get("NEXUSPRED_RITHMIC_APP_NAME", "Fluxbridge")    # the name registered with Rithmic
APP_VERSION = config.get_version() if hasattr(config, "get_version") else "5"

# root → exchange for the futures the bridge trades; anything else defaults to CME
EXCHANGES: dict[str, str] = {
    "ES": "CME", "MES": "CME", "NQ": "CME", "MNQ": "CME", "RTY": "CME", "M2K": "CME", "NKD": "CME",
    "6E": "CME", "6J": "CME", "6B": "CME", "6A": "CME", "6C": "CME", "M6E": "CME", "HE": "CME", "LE": "CME",
    "YM": "CBOT", "MYM": "CBOT", "ZB": "CBOT", "ZN": "CBOT", "ZF": "CBOT", "ZT": "CBOT", "ZC": "CBOT", "ZS": "CBOT", "ZW": "CBOT",
    "CL": "NYMEX", "MCL": "NYMEX", "NG": "NYMEX", "MNG": "NYMEX", "RB": "NYMEX", "HO": "NYMEX", "PL": "NYMEX",
    "GC": "COMEX", "MGC": "COMEX", "SI": "COMEX", "SIL": "COMEX", "HG": "COMEX", "MHG": "COMEX",
}
_MONTHS = "FGHJKMNQUVXZ"
_WORKING = {"OPEN", "WORKING", "PENDING", "OPEN PENDING", "MODIFY PENDING", "CANCEL PENDING", "TRIGGERED", "RELEASED", "PARTIALLY FILLED"}
_GONE = {"COMPLETE", "COMPLETED", "FILLED", "CANCELLED", "CANCELED", "REJECTED", "EXPIRED", "DELETED"}


def _int_id(text: str) -> int:
    """A stable positive int for a Rithmic string id (numeric strings stay numeric)."""
    t = str(text or "").strip()
    if t.isdigit() and len(t) < 18:
        return int(t)
    return (zlib.crc32(t.encode("utf-8")) & 0x7FFFFFFF) or 1


def _root(symbol: str) -> str:
    s = str(symbol or "").upper()
    if len(s) >= 3 and s[-1].isdigit() and s[-2] in _MONTHS:          # one-digit year (MNQZ6)
        return s[:-2]
    if len(s) >= 4 and s[-1].isdigit() and s[-2].isdigit() and s[-3] in _MONTHS:   # two-digit year (MNQZ26)
        return s[:-3]
    return s


def _rithmic_symbol(symbol: str) -> str:
    """Rithmic's one-digit-year form: MNQZ26 → MNQZ6 (MNQZ6 stays)."""
    s = str(symbol or "").upper()
    if len(s) >= 4 and s[-1].isdigit() and s[-2].isdigit() and s[-3] in _MONTHS:
        return s[:-2] + s[-1]
    return s


def exchange_for(symbol: str, overrides: Optional[dict[str, str]] = None) -> str:
    root = _root(symbol)
    return (overrides or {}).get(root) or EXCHANGES.get(root, "CME")


def _num(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _rp_error(responses: Any) -> str:
    """Rithmic answers carry ``rp_code`` (``["0"]`` = ok); the first non-zero code wins."""
    for r in responses if isinstance(responses, list) else [responses]:
        codes = list(getattr(r, "rp_code", []) or [])
        if codes and str(codes[0]) != "0":
            return " ".join(str(c) for c in codes)[:200]
    return ""


def _uncertain(exc: BaseException) -> bool:
    """Transport-style failures where the order plant may have received the request."""
    return isinstance(exc, (asyncio.TimeoutError, TimeoutError, ConnectionError, OSError))


def _outcome_unknown(name: str, operation: str, detail: Any) -> OrderOutcomeUnknown:
    msg = f"{name}: {operation} — outcome unknown, CHECK THE ACCOUNT ({detail})"
    state.log_event("error", msg)
    _fire(alerts.execution_problem(f"Order outcome unknown on {name}", msg))
    return OrderOutcomeUnknown(msg)


def _fingerprint(entry: dict[str, Any]) -> str:
    import json
    return json.dumps({"lid": entry.get("lid") or "", "name": entry.get("name") or "", "environment": entry.get("environment") or "demo",
                       "enabled": bool(entry.get("enabled")), "qty_multiplier": float(entry.get("qty_multiplier", 1) or 1),
                       "rithmic_user": entry.get("rithmic_user") or "", "rithmic_system": entry.get("rithmic_system") or "",
                       "rithmic_gateway": entry.get("rithmic_gateway") or "", "accounts": entry.get("accounts") or []}, sort_keys=True, default=str)


RECONNECT_GRACE_TICKS = 10        # 5 s for the library's own reconnect before a new client is built


def _disconnect_later(client: Any) -> None:
    async def run() -> None:
        try:
            await asyncio.wait_for(client.disconnect(), timeout=10.0)
        except Exception:  # noqa: BLE001
            pass
    try:
        from .tradovate import _fire
        _fire(run())
    except RuntimeError:                                # no running loop (tests, shutdown)
        pass


class RithmicSession:
    """One Rithmic login (see module docstring)."""
    kind = "rithmic"

    def __init__(self, idx: int, entry: dict[str, Any], area_id: int | None = None) -> None:
        self.idx = idx
        self.area_id = area_id
        self.lid = entry.get("lid") or ""
        self.name = entry.get("name") or f"account {idx + 1}"
        self.environment = "live" if entry.get("environment") == "live" else "demo"
        self.enabled = bool(entry.get("enabled"))
        self.qty_multiplier = entry.get("qty_multiplier", 1) or 1
        self.agent_id = 0                                   # agents relay HTTP only: never for Rithmic
        self.user = str(entry.get("rithmic_user") or "")
        self.password = str(entry.get("rithmic_password") or "")
        self.system_name = str(entry.get("rithmic_system") or DEFAULT_SYSTEM[self.environment])
        gw = str(entry.get("rithmic_gateway") or "").strip()
        self.gateway = GATEWAYS.get(gw.lower(), gw) if gw else GATEWAYS["paper" if self.environment == "demo" else "chicago"]
        self.exchanges = dict(entry.get("rithmic_exchanges") or {})
        self.account_spec = entry.get("account_spec") or ""
        self.account_id = int(entry.get("account_id") or 0)
        self.accounts = self._normalize_accounts(entry)
        self.fingerprint = _fingerprint(entry)
        self.penalty_until: float = 0.0
        self.rate_limits = 0
        self._client: Any = None
        self._lock = asyncio.Lock()
        self._acct_str: dict[int, str] = {}                 # int id → Rithmic account id
        self._contracts: dict[int, tuple[str, str]] = {}    # contract int id → (symbol, exchange)
        self._baskets: dict[int, tuple[str, str]] = {}      # order int id → (basket id, account id)
        self._front: dict[str, tuple[str, datetime]] = {}   # root → (front month, when)
        self._oco_warned = False
        self._acct_warned: set[str] = set()                 # accounts whose feed error was reported
        for a in self.accounts:
            if a.get("id") and a.get("spec"):
                self._acct_str[int(a["id"])] = str(a["spec"])

    # ------------------------------------------------------------ config
    def _normalize_accounts(self, entry: dict[str, Any]) -> list[dict[str, Any]]:
        out = []
        for a in entry.get("accounts") or []:
            spec = a.get("spec") or ""
            out.append({"spec": spec, "id": int(a.get("id") or _int_id(spec)), "enabled": bool(a.get("enabled", True)),
                        "qty_multiplier": float(a.get("qty_multiplier", self.qty_multiplier) or 1), "risk": dict(a.get("risk") or {})})
        return out

    def adopt_credentials(self, entry: dict[str, Any]) -> None:
        user, pw = str(entry.get("rithmic_user") or ""), str(entry.get("rithmic_password") or "")
        if (user, pw) != (self.user, self.password):
            self.user, self.password = user, pw
            old, self._client = self._client, None          # next call logs in with the new credentials
            if old is not None:
                _disconnect_later(old)                      # never leave the old sockets (and Rithmic's login count) behind

    def _refresh_fingerprint(self) -> None:
        entries = config.load_settings(area_id=self.area_id).get("token_accounts") or []
        if 0 <= self.idx < len(entries):
            self.fingerprint = _fingerprint(entries[self.idx])

    def has_token(self) -> bool:
        return bool(self.user and self.password)

    def seconds_until_refresh(self, fallback: int = 60) -> float:
        return float(max(15, fallback))

    async def proactive_refresh(self) -> None:
        return None

    # ------------------------------------------------------------ connection
    def _make_client(self) -> Any:
        """The library client (lazy import: the package is optional at runtime)."""
        try:
            from async_rithmic import OrderPlacement, ReconnectionSettings, RetrySettings, RithmicClient
        except ImportError as exc:  # pragma: no cover - environment without the package
            raise TradovateError("Rithmic support needs the 'async_rithmic' package (pip install async_rithmic)") from exc
        return RithmicClient(user=self.user, password=self.password, system_name=self.system_name,
                             app_name=APP_NAME, app_version=str(APP_VERSION), url=self.gateway,
                             manual_or_auto=OrderPlacement.AUTO,
                             reconnection_settings=ReconnectionSettings(max_retries=None, backoff_type="linear", interval=5, max_delay=60, jitter_range=(0.5, 2.0)),
                             retry_settings=RetrySettings(max_retries=2, timeout=20.0, jitter_range=(0.3, 1.0)))

    def _connected(self) -> bool:
        c = self._client
        if c is None:
            return False
        try:
            plants = getattr(c, "plants", {}) or {}
            return bool(getattr(plants.get("order"), "is_connected", False))
        except Exception:  # noqa: BLE001
            return False

    async def _ensure(self) -> Any:
        """A connected client (order + P&L + ticker plants), logging in when needed."""
        if not self.has_token():
            raise TradovateError(f"[{self.name}] Rithmic user / password not set")
        if not self.system_name:
            raise TradovateError(f"[{self.name}] Rithmic system name not set (e.g. 'Apex', 'TopstepTrader', 'Rithmic Paper Trading')")
        async with self._lock:
            if self._client is not None:
                if self._connected():
                    return self._client
                # the library reconnects on its own (max_retries=None): give it a
                # moment before replacing the client, and disconnect what we replace
                for _ in range(RECONNECT_GRACE_TICKS):
                    await asyncio.sleep(0.5)
                    if self._connected():
                        return self._client
                old, self._client = self._client, None
                _disconnect_later(old)
            client = self._make_client()
            from async_rithmic import SysInfraType
            await asyncio.wait_for(client.connect(plants=[SysInfraType.ORDER_PLANT, SysInfraType.PNL_PLANT, SysInfraType.TICKER_PLANT]), timeout=45.0)
            self._client = client
            return client

    async def _set_connected(self, connected: bool, **fields: Any) -> None:
        had_prior = state.has_session(self.name)
        was = state.session_status(self.name).get("connected") if had_prior else None
        state.set_session_status(self.name, connected=connected, agent_id=0, broker="rithmic", **fields)
        if had_prior and was and not connected:
            _fire(alerts.connection_lost(self.name, self.environment, fields.get("last_error", ""), broker=getattr(self, "kind", "tradovate")))
        elif had_prior and not was and connected:
            _fire(alerts.connection_restored(self.name, self.environment, broker=getattr(self, "kind", "tradovate")))

    def _merge_accounts(self, discovered: list[Any]) -> None:
        prev = {a["spec"]: a for a in self.accounts if a.get("spec")}
        merged = []
        for a in discovered:
            spec = str(getattr(a, "account_id", None) or (a.get("account_id") if isinstance(a, dict) else "") or "")
            if not spec:
                continue
            old = prev.get(spec) or {}
            entry = {"spec": spec, "id": _int_id(spec), "enabled": bool(old.get("enabled", True)) if old else True,
                     "qty_multiplier": float(old.get("qty_multiplier", self.qty_multiplier) or 1),
                     "label": str(getattr(a, "account_name", None) or (a.get("account_name") if isinstance(a, dict) else "") or "")}
            if old.get("risk"):
                entry["risk"] = dict(old["risk"])
            merged.append(entry)
            self._acct_str[entry["id"]] = spec
        self.accounts = merged

    async def connect(self) -> dict[str, Any]:
        try:
            client = await self._ensure()
            discovered = await client.list_accounts()
            if discovered:
                self._merge_accounts(list(discovered))
            elif self.accounts:
                state.log_event("warn", f"[{self.name}] Rithmic listed no accounts — keeping the {len(self.accounts)} known one(s)")
            primary = next((a for a in self.accounts if a.get("enabled")), None) or (self.accounts[0] if self.accounts else None)
            if primary:
                self.account_spec, self.account_id = primary["spec"], primary["id"]
            try:
                config.update_token_account(self.idx, area_id=self.area_id, lid=self.lid, accounts=self.accounts,
                                            account_spec=self.account_spec, account_id=self.account_id)
                self._refresh_fingerprint()
            except OSError:
                pass
            enabled_n = sum(1 for a in self.accounts if a.get("enabled"))
            await self._set_connected(True, environment=self.environment, account_spec=self.account_spec, account_id=self.account_id,
                                      accounts_total=len(self.accounts), accounts_enabled=enabled_n, user=self.user, last_error="",
                                      system=self.system_name, last_check=datetime.now(timezone.utc).isoformat())
            state.log_event("info", f"[{self.name}] Rithmic connected ({self.system_name}) — {len(self.accounts)} account(s), {enabled_n} enabled")
        except Exception as exc:  # noqa: BLE001
            await self._set_connected(False, last_error=str(exc)[:300], last_check=datetime.now(timezone.utc).isoformat())
            state.log_event("error", f"[{self.name}] Rithmic connect failed: {exc}")
            raise
        return state.session_status(self.name)

    async def health_check(self) -> dict[str, Any]:
        if not self.has_token():
            await self._set_connected(False, last_error="Rithmic user / password not set", last_check=datetime.now(timezone.utc).isoformat())
            return state.session_status(self.name)
        try:
            if not self._connected():
                await self._ensure()
            await self._set_connected(True, environment=self.environment, account_spec=self.account_spec, user=self.user, last_error="")
        except Exception as exc:  # noqa: BLE001
            await self._set_connected(False, last_error=str(exc)[:300])
        state.set_session_status(self.name, last_check=datetime.now(timezone.utc).isoformat())
        return state.session_status(self.name)

    async def close(self) -> None:
        c, self._client = self._client, None
        if c is not None:
            try:
                await c.disconnect()
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------ ids
    def _acct(self, account_spec: str | None, account_id: int | None) -> tuple[str, int]:
        """(Rithmic account id, int id) for a call — never the login's primary when a spec was named."""
        if account_spec:
            aid = int(account_id or 0) or next((int(a["id"]) for a in self.accounts if a.get("spec") == account_spec), 0)
            if not aid:
                raise TradovateError(f"[{self.name}] unknown Rithmic account {account_spec} — run Connect & Verify")
            self._acct_str.setdefault(aid, account_spec)
            return account_spec, aid
        aid = int(account_id or self.account_id or 0)
        spec = self._acct_str.get(aid) or self.account_spec
        if not spec:
            raise TradovateError(f"[{self.name}] no Rithmic account selected")
        return spec, aid

    def _cid(self, symbol: str, exchange: Optional[str] = None) -> int:
        sym = str(symbol).upper()
        exch = exchange or exchange_for(sym, self.exchanges)
        cid = _int_id(f"{exch}:{sym}")
        self._contracts[cid] = (sym, exch)
        return cid

    def _oid(self, basket_id: str, account: str) -> int:
        oid = _int_id(basket_id)
        self._baskets[oid] = (str(basket_id), account)
        return oid

    # ------------------------------------------------------------ feeds
    async def positions_snapshot(self) -> list[dict[str, Any]]:
        client = await self._ensure()
        out: list[dict[str, Any]] = []
        failed: list[str] = []
        for a in self.accounts:
            try:
                rows = await client.list_positions(account_id=a["spec"])
            except Exception as exc:  # noqa: BLE001
                self._account_failed(a["spec"], "positions", exc, failed)
                continue
            for p in rows or []:
                sym = str(getattr(p, "symbol", "") or "")
                if not sym:
                    continue
                net = int(_num(getattr(p, "net_quantity", None), _num(getattr(p, "buy_qty", 0)) - _num(getattr(p, "sell_qty", 0))))
                out.append({"accountId": int(a["id"]), "contractId": self._cid(sym, str(getattr(p, "exchange", "") or "") or None),
                            "netPos": net, "netPrice": _num(getattr(p, "avg_open_fill_price", None)),
                            "openPnL": _num(getattr(p, "open_position_pnl", None)), "symbol": sym})
        if failed and len(failed) == len(self.accounts):
            raise TradovateError(f"[{self.name}] positions: every account failed ({failed[0]})")
        return out

    @staticmethod
    def _status(o: Any) -> str:
        text = str(getattr(o, "status", "") or "").upper().strip()
        reason = str(getattr(o, "completion_reason", "") or "").upper().strip()
        unfilled = int(_num(getattr(o, "total_unfilled_size", None), 0))
        qty = int(_num(getattr(o, "quantity", None), 0))
        filled = int(_num(getattr(o, "total_fill_size", None), 0))
        if text in _WORKING or any(w in text for w in ("OPEN", "PENDING", "WORKING")):
            return "Working"
        if text in _GONE or reason in _GONE or "CANCEL" in text or "REJECT" in text:
            return "Filled" if filled and filled >= qty > 0 else "Canceled"
        if filled and filled >= qty > 0:
            return "Filled"
        return "Working" if unfilled > 0 or (qty and not filled and not text) else "Completed"

    @staticmethod
    def _order_type(o: Any) -> str:
        pt = str(getattr(o, "price_type", "") or "")
        if pt.isdigit():
            pt = {"1": "LIMIT", "2": "MARKET", "3": "STOP_LIMIT", "4": "STOP_MARKET"}.get(pt, pt)
        pt = pt.upper()
        return {"LIMIT": "Limit", "MARKET": "Market", "STOP_LIMIT": "StopLimit", "STOP_MARKET": "Stop"}.get(pt, pt.title() or "Market")

    def _order_row(self, o: Any, account: str, aid: int) -> dict[str, Any]:
        basket = str(getattr(o, "basket_id", "") or "")
        oid = self._oid(basket, account)
        sym = str(getattr(o, "symbol", "") or "")
        tt = str(getattr(o, "transaction_type", "") or "")
        action = "Buy" if tt in ("1", "BUY") or "BUY" in tt.upper() else "Sell"
        return {"id": oid, "accountId": aid, "contractId": self._cid(sym, str(getattr(o, "exchange", "") or "") or None), "symbol": sym,
                "action": action, "ordStatus": self._status(o), "ocoId": 0, "basket_id": basket,
                "_version": {"id": int(_num(getattr(o, "sequence_number", None), 0)) or oid, "orderQty": int(_num(getattr(o, "quantity", None), 0)),
                             "orderType": self._order_type(o), "price": _num(getattr(o, "price", None), None) if getattr(o, "price", None) not in (None, 0) else None,
                             "stopPrice": _num(getattr(o, "trigger_price", None), None) if getattr(o, "trigger_price", None) not in (None, 0) else None}}

    async def orders_snapshot(self) -> list[dict[str, Any]]:
        client = await self._ensure()
        out: list[dict[str, Any]] = []
        failed: list[str] = []
        for a in self.accounts:
            try:
                rows = await client.list_orders(account_id=a["spec"])
            except Exception as exc:  # noqa: BLE001
                self._account_failed(a["spec"], "orders", exc, failed)
                continue
            for o in rows or []:
                if getattr(o, "basket_id", None):
                    out.append(self._order_row(o, a["spec"], int(a["id"])))
        if failed and len(failed) == len(self.accounts):
            raise TradovateError(f"[{self.name}] orders: every account failed ({failed[0]})")
        self._versions = {r["id"]: r["_version"] for r in out}
        return out

    def _account_failed(self, spec: str, what: str, exc: Exception, failed: list[str]) -> None:
        """One account's feed error (a closed eval account is common): reported
        once per account, the other accounts of the login carry on."""
        failed.append(f"{spec}: {exc}"[:200])
        if spec not in self._acct_warned:
            self._acct_warned.add(spec)
            state.log_event("warn", f"[{self.name}] {what} of {spec} unavailable: {exc} — the other accounts of this login continue; "
                                    f"run Connect & Verify to drop accounts the broker no longer lists")

    async def order_versions(self, order_ids: list[int]) -> dict[int, dict[str, Any]]:
        versions = getattr(self, "_versions", None)
        if versions is None or any(int(i) not in versions for i in order_ids):
            await self.orders_snapshot()
            versions = getattr(self, "_versions", {})
        return {int(i): versions[int(i)] for i in order_ids if int(i) in versions}

    async def account_list(self) -> list[dict[str, Any]]:
        client = await self._ensure()
        return [{"id": _int_id(str(getattr(a, "account_id", ""))), "name": str(getattr(a, "account_id", "")),
                 "label": str(getattr(a, "account_name", "") or "")} for a in (await client.list_accounts()) or []]

    async def cash_snapshot(self, account_id: int) -> dict[str, Any]:
        client = await self._ensure()
        spec, _ = self._acct(None, int(account_id))
        rows = await client.list_account_summary(account_id=spec)
        row = next((r for r in rows or [] if str(getattr(r, "account_id", "")) == spec), (rows or [None])[0])
        if row is None:
            return {}
        return {"totalCashValue": _num(getattr(row, "account_balance", None)), "realizedPnL": _num(getattr(row, "day_closed_pnl", None)),
                "openPnL": _num(getattr(row, "day_open_pnl", None)), "weekRealizedPnL": None,
                "dayPnL": _num(getattr(row, "day_pnl", None)), "marginBalance": _num(getattr(row, "margin_balance", None))}

    async def auto_liq_rules(self) -> list[dict[str, Any]]:
        client = await self._ensure()
        try:
            rows = await client.get_account_rms()
        except Exception:  # noqa: BLE001
            return []
        out = []
        for r in rows or []:
            spec = str(getattr(r, "account_id", "") or "")
            if spec:
                out.append({"accountId": _int_id(spec), "dailyLossLimit": _num(getattr(r, "loss_limit", None), None),
                            "minAccountBalance": _num(getattr(r, "min_account_balance", None), None), "autoLiquidate": str(getattr(r, "auto_liquidate", ""))})
        return out

    async def user_id(self) -> int:
        return 0                                             # no Tradovate user sync socket for Rithmic

    # ------------------------------------------------------------ contracts
    async def contract_info(self, contract_id: int) -> dict[str, Any]:
        sym, exch = self._contracts.get(int(contract_id), ("", ""))
        return {"id": int(contract_id), "name": sym, "exchange": exch} if sym else {}

    async def contract_find(self, name: str) -> dict[str, Any]:
        return {"id": self._cid(name), "name": str(name).upper(), "exchange": exchange_for(name, self.exchanges)}

    async def contract_suggest(self, root: str, limit: int = 30) -> list[dict[str, Any]]:
        client = await self._ensure()
        exch = exchange_for(root, self.exchanges)
        try:
            rows = await client.search_symbols(_root(root), exchange=exch)
        except Exception:  # noqa: BLE001
            return []
        out = []
        for r in rows or []:
            sym = str(getattr(r, "symbol", "") or "")
            if _root(sym) == _root(root) and sym != _root(root):
                out.append({"name": sym, "id": self._cid(sym, exch)})
        return out[:limit]

    async def contract_maturity(self, maturity_id: int) -> dict[str, Any]:
        return {}

    async def resolve_contract(self, root_or_symbol: str) -> str:
        sym = str(root_or_symbol).upper().replace("1!", "").strip()
        if sym != _root(sym):
            return sym                                       # already a dated contract (MNQZ6)
        cached = self._front.get(sym)
        if cached and (datetime.now(timezone.utc) - cached[1]).total_seconds() < 3600:
            return cached[0]
        client = await self._ensure()
        try:
            front = await client.get_front_month_contract(sym, exchange_for(sym, self.exchanges))
        except Exception as exc:  # noqa: BLE001
            raise TradovateError(f"[{self.name}] no front month for {sym}: {exc}") from exc
        self._front[sym] = (str(front).upper(), datetime.now(timezone.utc))
        return str(front).upper()

    async def contract_id(self, symbol: str) -> int:
        return self._cid(symbol)

    async def raw_get(self, path: str, *, params: Optional[dict[str, Any]] = None) -> Any:
        raise NotImplementedError("Tradovate report endpoints are not available on a Rithmic login")

    # ------------------------------------------------------------ orders
    def _risk_gate(self, spec: str, name: str, action: str, symbol: str, qty: int, order_type: str, price: Any, stop_price: Any, aid: int) -> None:
        if risk.bypassed():
            return
        locked = risk.is_locked(self.area_id if self.area_id is not None else context.get_area(), spec)
        if locked:
            state.log_order({"action": action, "symbol": symbol, "account": name, "account_id": aid, "qty": qty, "order_type": order_type,
                             "price": price, "stop_price": stop_price, "order_id": None, "status": "rejected", "raw": {"errorText": f"risk guard: {locked}"}})
            raise TradovateError(f"{name} is locked by its risk guard for today ({locked})")

    async def place_order(self, *, symbol: str, action: str, qty: int, order_type: str,
                          price: float | None = None, stop_price: float | None = None,
                          account_spec: str | None = None, account_id: int | None = None,
                          account_name: str | None = None) -> dict[str, Any]:
        spec, aid = self._acct(account_spec, account_id)
        name = account_name or self.name
        self._risk_gate(spec, name, action, symbol, qty, order_type, price, stop_price, aid)
        from async_rithmic import OrderDuration, OrderType, TransactionType
        otype = {"Market": OrderType.MARKET, "Limit": OrderType.LIMIT, "Stop": OrderType.STOP_MARKET, "StopLimit": OrderType.STOP_LIMIT}.get(order_type)
        if otype is None:
            raise TradovateError(f"order type {order_type} is not supported on Rithmic")
        sym = str(symbol).upper()
        kw: dict[str, Any] = {"account_id": spec, "duration": OrderDuration.DAY}
        sent_price = price if order_type in ("Limit", "StopLimit") else None
        sent_stop = stop_price if order_type in ("Stop", "StopLimit") else None
        if sent_price is not None:
            kw["price"] = float(sent_price)
        if sent_stop is not None:
            kw["trigger_price"] = float(sent_stop)
        tag = "fb" + secrets.token_hex(6)
        raw: Any = None
        failure = ""
        try:
            client = await self._ensure()
            raw = await client.submit_order(tag, sym, exchange_for(sym, self.exchanges), int(qty),
                                            TransactionType.BUY if action == "Buy" else TransactionType.SELL, otype, **kw)
            failure = _rp_error(raw)
            basket = next((str(getattr(r, "basket_id", "")) for r in (raw if isinstance(raw, list) else [raw]) if getattr(r, "basket_id", None)), "")
            if not failure and not basket:
                failure = "no basket id in the answer"
        except TradovateError:
            raise
        except Exception as exc:  # noqa: BLE001
            if _uncertain(exc):
                state.log_order({"action": action, "symbol": sym, "account": name, "account_id": aid, "qty": qty, "order_type": order_type,
                                 "price": sent_price, "stop_price": sent_stop, "order_id": None, "user_tag": tag, "status": "unknown",
                                 "raw": {"errorText": str(exc) or type(exc).__name__}})
                raise _outcome_unknown(name, f"{action} {qty} {sym} {order_type} (tag {tag})", exc) from exc
            failure, basket = f"{type(exc).__name__}: {exc}"[:200], ""
        order_id = self._oid(basket, spec) if basket else None
        result = {"action": action, "symbol": sym, "account": name, "account_id": aid, "qty": qty, "order_type": order_type,
                  "price": sent_price, "stop_price": sent_stop, "order_id": order_id, "basket_id": basket or None, "user_tag": tag,
                  "status": "rejected" if failure else "submitted", "raw": _plain(raw)}
        state.log_order(result)
        if failure:
            raise TradovateError(f"{name}: {action} {qty} {sym} {order_type} rejected — {failure}")
        return result

    async def place_oco(self, *, symbol: str, action: str, qty: int, order_type: str,
                        price: float | None, stop_price: float | None, other: dict[str, Any],
                        account_spec: str | None = None, account_id: int | None = None,
                        account_name: str | None = None) -> dict[str, Any]:
        """No broker-side OCO in this version: two independent orders. The caller
        (copy mirror) cancels the twin when the leader's order is gone; a filled
        leg does NOT cancel the other automatically — reported once per login."""
        if not self._oco_warned:
            self._oco_warned = True
            state.log_event("warn", f"[{self.name}] Rithmic: OCO pairs are placed as two independent orders (no broker-side OCO yet) — a filled leg does not cancel the other")
        first = await self.place_order(symbol=symbol, action=action, qty=qty, order_type=order_type, price=price, stop_price=stop_price,
                                       account_spec=account_spec, account_id=account_id, account_name=account_name)
        try:
            second = await self.place_order(symbol=symbol, action=other["action"], qty=qty, order_type=other["order_type"], price=other.get("price"),
                                            stop_price=other.get("stop_price"), account_spec=account_spec, account_id=account_id, account_name=account_name)
        except OrderOutcomeUnknown:
            raise                                           # leg 2 may be live: nothing is cancelled blindly
        except TradovateError as original:
            try:
                await self.cancel_order(int(first["order_id"]), account_spec=account_spec, account_id=account_id)
            except OrderOutcomeUnknown as cleanup:
                raise cleanup from original
            except TradovateError as cleanup:
                raise TradovateError(
                    f"second OCO leg failed ({original}); first leg {first['order_id']} cleanup also failed: {cleanup}"
                ) from cleanup
            raise
        return {"order_id": first["order_id"], "oco_id": second["order_id"], "status": "submitted", "linked": False,
                "raw": {"first": first.get("raw"), "second": second.get("raw")}}

    async def modify_order(self, order_id: int, *, qty: int, order_type: str,
                           price: float | None = None, stop_price: float | None = None,
                           account_name: str | None = None, account_id: int | None = None, account_spec: str | None = None) -> dict[str, Any]:
        basket, spec = self._basket(order_id, account_spec, account_id)
        name = account_name or spec or self.name
        from async_rithmic import OrderType
        otype = {"Market": OrderType.MARKET, "Limit": OrderType.LIMIT, "Stop": OrderType.STOP_MARKET, "StopLimit": OrderType.STOP_LIMIT}.get(order_type)
        kw: dict[str, Any] = {"basket_id": basket, "account_id": spec, "qty": int(qty)}
        if otype is not None:
            kw["order_type"] = otype
        if order_type in ("Limit", "StopLimit") and price is not None:
            kw["price"] = float(price)
        if order_type in ("Stop", "StopLimit") and stop_price is not None:
            kw["trigger_price"] = float(stop_price)
        failure, raw = "", None
        try:
            client = await self._ensure()
            raw = await client.modify_order(**kw)
            failure = _rp_error(raw)
        except OrderOutcomeUnknown:
            raise
        except Exception as exc:  # noqa: BLE001
            if _uncertain(exc):
                state.log_order({"action": "Modify", "symbol": "", "account": name, "qty": qty, "order_type": order_type,
                                 "price": kw.get("price"), "stop_price": kw.get("trigger_price"), "order_id": order_id,
                                 "status": "unknown", "raw": {"errorText": str(exc) or type(exc).__name__}})
                raise _outcome_unknown(name, f"modify order {order_id}", exc) from exc
            failure = f"{type(exc).__name__}: {exc}"[:200]
        state.log_order({"action": "Modify", "symbol": "", "account": name, "qty": qty, "order_type": order_type,
                         "price": kw.get("price"), "stop_price": kw.get("trigger_price"), "order_id": order_id,
                         "status": "rejected" if failure else "modified", "raw": _plain(raw)})
        if failure:
            raise TradovateError(f"modify order {order_id} rejected — {failure}")
        return {"order_id": order_id, "status": "modified"}

    def _basket(self, order_id: int, account_spec: str | None = None, account_id: int | None = None) -> tuple[str, str]:
        """(basket id, account) of an order we placed or reloaded. The caller's
        account hint wins (twins reloaded after a restart are not in the map);
        without one, a login with several accounts never guesses the primary."""
        rec = self._baskets.get(int(order_id))
        hinted = self._acct(account_spec, account_id)[0] if (account_spec or account_id) else ""
        if rec is not None:
            return rec[0], (hinted or rec[1])
        if len(str(order_id)) < 18:                             # numeric basket ids round-trip unchanged
            if hinted:
                return str(order_id), hinted
            if len(self.accounts) <= 1:
                return str(order_id), self.account_spec
            raise TradovateError(f"[{self.name}] order {order_id}: account unknown (several accounts on this login) — pass the account")
        raise TradovateError(f"[{self.name}] unknown Rithmic order {order_id}")

    async def cancel_order(self, order_id: int, *, account_id: int | None = None, account_spec: str | None = None) -> dict[str, Any]:
        basket, spec = self._basket(order_id, account_spec, account_id)
        client = await self._ensure()
        try:
            raw = await client.cancel_order(basket_id=basket, account_id=spec)
        except OrderOutcomeUnknown:
            raise
        except Exception as exc:  # noqa: BLE001
            if _uncertain(exc):
                raise _outcome_unknown(spec or self.name, f"cancel order {order_id}", exc) from exc
            raise TradovateError(f"cancel order {order_id} rejected — {exc}") from exc
        failure = _rp_error(raw)
        if failure:
            raise TradovateError(f"cancel order {order_id} rejected — {failure}")
        return {"order_id": order_id, "status": "cancelled"}

    async def working_orders(self, account_id: int | None = None, *, account_spec: str | None = None) -> list[dict[str, Any]]:
        _, aid = self._acct(account_spec, account_id)
        return [o for o in await self.orders_snapshot() if o["accountId"] == aid and o["ordStatus"] == "Working"]

    async def liquidate_position(self, symbol: str, *, account_id: int | None = None,
                                 account_name: str | None = None, account_spec: str | None = None) -> dict[str, Any]:
        spec, aid = self._acct(account_spec, account_id)
        name = account_name or spec or self.name
        sym = str(symbol).upper()
        client = await self._ensure()
        failure, raw = "", None
        try:
            raw = await client.exit_position(account_id=spec, symbol=sym, exchange=exchange_for(sym, self.exchanges))
            failure = _rp_error(raw)
        except OrderOutcomeUnknown:
            raise
        except Exception as exc:  # noqa: BLE001
            if _uncertain(exc):
                state.log_order({"action": "Liquidate", "symbol": sym, "account": name, "account_id": aid, "qty": 0,
                                 "order_type": "Market", "status": "unknown", "raw": {"errorText": str(exc) or type(exc).__name__}})
                raise _outcome_unknown(name, f"liquidate {sym}", exc) from exc
            failure = f"{type(exc).__name__}: {exc}"[:200]
        state.log_order({"action": "Liquidate", "symbol": sym, "account": name, "account_id": aid, "qty": 0,
                         "order_type": "Market", "status": "rejected" if failure else "submitted", "raw": _plain(raw)})
        if failure:
            raise TradovateError(f"{name}: liquidate {sym} rejected — {failure}")
        return {"status": "submitted"}

    async def positions(self, *, account_id: int | None = None, account_name: str | None = None,
                        account_spec: str | None = None) -> list[dict[str, Any]]:
        _, aid = self._acct(account_spec, account_id)
        return [{"symbol": p["symbol"], "account": account_name or self.name, "netPos": p["netPos"], "netPrice": p.get("netPrice")}
                for p in await self.positions_snapshot() if p["accountId"] == aid and p["netPos"]]


def _plain(raw: Any) -> Any:
    """Protobuf answers as plain data for the order log."""
    if raw is None:
        return None
    items = raw if isinstance(raw, list) else [raw]
    out = []
    for r in items:
        if isinstance(r, dict):
            out.append(r)
            continue
        d = {}
        for f in ("basket_id", "user_tag", "rp_code", "rq_handler_rp_code", "text", "report_text", "status", "symbol"):
            v = getattr(r, f, None)
            if v not in (None, "", [], 0):
                d[f] = list(v) if isinstance(v, (list, tuple)) or type(v).__name__.endswith("Container") else v
        out.append(d or str(r)[:200])
    return out if len(out) != 1 else out[0]

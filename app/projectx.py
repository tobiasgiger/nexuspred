"""ProjectX broker adapter — :class:`app.broker.BrokerSession` over the ProjectX
Gateway REST API (TopstepX, Bulenox, Alpha Futures, Blusky, E8X, Tradeify, …).

One :class:`ProjectXSession` per login (``token_accounts[i]`` with
``broker: "projectx"``, ``px_user``, ``px_api_key``, ``px_firm``). Auth is
``POST /api/Auth/loginKey`` → bearer token (~24 h), renewed with
``/api/Auth/validate``. Everything is JSON over HTTPS; the real-time SignalR
hubs are not used — the bridge polls, as it does for Tradovate.

Identifiers: ProjectX account ids are integers (kept as they are); contract ids
are strings (``CON.F.US.MNQ.Z25``) mapped to stable integer ids with the reverse
map kept for :meth:`contract_info`. Contract *names* use two-digit years
(``MNQZ25``); the bridge's symbol map uses Tradovate's one-digit form
(``MNQZ6``) — both are accepted, resolution goes through the root and the
month/year. Some roots differ from the CME codes (``NQ`` is ``ENQ``, ``ES`` is
``EP``); see ``ROOT_ALIASES``.

Rate limit: the gateway allows about 200 requests per minute per user — the
session paces itself and backs off on 429.

Limits of this first version (verify with an API key on a practice account):
* OCO: the second leg is linked to the first (``linkedOrderId``); if the gateway
  does not cancel the sibling on a fill, the copy mirror's own cancel still applies,
* open P&L is estimated from the last one-minute bar (the gateway has no
  account P&L endpoint); realised P&L is the sum of today's trades,
* no execution-agent routing, no journal import (``raw_get`` raises).
"""
from __future__ import annotations

import asyncio
import logging
import time
import zlib
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

import httpx

from . import alerts, config, context, http, risk, state
from .tradovate import OrderOutcomeUnknown, RateLimited, TradovateError, _fire

SNAPSHOT_TTL_S = 3.0              # one Position/searchOpen and Account/search per login per P&L tick, not per account

log = logging.getLogger(__name__)

FIRMS: dict[str, str] = {
    "topstep": "https://api.topstepx.com",
    "demo": "https://gateway-api-demo.s2f.projectx.com",
    "alphaticks": "https://api.alphaticks.projectx.com",
    "bulenox": "https://api.bulenox.projectx.com",
    "blusky": "https://api.blusky.projectx.com",
    "e8x": "https://api.e8x.projectx.com",
    "fundingfutures": "https://api.fundingfutures.projectx.com",
    "thefuturesdesk": "https://api.thefuturesdesk.projectx.com",
    "futureselite": "https://api.futureselite.projectx.com",
    "fxifyfutures": "https://api.fxifyfutures.projectx.com",
    "goatfundedfutures": "https://api.goatfundedfutures.projectx.com",
    "tickticktrader": "https://api.tickticktrader.projectx.com",
    "toponefutures": "https://api.toponefutures.projectx.com",
    "tradeify": "https://api.tradeify.projectx.com",
    "daytraders": "https://api.daytraders.projectx.com",
    "lucidtrading": "https://api.lucidtrading.projectx.com",
    "holaprime": "https://api.holaprime.projectx.com",
    "nexgen": "https://api.nexgen.projectx.com",
    "aquafutures": "https://api.aquafutures.projectx.com",
}
ROOT_ALIASES: dict[str, str] = {"NQ": "ENQ", "ES": "EP", "CL": "CLE", "GC": "GCE", "SI": "SIE", "HG": "CPE", "NG": "NGE", "6E": "E6", "6J": "J6", "6B": "B6", "6A": "A6", "ZB": "US", "ZN": "TY", "ZF": "FV", "RB": "RBE", "HO": "HOE"}
ORDER_TYPES = {"Limit": 1, "Market": 2, "StopLimit": 3, "Stop": 4}
ORDER_TYPE_NAMES = {1: "Limit", 2: "Market", 3: "StopLimit", 4: "Stop", 5: "TrailingStop", 6: "JoinBid", 7: "JoinAsk"}
ORDER_STATUS = {0: "Working", 1: "Working", 2: "Filled", 3: "Canceled", 4: "Expired", 5: "Rejected", 6: "Working"}
SIDES = {"Buy": 0, "Sell": 1}
_MONTHS = "FGHJKMNQUVXZ"
REQUEST_SPACING_S = 0.3
TOKEN_TTL_S = 20 * 3600
ET = ZoneInfo("America/New_York")


def _int_id(text: str) -> int:
    t = str(text or "").strip()
    if t.isdigit() and len(t) < 18:
        return int(t)
    return (zlib.crc32(t.encode("utf-8")) & 0x7FFFFFFF) or 1


def _num(v: Any, default: Any = 0.0) -> Any:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def split_symbol(symbol: str) -> tuple[str, str, str]:
    """``MNQZ6`` / ``MNQZ26`` → (root, month letter, one-digit year); a bare root → (root, "", "")."""
    s = str(symbol or "").upper().replace("1!", "").strip()
    if len(s) >= 3 and s[-1].isdigit():
        if s[-2] in _MONTHS:
            return s[:-2], s[-2], s[-1]
        if len(s) >= 4 and s[-2].isdigit() and s[-3] in _MONTHS:
            return s[:-3], s[-3], s[-1]
    return s, "", ""


def _session_day_start(now: datetime) -> datetime:
    """Start of the current CME trading day (17:00 New York the day before)."""
    local = now.astimezone(ET)
    start = local.replace(hour=17, minute=0, second=0, microsecond=0)
    if local < start:
        start -= timedelta(days=1)
    return start.astimezone(timezone.utc)


def _fingerprint(entry: dict[str, Any]) -> str:
    import json
    return json.dumps({"lid": entry.get("lid") or "", "name": entry.get("name") or "", "environment": entry.get("environment") or "demo",
                       "enabled": bool(entry.get("enabled")), "qty_multiplier": float(entry.get("qty_multiplier", 1) or 1),
                       "px_user": entry.get("px_user") or "", "px_firm": entry.get("px_firm") or "", "accounts": entry.get("accounts") or []},
                      sort_keys=True, default=str)


class ProjectXSession:
    """One ProjectX login (see module docstring)."""
    kind = "projectx"

    def __init__(self, idx: int, entry: dict[str, Any], area_id: int | None = None) -> None:
        self.idx = idx
        self.area_id = area_id
        self.lid = entry.get("lid") or ""
        self.name = entry.get("name") or f"account {idx + 1}"
        self.environment = "live" if entry.get("environment") == "live" else "demo"
        self.enabled = bool(entry.get("enabled"))
        self.qty_multiplier = entry.get("qty_multiplier", 1) or 1
        self.agent_id = 0
        self.user = str(entry.get("px_user") or "")
        self.api_key = str(entry.get("px_api_key") or "")
        firm = str(entry.get("px_firm") or "topstep").strip()
        # a custom gateway must be https (a plain-http or non-URL value would send the API key in clear / nowhere)
        self.base_url = (FIRMS.get(firm.lower()) or (firm if firm.startswith("https://") else FIRMS["topstep"])).rstrip("/")
        self.firm = firm
        self.account_spec = entry.get("account_spec") or ""
        self.account_id = int(entry.get("account_id") or 0)
        self.accounts = self._normalize_accounts(entry)
        self.fingerprint = _fingerprint(entry)
        self._token: Optional[str] = None
        self._token_at: float = 0.0
        self._lock = asyncio.Lock()
        self._pace = asyncio.Lock()
        self._last_sent = 0.0
        self.penalty_until = 0.0
        self.rate_limits = 0
        self._contracts: dict[int, dict[str, Any]] = {}      # int id → {id, name, tickSize, tickValue, …}
        self._by_name: dict[str, dict[str, Any]] = {}        # contract name (2-digit year) → contract
        self._front: dict[str, tuple[str, float]] = {}       # root → (name, monotonic)
        self._orders_cache: dict[int, dict[str, Any]] = {}
        self._px_cache: dict[str, tuple[float, float]] = {}    # contract id → (last price, monotonic)
        self._snap_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}   # per-login snapshots shared by the P&L tick's accounts
        self._acct_warned: set[str] = set()
        self._oco_warned = False

    # ------------------------------------------------------------ config
    def _normalize_accounts(self, entry: dict[str, Any]) -> list[dict[str, Any]]:
        out = []
        for a in entry.get("accounts") or []:
            out.append({"spec": str(a.get("spec") or ""), "id": int(a.get("id") or 0), "enabled": bool(a.get("enabled", True)),
                        "qty_multiplier": float(a.get("qty_multiplier", self.qty_multiplier) or 1), "risk": dict(a.get("risk") or {})})
        return out

    def adopt_credentials(self, entry: dict[str, Any]) -> None:
        user, key = str(entry.get("px_user") or ""), str(entry.get("px_api_key") or "")
        if (user, key) != (self.user, self.api_key):
            self.user, self.api_key, self._token = user, key, None

    def _refresh_fingerprint(self) -> None:
        entries = config.load_settings(area_id=self.area_id).get("token_accounts") or []
        if 0 <= self.idx < len(entries):
            self.fingerprint = _fingerprint(entries[self.idx])

    def has_token(self) -> bool:
        return bool(self.user and self.api_key)

    def seconds_until_refresh(self, fallback: int = 60) -> float:
        return float(max(15, fallback))

    async def proactive_refresh(self) -> None:
        if self._token and time.monotonic() - self._token_at > TOKEN_TTL_S - 3600:
            await self._get_token(force=True)

    # ------------------------------------------------------------ http
    def _client(self) -> Any:
        return http.client("outbound")

    async def _get_token(self, force: bool = False, *, stale: str | None = None) -> str:
        """``stale`` names the token a 401 came back for: a re-login happens once
        for it, concurrent callers that hit the same 401 reuse the fresh token."""
        if not self.has_token():
            raise TradovateError(f"[{self.name}] ProjectX user name / API key not set")
        async with self._lock:
            fresh = bool(self._token) and time.monotonic() - self._token_at < TOKEN_TTL_S
            if self._token and fresh and not force and (stale is None or stale != self._token):
                return self._token
            r = await self._client().post(f"{self.base_url}/api/Auth/loginKey", json={"userName": self.user, "apiKey": self.api_key}, timeout=20.0)
            data = r.json() if r.content else {}
            if r.status_code != 200 or not data.get("success") or not data.get("token"):
                why = data.get("errorMessage") or (f"error code {data.get('errorCode')} (check user name, API key and firm)" if data.get("errorCode") is not None else f"HTTP {r.status_code}")
                raise TradovateError(f"[{self.name}] ProjectX login failed: {why}")
            self._token, self._token_at = str(data["token"]), time.monotonic()
            return self._token

    async def _post(self, path: str, body: dict[str, Any], *, retry: bool = True) -> dict[str, Any]:
        """One authenticated call, paced per login; 401 → re-login once, 429 → RateLimited."""
        token = await self._get_token()
        async with self._pace:
            wait = max(self.penalty_until - time.monotonic(), self._last_sent + REQUEST_SPACING_S - time.monotonic())
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_sent = time.monotonic()
        try:
            r = await self._client().post(f"{self.base_url}{path}", json=body, headers={"Authorization": f"Bearer {token}"}, timeout=20.0)
        except (httpx.ConnectTimeout, httpx.PoolTimeout, httpx.ConnectError) as exc:
            raise TradovateError(f"[{self.name}] ProjectX {path}: {exc!r}") from exc     # nothing was sent
        except httpx.RemoteProtocolError as exc:
            raise httpx.ReadTimeout(str(exc)) from exc                                  # sent, answer lost → outcome unknown
        if r.status_code == 401 and retry:
            await self._get_token(stale=token)
            return await self._post(path, body, retry=False)
        if r.status_code == 429:
            self.rate_limits += 1
            retry_after = _num(r.headers.get("Retry-After"), 5.0)
            self.penalty_until = time.monotonic() + retry_after
            raise RateLimited(path, r.text[:200], retry_after)
        try:
            data = r.json() if r.content else {}
        except ValueError as exc:
            raise TradovateError(f"[{self.name}] ProjectX {path}: invalid answer ({r.status_code})") from exc
        if r.status_code >= 400:
            raise TradovateError(f"[{self.name}] ProjectX {path}: HTTP {r.status_code} {str(data)[:200]}")
        if isinstance(data, dict) and data.get("success") is False:
            raise TradovateError(f"[{self.name}] ProjectX {path}: {data.get('errorMessage') or 'error ' + str(data.get('errorCode'))}")
        return data if isinstance(data, dict) else {"data": data}

    # ------------------------------------------------------------ connection
    async def _set_connected(self, connected: bool, **fields: Any) -> None:
        had_prior = state.has_session(self.name)
        was = state.session_status(self.name).get("connected") if had_prior else None
        state.set_session_status(self.name, connected=connected, agent_id=0, broker="projectx", **fields)
        if had_prior and was and not connected:
            _fire(alerts.connection_lost(self.name, self.environment, fields.get("last_error", ""), broker=getattr(self, "kind", "tradovate")))
        elif had_prior and not was and connected:
            _fire(alerts.connection_restored(self.name, self.environment, broker=getattr(self, "kind", "tradovate")))

    async def account_list(self) -> list[dict[str, Any]]:
        data = await self._post("/api/Account/search", {"onlyActiveAccounts": True})
        out = []
        for a in data.get("accounts") or []:
            if not isinstance(a, dict) or a.get("id") is None:
                continue
            out.append({"id": int(a["id"]), "name": str(a.get("name") or a["id"]), "balance": _num(a.get("balance"), None),
                        "canTrade": bool(a.get("canTrade", True)), "simulated": bool(a.get("simulated", False))})
        return out

    def _merge_accounts(self, discovered: list[dict[str, Any]]) -> None:
        prev = {a["spec"]: a for a in self.accounts if a.get("spec")}
        merged = []
        for a in discovered:
            spec = str(a.get("name") or a.get("id"))
            old = prev.get(spec) or {}
            entry = {"spec": spec, "id": int(a["id"]), "enabled": bool(old.get("enabled", True)) if old else True,
                     "qty_multiplier": float(old.get("qty_multiplier", self.qty_multiplier) or 1), "simulated": bool(a.get("simulated"))}
            if old.get("risk"):
                entry["risk"] = dict(old["risk"])
            merged.append(entry)
        self.accounts = merged

    async def connect(self) -> dict[str, Any]:
        try:
            await self._get_token(force=True)
            discovered = await self.account_list()
            if discovered:
                self._merge_accounts(discovered)
            elif self.accounts:
                state.log_event("warn", f"[{self.name}] ProjectX listed no active accounts — keeping the {len(self.accounts)} known one(s)")
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
                                      firm=self.firm, last_check=datetime.now(timezone.utc).isoformat())
            state.log_event("info", f"[{self.name}] ProjectX connected ({self.firm}) — {len(self.accounts)} account(s), {enabled_n} enabled")
        except Exception as exc:  # noqa: BLE001
            await self._set_connected(False, last_error=str(exc)[:300], last_check=datetime.now(timezone.utc).isoformat())
            state.log_event("error", f"[{self.name}] ProjectX connect failed: {exc}")
            raise
        return state.session_status(self.name)

    async def health_check(self) -> dict[str, Any]:
        if not self.has_token():
            await self._set_connected(False, last_error="ProjectX user name / API key not set", last_check=datetime.now(timezone.utc).isoformat())
            return state.session_status(self.name)
        try:
            await self.proactive_refresh()
            await self._post("/api/Auth/validate", {})
            await self._set_connected(True, environment=self.environment, account_spec=self.account_spec, user=self.user, last_error="")
        except RateLimited as exc:
            state.set_session_status(self.name, last_error=f"rate limited: {exc}")
        except Exception as exc:  # noqa: BLE001
            await self._set_connected(False, last_error=str(exc)[:300])
        state.set_session_status(self.name, last_check=datetime.now(timezone.utc).isoformat())
        return state.session_status(self.name)

    # ------------------------------------------------------------ ids / accounts
    def _acct(self, account_spec: str | None, account_id: int | None) -> tuple[str, int]:
        if account_spec:
            aid = int(account_id or 0) or next((int(a["id"]) for a in self.accounts if a.get("spec") == account_spec), 0)
            if not aid:
                raise TradovateError(f"[{self.name}] unknown ProjectX account {account_spec} — run Connect & Verify")
            return account_spec, aid
        aid = int(account_id or self.account_id or 0)
        spec = next((a["spec"] for a in self.accounts if int(a["id"]) == aid), self.account_spec)
        if not aid:
            raise TradovateError(f"[{self.name}] no ProjectX account selected")
        return spec, aid

    # ------------------------------------------------------------ contracts
    def _remember(self, c: dict[str, Any]) -> int:
        cid = _int_id(str(c.get("id")))
        rec = {"id": str(c.get("id")), "name": str(c.get("name") or "").upper(), "tickSize": _num(c.get("tickSize"), 0.25),
               "tickValue": _num(c.get("tickValue"), 0.0), "description": str(c.get("description") or ""), "active": bool(c.get("activeContract"))}
        self._contracts[cid] = rec
        if rec["name"]:
            self._by_name[rec["name"]] = rec
        return cid

    async def _search(self, root: str) -> list[dict[str, Any]]:
        data = await self._post("/api/Contract/search", {"searchText": root, "live": self.environment == "live"})
        found = [c for c in data.get("contracts") or [] if isinstance(c, dict)]
        for c in found:
            self._remember(c)
        return found

    async def _contract_for(self, symbol: str, *, refresh: bool = False) -> dict[str, Any]:
        """The gateway contract for a bridge symbol (root, MNQZ6 or MNQZ25).
        ``refresh`` skips the cache: the front month is re-asked from the gateway
        (a cached record keeps ``active`` from the day it was fetched)."""
        root, month, year = split_symbol(symbol)
        want = f"{root}{month}{year}" if month else ""
        for name, rec in (self._by_name.items() if not refresh else ()):
            r2, m2, y2 = split_symbol(name)
            if r2 in (root, ROOT_ALIASES.get(root, root)) and (not want or (m2, y2) == (month, year)):
                if want or rec["active"]:
                    return rec
        for text in dict.fromkeys([root, ROOT_ALIASES.get(root, root)]):
            found = await self._search(text)
            cands = []
            for c in found:
                rec = self._contracts[_int_id(str(c.get("id")))]
                r2, m2, y2 = split_symbol(rec["name"])
                if r2 not in (root, ROOT_ALIASES.get(root, root)):
                    continue
                if want:
                    if (m2, y2) == (month, year):
                        return rec
                else:
                    cands.append(rec)
            if not want and cands:
                return next((c for c in cands if c["active"]), cands[0])
        raise TradovateError(f"[{self.name}] ProjectX has no contract for {symbol}")

    async def resolve_contract(self, root_or_symbol: str) -> str:
        """Front month for a root; a dated contract is returned in the bridge's
        one-digit-year form once the gateway knows it."""
        root, month, year = split_symbol(root_or_symbol)
        if month:
            rec = await self._contract_for(root_or_symbol)
            r2, m2, y2 = split_symbol(rec["name"])
            return f"{root}{m2}{y2}"
        cached = self._front.get(root)
        if cached and time.monotonic() - cached[1] < 3600:
            return cached[0]
        rec = await self._contract_for(root, refresh=True)
        r2, m2, y2 = split_symbol(rec["name"])
        name = f"{root}{m2}{y2}"
        self._front[root] = (name, time.monotonic())
        return name

    async def contract_id(self, symbol: str) -> int:
        rec = await self._contract_for(symbol)
        return _int_id(rec["id"])

    async def contract_info(self, contract_id: int) -> dict[str, Any]:
        rec = self._contracts.get(int(contract_id))
        if rec is None:
            return {}
        r2, m2, y2 = split_symbol(rec["name"])
        root = next((k for k, v in ROOT_ALIASES.items() if v == r2), r2)
        return {"id": int(contract_id), "name": f"{root}{m2}{y2}" if m2 else rec["name"], "gateway_id": rec["id"], "tickSize": rec["tickSize"], "tickValue": rec["tickValue"]}

    async def contract_find(self, name: str) -> dict[str, Any]:
        rec = await self._contract_for(name)
        return {"id": _int_id(rec["id"]), "name": str(name).upper(), "gateway_id": rec["id"]}

    async def contract_suggest(self, root: str, limit: int = 30) -> list[dict[str, Any]]:
        r, _, _ = split_symbol(root)
        out = []
        for text in dict.fromkeys([r, ROOT_ALIASES.get(r, r)]):
            for c in await self._search(text):
                rec = self._contracts[_int_id(str(c.get("id")))]
                r2, m2, y2 = split_symbol(rec["name"])
                if r2 in (r, ROOT_ALIASES.get(r, r)) and m2:
                    out.append({"name": f"{r}{m2}{y2}", "id": _int_id(rec["id"])})
        return out[:limit]

    async def contract_maturity(self, maturity_id: int) -> dict[str, Any]:
        return {}

    async def raw_get(self, path: str, *, params: Optional[dict[str, Any]] = None) -> Any:
        raise NotImplementedError("Tradovate report endpoints are not available on a ProjectX login")

    async def user_id(self) -> int:
        return 0

    # ------------------------------------------------------------ feeds
    async def _contract_name(self, gateway_id: str) -> str:
        cid = _int_id(gateway_id)
        if cid not in self._contracts:
            try:
                data = await self._post("/api/Contract/searchById", {"contractId": gateway_id})
                c = data.get("contract") or (data.get("contracts") or [None])[0]
                if isinstance(c, dict):
                    self._remember(c)
            except Exception:  # noqa: BLE001
                pass
        rec = self._contracts.get(cid)
        return rec["name"] if rec else gateway_id

    def _account_failed(self, spec: str, what: str, exc: Exception, failed: list[str]) -> None:
        failed.append(f"{spec}: {exc}"[:200])
        if spec not in self._acct_warned:
            self._acct_warned.add(spec)
            state.log_event("warn", f"[{self.name}] {what} of {spec} unavailable: {exc} — the other accounts of this login continue; "
                                    f"run Connect & Verify to drop accounts the firm no longer lists")

    async def positions_snapshot(self, *, cached: bool = False) -> list[dict[str, Any]]:
        """The login's open positions. ``cached`` (the P&L tick, which asks once
        per account) reuses a snapshot a few seconds old; the copy engine's
        leader poll always fetches."""
        hit = self._snap_cache.get("positions")
        if cached and hit and time.monotonic() - hit[0] < SNAPSHOT_TTL_S:
            return [dict(r) for r in hit[1]]
        out: list[dict[str, Any]] = []
        failed: list[str] = []
        for a in self.accounts:
            try:
                data = await self._post("/api/Position/searchOpen", {"accountId": int(a["id"])})
            except (httpx.TimeoutException, asyncio.TimeoutError) as exc:
                self._account_failed(a.get("spec", str(a["id"])), "positions", exc, failed)
                continue
            except TradovateError as exc:
                if isinstance(exc, RateLimited):
                    raise
                self._account_failed(a.get("spec", str(a["id"])), "positions", exc, failed)
                continue
            for p in data.get("positions") or []:
                if not isinstance(p, dict) or not p.get("contractId"):
                    continue
                gid = str(p["contractId"])
                name = await self._contract_name(gid)
                size = int(_num(p.get("size"), 0))
                net = size if int(_num(p.get("type"), 1)) == 1 else -size
                out.append({"accountId": int(a["id"]), "contractId": _int_id(gid), "netPos": net, "netPrice": _num(p.get("averagePrice"), None),
                            "symbol": name, "gateway_id": gid})
        if failed and len(failed) == len(self.accounts):
            raise TradovateError(f"[{self.name}] positions: every account failed ({failed[0]})")
        # every fresh fetch feeds the short cache (the P&L tick's per-account cash
        # snapshots reuse the login's fetch); only the *read* is gated by ``cached``
        self._snap_cache["positions"] = (time.monotonic(), [dict(r) for r in out])
        return out

    def _order_row(self, o: dict[str, Any]) -> dict[str, Any]:
        gid = str(o.get("contractId") or "")
        size = int(_num(o.get("size"), 0))
        version = _int_id(f"{o.get('id')}|{o.get('updateTimestamp') or ''}|{size}|{o.get('limitPrice')}|{o.get('stopPrice')}")
        return {"id": int(o["id"]), "accountId": int(_num(o.get("accountId"), 0)), "contractId": _int_id(gid), "gateway_id": gid,
                "symbol": self._contracts.get(_int_id(gid), {}).get("name", gid), "action": "Buy" if int(_num(o.get("side"), 0)) == 0 else "Sell",
                "ordStatus": ORDER_STATUS.get(int(_num(o.get("status"), 0)), "Working"), "ocoId": int(_num(o.get("linkedOrderId"), 0)),
                "_version": {"id": version, "orderQty": size, "orderType": ORDER_TYPE_NAMES.get(int(_num(o.get("type"), 2)), "Market"),
                             "price": _num(o.get("limitPrice"), None), "stopPrice": _num(o.get("stopPrice"), None)}}

    async def orders_snapshot(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        failed: list[str] = []
        start = (datetime.now(timezone.utc) - timedelta(hours=36)).isoformat()
        for a in self.accounts:
            try:
                data = await self._post("/api/Order/search", {"accountId": int(a["id"]), "startTimestamp": start})
            except (httpx.TimeoutException, asyncio.TimeoutError) as exc:
                self._account_failed(a.get("spec", str(a["id"])), "orders", exc, failed)
                continue
            except TradovateError as exc:
                if isinstance(exc, RateLimited):
                    raise
                self._account_failed(a.get("spec", str(a["id"])), "orders", exc, failed)
                continue
            for o in data.get("orders") or []:
                if isinstance(o, dict) and o.get("id") is not None:
                    out.append(self._order_row(o))
        if failed and len(failed) == len(self.accounts):
            raise TradovateError(f"[{self.name}] orders: every account failed ({failed[0]})")
        self._orders_cache = {r["id"]: r["_version"] for r in out}
        return out

    async def order_versions(self, order_ids: list[int]) -> dict[int, dict[str, Any]]:
        if any(int(i) not in self._orders_cache for i in order_ids):
            await self.orders_snapshot()
        return {int(i): self._orders_cache[int(i)] for i in order_ids if int(i) in self._orders_cache}

    async def working_orders(self, account_id: int | None = None, *, account_spec: str | None = None) -> list[dict[str, Any]]:
        _, aid = self._acct(account_spec, account_id)
        data = await self._post("/api/Order/searchOpen", {"accountId": aid})
        rows = [self._order_row(o) for o in data.get("orders") or [] if isinstance(o, dict) and o.get("id") is not None]
        for r in rows:
            self._orders_cache[r["id"]] = r["_version"]
        return [r for r in rows if r["ordStatus"] == "Working"]

    async def _last_price(self, gid: str) -> Optional[float]:
        cached = self._px_cache.get(gid)
        if cached and time.monotonic() - cached[1] < 10:
            return cached[0]
        try:
            now = datetime.now(timezone.utc)
            data = await self._post("/api/History/retrieveBars", {"contractId": gid, "live": self.environment == "live",
                                                                  "startTime": (now - timedelta(hours=6)).isoformat(), "endTime": now.isoformat(),
                                                                  "unit": 2, "unitNumber": 1, "limit": 1, "includePartialBar": True})
            bars = data.get("bars") or []
            px = _num((bars[0] or {}).get("c"), None) if bars else None
        except Exception:  # noqa: BLE001
            px = None
        if px is not None:
            self._px_cache[gid] = (px, time.monotonic())
        return px

    async def cash_snapshot(self, account_id: int) -> dict[str, Any]:
        """Balance from the account list, realised P&L = today's trades, open
        P&L estimated from the last bar of each open position."""
        aid = int(account_id)
        cached = self._snap_cache.get("accounts")
        if cached and time.monotonic() - cached[0] < SNAPSHOT_TTL_S:
            rows = cached[1]
        else:
            rows = await self.account_list()
            self._snap_cache["accounts"] = (time.monotonic(), rows)
        accounts = {a["id"]: a for a in rows}
        acct = accounts.get(aid)
        if acct is None:
            return {}
        start = _session_day_start(datetime.now(timezone.utc))
        data = await self._post("/api/Trade/search", {"accountId": aid, "startTimestamp": start.isoformat()})
        realized = sum(_num(t.get("profitAndLoss"), 0.0) - _num(t.get("fees"), 0.0) for t in data.get("trades") or [] if isinstance(t, dict) and not t.get("voided"))
        open_pnl = 0.0
        for p in await self.positions_snapshot(cached=True):
            if p["accountId"] != aid or not p["netPos"]:
                continue
            rec = self._contracts.get(p["contractId"]) or {}
            px = await self._last_price(p["gateway_id"])
            if px is None or not rec.get("tickSize") or p.get("netPrice") is None:
                continue
            per_point = _num(rec.get("tickValue"), 0.0) / _num(rec.get("tickSize"), 1.0)
            open_pnl += (px - float(p["netPrice"])) * p["netPos"] * per_point
        return {"totalCashValue": _num(acct.get("balance"), 0.0), "realizedPnL": round(realized, 2), "openPnL": round(open_pnl, 2),
                "weekRealizedPnL": None, "canTrade": acct.get("canTrade", True)}

    async def auto_liq_rules(self) -> list[dict[str, Any]]:
        return []

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
                          account_name: str | None = None, linked_order_id: int | None = None) -> dict[str, Any]:
        spec, aid = self._acct(account_spec, account_id)
        name = account_name or self.name
        self._risk_gate(spec, name, action, symbol, qty, order_type, price, stop_price, aid)
        if order_type not in ORDER_TYPES:
            raise TradovateError(f"order type {order_type} is not supported on ProjectX")
        rec = await self._contract_for(symbol)
        sent_price = price if order_type in ("Limit", "StopLimit") else None
        sent_stop = stop_price if order_type in ("Stop", "StopLimit") else None
        body: dict[str, Any] = {"accountId": aid, "contractId": rec["id"], "type": ORDER_TYPES[order_type], "side": SIDES.get(action, 0), "size": int(qty),
                                "limitPrice": sent_price, "stopPrice": sent_stop, "trailPrice": None, "customTag": None, "linkedOrderId": linked_order_id}
        data: Any = None
        failure = ""
        try:
            data = await self._post("/api/Order/place", body)
            if not data.get("orderId"):
                failure = str(data.get("errorMessage") or "no orderId in the answer")
        except TradovateError as exc:
            data, failure = {"errorText": str(exc)}, str(exc)
        except (httpx.TimeoutException, asyncio.TimeoutError):
            unknown = f"{name}: {action} {qty} {symbol} {order_type} timed out — outcome unknown, CHECK THE ACCOUNT"
            state.log_order({"action": action, "symbol": str(symbol).upper(), "account": name, "account_id": aid, "qty": qty,
                             "order_type": order_type, "price": sent_price, "stop_price": sent_stop, "order_id": None,
                             "status": "unknown", "raw": {"errorText": "timeout"}})
            state.log_event("error", unknown)
            from .tradovate import OrderOutcomeUnknown, _fire
            _fire(alerts.execution_problem(f"Order outcome unknown on {name}", unknown))
            raise OrderOutcomeUnknown(unknown) from None
        result = {"action": action, "symbol": str(symbol).upper(), "account": name, "account_id": aid, "qty": qty, "order_type": order_type,
                  "price": sent_price, "stop_price": sent_stop, "order_id": int(data["orderId"]) if not failure else None,
                  "status": "rejected" if failure else "submitted", "raw": data}
        state.log_order(result)
        if failure:
            raise TradovateError(f"{name}: {action} {qty} {symbol} {order_type} rejected — {failure}")
        return result

    async def place_oco(self, *, symbol: str, action: str, qty: int, order_type: str,
                        price: float | None, stop_price: float | None, other: dict[str, Any],
                        account_spec: str | None = None, account_id: int | None = None,
                        account_name: str | None = None) -> dict[str, Any]:
        """Two orders, the second linked to the first (``linkedOrderId``). Whether the
        gateway cancels the sibling on a fill is to be verified — the copy mirror
        cancels the twin on its own when the leader's order is gone."""
        first = await self.place_order(symbol=symbol, action=action, qty=qty, order_type=order_type, price=price, stop_price=stop_price,
                                       account_spec=account_spec, account_id=account_id, account_name=account_name)
        try:
            second = await self.place_order(symbol=symbol, action=other["action"], qty=qty, order_type=other["order_type"], price=other.get("price"),
                                            stop_price=other.get("stop_price"), account_spec=account_spec, account_id=account_id,
                                            account_name=account_name, linked_order_id=first["order_id"])
        except TradovateError as exc:
            if isinstance(exc, OrderOutcomeUnknown):
                raise                                   # leg 2 may be live and linked: never cancel leg 1 blindly
            try:
                await self.cancel_order(int(first["order_id"]), account_id=account_id, account_spec=account_spec)
            except Exception:  # noqa: BLE001
                pass
            raise
        return {"order_id": first["order_id"], "oco_id": second["order_id"], "status": "submitted", "linked": True,
                "raw": {"first": first.get("raw"), "second": second.get("raw")}}

    def _account_of_order(self, order_id: int, account_id: int | None, account_spec: str | None) -> int:
        if account_id or account_spec:
            return self._acct(account_spec, account_id)[1]
        return int(self.account_id or (self.accounts[0]["id"] if self.accounts else 0))

    async def modify_order(self, order_id: int, *, qty: int, order_type: str,
                           price: float | None = None, stop_price: float | None = None,
                           account_name: str | None = None, account_id: int | None = None, account_spec: str | None = None) -> dict[str, Any]:
        aid = await self._find_account(order_id, account_id, account_spec)
        body = {"accountId": aid, "orderId": int(order_id), "size": int(qty),
                "limitPrice": price if order_type in ("Limit", "StopLimit") else None,
                "stopPrice": stop_price if order_type in ("Stop", "StopLimit") else None, "trailPrice": None}
        failure, data = "", None
        try:
            data = await self._post("/api/Order/modify", body)
        except TradovateError as exc:
            failure = str(exc)
        state.log_order({"action": "Modify", "symbol": "", "account": account_name or self.name, "qty": qty, "order_type": order_type,
                         "price": body["limitPrice"], "stop_price": body["stopPrice"], "order_id": order_id,
                         "status": "rejected" if failure else "modified", "raw": data})
        if failure:
            raise TradovateError(f"modify order {order_id} rejected — {failure}")
        return {"order_id": order_id, "status": "modified"}

    async def _find_account(self, order_id: int, account_id: int | None, account_spec: str | None) -> int:
        if account_id or account_spec:
            return self._acct(account_spec, account_id)[1]
        for a in self.accounts:                      # an order id is unique per gateway: find its account
            try:
                data = await self._post("/api/Order/searchOpen", {"accountId": int(a["id"])})
            except Exception:  # noqa: BLE001
                continue
            if any(int(_num(o.get("id"), -1)) == int(order_id) for o in data.get("orders") or [] if isinstance(o, dict)):
                return int(a["id"])
        if len(self.accounts) == 1:
            return int(self.accounts[0]["id"])
        raise TradovateError(f"[{self.name}] order {order_id} is not open on any account of this login")

    async def cancel_order(self, order_id: int, *, account_id: int | None = None, account_spec: str | None = None) -> dict[str, Any]:
        aid = await self._find_account(order_id, account_id, account_spec)
        try:
            await self._post("/api/Order/cancel", {"accountId": aid, "orderId": int(order_id)})
        except TradovateError as exc:
            raise TradovateError(f"cancel order {order_id} rejected — {exc}") from exc
        return {"order_id": order_id, "status": "cancelled"}

    async def liquidate_position(self, symbol: str, *, account_id: int | None = None,
                                 account_name: str | None = None, account_spec: str | None = None) -> dict[str, Any]:
        spec, aid = self._acct(account_spec, account_id)
        rec = await self._contract_for(symbol)
        failure, data = "", None
        try:
            data = await self._post("/api/Position/closeContract", {"accountId": aid, "contractId": rec["id"]})
        except TradovateError as exc:
            failure = str(exc)
        state.log_order({"action": "Liquidate", "symbol": str(symbol).upper(), "account": account_name or self.name, "account_id": aid, "qty": 0,
                         "order_type": "Market", "status": "rejected" if failure else "submitted", "raw": data})
        if failure:
            raise TradovateError(f"{account_name or self.name}: liquidate {symbol} rejected — {failure}")
        return {"status": "submitted"}

    async def positions(self, *, account_id: int | None = None, account_name: str | None = None,
                        account_spec: str | None = None) -> list[dict[str, Any]]:
        _, aid = self._acct(account_spec, account_id)
        out = []
        for p in await self.positions_snapshot():
            if p["accountId"] == aid and p["netPos"]:
                info = await self.contract_info(p["contractId"])
                out.append({"symbol": info.get("name") or p["symbol"], "account": account_name or self.name, "netPos": p["netPos"], "netPrice": p.get("netPrice")})
        return out

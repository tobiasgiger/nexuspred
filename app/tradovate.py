"""Token-based, multi-account Tradovate client.

Each configured account is an independent :class:`TradovateSession` with its OWN
access token, renewed via ``/auth/renewaccesstoken`` (access token, then the check
token). There is no username/password login — sessions live and die by their token.
:class:`SessionManager` builds the sessions from ``token_accounts`` in settings.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import json
from datetime import datetime, timedelta, timezone
from typing import Any

from . import alerts, config, context, http, risk, state

LIVE_BASE = "https://live.tradovateapi.com/v1"
DEMO_BASE = "https://demo.tradovateapi.com/v1"

_MONTH_CODES = {"F": 1, "G": 2, "H": 3, "J": 4, "K": 5, "M": 6,
                "N": 7, "Q": 8, "U": 9, "V": 10, "X": 11, "Z": 12}


class TradovateError(Exception):
    """Raised when the Tradovate API returns an error."""


class RateLimited(TradovateError):
    """Tradovate answered 429: the request was refused for ``retry_after`` s
    (``p-time`` in the body when given). Not a connectivity problem."""
    def __init__(self, path: str, text: str, retry_after: float) -> None:
        super().__init__(f"429 {path}: rate limited by Tradovate, retry in {retry_after:.0f} s")
        self.path, self.text, self.retry_after = path, text, retry_after


def _penalty_seconds(text: str, default: float = 5.0) -> float:
    try:
        data = json.loads(text) if text else {}
        p = float(data.get("p-time") or 0) if isinstance(data, dict) else 0.0
        return max(1.0, min(p, 120.0)) if p else default
    except (ValueError, TypeError):
        return default



def _front_month_key(name: str, root: str) -> tuple[int, int]:
    """Sort key (year, month) parsed from a contract name like ``MNQM5``."""
    suffix = name[len(root):]
    if len(suffix) < 2 or suffix[0] not in _MONTH_CODES:
        return (9999, 99)
    month = _MONTH_CODES[suffix[0]]
    digits = suffix[1:]
    now = datetime.now(timezone.utc)
    try:
        if len(digits) == 1:
            year = now.year - (now.year % 10) + int(digits)
            if year < now.year - 1:
                year += 10
        else:
            year = 2000 + int(digits)
    except ValueError:
        return (9999, 99)
    past = (year, month) < (now.year, now.month)
    return (year + (100 if past else 0), month)


def _decode_jwt_exp(token: str | None) -> datetime | None:
    """Return the ``exp`` claim of a JWT access token as a UTC datetime, if present."""
    if not token:
        return None
    try:
        payload_b64 = token.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        exp = payload.get("exp")
        if exp:
            return datetime.fromtimestamp(int(exp), tz=timezone.utc)
    except (IndexError, ValueError, binascii.Error, json.JSONDecodeError):
        pass
    return None


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _fingerprint(entry: dict[str, Any]) -> str:
    """The parts of a token_accounts entry that shape a session (everything but
    the credentials, which are adopted in place — see ``adopt_credentials``)."""
    return json.dumps({
        "name": entry.get("name") or "",
        "environment": entry.get("environment") or "demo",
        "enabled": bool(entry.get("enabled")),
        "qty_multiplier": entry.get("qty_multiplier", 1) or 1,
        "account_spec": entry.get("account_spec") or "",
        "account_id": entry.get("account_id") or 0,
        "accounts": entry.get("accounts") or [],
        "agent_id": int(entry.get("agent_id") or 0),
    }, sort_keys=True, default=str)


_bg_tasks: set[asyncio.Task] = set()


def _fire(coro: Any) -> None:
    """Run an alert in the background (context — and so the area — is inherited).
    A 15 s SMTP handshake must never stall a health check or a connect."""
    task = asyncio.get_running_loop().create_task(coro)
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


class TradovateSession:
    """One Tradovate account, authenticated by its own (renewable) access token."""

    def __init__(self, idx: int, entry: dict[str, Any], area_id: int | None = None) -> None:
        self.idx = idx
        self.area_id = area_id
        self.name = entry.get("name") or f"account {idx + 1}"
        self.environment = entry.get("environment") or "demo"
        self.enabled = bool(entry.get("enabled"))
        self.qty_multiplier = entry.get("qty_multiplier", 1) or 1
        self.account_spec = entry.get("account_spec") or ""
        self.account_id = entry.get("account_id") or 0
        self.agent_id = int(entry.get("agent_id") or 0)  # 0 = direct; else paired execution agent
        # One token (login) can expose several Tradovate trade accounts. Each is
        # independently toggleable for execution. See _normalize_accounts.
        self.accounts = self._normalize_accounts(entry)
        self._token = entry.get("access_token") or None
        self._md_token = entry.get("md_token") or None
        self._token_expires = _parse_iso(entry.get("token_expires")) or _decode_jwt_exp(self._token)
        self._lock = asyncio.Lock()
        self._contract_cache: dict[str, tuple[str, datetime]] = {}
        self._contract_id_cache: dict[str, tuple[int, datetime]] = {}
        self.fingerprint = _fingerprint(entry)
        if self._token_expires:
            state.set_session_status(self.name, token_expires=self._token_expires.isoformat())

    def adopt_credentials(self, entry: dict[str, Any]) -> None:
        """Pick up a token the user (re)pasted in Settings without rebuilding the
        session. A token the session persisted itself (after a renewal) is
        already current, so the in-memory expiry stays authoritative."""
        token = entry.get("access_token") or None
        md = entry.get("md_token") or None
        if token == self._token:
            self._md_token = md
            return
        self._token = token
        self._md_token = md
        self._token_expires = _parse_iso(entry.get("token_expires")) or _decode_jwt_exp(token)
        if self._token_expires:
            state.set_session_status(self.name, token_expires=self._token_expires.isoformat())

    def _refresh_fingerprint(self) -> None:
        """Re-sync after the session persisted its own account discovery, so the
        next reload() doesn't mistake that write for a user edit."""
        entries = config.load_settings(area_id=self.area_id).get("token_accounts") or []
        if 0 <= self.idx < len(entries):
            self.fingerprint = _fingerprint(entries[self.idx])

    def _normalize_accounts(self, entry: dict[str, Any]) -> list[dict[str, Any]]:
        """Build the per-login list of trade accounts with their execution toggles.

        Falls back to a single implicit account (from account_spec/account_id) for
        configs saved before multi-account support, so behaviour is preserved until
        the next Connect & Verify discovers the full list.
        """
        raw = entry.get("accounts")
        if raw:
            return [{
                "spec": a.get("spec") or a.get("account_spec") or "",
                "id": a.get("id") or a.get("account_id") or 0,
                "enabled": bool(a.get("enabled", True)),
                "qty_multiplier": float(a.get("qty_multiplier", self.qty_multiplier) or 1),
                "risk": dict(a.get("risk") or {}),
            } for a in raw]
        if self.account_spec or self.account_id:
            return [{"spec": self.account_spec, "id": self.account_id,
                     "enabled": True, "qty_multiplier": self.qty_multiplier}]
        return []

    # ------------------------------------------------------------------ http
    def _base_url(self) -> str:
        return LIVE_BASE if self.environment == "live" else DEMO_BASE

    async def _request(self, method: str, path: str, *, auth: bool = True, **kwargs: Any) -> Any:
        headers = kwargs.pop("headers", {})
        if auth:
            token = await self._get_token()
            headers["Authorization"] = f"Bearer {token}"
        url = f"{self._base_url()}{path}"
        if self.agent_id:
            # Every call of this login goes out from the paired execution agent's
            # IP — orders, token renewal, health checks alike. Never falls back
            # to the bridge's own address.
            from . import relay
            try:
                status, text = await relay.request(
                    int(self.agent_id), method=method, url=url, headers=headers,
                    json_body=kwargs.get("json"), params=kwargs.get("params"),
                    timeout=float(kwargs.get("timeout") or 20.0),
                    area_id=self.area_id if self.area_id is not None else context.get_area())
            except relay.AgentOffline as exc:
                raise TradovateError(f"[{self.name}] {exc}") from exc
            if status == 429:
                raise RateLimited(path, text, _penalty_seconds(text))
            if status >= 400:
                raise TradovateError(f"{status} {path}: {text}")
            return json.loads(text) if text else None
        # Pooled, keep-alive client: no TLS handshake per order (see app.http).
        resp = await http.client("tradovate").request(method, url, headers=headers, **kwargs)
        if resp.status_code == 429:
            raise RateLimited(path, resp.text, _penalty_seconds(resp.text))
        if resp.status_code >= 400:
            raise TradovateError(f"{resp.status_code} {path}: {resp.text}")
        return resp.json() if resp.text else None

    # ------------------------------------------------------------------ token
    def _token_valid(self, buffer_minutes: int = 5) -> bool:
        return bool(
            self._token and self._token_expires
            and datetime.now(timezone.utc) < self._token_expires - timedelta(minutes=buffer_minutes)
        )

    def seconds_until_refresh(self, fallback: int = 60) -> float:
        if not self._token_expires:
            return float(fallback)
        secs = (self._token_expires - datetime.now(timezone.utc)).total_seconds() - 300
        return max(15.0, min(secs, 25 * 60.0))

    def has_token(self) -> bool:
        return bool(self._token)

    async def _get_token(self) -> str:
        async with self._lock:
            if self._token_valid():
                return self._token  # type: ignore[return-value]
            if self._token:
                try:
                    await self._renew()
                    if self._token_valid(buffer_minutes=0):
                        return self._token  # type: ignore[return-value]
                except TradovateError as exc:
                    state.log_event("warn", f"[{self.name}] token renew failed: {exc}")
            raise TradovateError(
                f"[{self.name}] token expired and could not be renewed — paste a fresh token"
            )

    def _store_token(self, data: dict[str, Any]) -> None:
        self._token = data["accessToken"]
        self._md_token = data.get("mdAccessToken") or self._md_token
        expires = _decode_jwt_exp(self._token) or _parse_iso(data.get("expirationTime"))
        self._token_expires = expires or datetime.now(timezone.utc) + timedelta(minutes=75)
        state.set_session_status(self.name, token_expires=self._token_expires.isoformat())
        # Persist best-effort so a redeploy keeps the renewed token.
        try:
            config.update_token_account(
                self.idx, area_id=self.area_id,
                access_token=self._token, md_token=self._md_token or "",
                token_expires=self._token_expires.isoformat(),
            )
        except OSError as exc:
            state.log_event("warn", f"[{self.name}] could not persist token: {exc}")

    async def _renew(self) -> None:
        last_err = "no token to renew"
        for label, token in (("access token", self._token), ("check token", self._md_token)):
            if not token:
                continue
            try:
                data = await self._request(
                    "POST", "/auth/renewaccesstoken", auth=False,
                    headers={"Authorization": f"Bearer {token}"},
                )
            except TradovateError as exc:
                last_err = str(exc)
                continue
            if data and data.get("accessToken"):
                self._store_token(data)
                state.set_session_status(self.name, last_renew=datetime.now(timezone.utc).isoformat())
                state.log_event("info", f"[{self.name}] access token renewed (via {label})")
                return
            last_err = (data or {}).get("errorText", last_err)
        raise TradovateError(f"renew failed: {last_err}")

    async def proactive_refresh(self) -> None:
        """Renew the token before it expires (no credentials fallback)."""
        async with self._lock:
            if not self._token:
                raise TradovateError(f"[{self.name}] no token configured")
            await self._renew()

    # ----------------------------------------------------------------- account
    async def list_accounts(self) -> list[dict[str, Any]]:
        return await self._request("GET", "/account/list") or []

    def _merge_accounts(self, discovered: list[dict[str, Any]]) -> None:
        """Merge the accounts returned by Tradovate with the stored toggles.

        Discovered accounts are marked available (``enabled``) — actual execution
        is decided per-webhook (Webhooks tab), not by a global per-account switch,
        so every discovered account is simply made routable and left for each
        webhook to opt in. Qty-multiplier choices are preserved by account spec.
        """
        prev = {a["spec"]: a for a in self.accounts if a.get("spec")}
        merged: list[dict[str, Any]] = []
        for a in discovered:
            spec = a.get("name") or ""
            old = prev.get(spec)
            mult = float((old or {}).get("qty_multiplier", self.qty_multiplier) or 1)
            merged.append({"spec": spec, "id": a.get("id") or 0,
                           "enabled": True, "qty_multiplier": mult})
        self.accounts = merged

    async def _set_connected(self, connected: bool, **fields: Any) -> None:
        """Update connection status, firing a connection lost/restored alert on
        transition. The very first observation of a session is never alerted
        on (there's no prior state to have transitioned from)."""
        had_prior = state.has_session(self.name)
        was_connected = state.session_status(self.name).get("connected") if had_prior else None
        state.set_session_status(self.name, connected=connected, agent_id=self.agent_id, **fields)
        if had_prior and was_connected and not connected:
            _fire(alerts.connection_lost(self.name, self.environment, fields.get("last_error", "")))
        elif had_prior and not was_connected and connected:
            _fire(alerts.connection_restored(self.name, self.environment))

    async def connect(self) -> dict[str, Any]:
        try:
            await self._get_token()
            discovered = await self.list_accounts()
            self._merge_accounts(discovered)
            primary = next((a for a in self.accounts if a.get("enabled")), None) \
                or (self.accounts[0] if self.accounts else None)
            if primary:
                self.account_spec = primary["spec"]
                self.account_id = primary["id"]
            try:
                config.update_token_account(
                    self.idx, area_id=self.area_id, accounts=self.accounts,
                    account_spec=self.account_spec, account_id=self.account_id)
                self._refresh_fingerprint()
            except OSError:
                pass
            me = await self._request("GET", "/auth/me")
            enabled_n = sum(1 for a in self.accounts if a.get("enabled"))
            await self._set_connected(
                True, environment=self.environment,
                account_spec=self.account_spec, account_id=self.account_id,
                accounts_total=len(self.accounts), accounts_enabled=enabled_n,
                user=(me or {}).get("name", ""), last_error="",
                last_check=datetime.now(timezone.utc).isoformat(),
            )
            state.log_event(
                "info", f"[{self.name}] connected — {len(self.accounts)} account(s), "
                f"{enabled_n} enabled for execution")
        except Exception as exc:  # noqa: BLE001
            await self._set_connected(False, last_error=str(exc),
                                      last_check=datetime.now(timezone.utc).isoformat())
            state.log_event("error", f"[{self.name}] connect failed: {exc}")
            raise
        return state.session_status(self.name)

    async def health_check(self) -> dict[str, Any]:
        if not self._token:
            await self._set_connected(False, last_error="No token set",
                                      last_check=datetime.now(timezone.utc).isoformat())
            return state.session_status(self.name)
        try:
            await self._get_token()
            me = await self._request("GET", "/auth/me")
            await self._set_connected(True, environment=self.environment,
                                      account_spec=self.account_spec, user=(me or {}).get("name", ""),
                                      last_error="")
        except Exception as exc:  # noqa: BLE001
            await self._set_connected(False, last_error=str(exc))
        state.set_session_status(self.name, last_check=datetime.now(timezone.utc).isoformat())
        return state.session_status(self.name)

    # ---------------------------------------------------------------- contracts
    async def resolve_contract(self, root_or_symbol: str) -> str:
        cached = self._contract_cache.get(root_or_symbol)
        if cached and datetime.now(timezone.utc) - cached[1] < timedelta(hours=1):
            return cached[0]
        resolved = await self._resolve_contract_uncached(root_or_symbol)
        self._contract_cache[root_or_symbol] = (resolved, datetime.now(timezone.utc))
        return resolved

    async def _resolve_contract_uncached(self, root_or_symbol: str) -> str:
        try:
            found = await self._request("GET", "/contract/find", params={"name": root_or_symbol})
            if found and found.get("name"):
                return found["name"]
        except TradovateError:
            pass
        try:
            suggestions = await self._request(
                "GET", "/contract/suggest", params={"t": root_or_symbol, "l": 20})
        except TradovateError as exc:
            raise TradovateError(f"No contract found for '{root_or_symbol}': {exc}")
        candidates = [c for c in (suggestions or []) if c.get("name", "").startswith(root_or_symbol)]
        if not candidates:
            raise TradovateError(f"No contract found for '{root_or_symbol}'")
        candidates.sort(key=lambda c: (_front_month_key(c.get("name", ""), root_or_symbol),
                                       c.get("expirationDate") or c.get("name", "")))
        return candidates[0]["name"]

    # ---------------------------------------------------------------- orders
    async def place_order(self, *, symbol: str, action: str, qty: int, order_type: str,
                          price: float | None = None, stop_price: float | None = None,
                          account_spec: str | None = None, account_id: int | None = None,
                          account_name: str | None = None) -> dict[str, Any]:
        spec = account_spec or self.account_spec
        aid = account_id or self.account_id
        name = account_name or self.name
        if not risk.bypassed():
            locked = risk.is_locked(self.area_id if self.area_id is not None else context.get_area(), spec)
            if locked:
                state.log_order({"action": action, "symbol": symbol, "account": name, "qty": qty,
                                 "order_type": order_type, "price": price, "stop_price": stop_price,
                                 "order_id": None, "status": "rejected", "raw": {"errorText": f"risk guard: {locked}"}})
                raise TradovateError(f"{name} is locked by its risk guard for today ({locked})")
        body: dict[str, Any] = {
            "accountSpec": spec, "accountId": aid,
            "action": action, "symbol": symbol, "orderQty": qty,
            "orderType": order_type, "isAutomated": True,
        }
        sent_price = price if order_type in ("Limit", "StopLimit") else None
        sent_stop = stop_price if order_type in ("Stop", "StopLimit") else None
        if sent_price is not None:
            body["price"] = sent_price
        if sent_stop is not None:
            body["stopPrice"] = sent_stop
        data = await self._request("POST", "/order/placeorder", json=body)
        result = {
            "action": action, "symbol": symbol, "account": name, "qty": qty,
            "order_type": order_type, "price": sent_price, "stop_price": sent_stop,
            "order_id": (data or {}).get("orderId"),
            "status": "submitted" if data and data.get("orderId") else "rejected",
            "raw": data,
        }
        state.log_order(result)
        return result

    async def place_oco(self, *, symbol: str, action: str, qty: int, order_type: str,
                        price: float | None, stop_price: float | None, other: dict[str, Any],
                        account_spec: str | None = None, account_id: int | None = None,
                        account_name: str | None = None) -> dict[str, Any]:
        """Two orders that cancel each other (``/order/placeoco``): the first from
        the keyword arguments, the second from ``other`` (``action``, ``order_type``,
        ``price`` / ``stop_price``). Returns ``{order_id, oco_id, status, raw}``."""
        spec = account_spec or self.account_spec
        aid = account_id or self.account_id
        name = account_name or self.name
        if not risk.bypassed():
            locked = risk.is_locked(self.area_id if self.area_id is not None else context.get_area(), spec)
            if locked:
                raise TradovateError(f"{name} is locked by its risk guard for today ({locked})")
        body: dict[str, Any] = {"accountSpec": spec, "accountId": aid, "action": action, "symbol": symbol,
                                "orderQty": qty, "orderType": order_type, "isAutomated": True}
        if order_type in ("Limit", "StopLimit") and price is not None:
            body["price"] = price
        if order_type in ("Stop", "StopLimit") and stop_price is not None:
            body["stopPrice"] = stop_price
        o: dict[str, Any] = {"action": other["action"], "orderType": other["order_type"]}
        if other["order_type"] in ("Limit", "StopLimit") and other.get("price") is not None:
            o["price"] = other["price"]
        if other["order_type"] in ("Stop", "StopLimit") and other.get("stop_price") is not None:
            o["stopPrice"] = other["stop_price"]
        body["other"] = o
        data = await self._request("POST", "/order/placeoco", json=body)
        ok = bool(data and data.get("orderId"))
        for leg, kind in ((body, order_type), (o, other["order_type"])):
            state.log_order({"action": leg["action"], "symbol": symbol, "account": name, "qty": qty, "order_type": kind,
                             "price": leg.get("price"), "stop_price": leg.get("stopPrice"),
                             "order_id": (data or {}).get("orderId") if leg is body else (data or {}).get("ocoId"),
                             "status": "submitted" if ok else "rejected", "raw": data})
        return {"order_id": (data or {}).get("orderId"), "oco_id": (data or {}).get("ocoId"),
                "status": "submitted" if ok else "rejected", "raw": data}

    async def order_versions(self, order_ids: list[int]) -> dict[int, dict[str, Any]]:
        """Latest order version (qty, type, price, stop) per order id."""
        if not order_ids:
            return {}
        raw = await self._request("GET", "/orderVersion/ldeps", params={"masterids": ",".join(str(i) for i in order_ids)}) or []
        out: dict[int, dict[str, Any]] = {}
        for v in raw if isinstance(raw, list) else []:
            oid = int(v.get("orderId") or 0)
            if oid and (oid not in out or int(v.get("id") or 0) > int(out[oid].get("id") or 0)):
                out[oid] = v
        return out

    async def modify_order(self, order_id: int, *, qty: int, order_type: str,
                           price: float | None = None, stop_price: float | None = None,
                           account_name: str | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {"orderId": order_id, "orderQty": qty, "orderType": order_type}
        if order_type in ("Limit", "StopLimit") and price is not None:
            body["price"] = price
        if order_type in ("Stop", "StopLimit") and stop_price is not None:
            body["stopPrice"] = stop_price
        data = await self._request("POST", "/order/modifyorder", json=body)
        state.log_order({"action": "Modify", "symbol": "", "account": account_name or self.name,
                         "qty": qty, "order_type": order_type, "price": body.get("price"),
                         "stop_price": body.get("stopPrice"), "order_id": order_id,
                         "status": "modified", "raw": data})
        return data

    async def cancel_order(self, order_id: int) -> dict[str, Any]:
        return await self._request("POST", "/order/cancelorder", json={"orderId": order_id})

    async def working_orders(self, account_id: int | None = None) -> list[dict[str, Any]]:
        aid = account_id or self.account_id
        orders = await self._request("GET", "/order/list") or []
        active = {"Working", "Pending", "PendingNew", "Suspended"}
        return [o for o in orders
                if o.get("ordStatus") in active and o.get("accountId") == aid]

    async def contract_id(self, symbol: str) -> int:
        """Tradovate's numeric contract id for a contract name (cached 1 h).
        Used to cancel only the orders that belong to one contract."""
        cached = self._contract_id_cache.get(symbol)
        if cached and datetime.now(timezone.utc) - cached[1] < timedelta(hours=1):
            return cached[0]
        found = await self._request("GET", "/contract/find", params={"name": symbol})
        cid = int((found or {}).get("id") or 0)
        if not cid:
            raise TradovateError(f"Cannot resolve contract id for {symbol}")
        self._contract_id_cache[symbol] = (cid, datetime.now(timezone.utc))
        return cid

    async def liquidate_position(self, symbol: str, *, account_id: int | None = None,
                                 account_name: str | None = None) -> dict[str, Any]:
        aid = account_id or self.account_id
        contract = await self._request("GET", "/contract/find", params={"name": symbol})
        if not contract or not contract.get("id"):
            raise TradovateError(f"Cannot resolve contract id for {symbol}")
        data = await self._request("POST", "/order/liquidateposition",
                                   json={"accountId": aid,
                                         "contractId": contract["id"], "admin": False})
        state.log_order({"action": "Liquidate", "symbol": symbol,
                         "account": account_name or self.name,
                         "qty": 0, "order_type": "Market", "status": "submitted", "raw": data})
        return data

    async def positions(self, *, account_id: int | None = None,
                        account_name: str | None = None) -> list[dict[str, Any]]:
        aid = account_id or self.account_id
        name = account_name or self.name
        raw = await self._request("GET", "/position/list") or []
        names: dict[int, str] = {}
        out: list[dict[str, Any]] = []
        for p in raw:
            if p.get("accountId") != aid or not (p.get("netPos") or 0):
                continue
            cid = p.get("contractId")
            cname = names.get(cid)
            if cname is None:
                try:
                    item = await self._request("GET", "/contract/item", params={"id": cid})
                    cname = (item or {}).get("name") or str(cid)
                except TradovateError:
                    cname = str(cid)
                names[cid] = cname
            out.append({"symbol": cname, "account": name, "netPos": p.get("netPos"),
                        "netPrice": p.get("netPrice")})
        return out


class AccountExecutor:
    """One trade account inside a :class:`TradovateSession`, used as an order target.

    Exposes the same interface the signal engine expects (``name``,
    ``qty_multiplier``, ``resolve_contract``, ``place_order`` …) but binds every
    call to a specific Tradovate account, so a single login can mirror orders to
    several accounts. The underlying session is shared (one token, one contract
    cache, one renewal loop).
    """

    def __init__(self, session: "TradovateSession", account: dict[str, Any]) -> None:
        self.session = session
        self.spec = account.get("spec") or session.account_spec
        self.id = account.get("id") or session.account_id
        self.qty_multiplier = account.get("qty_multiplier", 1) or 1
        # Unique per trade account (Tradovate specs are unique); used to key the
        # bridge's active-trade tracking and per-account order results.
        self.name = self.spec or session.name

    async def resolve_contract(self, root_or_symbol: str) -> str:
        return await self.session.resolve_contract(root_or_symbol)

    async def place_order(self, **kw: Any) -> dict[str, Any]:
        return await self.session.place_order(
            account_spec=self.spec, account_id=self.id, account_name=self.name, **kw)

    async def modify_order(self, order_id: int, **kw: Any) -> dict[str, Any]:
        return await self.session.modify_order(order_id, account_name=self.name, **kw)

    async def place_oco(self, **kw: Any) -> dict[str, Any]:
        return await self.session.place_oco(account_spec=self.spec, account_id=self.id, account_name=self.name, **kw)

    async def order_versions(self, order_ids: list[int]) -> dict[int, dict[str, Any]]:
        return await self.session.order_versions(order_ids)

    async def cancel_order(self, order_id: int) -> dict[str, Any]:
        return await self.session.cancel_order(order_id)

    async def working_orders(self) -> list[dict[str, Any]]:
        return await self.session.working_orders(account_id=self.id)

    async def contract_id(self, symbol: str) -> int:
        return await self.session.contract_id(symbol)

    async def liquidate_position(self, symbol: str) -> dict[str, Any]:
        return await self.session.liquidate_position(
            symbol, account_id=self.id, account_name=self.name)

    async def positions(self) -> list[dict[str, Any]]:
        return await self.session.positions(account_id=self.id, account_name=self.name)


class SessionManager:
    """Builds and tracks one TradovateSession per configured token (login).

    Each login can expose several trade accounts; ``enabled`` flattens those into
    per-account executors so every signal can fan out to all accounts that are
    switched on for execution.
    """

    def __init__(self, area_id: int | None = None) -> None:
        self.area_id = area_id
        self._sessions: list[TradovateSession] | None = None

    def reload(self) -> None:
        """Sync sessions with settings — keeping every session whose login config
        (name / environment / enabled / accounts) is unchanged, so its token
        state, renew lock and contract cache survive. Re-pasted credentials are
        adopted in place. (v4 rebuilt every session on each health cycle.)"""
        entries = config.load_settings(area_id=self.area_id).get("token_accounts") or []
        prev = self._sessions or []
        fresh: list[TradovateSession] = []
        for i, e in enumerate(entries):
            old = prev[i] if i < len(prev) else None
            if old is not None and old.fingerprint == _fingerprint(e):
                old.adopt_credentials(e)
                fresh.append(old)
            else:
                fresh.append(TradovateSession(i, e, area_id=self.area_id))
        self._sessions = fresh

    def all(self) -> list[TradovateSession]:
        if self._sessions is None:
            self.reload()
        return list(self._sessions or [])

    def all_accounts(self) -> list[tuple["TradovateSession", dict[str, Any]]]:
        """Every (session, account) pair across all logins — for the overview."""
        return [(s, a) for s in self.all() for a in s.accounts]

    def enabled(self) -> list[AccountExecutor]:
        """Executors for every trade account switched on under an enabled login."""
        out: list[AccountExecutor] = []
        for s in self.all():
            if not s.enabled:
                continue
            for a in s.accounts:
                if a.get("enabled"):
                    out.append(AccountExecutor(s, a))
        return out

    def executor_for(
        self, token_idx: int, spec: str, qty_multiplier: float = 1
    ) -> AccountExecutor | None:
        """Build an executor for one specific (login, trade account) pair, with a
        caller-supplied qty multiplier — used by per-webhook routing, independent
        of that account's own execution toggle under Settings → Trade Accounts.
        Returns None if the login or account no longer exists (e.g. deleted)."""
        sessions = self.all()
        if not (0 <= token_idx < len(sessions)):
            return None
        session = sessions[token_idx]
        if not session.enabled:
            return None
        account = next((a for a in session.accounts if a.get("spec") == spec), None)
        if account is None:
            return None
        return AccountExecutor(session, {**account, "qty_multiplier": qty_multiplier})


# One SessionManager per area (user workspace).
import threading as _threading

from . import context as _context

_managers: dict[int, SessionManager] = {}
_managers_lock = _threading.Lock()


def manager_for(area_id: int) -> SessionManager:
    with _managers_lock:
        m = _managers.get(area_id)
        if m is None:
            m = _managers[area_id] = SessionManager(area_id)
        return m


def manager() -> SessionManager:
    """The SessionManager for the current context area."""
    return manager_for(_context.get_area())


def all_managers() -> list[SessionManager]:
    with _managers_lock:
        return list(_managers.values())

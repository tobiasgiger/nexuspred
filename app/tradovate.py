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

import httpx

from . import config, state

LIVE_BASE = "https://live.tradovateapi.com/v1"
DEMO_BASE = "https://demo.tradovateapi.com/v1"

_MONTH_CODES = {"F": 1, "G": 2, "H": 3, "J": 4, "K": 5, "M": 6,
                "N": 7, "Q": 8, "U": 9, "V": 10, "X": 11, "Z": 12}


class TradovateError(Exception):
    """Raised when the Tradovate API returns an error."""


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


class TradovateSession:
    """One Tradovate account, authenticated by its own (renewable) access token."""

    def __init__(self, idx: int, entry: dict[str, Any]) -> None:
        self.idx = idx
        self.name = entry.get("name") or f"account {idx + 1}"
        self.environment = entry.get("environment") or "demo"
        self.enabled = bool(entry.get("enabled"))
        self.qty_multiplier = entry.get("qty_multiplier", 1) or 1
        self.account_spec = entry.get("account_spec") or ""
        self.account_id = entry.get("account_id") or 0
        self._token = entry.get("access_token") or None
        self._md_token = entry.get("md_token") or None
        self._token_expires = _parse_iso(entry.get("token_expires")) or _decode_jwt_exp(self._token)
        self._lock = asyncio.Lock()
        self._contract_cache: dict[str, tuple[str, datetime]] = {}
        if self._token_expires:
            state.set_session_status(self.name, token_expires=self._token_expires.isoformat())

    # ------------------------------------------------------------------ http
    def _base_url(self) -> str:
        return LIVE_BASE if self.environment == "live" else DEMO_BASE

    async def _request(self, method: str, path: str, *, auth: bool = True, **kwargs: Any) -> Any:
        headers = kwargs.pop("headers", {})
        if auth:
            token = await self._get_token()
            headers["Authorization"] = f"Bearer {token}"
        url = f"{self._base_url()}{path}"
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.request(method, url, headers=headers, **kwargs)
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
                self.idx, access_token=self._token, md_token=self._md_token or "",
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

    async def connect(self) -> dict[str, Any]:
        try:
            await self._get_token()
            accounts = await self.list_accounts()
            account = None
            if self.account_spec:
                account = next((a for a in accounts if a.get("name") == self.account_spec), None)
            account = account or (accounts[0] if accounts else None)
            if account:
                self.account_spec = account["name"]
                self.account_id = account["id"]
                try:
                    config.update_token_account(
                        self.idx, account_spec=self.account_spec, account_id=self.account_id)
                except OSError:
                    pass
            me = await self._request("GET", "/auth/me")
            state.set_session_status(
                self.name, connected=True, environment=self.environment,
                account_spec=self.account_spec, account_id=self.account_id,
                user=(me or {}).get("name", ""), last_error="",
                last_check=datetime.now(timezone.utc).isoformat(),
            )
            state.log_event("info", f"[{self.name}] connected ({self.account_spec})")
        except Exception as exc:  # noqa: BLE001
            state.set_session_status(self.name, connected=False, last_error=str(exc),
                                     last_check=datetime.now(timezone.utc).isoformat())
            state.log_event("error", f"[{self.name}] connect failed: {exc}")
            raise
        return state.session_status(self.name)

    async def health_check(self) -> dict[str, Any]:
        if not self._token:
            state.set_session_status(self.name, connected=False, last_error="No token set",
                                     last_check=datetime.now(timezone.utc).isoformat())
            return state.session_status(self.name)
        try:
            await self._get_token()
            me = await self._request("GET", "/auth/me")
            state.set_session_status(self.name, connected=True, environment=self.environment,
                                     account_spec=self.account_spec, user=(me or {}).get("name", ""),
                                     last_error="")
        except Exception as exc:  # noqa: BLE001
            state.set_session_status(self.name, connected=False, last_error=str(exc))
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
                          price: float | None = None, stop_price: float | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {
            "accountSpec": self.account_spec, "accountId": self.account_id,
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
            "action": action, "symbol": symbol, "account": self.name, "qty": qty,
            "order_type": order_type, "price": sent_price, "stop_price": sent_stop,
            "order_id": (data or {}).get("orderId"),
            "status": "submitted" if data and data.get("orderId") else "rejected",
            "raw": data,
        }
        state.log_order(result)
        return result

    async def modify_order(self, order_id: int, *, qty: int, order_type: str,
                           price: float | None = None, stop_price: float | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {"orderId": order_id, "orderQty": qty, "orderType": order_type}
        if order_type in ("Limit", "StopLimit") and price is not None:
            body["price"] = price
        if order_type in ("Stop", "StopLimit") and stop_price is not None:
            body["stopPrice"] = stop_price
        data = await self._request("POST", "/order/modifyorder", json=body)
        state.log_order({"action": "Modify", "symbol": "", "account": self.name, "qty": qty,
                         "order_type": order_type, "price": body.get("price"),
                         "stop_price": body.get("stopPrice"), "order_id": order_id,
                         "status": "modified", "raw": data})
        return data

    async def cancel_order(self, order_id: int) -> dict[str, Any]:
        return await self._request("POST", "/order/cancelorder", json={"orderId": order_id})

    async def working_orders(self) -> list[dict[str, Any]]:
        orders = await self._request("GET", "/order/list") or []
        active = {"Working", "Pending", "PendingNew", "Suspended"}
        return [o for o in orders
                if o.get("ordStatus") in active and o.get("accountId") == self.account_id]

    async def liquidate_position(self, symbol: str) -> dict[str, Any]:
        contract = await self._request("GET", "/contract/find", params={"name": symbol})
        if not contract or not contract.get("id"):
            raise TradovateError(f"Cannot resolve contract id for {symbol}")
        data = await self._request("POST", "/order/liquidateposition",
                                   json={"accountId": self.account_id,
                                         "contractId": contract["id"], "admin": False})
        state.log_order({"action": "Liquidate", "symbol": symbol, "account": self.name,
                         "qty": 0, "order_type": "Market", "status": "submitted", "raw": data})
        return data

    async def positions(self) -> list[dict[str, Any]]:
        raw = await self._request("GET", "/position/list") or []
        names: dict[int, str] = {}
        out: list[dict[str, Any]] = []
        for p in raw:
            if p.get("accountId") != self.account_id or not (p.get("netPos") or 0):
                continue
            cid = p.get("contractId")
            name = names.get(cid)
            if name is None:
                try:
                    item = await self._request("GET", "/contract/item", params={"id": cid})
                    name = (item or {}).get("name") or str(cid)
                except TradovateError:
                    name = str(cid)
                names[cid] = name
            out.append({"symbol": name, "account": self.name, "netPos": p.get("netPos"),
                        "netPrice": p.get("netPrice")})
        return out


class SessionManager:
    """Builds and tracks one TradovateSession per configured token account."""

    def __init__(self) -> None:
        self._sessions: list[TradovateSession] | None = None

    def reload(self) -> None:
        entries = config.load_settings().get("token_accounts") or []
        self._sessions = [TradovateSession(i, e) for i, e in enumerate(entries)]

    def all(self) -> list[TradovateSession]:
        if self._sessions is None:
            self.reload()
        return list(self._sessions or [])

    def enabled(self) -> list[TradovateSession]:
        return [s for s in self.all() if s.enabled]


manager = SessionManager()

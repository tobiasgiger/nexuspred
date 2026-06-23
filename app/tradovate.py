"""Minimal async Tradovate REST client.

Only the endpoints the bridge needs are implemented: authentication, account/contract
lookup, order placement/modify/cancel and position liquidation. The client caches the
access token until shortly before it expires.

Docs: https://api.tradovate.com/
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from . import config, state

LIVE_BASE = "https://live.tradovateapi.com/v1"
DEMO_BASE = "https://demo.tradovateapi.com/v1"


class TradovateError(Exception):
    """Raised when the Tradovate API returns an error."""


class TradovateClient:
    def __init__(self) -> None:
        self._token: str | None = None          # API user session token
        self._md_token: str | None = None        # market-data token
        self._token_expires: datetime | None = None
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _base_url() -> str:
        s = config.load_settings()
        return LIVE_BASE if s.get("environment") == "live" else DEMO_BASE

    async def _request(
        self, method: str, path: str, *, auth: bool = True, **kwargs: Any
    ) -> Any:
        headers = kwargs.pop("headers", {})
        if auth:
            token = await self._get_token()
            headers["Authorization"] = f"Bearer {token}"
        url = f"{self._base_url()}{path}"
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.request(method, url, headers=headers, **kwargs)
        if resp.status_code >= 400:
            raise TradovateError(f"{resp.status_code} {path}: {resp.text}")
        if resp.text:
            return resp.json()
        return None

    # ----------------------------------------------------------------- auth
    def _token_valid(self, buffer_minutes: int = 5) -> bool:
        return bool(
            self._token
            and self._token_expires
            and datetime.now(timezone.utc)
            < self._token_expires - timedelta(minutes=buffer_minutes)
        )

    async def _get_token(self) -> str:
        async with self._lock:
            # Still comfortably valid → reuse.
            if self._token_valid():
                return self._token  # type: ignore[return-value]
            # We have a token but it's near/at expiry → renew (no password resend).
            if self._token:
                try:
                    await self._renew()
                    if self._token_valid(buffer_minutes=0):
                        return self._token  # type: ignore[return-value]
                except TradovateError as exc:
                    state.log_event("warn", f"Token renew failed, re-authenticating: {exc}")
            # No token, or renew failed → full authentication.
            await self._authenticate()
            assert self._token is not None
            return self._token

    def _store_token(self, data: dict[str, Any]) -> None:
        self._token = data["accessToken"]
        self._md_token = data.get("mdAccessToken")
        expiration = data.get("expirationTime")
        if expiration:
            self._token_expires = datetime.fromisoformat(
                expiration.replace("Z", "+00:00")
            )
        else:
            self._token_expires = datetime.now(timezone.utc) + timedelta(hours=1)
        state.connection["token_expires"] = (
            self._token_expires.isoformat() if self._token_expires else None
        )

    async def _renew(self) -> None:
        """Renew the access token using the existing session (no credentials)."""
        headers = {"Authorization": f"Bearer {self._token}"}
        data = await self._request(
            "POST", "/auth/renewaccesstoken", auth=False, headers=headers
        )
        if not data or not data.get("accessToken"):
            err = (data or {}).get("errorText", "renew returned no token")
            raise TradovateError(f"Renew failed: {err}")
        self._store_token(data)
        state.connection["last_renew"] = datetime.now(timezone.utc).isoformat()
        state.log_event("info", "Access token renewed")

    async def _authenticate(self) -> None:
        s = config.load_settings()
        missing = [
            k for k in ("username", "password", "app_id", "cid", "sec")
            if not s.get(k)
        ]
        if missing:
            raise TradovateError(f"Missing credentials: {', '.join(missing)}")

        body = {
            "name": s["username"],
            "password": s["password"],
            "appId": s["app_id"],
            "appVersion": s.get("app_version", "1.0"),
            "cid": s["cid"],
            "sec": s["sec"],
        }
        if s.get("device_id"):
            body["deviceId"] = s["device_id"]

        data = await self._request(
            "POST", "/auth/accesstokenrequest", auth=False, json=body
        )
        if not data or not data.get("accessToken"):
            err = (data or {}).get("errorText", "unknown error")
            raise TradovateError(f"Authentication failed: {err}")

        self._store_token(data)
        state.connection["last_auth"] = datetime.now(timezone.utc).isoformat()

    async def connect(self) -> dict[str, Any]:
        """Authenticate and resolve the trading account; update status snapshot."""
        s = config.load_settings()
        try:
            await self._get_token()
            accounts = await self._request("GET", "/account/list")
            account = self._select_account(accounts, s.get("account_spec"))
            if account:
                config.save_settings(
                    {"account_spec": account["name"], "account_id": account["id"]}
                )
            state.connection.update(
                connected=True,
                environment=s.get("environment", "demo"),
                account_spec=account["name"] if account else "",
                account_id=account["id"] if account else 0,
                last_error="",
            )
            state.log_event("info", "Connected to Tradovate")
            return state.connection
        except Exception as exc:  # noqa: BLE001 - surface any failure to dashboard
            state.connection.update(connected=False, last_error=str(exc))
            state.log_event("error", f"Connect failed: {exc}")
            raise

    async def health_check(self) -> dict[str, Any]:
        """Verify the connection is up: ensure a valid token (renewing if needed)
        and make a lightweight authenticated call. Updates the status snapshot.

        Safe to call on a timer — never raises; failures are recorded on the
        connection state so the dashboard can show them.
        """
        s = config.load_settings()
        if not all(s.get(k) for k in ("username", "password", "app_id", "cid", "sec")):
            state.connection.update(connected=False, last_error="Credentials not set")
            state.connection["last_check"] = datetime.now(timezone.utc).isoformat()
            return state.connection
        try:
            await self._get_token()  # validates / renews / re-auths as needed
            me = await self._request("GET", "/auth/me")
            state.connection.update(
                connected=True,
                environment=s.get("environment", "demo"),
                last_error="",
                user=(me or {}).get("name", ""),
            )
        except Exception as exc:  # noqa: BLE001
            state.connection.update(connected=False, last_error=str(exc))
        state.connection["last_check"] = datetime.now(timezone.utc).isoformat()
        return state.connection

    @staticmethod
    def _select_account(
        accounts: list[dict[str, Any]] | None, spec: str | None
    ) -> dict[str, Any] | None:
        if not accounts:
            return None
        if spec:
            for acc in accounts:
                if acc.get("name") == spec:
                    return acc
        return accounts[0]

    # ------------------------------------------------------------- contracts
    async def resolve_contract(self, root_or_symbol: str) -> str:
        """Return a tradable Tradovate contract symbol for a root like ``MNQ``.

        If a fully dated symbol is supplied (e.g. ``MNQU5``) it is verified and
        returned as-is; otherwise the nearest (front-month) contract is chosen.
        """
        # Already a dated contract? verify it exists.
        found = await self._request(
            "GET", "/contract/find", params={"name": root_or_symbol}
        )
        if found and found.get("name"):
            return found["name"]

        # Otherwise suggest contracts for the root and pick the front month.
        suggestions = await self._request(
            "GET", "/contract/suggest", params={"t": root_or_symbol, "l": 20}
        )
        candidates = [
            c for c in (suggestions or [])
            if c.get("name", "").startswith(root_or_symbol)
        ]
        if not candidates:
            raise TradovateError(f"No contract found for '{root_or_symbol}'")
        # Front month = earliest expiration among active contracts.
        candidates.sort(key=lambda c: c.get("expirationDate") or c.get("name", ""))
        return candidates[0]["name"]

    # ---------------------------------------------------------------- orders
    async def place_order(
        self,
        *,
        symbol: str,
        action: str,
        qty: int,
        order_type: str,
        price: float | None = None,
        stop_price: float | None = None,
    ) -> dict[str, Any]:
        s = config.load_settings()
        body: dict[str, Any] = {
            "accountSpec": s["account_spec"],
            "accountId": s["account_id"],
            "action": action,            # "Buy" or "Sell"
            "symbol": symbol,
            "orderQty": qty,
            "orderType": order_type,     # "Market" | "Limit" | "Stop"
            "isAutomated": True,
        }
        if price is not None:
            body["price"] = price
        if stop_price is not None:
            body["stopPrice"] = stop_price

        data = await self._request("POST", "/order/placeorder", json=body)
        result = {
            "action": action,
            "symbol": symbol,
            "qty": qty,
            "order_type": order_type,
            "price": price,
            "stop_price": stop_price,
            "order_id": (data or {}).get("orderId"),
            "status": "submitted" if data and data.get("orderId") else "rejected",
            "raw": data,
        }
        state.log_order(result)
        return result

    async def modify_order(
        self, order_id: int, *, price: float | None = None,
        stop_price: float | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"orderId": order_id}
        if price is not None:
            body["price"] = price
        if stop_price is not None:
            body["stopPrice"] = stop_price
        return await self._request("POST", "/order/modifyorder", json=body)

    async def cancel_order(self, order_id: int) -> dict[str, Any]:
        return await self._request(
            "POST", "/order/cancelorder", json={"orderId": order_id}
        )

    async def working_orders(self) -> list[dict[str, Any]]:
        orders = await self._request("GET", "/order/list") or []
        active = {"Working", "Pending", "PendingNew", "Suspended"}
        return [o for o in orders if o.get("ordStatus") in active]

    async def liquidate_position(self, symbol: str) -> dict[str, Any]:
        """Flatten the position for ``symbol`` with a market order."""
        s = config.load_settings()
        contract = await self._request(
            "GET", "/contract/find", params={"name": symbol}
        )
        if not contract or not contract.get("id"):
            raise TradovateError(f"Cannot resolve contract id for {symbol}")
        body = {
            "accountId": s["account_id"],
            "contractId": contract["id"],
            "admin": False,
        }
        data = await self._request("POST", "/order/liquidateposition", json=body)
        state.log_order(
            {
                "action": "Liquidate",
                "symbol": symbol,
                "qty": 0,
                "order_type": "Market",
                "status": "submitted",
                "raw": data,
            }
        )
        return data

    async def positions(self) -> list[dict[str, Any]]:
        return await self._request("GET", "/position/list") or []


# Singleton client used across the app.
client = TradovateClient()

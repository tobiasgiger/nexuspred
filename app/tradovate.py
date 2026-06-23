"""Minimal async Tradovate REST client.

Only the endpoints the bridge needs are implemented: authentication, account/contract
lookup, order placement/modify/cancel and position liquidation. The client caches the
access token until shortly before it expires.

Docs: https://api.tradovate.com/
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

# OAuth2 (authorization-code → refresh-token) flow.
OAUTH_AUTHORIZE_URL = "https://trader.tradovate.com/oauth"

# Fallback "web trader" app identities. Tradovate's web platform authenticates
# with these client ids, which do NOT require the paid API access add-on. If the
# user hasn't supplied their own API key (cid/sec), we try these in order — the
# same trick Bridge-Bot-TV uses to connect without an API subscription.
WEB_APP_CONFIGS: list[dict[str, Any]] = [
    {"appId": "Tradovate", "cid": 8, "sec": ""},
    {"appId": "Tradovate", "cid": 2, "sec": ""},
    {"appId": "Tradovate Web", "cid": 8, "sec": ""},
]


class TradovateError(Exception):
    """Raised when the Tradovate API returns an error."""


class TradovatePenalty(TradovateError):
    """Raised when Tradovate returns a time penalty / captcha (p-ticket)."""


def _decode_jwt_exp(token: str) -> datetime | None:
    """Return the ``exp`` claim of a JWT access token as a UTC datetime, if present.

    Tradovate's access tokens are JWTs; the ``exp`` claim is the most reliable
    expiry source (Bridge-Bot-TV reads it the same way).
    """
    try:
        payload_b64 = token.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)  # pad to a multiple of 4
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        exp = payload.get("exp")
        if exp:
            return datetime.fromtimestamp(int(exp), tz=timezone.utc)
    except (IndexError, ValueError, binascii.Error, json.JSONDecodeError):
        pass
    return None


class TradovateClient:
    def __init__(self) -> None:
        self._token: str | None = None          # API user session token
        self._md_token: str | None = None        # market-data (check) token
        self._refresh_token: str | None = None    # OAuth refresh token
        self._token_expires: datetime | None = None
        self._cooldown_until: datetime | None = None  # backoff against lockout
        self._auth_fails = 0
        self._loaded = False
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

    def invalidate(self) -> None:
        """Drop cached tokens so the next call re-reads settings (after edits)."""
        self._loaded = False
        self._token = None
        self._md_token = None
        self._refresh_token = None
        self._token_expires = None
        self._cooldown_until = None
        self._auth_fails = 0

    def _load_persisted(self) -> None:
        """Hydrate tokens from settings once (survives restarts; powers token mode)."""
        if self._loaded:
            return
        s = config.load_settings()
        self._token = self._token or s.get("access_token") or None
        self._md_token = self._md_token or s.get("md_token") or None
        self._refresh_token = self._refresh_token or s.get("refresh_token") or None
        exp = s.get("token_expires")
        if exp and not self._token_expires:
            try:
                self._token_expires = datetime.fromisoformat(exp)
            except ValueError:
                self._token_expires = self._token and _decode_jwt_exp(self._token)
        elif self._token and not self._token_expires:
            self._token_expires = _decode_jwt_exp(self._token)
        self._loaded = True

    async def _get_token(self) -> str:
        async with self._lock:
            self._load_persisted()
            if self._token_valid():
                return self._token  # type: ignore[return-value]

            mode = config.load_settings().get("auth_mode", "credentials")
            # 1) Try a lightweight refresh that needs no password.
            try:
                if mode == "oauth" and self._refresh_token:
                    await self._oauth_refresh()
                elif self._token:
                    await self._renew()
                if self._token_valid(buffer_minutes=0):
                    return self._token  # type: ignore[return-value]
            except TradovateError as exc:
                state.log_event("warn", f"Token refresh failed, re-authenticating: {exc}")

            # 2) Full (re)authentication — gated by the cooldown to avoid lockout.
            now = datetime.now(timezone.utc)
            if self._cooldown_until and now < self._cooldown_until:
                wait = int((self._cooldown_until - now).total_seconds())
                raise TradovateError(
                    f"Authentication cooling down for {wait}s (rate-limit/penalty). "
                    "Check your credentials before retrying."
                )
            await self._authenticate()
            assert self._token is not None
            return self._token

    def _store_token(self, data: dict[str, Any]) -> None:
        self._token = data["accessToken"]
        self._md_token = data.get("mdAccessToken") or self._md_token
        if data.get("refreshToken"):
            self._refresh_token = data["refreshToken"]
        # Expiry: prefer the JWT exp claim, then expirationTime (ISO string),
        # then a conservative default. (Bridge-Bot-TV's bug was treating the ISO
        # expirationTime as an integer number of seconds — we avoid that here.)
        expires = _decode_jwt_exp(self._token) if self._token else None
        if not expires:
            raw = data.get("expirationTime")
            if isinstance(raw, str):
                try:
                    expires = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                except ValueError:
                    expires = None
        self._token_expires = expires or datetime.now(timezone.utc) + timedelta(hours=1)

        self._auth_fails = 0
        self._cooldown_until = None
        state.connection["token_expires"] = self._token_expires.isoformat()
        state.connection["cooldown_until"] = None
        # Persist so tokens survive a restart (renewed without a password).
        config.save_settings({
            "access_token": self._token,
            "md_token": self._md_token or "",
            "refresh_token": self._refresh_token or "",
            "token_expires": self._token_expires.isoformat(),
        })

    def _apply_penalty(self, data: dict[str, Any]) -> None:
        """Record a Tradovate p-ticket time penalty / captcha as a cooldown."""
        wait = int(data.get("p-time", 60) or 60)
        self._cooldown_until = datetime.now(timezone.utc) + timedelta(seconds=wait)
        state.connection["cooldown_until"] = self._cooldown_until.isoformat()
        captcha = " (captcha required — log in via the web platform once)" if \
            data.get("p-captcha") else ""
        raise TradovatePenalty(
            f"Tradovate time penalty: wait {wait}s before retrying{captcha}"
        )

    def _begin_backoff(self, reason: str) -> None:
        """Exponential backoff after a failed password auth, to dodge IP lockout."""
        self._auth_fails += 1
        # Tradovate locks an IP for ~5–10 min after ~5 bad attempts.
        secs = 300 if "password" in reason.lower() else min(300, 20 * 2 ** self._auth_fails)
        self._cooldown_until = datetime.now(timezone.utc) + timedelta(seconds=secs)
        state.connection["cooldown_until"] = self._cooldown_until.isoformat()

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

    def _candidate_bodies(self, s: dict[str, Any]) -> list[dict[str, Any]]:
        """Build the ordered list of accesstokenrequest bodies to try.

        The user's own API key (cid/sec) is tried first; if none is set — or as a
        fallback — the web-trader identities are tried so login works without the
        paid API add-on.
        """
        base = {
            "name": s.get("username", ""),
            "password": s.get("password", ""),
            "appVersion": s.get("app_version", "1.0"),
            "deviceId": s.get("device_id") or "nexuspred",
        }
        bodies: list[dict[str, Any]] = []
        if s.get("cid") and s.get("sec"):
            bodies.append({**base, "appId": s.get("app_id") or "nexuspred",
                           "cid": s["cid"], "sec": s["sec"]})
        if s.get("use_web_trader_fallback", True) or not bodies:
            for cfg in WEB_APP_CONFIGS:
                bodies.append({**base, "appId": cfg["appId"],
                               "cid": cfg["cid"], "sec": cfg["sec"]})
        return bodies

    async def _authenticate(self) -> None:
        s = config.load_settings()
        mode = s.get("auth_mode", "credentials")

        if mode == "token":
            # A token was pasted into settings but is missing/expired in memory.
            self._load_persisted()
            if self._token and self._token_valid(buffer_minutes=0):
                return
            raise TradovateError(
                "Token mode: stored access token is missing or expired — paste a fresh one"
            )

        if mode == "oauth":
            if self._refresh_token:
                await self._oauth_refresh()
                return
            raise TradovateError("OAuth mode: not authorized yet — click ‘Authorize’")

        # --- credentials mode -------------------------------------------------
        if not s.get("username") or not s.get("password"):
            raise TradovateError("Missing username/password")

        last_err = "no app configuration succeeded"
        for body in self._candidate_bodies(s):
            try:
                data = await self._request(
                    "POST", "/auth/accesstokenrequest", auth=False, json=body
                )
            except TradovateError as exc:
                last_err = str(exc)
                continue
            if data and (data.get("p-ticket") or data.get("p-captcha")):
                self._apply_penalty(data)  # raises TradovatePenalty
            if data and data.get("accessToken"):
                self._store_token(data)
                state.connection["last_auth"] = datetime.now(timezone.utc).isoformat()
                state.log_event(
                    "info", f"Authenticated (appId={body['appId']}, cid={body['cid']})"
                )
                return
            last_err = (data or {}).get("errorText", "authentication rejected")

        self._begin_backoff(last_err)
        raise TradovateError(f"Authentication failed: {last_err}")

    # ----------------------------------------------------------------- oauth
    def oauth_authorize_url(self, redirect_uri: str) -> str:
        """Build the Tradovate OAuth consent URL for the authorization-code flow."""
        s = config.load_settings()
        client_id = s.get("oauth_client_id") or "3159"
        return (
            f"{OAUTH_AUTHORIZE_URL}?response_type=code&client_id={client_id}"
            f"&redirect_uri={redirect_uri}&scope=trading"
        )

    async def oauth_exchange_code(self, code: str, redirect_uri: str) -> None:
        """Exchange an authorization code for an access + refresh token."""
        s = config.load_settings()
        body = {
            "grant_type": "authorization_code",
            "code": code,
            "client_id": s.get("oauth_client_id") or "3159",
            "redirect_uri": redirect_uri,
        }
        if s.get("oauth_client_secret"):
            body["client_secret"] = s["oauth_client_secret"]
        data = await self._request("POST", "/auth/oauthtoken", auth=False, json=body)
        if not data or not data.get("accessToken"):
            raise TradovateError(f"OAuth exchange failed: {(data or {}).get('error_description', data)}")
        self._store_token(data)
        state.connection["last_auth"] = datetime.now(timezone.utc).isoformat()
        state.log_event("info", "OAuth authorization complete")

    async def _oauth_refresh(self) -> None:
        """Get a fresh access token from the OAuth refresh token (same env host)."""
        if not self._refresh_token:
            raise TradovateError("No OAuth refresh token")
        s = config.load_settings()
        body = {
            "grant_type": "refresh_token",
            "refresh_token": self._refresh_token,
            "client_id": s.get("oauth_client_id") or "3159",
        }
        if s.get("oauth_client_secret"):
            body["client_secret"] = s["oauth_client_secret"]
        data = await self._request("POST", "/auth/oauthtoken", auth=False, json=body)
        if not data or not data.get("accessToken"):
            raise TradovateError(f"OAuth refresh failed: {(data or {}).get('error_description', data)}")
        self._store_token(data)
        state.connection["last_renew"] = datetime.now(timezone.utc).isoformat()
        state.log_event("info", "OAuth access token refreshed")

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

    def _can_authenticate(self, s: dict[str, Any]) -> bool:
        """Whether the current settings can produce a token in the selected mode."""
        mode = s.get("auth_mode", "credentials")
        if mode == "token":
            return bool(self._token or s.get("access_token"))
        if mode == "oauth":
            return bool(self._refresh_token or s.get("refresh_token") or self._token)
        return bool(s.get("username") and s.get("password"))

    async def health_check(self) -> dict[str, Any]:
        """Verify the connection is up: ensure a valid token (renewing if needed)
        and make a lightweight authenticated call. Updates the status snapshot.

        Safe to call on a timer — never raises; failures are recorded on the
        connection state so the dashboard can show them.
        """
        s = config.load_settings()
        self._load_persisted()
        if not self._can_authenticate(s):
            state.connection.update(
                connected=False, last_error="Not configured (no credentials/token)"
            )
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

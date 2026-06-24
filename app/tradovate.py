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
import os
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from . import config, state

LIVE_BASE = "https://live.tradovateapi.com/v1"
DEMO_BASE = "https://demo.tradovateapi.com/v1"

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


# Futures month codes → calendar month, for front-month selection.
_MONTH_CODES = {"F": 1, "G": 2, "H": 3, "J": 4, "K": 5, "M": 6,
                "N": 7, "Q": 8, "U": 9, "V": 10, "X": 11, "Z": 12}


def _front_month_key(name: str, root: str) -> tuple[int, int]:
    """Sort key (year, month) parsed from a contract name like ``MNQM5``.

    Contracts at/after the current month sort before past ones, so the nearest
    active (front) month comes first. Unparseable names sort last.
    """
    suffix = name[len(root):]
    if len(suffix) < 2 or suffix[0] not in _MONTH_CODES:
        return (9999, 99)
    month = _MONTH_CODES[suffix[0]]
    digits = suffix[1:]
    now = datetime.now(timezone.utc)
    try:
        if len(digits) == 1:                 # single-digit year, e.g. "5" → 2025
            year = now.year - (now.year % 10) + int(digits)
            if year < now.year - 1:          # rolled into the next decade
                year += 10
        else:
            year = 2000 + int(digits)
    except ValueError:
        return (9999, 99)
    # Past contracts get pushed to the back by adding a century.
    past = (year, month) < (now.year, now.month)
    return (year + (100 if past else 0), month)


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


# Auth fields that can be supplied via environment variables (handy on managed
# hosts like Render, where they persist across deploys). Env overrides the stored
# value. Credentials (username/password) never expire — unlike an access token —
# so they're the durable way to stay logged in across redeploys.
_ENV_FIELDS = {
    "username": "TRADOVATE_USERNAME",
    "password": "TRADOVATE_PASSWORD",
    "cid": "TRADOVATE_CID",
    "sec": "TRADOVATE_SEC",
    "app_id": "TRADOVATE_APP_ID",
    "device_id": "TRADOVATE_DEVICE_ID",
    "environment": "TRADOVATE_ENVIRONMENT",
}


def _auth_settings() -> dict[str, Any]:
    """Stored settings overlaid with any auth env vars (env wins)."""
    s = dict(config.load_settings())
    for key, env in _ENV_FIELDS.items():
        val = os.getenv(env)
        if val:
            s[key] = val
    return s


class TradovateClient:
    def __init__(self) -> None:
        self._token: str | None = None          # API user session token
        self._md_token: str | None = None        # check (market-data) token
        self._token_expires: datetime | None = None
        self._cooldown_until: datetime | None = None  # backoff against lockout
        self._auth_fails = 0
        self._loaded = False
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _base_url() -> str:
        env = os.getenv("TRADOVATE_ENVIRONMENT") or config.load_settings().get("environment")
        return LIVE_BASE if env == "live" else DEMO_BASE

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

    def seconds_until_refresh(self, fallback: int = 60) -> float:
        """Delay until the next proactive refresh.

        Renew at least 5 minutes before the token expires, and re-check at least
        every 25 minutes (Bridge-Bot-TV's interval) even if expiry is far off.
        """
        if not self._token_expires:
            return float(fallback)
        secs = (self._token_expires - datetime.now(timezone.utc)).total_seconds() - 300
        return max(15.0, min(secs, 25 * 60.0))

    async def proactive_refresh(self) -> None:
        """Force a token renewal now (access → check token → credentials login),
        even if the current token is still valid, so it never reaches expiry.

        Mirrors Bridge-Bot-TV's interval-based auto-refresh. Safe re: the lock —
        _renew/_authenticate use auth=False requests and never re-acquire it.
        """
        async with self._lock:
            self._load_persisted()
            if not self._token:
                await self._authenticate()
                return
            try:
                await self._renew()
            except TradovateError as exc:
                now = datetime.now(timezone.utc)
                if self._cooldown_until and now < self._cooldown_until:
                    raise
                state.log_event("warn", f"Proactive renew failed, logging in: {exc}")
                await self._authenticate()

    def invalidate(self) -> None:
        """Drop cached tokens so the next call re-reads env/settings (after edits)."""
        self._loaded = False
        self._token = None
        self._md_token = None
        self._token_expires = None
        self._cooldown_until = None
        self._auth_fails = 0

    def _load_persisted(self) -> None:
        """Hydrate tokens once from the env vars and/or stored settings.

        Both the ``TRADOVATE_ACCESS_TOKEN`` env var and the token persisted on disk
        (the result of earlier renewals) are considered, and the one with the
        **later expiry wins**. This is the key to surviving a redeploy: a static
        env token goes stale, so the freshly-renewed token on the persistent disk
        must take precedence over it.
        """
        if self._loaded:
            return
        s = config.load_settings()
        very_old = datetime.min.replace(tzinfo=timezone.utc)

        candidates: list[tuple[str, str | None, datetime]] = []
        env_access = os.getenv("TRADOVATE_ACCESS_TOKEN", "").strip()
        if env_access:
            candidates.append((
                env_access, os.getenv("TRADOVATE_CHECK_TOKEN", "").strip() or None,
                _decode_jwt_exp(env_access) or very_old,
            ))
        stored = s.get("access_token")
        if stored:
            exp = _decode_jwt_exp(stored)
            if not exp and s.get("token_expires"):
                try:
                    exp = datetime.fromisoformat(s["token_expires"])
                except ValueError:
                    exp = None
            candidates.append((stored, s.get("md_token") or None, exp or very_old))

        if candidates:
            token, check, exp = max(candidates, key=lambda c: c[2])
            self._token = token
            self._md_token = check
            self._token_expires = None if exp == very_old else exp
            if self._token_expires:
                state.connection["token_expires"] = self._token_expires.isoformat()
        self._loaded = True

    async def _get_token(self) -> str:
        async with self._lock:
            self._load_persisted()
            if self._token_valid():
                return self._token  # type: ignore[return-value]

            # 1) Renew the existing token without a password (access → check token).
            if self._token:
                try:
                    await self._renew()
                    if self._token_valid(buffer_minutes=0):
                        return self._token  # type: ignore[return-value]
                except TradovateError as exc:
                    state.log_event("warn", f"Token renew failed, logging in: {exc}")

            # 2) Full credentials login — gated by the cooldown to avoid lockout.
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
        self._token_expires = expires or datetime.now(timezone.utc) + timedelta(minutes=75)

        self._auth_fails = 0
        self._cooldown_until = None
        state.connection["token_expires"] = self._token_expires.isoformat()
        state.connection["cooldown_until"] = None
        # Persist so tokens survive a restart — best-effort: a failed write (e.g.
        # a read-only data dir) must NOT break renewal, or the session would drop.
        try:
            config.save_settings({
                "access_token": self._token,
                "md_token": self._md_token or "",
                "token_expires": self._token_expires.isoformat(),
            })
        except OSError as exc:
            state.log_event("warn", f"Could not persist token (using in-memory): {exc}")

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
        """Renew the access token without credentials.

        Mirrors Bridge-Bot-TV: try the access token first, then fall back to the
        check (market-data) token. If both fail, the caller logs in.
        """
        last_err = "renew returned no token"
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
                state.connection["last_renew"] = datetime.now(timezone.utc).isoformat()
                state.log_event("info", f"Access token renewed (via {label})")
                return
            last_err = (data or {}).get("errorText", last_err)
        raise TradovateError(f"Renew failed: {last_err}")

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
        """Full credentials login (the fallback when no/expired token is available)."""
        s = _auth_settings()
        if not s.get("username") or not s.get("password"):
            raise TradovateError(
                "No usable token and no username/password — set TRADOVATE_ACCESS_TOKEN "
                "or credentials (TRADOVATE_USERNAME / TRADOVATE_PASSWORD)"
            )

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

    async def connect(self) -> dict[str, Any]:
        """Authenticate and resolve the trading account; update status snapshot."""
        s = _auth_settings()
        try:
            await self._get_token()
            accounts = await self.list_accounts()
            merged = self._merge_accounts(accounts, s)
            account = self._select_account(accounts, s.get("account_spec"))
            updates: dict[str, Any] = {"accounts": merged}
            if account:
                updates["account_spec"] = account["name"]
                updates["account_id"] = account["id"]
            config.save_settings(updates)
            enabled = [a for a in merged if a["enabled"]]
            state.connection.update(
                connected=True,
                environment=s.get("environment", "demo"),
                account_spec=account["name"] if account else "",
                account_id=account["id"] if account else 0,
                last_error="",
            )
            state.connection["accounts_total"] = len(merged)
            state.connection["accounts_enabled"] = len(enabled)
            state.log_event(
                "info",
                f"Connected to Tradovate — {len(enabled)}/{len(merged)} account(s) enabled",
            )
            return state.connection
        except Exception as exc:  # noqa: BLE001 - surface any failure to dashboard
            state.connection.update(connected=False, last_error=str(exc))
            state.log_event("error", f"Connect failed: {exc}")
            raise

    def _can_authenticate(self, s: dict[str, Any]) -> bool:
        """Whether a token can be obtained: a token (env/stored) or credentials."""
        has_token = bool(
            self._token or s.get("access_token")
            or os.getenv("TRADOVATE_ACCESS_TOKEN")
        )
        has_creds = bool(
            (s.get("username") or os.getenv("TRADOVATE_USERNAME"))
            and (s.get("password") or os.getenv("TRADOVATE_PASSWORD"))
        )
        return has_token or has_creds

    async def health_check(self) -> dict[str, Any]:
        """Verify the connection is up: ensure a valid token (renewing if needed)
        and make a lightweight authenticated call. Updates the status snapshot.

        Safe to call on a timer — never raises; failures are recorded on the
        connection state so the dashboard can show them.
        """
        s = _auth_settings()
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

    @staticmethod
    def _merge_accounts(
        fetched: list[dict[str, Any]], s: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """Merge accounts from /account/list with the stored enable/multiplier flags.

        Existing choices are preserved. On first discovery, the legacy primary
        account (or the first account, if none) is enabled so behaviour matches the
        previous single-account default.
        """
        existing = {a.get("id"): a for a in (s.get("accounts") or [])}
        legacy_id = s.get("account_id")
        merged: list[dict[str, Any]] = []
        for i, acc in enumerate(fetched):
            prev = existing.get(acc["id"])
            if prev is not None:
                enabled = bool(prev.get("enabled"))
                mult = prev.get("qty_multiplier", 1)
            elif legacy_id:
                enabled = acc["id"] == legacy_id
                mult = 1
            else:
                enabled = i == 0
                mult = 1
            merged.append({
                "id": acc["id"],
                "name": acc["name"],
                "enabled": enabled,
                "qty_multiplier": mult,
            })
        return merged

    # ------------------------------------------------------------- contracts
    async def resolve_contract(self, root_or_symbol: str) -> str:
        """Return a tradable Tradovate contract symbol for a root like ``MNQ``.

        If a fully dated symbol is supplied (e.g. ``MNQU5``) it is verified and
        returned as-is; otherwise the nearest (front-month) contract is chosen.
        """
        # Already a dated contract? verify it exists. Tradovate answers /contract/find
        # with HTTP 404 when the name isn't an exact contract, so treat that as
        # "not found" and fall through to /contract/suggest.
        try:
            found = await self._request(
                "GET", "/contract/find", params={"name": root_or_symbol}
            )
            if found and found.get("name"):
                return found["name"]
        except TradovateError:
            pass

        # Otherwise suggest contracts for the root and pick the front month.
        try:
            suggestions = await self._request(
                "GET", "/contract/suggest", params={"t": root_or_symbol, "l": 20}
            )
        except TradovateError as exc:
            raise TradovateError(f"No contract found for '{root_or_symbol}': {exc}")
        candidates = [
            c for c in (suggestions or [])
            if c.get("name", "").startswith(root_or_symbol)
        ]
        if not candidates:
            raise TradovateError(f"No contract found for '{root_or_symbol}'")
        # Front month = nearest active contract (parsed from the month/year code;
        # fall back to expirationDate/name if the code can't be parsed).
        candidates.sort(
            key=lambda c: (
                _front_month_key(c.get("name", ""), root_or_symbol),
                c.get("expirationDate") or c.get("name", ""),
            )
        )
        return candidates[0]["name"]

    async def list_accounts(self) -> list[dict[str, Any]]:
        return await self._request("GET", "/account/list") or []

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
        account_spec: str | None = None,
        account_id: int | None = None,
    ) -> dict[str, Any]:
        s = config.load_settings()
        spec = account_spec or s.get("account_spec")
        acct_id = account_id or s.get("account_id")
        body: dict[str, Any] = {
            "accountSpec": spec,
            "accountId": acct_id,
            "action": action,            # "Buy" or "Sell"
            "symbol": symbol,
            "orderQty": qty,
            "orderType": order_type,     # "Market" | "Limit" | "Stop"
            "isAutomated": True,
        }
        # Tradovate rejects a price on Market orders and a stopPrice on non-stop
        # orders, so only attach each field to the order types that accept it.
        sent_price = price if order_type in ("Limit", "StopLimit") else None
        sent_stop = stop_price if order_type in ("Stop", "StopLimit") else None
        if sent_price is not None:
            body["price"] = sent_price
        if sent_stop is not None:
            body["stopPrice"] = sent_stop

        data = await self._request("POST", "/order/placeorder", json=body)
        result = {
            "action": action,
            "symbol": symbol,
            "account": spec,
            "qty": qty,
            "order_type": order_type,
            "price": sent_price,
            "stop_price": sent_stop,
            "order_id": (data or {}).get("orderId"),
            "status": "submitted" if data and data.get("orderId") else "rejected",
            "raw": data,
        }
        state.log_order(result)
        return result

    async def modify_order(
        self, order_id: int, *, qty: int, order_type: str,
        price: float | None = None, stop_price: float | None = None,
    ) -> dict[str, Any]:
        # Tradovate's /order/modifyorder requires orderQty and orderType (not just
        # the changed field), so we always send the full order shape.
        body: dict[str, Any] = {
            "orderId": order_id, "orderQty": qty, "orderType": order_type,
        }
        if order_type in ("Limit", "StopLimit") and price is not None:
            body["price"] = price
        if order_type in ("Stop", "StopLimit") and stop_price is not None:
            body["stopPrice"] = stop_price
        data = await self._request("POST", "/order/modifyorder", json=body)
        state.log_order({
            "action": "Modify", "symbol": "", "qty": qty, "order_type": order_type,
            "price": body.get("price"), "stop_price": body.get("stopPrice"),
            "order_id": order_id, "status": "modified", "raw": data,
        })
        return data

    async def cancel_order(self, order_id: int) -> dict[str, Any]:
        return await self._request(
            "POST", "/order/cancelorder", json={"orderId": order_id}
        )

    async def working_orders(self, account_id: int | None = None) -> list[dict[str, Any]]:
        orders = await self._request("GET", "/order/list") or []
        active = {"Working", "Pending", "PendingNew", "Suspended"}
        result = [o for o in orders if o.get("ordStatus") in active]
        if account_id is not None:
            result = [o for o in result if o.get("accountId") == account_id]
        return result

    async def liquidate_position(
        self, symbol: str, account_id: int | None = None
    ) -> dict[str, Any]:
        """Flatten the position for ``symbol`` in the given account."""
        s = config.load_settings()
        acct_id = account_id or s.get("account_id")
        contract = await self._request(
            "GET", "/contract/find", params={"name": symbol}
        )
        if not contract or not contract.get("id"):
            raise TradovateError(f"Cannot resolve contract id for {symbol}")
        body = {
            "accountId": acct_id,
            "contractId": contract["id"],
            "admin": False,
        }
        data = await self._request("POST", "/order/liquidateposition", json=body)
        state.log_order(
            {
                "action": "Liquidate",
                "symbol": symbol,
                "account": acct_id,
                "qty": 0,
                "order_type": "Market",
                "status": "submitted",
                "raw": data,
            }
        )
        return data

    async def positions(self) -> list[dict[str, Any]]:
        """Return only *open* positions (netPos != 0) with the contract symbol.

        Tradovate's /position/list reports the numeric contractId and includes
        flat (closed) positions; we filter those out and resolve each contract id
        to its readable name.
        """
        raw = await self._request("GET", "/position/list") or []
        names: dict[int, str] = {}
        out: list[dict[str, Any]] = []
        for p in raw:
            net = p.get("netPos") or 0
            if not net:                      # skip flat / closed positions
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
            out.append({
                "symbol": name,
                "account_id": p.get("accountId"),
                "netPos": net,
                "netPrice": p.get("netPrice"),
                "openPL": p.get("openPL") or 0,
            })
        return out


# Singleton client used across the app.
client = TradovateClient()

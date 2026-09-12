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

from . import alerts, broker, config, context, http, risk, sizing, state

REQUEST_SPACING_S = 0.2   # minimum gap between two requests of one login (5/s)
PRIORITY_SPACING_S = 0.06  # orders / cancels / liquidations: a small gap of their own, never behind polls
PRIORITY_PENALTY_WAIT_S = 3.0  # a 429 penalty shorter than this is waited out for an order; longer → refused
PRIORITY_PATHS = ("/order/placeorder", "/order/placeoco", "/order/modifyorder", "/order/cancelorder", "/order/liquidateposition")
LIVE_BASE = "https://live.tradovateapi.com/v1"
DEMO_BASE = "https://demo.tradovateapi.com/v1"

_MONTH_CODES = {"F": 1, "G": 2, "H": 3, "J": 4, "K": 5, "M": 6,
                "N": 7, "Q": 8, "U": 9, "V": 10, "X": 11, "Z": 12}


class TradovateError(Exception):
    """Raised when the Tradovate API returns an error."""


class OrderOutcomeUnknown(TradovateError):
    """The order request timed out after it may have reached the broker: not a
    rejection. Callers never cancel or re-place blindly on it."""


WORKING_STATUSES = frozenset({"Working", "Pending", "PendingNew", "PendingReplace", "PendingCancel", "Suspended"})


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
        "lid": entry.get("lid") or "",
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


def _close_later(session: Any) -> None:
    """Close a replaced broker session in the background (sockets, login slots)."""
    async def run() -> None:
        try:
            await asyncio.wait_for(session.close(), timeout=10.0)
        except Exception:  # noqa: BLE001
            pass
    try:
        _fire(run())
    except RuntimeError:                                # no running loop (tests)
        pass


def _fire(coro: Any) -> None:
    """Run an alert in the background (context — and so the area — is inherited).
    A 15 s SMTP handshake must never stall a health check or a connect."""
    task = asyncio.get_running_loop().create_task(coro)
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


def _outcome_unknown(name: str, path: str, detail: Any) -> OrderOutcomeUnknown:
    """A priority mutation lost its answer after it may have reached Tradovate."""
    msg = f"[{name}] {path}: {detail} — outcome unknown, CHECK THE ACCOUNT"
    state.log_event("error", msg)
    _fire(alerts.execution_problem(
        f"Order outcome unknown on {name}",
        f"{path}: {detail}. Check the account for an untracked position or order.",
    ))
    return OrderOutcomeUnknown(msg)


class TradovateSession:
    """One Tradovate account, authenticated by its own (renewable) access token."""

    def __init__(self, idx: int, entry: dict[str, Any], area_id: int | None = None) -> None:
        self.idx = idx
        self.area_id = area_id
        self.lid = entry.get("lid") or ""
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
        self._pace_lock = asyncio.Lock()          # one request at a time per login
        self._prio_lock = asyncio.Lock()          # orders have their own, shorter spacing
        self._last_sent: float = 0.0
        self._last_prio: float = 0.0
        self.penalty_until: float = 0.0           # monotonic; set by a 429
        self.rate_limits = 0
        self._contract_id_cache: dict[str, tuple[int, datetime]] = {}
        self._contract_names: dict[int, str] = {}      # contract id -> name (positions view)
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
        """One paced request: at most one every ``REQUEST_SPACING_S`` per login,
        and nothing at all while a 429 penalty is running — every loop that
        uses this login shares that budget, so one busy loop cannot get the
        whole login banned."""
        import time as _time
        if path.startswith(PRIORITY_PATHS):
            # orders, cancels, liquidations: never queue behind polls; a burst of
            # them is spaced a little so it does not trip the 429 that would then
            # refuse the stop of the same bracket; a short running penalty is
            # waited out, a long one is an immediate refusal (never a minute late)
            async with self._prio_lock:
                left = self.penalty_until - _time.monotonic()
                if left > PRIORITY_PENALTY_WAIT_S:
                    raise RateLimited(path, "", left)
                gap = max(left, self._last_prio + PRIORITY_SPACING_S - _time.monotonic())
                if gap > 0:
                    await asyncio.sleep(gap)
                self._last_prio = _time.monotonic()
        else:
            async with self._pace_lock:
                now = _time.monotonic()
                wait = max(self.penalty_until - now, self._last_sent + REQUEST_SPACING_S - now)
                if wait > 0:
                    await asyncio.sleep(min(wait, 120.0))
                self._last_sent = _time.monotonic()
        try:
            return await self._request_raw(method, path, auth=auth, **kwargs)
        except RateLimited as exc:
            self.rate_limits += 1
            self.penalty_until = _time.monotonic() + exc.retry_after
            raise

    async def _request_raw(self, method: str, path: str, *, auth: bool = True, **kwargs: Any) -> Any:
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
            except relay.ResultUnknown as exc:
                if path.startswith(PRIORITY_PATHS):
                    raise _outcome_unknown(self.name, path, f"execution agent: {exc}") from exc
                raise TradovateError(f"[{self.name}] {exc}") from exc
            except relay.AgentOffline as exc:
                raise TradovateError(f"[{self.name}] {exc}") from exc
            if status == 429:
                raise RateLimited(path, text, _penalty_seconds(text))
            if status >= 400:
                raise TradovateError(f"{status} {path}: {text}")
            return json.loads(text) if text else None
        # Pooled, keep-alive client: no TLS handshake per order (see app.http).
        try:
            resp = await http.client("tradovate").request(method, url, headers=headers, **kwargs)
        except httpx.TransportError as exc:
            if path.startswith(PRIORITY_PATHS):
                raise _outcome_unknown(self.name, path, exc) from exc
            raise TradovateError(f"[{self.name}] {path}: {exc}") from exc
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
                    if self._token_valid(buffer_minutes=0):
                        return self._token  # type: ignore[return-value]   # still valid: use it, retry the renewal later
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
                self.idx, area_id=self.area_id, lid=self.lid,
                access_token=self._token, md_token=self._md_token or "",
                token_expires=self._token_expires.isoformat(),
            )
        except Exception as exc:  # noqa: BLE001 - a renewed token must never fail because the disk did
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
    # ---- broker feed surface (see app.broker.BrokerSession) ------------------
    kind = "tradovate"

    async def positions_snapshot(self) -> list[dict[str, Any]]:
        raw = await self._request("GET", "/position/list") or []
        return raw if isinstance(raw, list) else []

    async def orders_snapshot(self) -> list[dict[str, Any]]:
        raw = await self._request("GET", "/order/list") or []
        return raw if isinstance(raw, list) else []

    async def account_list(self) -> list[dict[str, Any]]:
        raw = await self._request("GET", "/account/list") or []
        return raw if isinstance(raw, list) else []

    async def cash_snapshot(self, account_id: int) -> dict[str, Any]:
        data = await self._request("POST", "/cashBalance/getcashbalancesnapshot", json={"accountId": int(account_id)})
        return data if isinstance(data, dict) else {}

    async def auto_liq_rules(self) -> list[dict[str, Any]]:
        raw = await self._request("GET", "/userAccountAutoLiq/list") or []
        return raw if isinstance(raw, list) else []

    async def user_id(self) -> int:
        try:
            me = await self._request("GET", "/auth/me") or {}
            uid = int(me.get("userId") or me.get("id") or 0)
            if uid:
                return uid
        except Exception:  # noqa: BLE001
            pass
        try:
            users = await self._request("GET", "/user/list") or []
            return int((users[0] or {}).get("id") or 0) if isinstance(users, list) and users else 0
        except Exception:  # noqa: BLE001
            return 0

    async def contract_info(self, contract_id: int) -> dict[str, Any]:
        item = await self._request("GET", "/contract/item", params={"id": int(contract_id)})
        return item if isinstance(item, dict) else {}

    async def contract_find(self, name: str) -> dict[str, Any]:
        found = await self._request("GET", "/contract/find", params={"name": name})
        return found if isinstance(found, dict) else {}

    async def contract_suggest(self, root: str, limit: int = 30) -> list[dict[str, Any]]:
        raw = await self._request("GET", "/contract/suggest", params={"t": root, "l": int(limit)}) or []
        return raw if isinstance(raw, list) else []

    async def contract_maturity(self, maturity_id: int) -> dict[str, Any]:
        mat = await self._request("GET", "/contractMaturity/item", params={"id": int(maturity_id)})
        return mat if isinstance(mat, dict) else {}

    async def raw_get(self, path: str, *, params: dict[str, Any] | None = None) -> Any:
        """Tradovate-specific report endpoints (journal importer)."""
        return await self._request("GET", path, params=params) if params else await self._request("GET", path)

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
            old = prev.get(spec) or {}
            mult = float(old.get("qty_multiplier", self.qty_multiplier) or 1)
            entry = {"spec": spec, "id": a.get("id") or 0, "enabled": bool(old.get("enabled", True)) if old else True, "qty_multiplier": mult}
            if old.get("risk"):
                entry["risk"] = dict(old["risk"])          # per-account settings survive a re-discovery
            merged.append(entry)
        self.accounts = merged

    async def _set_connected(self, connected: bool, **fields: Any) -> None:
        """Update connection status, firing a connection lost/restored alert on
        transition. The very first observation of a session is never alerted
        on (there's no prior state to have transitioned from)."""
        had_prior = state.has_session(self.name)
        was_connected = state.session_status(self.name).get("connected") if had_prior else None
        state.set_session_status(self.name, connected=connected, agent_id=self.agent_id, **fields)
        if had_prior and was_connected and not connected:
            _fire(alerts.connection_lost(self.name, self.environment, fields.get("last_error", ""), broker=getattr(self, "kind", "tradovate")))
        elif had_prior and not was_connected and connected:
            _fire(alerts.connection_restored(self.name, self.environment, broker=getattr(self, "kind", "tradovate")))

    async def connect(self) -> dict[str, Any]:
        try:
            await self._get_token()
            discovered = await self.list_accounts()
            if discovered:
                self._merge_accounts(discovered)
            elif self.accounts:
                state.log_event("warn", f"[{self.name}] Tradovate listed no accounts — keeping the {len(self.accounts)} known one(s)")
            primary = next((a for a in self.accounts if a.get("enabled")), None) \
                or (self.accounts[0] if self.accounts else None)
            if primary:
                self.account_spec = primary["spec"]
                self.account_id = primary["id"]
            try:
                config.update_token_account(
                    self.idx, area_id=self.area_id, lid=self.lid, accounts=self.accounts,
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
        except RateLimited as exc:
            state.set_session_status(self.name, last_error=f"rate limited: {exc}")   # throttled, not disconnected
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
        from . import rollover
        dated = rollover.parse_contract(root_or_symbol) is not None   # an exact contract: taken as given

        def still_ahead(name: str) -> bool:
            p = rollover.parse_contract(name)
            if not p:
                return True
            return rollover.roll_date(*p)[0] >= datetime.now(timezone.utc).date()
        try:
            found = await self._request("GET", "/contract/find", params={"name": root_or_symbol})
            if found and found.get("name") and (dated or still_ahead(found["name"])):
                return found["name"]
        except TradovateError:
            pass
        try:
            suggestions = await self._request(
                "GET", "/contract/suggest", params={"t": root_or_symbol, "l": 20})
        except TradovateError as exc:
            raise TradovateError(f"No contract found for '{root_or_symbol}': {exc}")
        candidates = [c for c in (suggestions or []) if c.get("name", "").startswith(root_or_symbol)]
        ahead = [c for c in candidates if still_ahead(c.get("name", ""))]
        if ahead:
            candidates = ahead                     # skip months already past their roll date
        if not candidates:
            raise TradovateError(f"No contract found for '{root_or_symbol}'")
        candidates.sort(key=lambda c: (_front_month_key(c.get("name", ""), root_or_symbol),
                                       c.get("expirationDate") or c.get("name", "")))
        return candidates[0]["name"]

    # -------------------------------------------------------- order results
    @staticmethod
    def _order_failure(data: Any) -> str | None:
        """Tradovate answers a refused order/cancel/modify with HTTP 200 and a
        ``failureReason`` / ``failureText`` body. Returns that text, else None."""
        if isinstance(data, dict) and (data.get("failureReason") or data.get("failureText")):
            return str(data.get("failureText") or data.get("failureReason"))
        if isinstance(data, dict) and data.get("errorText"):
            return str(data["errorText"])
        return None

    # ------------------------------------------------------------ targeting
    def _target(self, account_spec: str | None, account_id: int | None) -> tuple[str, int]:
        """(spec, id) of the account a call is meant for. A call that names a
        trade account (``account_spec``) must never drift to the login's primary
        account: the id is taken from the call, else from the discovered account
        list by spec — and if it is still unknown the call is refused. Only a
        call without any spec (the legacy single-account path) uses the primary."""
        if account_spec:
            aid = int(account_id or 0)
            if not aid:
                for a in self.accounts:
                    if a.get("spec") == account_spec and a.get("id"):
                        aid = int(a["id"])
                        break
            if not aid and account_spec == self.account_spec and self.account_id:
                aid = int(self.account_id)
            if not aid:
                raise TradovateError(f"[{self.name}] no Tradovate account id known for {account_spec} — run Connect & Verify")
            return account_spec, aid
        return self.account_spec, int(account_id or self.account_id or 0)

    # ---------------------------------------------------------------- orders
    async def place_order(self, *, symbol: str, action: str, qty: int, order_type: str,
                          price: float | None = None, stop_price: float | None = None,
                          account_spec: str | None = None, account_id: int | None = None,
                          account_name: str | None = None) -> dict[str, Any]:
        spec, aid = self._target(account_spec, account_id)
        name = account_name or self.name
        if not risk.bypassed():
            locked = risk.is_locked(self.area_id if self.area_id is not None else context.get_area(), spec)
            if locked:
                state.log_order({"action": action, "symbol": symbol, "account": name, "account_id": aid, "qty": qty,
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
        failure = self._order_failure(data) or ("" if data and data.get("orderId") else "no orderId in the answer")
        result = {
            "action": action, "symbol": symbol, "account": name, "account_id": aid, "qty": qty,
            "order_type": order_type, "price": sent_price, "stop_price": sent_stop,
            "order_id": (data or {}).get("orderId"),
            "status": "rejected" if failure else "submitted",
            "raw": data,
        }
        state.log_order(result)
        if failure:
            raise TradovateError(f"{name}: {action} {qty} {symbol} {order_type} rejected — {failure}")
        return result

    async def place_oco(self, *, symbol: str, action: str, qty: int, order_type: str,
                        price: float | None, stop_price: float | None, other: dict[str, Any],
                        account_spec: str | None = None, account_id: int | None = None,
                        account_name: str | None = None) -> dict[str, Any]:
        """Two orders that cancel each other (``/order/placeoco``): the first from
        the keyword arguments, the second from ``other`` (``action``, ``order_type``,
        ``price`` / ``stop_price``). Returns ``{order_id, oco_id, status, raw}``."""
        spec, aid = self._target(account_spec, account_id)
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
        failure = self._order_failure(data) or ("" if data and data.get("orderId") else "no orderId in the answer")
        ok = not failure
        for leg, kind in ((body, order_type), (o, other["order_type"])):
            state.log_order({"action": leg["action"], "symbol": symbol, "account": name, "qty": qty, "order_type": kind,
                             "price": leg.get("price"), "stop_price": leg.get("stopPrice"),
                             "order_id": (data or {}).get("orderId") if leg is body else (data or {}).get("ocoId"),
                             "status": "submitted" if ok else "rejected", "raw": data})
        if failure:
            raise TradovateError(f"{name}: OCO {action} {qty} {symbol} rejected — {failure}")
        return {"order_id": (data or {}).get("orderId"), "oco_id": (data or {}).get("ocoId"),
                "status": "submitted", "raw": data}

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
        failure = self._order_failure(data)
        state.log_order({"action": "Modify", "symbol": "", "account": account_name or self.name,
                         "qty": qty, "order_type": order_type, "price": body.get("price"),
                         "stop_price": body.get("stopPrice"), "order_id": order_id,
                         "status": "rejected" if failure else "modified", "raw": data})
        if failure:
            raise TradovateError(f"modify order {order_id} rejected — {failure}")
        return data

    async def cancel_order(self, order_id: int) -> dict[str, Any]:
        data = await self._request("POST", "/order/cancelorder", json={"orderId": order_id})
        failure = self._order_failure(data)
        if failure:
            raise TradovateError(f"cancel order {order_id} rejected — {failure}")
        return data

    async def working_orders(self, account_id: int | None = None, *, account_spec: str | None = None) -> list[dict[str, Any]]:
        _, aid = self._target(account_spec, account_id)
        orders = await self._request("GET", "/order/list") or []
        return [o for o in orders
                if o.get("ordStatus") in WORKING_STATUSES and o.get("accountId") == aid]

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
                                 account_name: str | None = None, account_spec: str | None = None) -> dict[str, Any]:
        _, aid = self._target(account_spec, account_id)
        cid = await self.contract_id(symbol)          # cached: no extra round trip on a close
        data = await self._request("POST", "/order/liquidateposition",
                                   json={"accountId": aid, "contractId": cid, "admin": False})
        failure = self._order_failure(data)
        state.log_order({"action": "Liquidate", "symbol": symbol,
                         "account": account_name or self.name, "account_id": aid,
                         "qty": 0, "order_type": "Market", "status": "rejected" if failure else "submitted", "raw": data})
        if failure:
            raise TradovateError(f"{account_name or self.name}: liquidate {symbol} rejected — {failure}")
        return data

    async def contract_name(self, contract_id: int) -> str:
        """The contract's name for a broker id — cached for the session (a
        contract id never changes its name)."""
        cached = self._contract_names.get(contract_id)
        if cached is not None:
            return cached
        try:
            item = await self._request("GET", "/contract/item", params={"id": contract_id})
            name = (item or {}).get("name") or str(contract_id)
        except TradovateError:
            return str(contract_id)              # not cached: the next look retries
        if len(self._contract_names) > 512:
            self._contract_names.clear()
        self._contract_names[contract_id] = name
        return name

    def positions_from(self, raw: list[dict[str, Any]], *, account_id: int, account_name: str) -> list[dict[str, Any]]:
        """One account's open positions out of a ``/position/list`` snapshot
        (names resolved by :meth:`positions_named`)."""
        return [{"symbol": p.get("contractId"), "account": account_name, "netPos": p.get("netPos"),
                 "netPrice": p.get("netPrice")}
                for p in raw if p.get("accountId") == account_id and (p.get("netPos") or 0)]

    async def positions_named(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        for r in rows:
            r["symbol"] = await self.contract_name(r["symbol"])
        return rows

    async def positions(self, *, account_id: int | None = None,
                        account_name: str | None = None, account_spec: str | None = None) -> list[dict[str, Any]]:
        _, aid = self._target(account_spec, account_id)
        raw = await self._request("GET", "/position/list") or []
        return await self.positions_named(self.positions_from(raw, account_id=aid, account_name=account_name or self.name))


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
        aid = int(account.get("id") or 0)
        if not aid and self.spec:
            # never inherit the login's primary account: look the id up by spec
            aid = next((int(a["id"]) for a in session.accounts if a.get("spec") == self.spec and a.get("id")), 0)
            if not aid and self.spec == session.account_spec:
                aid = int(session.account_id or 0)
        self.id = aid
        self.qty_multiplier = account.get("qty_multiplier", 1) or 1
        try:
            self.sizing = sizing.normalize(account)
        except (TypeError, ValueError):
            self.sizing = {"mode": "same", "multiplier": 1.0, "fixed": 1, "max_contracts": 0}
        # Unique per trade account (Tradovate specs are unique); used to key the
        # bridge's active-trade tracking and per-account order results.
        self.name = self.spec or session.name

    async def resolve_contract(self, root_or_symbol: str) -> str:
        return await self.session.resolve_contract(root_or_symbol)

    async def place_order(self, **kw: Any) -> dict[str, Any]:
        return await self.session.place_order(
            account_spec=self.spec, account_id=self.id, account_name=self.name, **kw)

    def _acct_hint(self) -> dict[str, Any]:
        """Brokers that key orders by account get the account with every order call."""
        return {"account_id": self.id, "account_spec": self.spec} if getattr(self.session, "kind", "tradovate") != "tradovate" else {}

    async def modify_order(self, order_id: int, **kw: Any) -> dict[str, Any]:
        return await self.session.modify_order(order_id, account_name=self.name, **self._acct_hint(), **kw)

    async def place_oco(self, **kw: Any) -> dict[str, Any]:
        return await self.session.place_oco(account_spec=self.spec, account_id=self.id, account_name=self.name, **kw)

    async def order_versions(self, order_ids: list[int]) -> dict[int, dict[str, Any]]:
        return await self.session.order_versions(order_ids)

    async def cancel_order(self, order_id: int) -> dict[str, Any]:
        return await self.session.cancel_order(order_id, **self._acct_hint())

    async def working_orders(self) -> list[dict[str, Any]]:
        return await self.session.working_orders(account_id=self.id, account_spec=self.spec)

    async def contract_id(self, symbol: str) -> int:
        return await self.session.contract_id(symbol)

    async def liquidate_position(self, symbol: str) -> dict[str, Any]:
        return await self.session.liquidate_position(
            symbol, account_id=self.id, account_name=self.name, account_spec=self.spec)

    async def positions(self) -> list[dict[str, Any]]:
        return await self.session.positions(account_id=self.id, account_name=self.name, account_spec=self.spec)


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
            if broker.broker_of(e) in ("rithmic", "projectx"):
                if broker.broker_of(e) == "rithmic":
                    from .rithmic import RithmicSession as _Cls, _fingerprint as _fp
                else:
                    from .projectx import ProjectXSession as _Cls, _fingerprint as _fp   # type: ignore[assignment]
                old = prev[i] if i < len(prev) else None
                if isinstance(old, _Cls) and old.fingerprint == _fp(e):
                    old.adopt_credentials(e)
                    fresh.append(old)
                else:
                    fresh.append(_Cls(i, e, area_id=self.area_id))   # type: ignore[arg-type]
                continue
            if broker.broker_of(e) not in broker.BROKERS:
                # a login for a broker this build does not ship never trades by accident:
                # it gets a disabled placeholder session that reports why
                e = {**e, "enabled": False, "access_token": ""}
                state.set_session_status(e.get("name") or f"account {i + 1}", connected=False,
                                         last_error=f"broker '{broker.broker_of(e)}' is not supported by this version")
            old = prev[i] if i < len(prev) else None
            if old is not None and old.fingerprint == _fingerprint(e):
                old.adopt_credentials(e)
                fresh.append(old)
            else:
                fresh.append(TradovateSession(i, e, area_id=self.area_id))
        kept = {id(s) for s in fresh}
        for old in prev:
            if id(old) not in kept and hasattr(old, "close"):
                _close_later(old)                       # a replaced Rithmic/ProjectX session drops its sockets
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

    def session_for(self, lid: str | None, token_idx: int | None = None) -> TradovateSession | None:
        """A login by its stable id, else by position (legacy routes)."""
        sessions = self.all()
        if lid:
            for s in sessions:
                if s.lid == lid:
                    return s
            return None
        if token_idx is not None and 0 <= token_idx < len(sessions):
            return sessions[token_idx]
        return None

    def executor_for(
        self, token_idx: int, spec: str, qty_multiplier: float = 1, sizing: dict[str, Any] | None = None,
        lid: str | None = None
    ) -> AccountExecutor | None:
        """Build an executor for one specific (login, trade account) pair, with a
        caller-supplied qty multiplier — used by per-webhook routing, independent
        of that account's own execution toggle under Settings → Trade Accounts.
        Returns None if the login or account no longer exists (e.g. deleted)."""
        session = self.session_for(lid, token_idx)
        if session is None or not session.enabled:
            return None
        account = next((a for a in session.accounts if a.get("spec") == spec), None)
        if account is None:
            return None
        extra: dict[str, Any] = {"qty_multiplier": qty_multiplier}
        if sizing:
            extra["sizing"] = sizing
        return AccountExecutor(session, {**account, **extra})


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

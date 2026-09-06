"""Request-level hardening shared by the whole app.

* :class:`BodyLimitMiddleware` — caps request bodies (TradingView alerts are a
  few hundred bytes; a multi-megabyte POST is never legitimate).
* :func:`security_middleware` — the per-request policy: same-origin check for
  state-changing requests (CSRF), rate limits on the credential endpoints, and
  the security headers (CSP with a per-request nonce, HSTS, frame denial, no
  caching of API responses).
* :func:`check_outbound_url` — SSRF guard for user-supplied URLs the bridge
  itself will POST to (Discord alert webhook, custom signal targets).

Everything is in-memory and single-process, matching the rest of the runtime
(the app runs one uvicorn worker by design).
"""
from __future__ import annotations

import ipaddress
import secrets
import socket
import threading
import time
from collections import deque
from typing import Any, Callable, Deque
from urllib.parse import urlsplit

from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

MAX_BODY_BYTES = 256 * 1024  # generous for any alert / settings payload

# --------------------------------------------------------------- client IP
def client_ip(request: Request) -> str:
    """Best-effort client address. Behind Render/nginx the socket peer is the
    proxy, so the first ``X-Forwarded-For`` hop is used when present. A forged
    header only lets a caller bucket itself differently, never bypass a limit."""
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        first = xff.split(",")[0].strip()
        if first:
            return first
    return request.client.host if request.client else "unknown"


# ------------------------------------------------------------ rate limiting
class RateLimiter:
    """Sliding-window counter per key: at most ``limit`` hits per ``window`` s."""

    def __init__(self, limit: int, window: float) -> None:
        self.limit = limit
        self.window = window
        self._hits: dict[str, Deque[float]] = {}
        self._lock = threading.Lock()

    def hit(self, key: str) -> bool:
        """Record a hit; return True when the caller is still within the limit."""
        now = time.monotonic()
        with self._lock:
            q = self._hits.get(key)
            if q is None:
                q = self._hits[key] = deque()
            while q and q[0] <= now - self.window:
                q.popleft()
            if len(q) >= self.limit:
                return False
            q.append(now)
            # Opportunistic pruning so idle keys don't accumulate forever.
            if len(self._hits) > 10_000:
                for k in [k for k, v in self._hits.items() if not v or v[-1] <= now - self.window]:
                    self._hits.pop(k, None)
            return True

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


# Credential endpoints: per client IP. Wide enough for a typo-prone human,
# far too tight for an online guessing attack (PBKDF2 makes each try ~100 ms
# of server CPU as well).
_LIMITS: dict[str, RateLimiter] = {
    "/login": RateLimiter(10, 60.0),
    "/setup": RateLimiter(5, 60.0),
    "/register": RateLimiter(10, 60.0),
    "/reset": RateLimiter(10, 60.0),
    "/api/account/password": RateLimiter(5, 60.0),
}
# Global ceiling per IP across all credential endpoints (an attacker rotating
# between them still hits this).
_GLOBAL = RateLimiter(30, 60.0)

# Auth form pages redirect back to themselves with ``?error=rate`` so the user
# sees a message instead of a bare 429.
_FORM_PAGES = {"/login", "/setup", "/register", "/reset"}


def reset_limits() -> None:
    for rl in _LIMITS.values():
        rl.reset()
    _GLOBAL.reset()


def _rate_limited(request: Request) -> Response | None:
    if request.method != "POST":
        return None
    rl = _LIMITS.get(request.url.path)
    if rl is None:
        return None
    ip = client_ip(request)
    if rl.hit(ip) and _GLOBAL.hit(ip):
        return None
    if request.url.path in _FORM_PAGES:
        return RedirectResponse(f"{request.url.path}?error=rate", status_code=302,
                                headers={"Retry-After": "60"})
    return JSONResponse({"detail": "Too many attempts — try again in a minute"},
                        status_code=429, headers={"Retry-After": "60"})


# --------------------------------------------------------------- CSRF check
_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def request_host(request: Request) -> str:
    return (request.headers.get("x-forwarded-host") or request.headers.get("host")
            or request.url.netloc).split(",")[0].strip().lower()


def _origin_host(value: str) -> str | None:
    try:
        parts = urlsplit(value)
    except ValueError:
        return None
    return (parts.netloc or "").lower() or None


def cross_site(request: Request) -> bool:
    """True when a state-changing request demonstrably comes from another site.

    Browsers send ``Sec-Fetch-Site`` and/or ``Origin`` on cross-site requests;
    both are compared against the host the request arrived on. Requests without
    either header (curl, TradingView, same-site navigations in older browsers)
    are left alone — the session cookie is ``SameSite=Lax`` anyway, so a
    cross-site POST could not carry it in the first place."""
    if request.method in _SAFE_METHODS:
        return False
    site = request.headers.get("sec-fetch-site", "").lower()
    if site == "cross-site":
        return True
    origin = request.headers.get("origin")
    if origin and origin.lower() != "null":
        return _origin_host(origin) != request_host(request)
    referer = request.headers.get("referer")
    if referer and site == "":
        return _origin_host(referer) != request_host(request)
    return False


# ---------------------------------------------------------------- headers
def _csp(nonce: str, *, relaxed: bool) -> str:
    script = "'self' 'unsafe-inline'" if relaxed else f"'self' 'nonce-{nonce}'"
    return (
        f"default-src 'self'; script-src {script}; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; font-src 'self' data:; connect-src 'self'; "
        "frame-ancestors 'none'; base-uri 'self'; form-action 'self'; object-src 'none'"
    )


def apply_headers(request: Request, response: Response, nonce: str) -> None:
    h = response.headers
    path = request.url.path
    h.setdefault("X-Content-Type-Options", "nosniff")
    h.setdefault("X-Frame-Options", "DENY")
    h.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    h.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=(), usb=()")
    h.setdefault("Cross-Origin-Opener-Policy", "same-origin")
    if not path.startswith("/static/"):
        # The bundled setup guide is a self-contained page with inline styles
        # (no scripts); everything else gets the nonce policy.
        h.setdefault("Content-Security-Policy", _csp(nonce, relaxed=path == "/guide"))
    if path.startswith("/api/") or path in ("/login", "/setup", "/register", "/reset"):
        h.setdefault("Cache-Control", "no-store")
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    if proto == "https":
        h.setdefault("Strict-Transport-Security", "max-age=15552000")  # 180 days


# ---------------------------------------------------------- the middleware
async def security_middleware(request: Request, call_next: Callable[..., Any]) -> Response:
    """CSRF origin check → rate limits → handler → security headers."""
    request.state.csp_nonce = nonce = secrets.token_urlsafe(16)
    path = request.url.path
    if not path.startswith("/webhook/") and cross_site(request):
        resp: Response = JSONResponse({"detail": "Cross-site request rejected"}, status_code=403)
    else:
        resp = _rate_limited(request) or await call_next(request)
    apply_headers(request, resp, nonce)
    return resp


class BodyLimitMiddleware:
    """Pure-ASGI cap on request-body size (Content-Length *and* streamed bytes)."""

    def __init__(self, app: Any, max_bytes: int = MAX_BODY_BYTES) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        declared = headers.get(b"content-length")
        if declared is not None:
            try:
                too_big = int(declared) > self.max_bytes
            except ValueError:
                too_big = True
            if too_big:
                await self._reject(send)
                return

        seen = 0
        limit = self.max_bytes
        responded = False

        async def guarded_receive() -> dict[str, Any]:
            nonlocal seen
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body") or b"")
                if seen > limit:
                    raise _BodyTooLarge()
            return message

        async def guarded_send(message: dict[str, Any]) -> None:
            nonlocal responded
            if message["type"] == "http.response.start":
                responded = True
            await send(message)

        try:
            await self.app(scope, guarded_receive, guarded_send)
        except Exception as exc:
            # Starlette's BaseHTTPMiddleware runs ``receive`` inside a task group,
            # so the marker may arrive wrapped in an ExceptionGroup (Python 3.9
            # compatible walk instead of ``except*``).
            if not _contains(exc, _BodyTooLarge):
                raise
            if not responded:
                await self._reject(send)

    @staticmethod
    async def _reject(send: Any) -> None:
        body = b'{"detail":"Request body too large"}'
        await send({"type": "http.response.start", "status": 413,
                    "headers": [(b"content-type", b"application/json"),
                                (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})


class _BodyTooLarge(Exception):
    pass


def _contains(exc: BaseException, cls: type) -> bool:
    if isinstance(exc, cls):
        return True
    subs = getattr(exc, "exceptions", None)  # ExceptionGroup (3.11+) / anyio backport
    return bool(subs) and any(_contains(e, cls) for e in subs)


# ------------------------------------------------------------- SSRF guard
def _is_public_ip(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return not (addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_multicast
                or addr.is_reserved or addr.is_unspecified)


def check_outbound_url(url: str) -> str | None:
    """Return an error message when ``url`` must not be used as a POST target
    (non-http(s) scheme, credentials in the URL, or a host that resolves to a
    loopback / private / link-local address), else ``None``.

    Runs a blocking DNS lookup — call it from a worker thread in async code."""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return "Invalid URL"
    if parts.scheme not in ("http", "https"):
        return "URL must start with http:// or https://"
    if not parts.hostname:
        return "URL has no host"
    if parts.username or parts.password:
        return "URL must not contain credentials"
    host = parts.hostname.lower()
    if host == "localhost" or host.endswith(".localhost") or host.endswith(".internal"):
        return "URL points at an internal address"
    try:
        infos = socket.getaddrinfo(host, parts.port or (443 if parts.scheme == "https" else 80),
                                   proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        return f"Host '{host}' does not resolve"
    for info in infos:
        if not _is_public_ip(info[4][0]):
            return "URL points at an internal address"
    return None

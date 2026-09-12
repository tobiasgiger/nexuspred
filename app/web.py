"""Helpers shared by the routers: template engine, session-cookie plumbing,
the admin gate, and the paths reachable without a login."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates

from . import auth, config, i18n

BASE_DIR = Path(__file__).resolve().parent.parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

# Paths reachable without a login session: the webhook (TradingView can't send
# auth), static assets, health check, guide/favicon, and the auth pages.
# Prefixes match whole subtrees; pages match exactly (``/loginx`` is *not* exempt).
AUTH_EXEMPT_PREFIXES = ("/webhook/", "/static/", "/api/agent/")
AUTH_EXEMPT_PATHS = frozenset({
    "/healthz", "/metrics", "/guide", "/favicon.ico", "/sw.js",
    "/login", "/logout", "/register", "/setup", "/reset", "/login/2fa",
})
# Paths a signed-in user who still has to enrol in two-factor may reach.
MFA_SETUP_PATHS = frozenset({"/2fa/setup", "/logout", "/api/me"})
MFA_SETUP_PREFIXES = ("/api/account/2fa",)


def mfa_setup_allowed(path: str) -> bool:
    return path in MFA_SETUP_PATHS or path.startswith(MFA_SETUP_PREFIXES)
AUTH_EXEMPT = AUTH_EXEMPT_PREFIXES + tuple(sorted(AUTH_EXEMPT_PATHS))  # backwards-compat alias


def is_auth_exempt(path: str) -> bool:
    return path in AUTH_EXEMPT_PATHS or path.startswith(AUTH_EXEMPT_PREFIXES)


def render(request: Request, name: str, context: dict[str, Any] | None = None) -> HTMLResponse:
    """Render a template with the request and its CSP nonce in scope."""
    lang = i18n.language_of(request)
    tr = i18n.translator(lang)
    ctx = {"nonce": getattr(request.state, "csp_nonce", ""), "lang": lang, "t": tr}
    if context:
        ctx.update(context)
        if ctx.get("error"):
            ctx["error"] = tr(str(ctx["error"]))       # the auth forms' error texts are English source strings
    return templates.TemplateResponse(request, name, ctx)


def secure(request: Request) -> bool:
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    return proto == "https"


def wants_html(request: Request) -> bool:
    return request.method == "GET" and "text/html" in request.headers.get("accept", "")


def set_session_cookie(resp: Response, request: Request, user_id: int) -> None:
    resp.set_cookie(auth.COOKIE, auth.make_session(user_id), max_age=auth.SESSION_TTL,
                    httponly=True, secure=secure(request), samesite="lax", path="/")


def base_url(request: Request) -> str:
    """The origin to build absolute links (invites, password resets) on.

    ``NEXUSPRED_PUBLIC_URL`` pins it (recommended on any public host: a forged
    ``Host`` header can then never end up in an emailed link); otherwise the
    request's own scheme + host are used."""
    if config.PUBLIC_URL:
        return config.PUBLIC_URL
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
    return f"{proto}://{host}"


def require_feature(request: Request, feature: str) -> None:
    """403 unless the caller's area has been granted ``feature`` by an admin."""
    from . import db
    area = getattr(request.state, "area_id", None)
    if area is None or not db.get_area_features(area).get(feature):
        raise HTTPException(status_code=403, detail=f"Feature '{feature}' is not enabled for your account")


def require_admin(request: Request) -> dict[str, Any]:
    user = getattr(request.state, "user", None)
    if not user or not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="Admin only")
    return user

"""Helpers shared by the routers: template engine, session-cookie plumbing,
the admin gate, and the paths reachable without a login."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import HTTPException, Request
from fastapi.responses import Response
from fastapi.templating import Jinja2Templates

from . import auth

BASE_DIR = Path(__file__).resolve().parent.parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

# Paths reachable without a login session: the webhook (TradingView can't send
# auth), static assets, health check, guide/favicon, and the auth pages.
AUTH_EXEMPT = (
    "/webhook/", "/static/", "/healthz", "/guide", "/favicon.ico",
    "/login", "/logout", "/register", "/setup", "/reset",
)


def secure(request: Request) -> bool:
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    return proto == "https"


def wants_html(request: Request) -> bool:
    return request.method == "GET" and "text/html" in request.headers.get("accept", "")


def set_session_cookie(resp: Response, request: Request, user_id: int) -> None:
    resp.set_cookie(auth.COOKIE, auth.make_session(user_id), max_age=auth.SESSION_TTL,
                    httponly=True, secure=secure(request), samesite="lax", path="/")


def base_url(request: Request) -> str:
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
    return f"{proto}://{host}"


def require_admin(request: Request) -> dict[str, Any]:
    user = getattr(request.state, "user", None)
    if not user or not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="Admin only")
    return user

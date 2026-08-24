"""FastAPI application: webhook endpoint + dashboard + management API."""
from __future__ import annotations

import base64
import os
import secrets
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import alerts, auth, config, context, db, signals, state, tradovate, updater
from .discord_signals import dispatcher as discord_dispatcher
from .discord_signals import listener as discord_listener
from .discord_signals.routes import router as discord_router
from .simulator import SCENARIOS, sim_client
from .tradovate import TradovateError

BASE_DIR = Path(__file__).resolve().parent.parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

app = FastAPI(title="Fluxbridge", version=config.get_version())
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
app.include_router(discord_router)  # Discord signal module (same server + auth)

# Paths reachable without a login session: the webhook (TradingView can't send
# auth), static assets, health check, guide/favicon, and the auth pages.
_AUTH_EXEMPT = (
    "/webhook/", "/static/", "/healthz", "/guide", "/favicon.ico",
    "/login", "/logout", "/register", "/setup", "/reset",
)


def _secure(request: Request) -> bool:
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    return proto == "https"


def _wants_html(request: Request) -> bool:
    return request.method == "GET" and "text/html" in request.headers.get("accept", "")


def _set_session_cookie(resp: Response, request: Request, user_id: int) -> None:
    resp.set_cookie(auth.COOKIE, auth.make_session(user_id), max_age=auth.SESSION_TTL,
                    httponly=True, secure=_secure(request), samesite="lax", path="/")


@app.middleware("http")
async def _auth_middleware(request: Request, call_next):
    """Require a login session; set the request's area context to the user's area."""
    path = request.url.path
    if path.startswith(_AUTH_EXEMPT):
        return await call_next(request)

    if db.user_count() == 0:  # first run: force admin setup
        if _wants_html(request):
            return RedirectResponse("/setup", status_code=302)
        return JSONResponse({"detail": "Setup required"}, status_code=503)

    user = auth.current_user(request)
    if not user:
        if _wants_html(request):
            return RedirectResponse("/login", status_code=302)
        return JSONResponse({"detail": "Authentication required"}, status_code=401)

    area = db.user_primary_area(user["id"]) or context.DEFAULT_AREA_ID
    request.state.user = user
    request.state.area_id = area
    tok = context.set_area(area)
    try:
        return await call_next(request)
    finally:
        context.reset_area(tok)


# ===================================================================== Auth
@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, error: str = "") -> HTMLResponse:
    if db.user_count() == 0:
        return RedirectResponse("/setup", status_code=302)
    if auth.current_user(request):
        return RedirectResponse("/", status_code=302)
    msgs = {"bad": "Wrong email or password."}
    return templates.TemplateResponse(
        "login.html", {"request": request, "error": msgs.get(error, "")})


@app.post("/login")
async def login_submit(request: Request):
    form = await request.form()
    user = db.authenticate(str(form.get("email", "")), str(form.get("password", "")))
    if not user:
        return RedirectResponse("/login?error=bad", status_code=302)
    resp = RedirectResponse("/", status_code=302)
    _set_session_cookie(resp, request, user["id"])
    return resp


@app.get("/logout")
async def logout() -> RedirectResponse:
    resp = RedirectResponse("/login", status_code=302)
    resp.delete_cookie(auth.COOKIE, path="/")
    return resp


@app.get("/setup", response_class=HTMLResponse)
async def setup_page(request: Request, error: str = "") -> HTMLResponse:
    if db.user_count() > 0:
        return RedirectResponse("/login", status_code=302)
    msgs = {"mismatch": "Passwords don't match.", "short": "Password must be at least 8 characters.",
            "email": "Enter a valid email."}
    return templates.TemplateResponse(
        "setup.html", {"request": request, "error": msgs.get(error, "")})


@app.post("/setup")
async def setup_submit(request: Request):
    if db.user_count() > 0:
        return RedirectResponse("/login", status_code=302)
    form = await request.form()
    email = str(form.get("email", "")).strip().lower()
    pw = str(form.get("password", ""))
    if "@" not in email:
        return RedirectResponse("/setup?error=email", status_code=302)
    if pw != str(form.get("password2", "")):
        return RedirectResponse("/setup?error=mismatch", status_code=302)
    if len(pw) < 8:
        return RedirectResponse("/setup?error=short", status_code=302)
    # Seed the first admin's area with any pre-multi-tenant settings.json.
    legacy = config.legacy_settings_file() or {}
    user = db.create_user(email, pw, is_admin=True, initial_settings=legacy)
    area = db.user_primary_area(user["id"])
    config.invalidate(area)
    config.migrate_legacy_webhook(area_id=area)
    state.log_event("info", f"Admin account created: {email}")
    resp = RedirectResponse("/", status_code=302)
    _set_session_cookie(resp, request, user["id"])
    return resp


@app.get("/register", response_class=HTMLResponse)
async def register_page(request: Request, code: str = "", error: str = "") -> HTMLResponse:
    invite = db.get_invite(code) if code else None
    valid = bool(invite and not invite.get("used_by"))
    msgs = {"mismatch": "Passwords don't match.", "short": "Password must be at least 8 characters.",
            "email": "Enter a valid email.", "exists": "An account with that email already exists.",
            "invite": "This invite is invalid or already used."}
    return templates.TemplateResponse(
        "register.html",
        {"request": request, "code": code, "valid_invite": valid,
         "invite_email": (invite or {}).get("email", ""), "error": msgs.get(error, "")})


@app.post("/register")
async def register_submit(request: Request):
    form = await request.form()
    code = str(form.get("code", ""))
    invite = db.get_invite(code)
    if not invite or invite.get("used_by"):
        return RedirectResponse(f"/register?code={code}&error=invite", status_code=302)
    email = str(form.get("email", "")).strip().lower()
    pw = str(form.get("password", ""))
    if "@" not in email:
        return RedirectResponse(f"/register?code={code}&error=email", status_code=302)
    if pw != str(form.get("password2", "")):
        return RedirectResponse(f"/register?code={code}&error=mismatch", status_code=302)
    if len(pw) < 8:
        return RedirectResponse(f"/register?code={code}&error=short", status_code=302)
    if db.get_user_by_email(email):
        return RedirectResponse(f"/register?code={code}&error=exists", status_code=302)
    user = db.create_user(email, pw, is_admin=invite.get("is_admin", False))
    db.consume_invite(code, user["id"])
    area = db.user_primary_area(user["id"])
    config.migrate_legacy_webhook(area_id=area)  # give the new area a Default webhook
    state.log_event("info", f"Account registered: {email}")
    resp = RedirectResponse("/", status_code=302)
    _set_session_cookie(resp, request, user["id"])
    return resp


# ===================================================== User / admin management
def _base_url(request: Request) -> str:
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
    return f"{proto}://{host}"


def _require_admin(request: Request) -> dict[str, Any]:
    user = getattr(request.state, "user", None)
    if not user or not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="Admin only")
    return user


@app.get("/api/me")
async def api_me(request: Request) -> dict[str, Any]:
    u = request.state.user
    return {"id": u["id"], "email": u["email"], "is_admin": u["is_admin"],
            "features": db.user_features(u["id"])}


@app.get("/api/users")
async def api_users(request: Request) -> dict[str, Any]:
    _require_admin(request)
    return {"users": db.list_users(), "features": db.FEATURES}


@app.post("/api/users/{user_id}/features")
async def api_set_user_feature(request: Request, user_id: int) -> dict[str, Any]:
    """Admin toggles a feature entitlement (e.g. Discord Signals) for a user."""
    admin = _require_admin(request)
    body = await request.json()
    feature = str(body.get("feature", ""))
    enabled = bool(body.get("enabled"))
    if feature not in db.FEATURES:
        raise HTTPException(status_code=400, detail="Unknown feature")
    area_id = db.user_primary_area(user_id)
    if not area_id:
        raise HTTPException(status_code=404, detail="User has no area")
    feats = db.set_area_feature(area_id, feature, enabled)
    target = (db.get_user(user_id) or {}).get("email", str(user_id))
    db.log_action(admin["id"], admin["email"], "feature_set", target,
                  f"{feature} = {'on' if enabled else 'off'}")
    # Nudge the area's Discord listener so it (dis)connects promptly; the
    # supervisor re-reads the entitlement each loop, so this is only a shortcut.
    try:
        with context.use_area(area_id):
            discord_listener.manager_for(area_id).start()
    except Exception:  # noqa: BLE001
        pass
    return {"user_id": user_id, "features": feats}


@app.post("/api/users/invite")
async def api_create_invite(request: Request) -> dict[str, Any]:
    admin = _require_admin(request)
    body = await request.json()
    # Accept the neutral `elevated` key (what the dashboard sends) and fall back
    # to the legacy `is_admin`. The client avoids the `is_admin` key because some
    # WAFs block request bodies containing it as a privilege-escalation attempt.
    elevate = body.get("elevated")
    if elevate is None:
        elevate = body.get("is_admin")
    email = str(body.get("email", "")).strip()
    code = db.create_invite(admin["id"], email=email, is_admin=bool(elevate))
    db.log_action(admin["id"], admin["email"], "invite_create", email or "anyone",
                  "admin invite" if elevate else "")
    url = f"{_base_url(request)}/register?code={code}"
    emailed = False
    if email and "@" in email and bool(body.get("send_email")):
        emailed = await alerts.send_email_to(
            email, "You're invited to Fluxbridge",
            f"You've been invited to Fluxbridge. Create your account here:\n\n{url}\n\n"
            "This link is single-use. If you didn't expect this, you can ignore it.")
    return {"code": code, "url": url, "emailed": emailed, "smtp_configured": alerts.smtp_configured()}


@app.get("/api/invites")
async def api_invites(request: Request) -> list[dict[str, Any]]:
    _require_admin(request)
    return db.list_invites()


@app.delete("/api/invites/{code}")
async def api_delete_invite(request: Request, code: str) -> dict[str, Any]:
    admin = _require_admin(request)
    db.delete_invite(code)
    db.log_action(admin["id"], admin["email"], "invite_revoke", code[:8] + "…")
    return {"status": "deleted", "code": code}


@app.delete("/api/users/{user_id}")
async def api_delete_user(request: Request, user_id: int) -> dict[str, Any]:
    admin = _require_admin(request)
    if user_id == admin["id"]:
        raise HTTPException(status_code=400, detail="You can't delete your own account")
    target = (db.get_user(user_id) or {}).get("email", str(user_id))
    db.delete_user(user_id)
    db.log_action(admin["id"], admin["email"], "user_delete", target)
    state.log_event("info", f"User {user_id} deleted by {admin['email']}")
    return {"status": "deleted", "id": user_id}


@app.get("/api/audit")
async def api_audit(request: Request) -> list[dict[str, Any]]:
    _require_admin(request)
    return db.list_audit(100)


@app.post("/api/account/password")
async def api_change_password(request: Request) -> dict[str, Any]:
    """Self-service password change: verify the current password, then set a new one."""
    user = request.state.user
    body = await request.json()
    current = str(body.get("current", ""))
    new = str(body.get("new", ""))
    if len(new) < 8:
        raise HTTPException(status_code=400, detail="New password must be at least 8 characters")
    if not db.authenticate(user["email"], current):
        raise HTTPException(status_code=400, detail="Current password is incorrect")
    db.set_password(user["id"], new)
    db.log_action(user["id"], user["email"], "password_change", user["email"])
    state.log_event("info", f"Password changed for {user['email']}")
    return {"status": "ok"}


@app.post("/api/users/{user_id}/reset")
async def api_create_reset(request: Request, user_id: int) -> dict[str, Any]:
    """Admin generates a one-time password-reset link for a user."""
    admin = _require_admin(request)
    target = db.get_user(user_id)
    if not target:
        raise HTTPException(status_code=404, detail="No such user")
    token = db.create_password_reset(user_id)
    db.log_action(admin["id"], admin["email"], "password_reset", target["email"])
    url = f"{_base_url(request)}/reset?token={token}"
    emailed = await alerts.send_email_to(
        target["email"], "Reset your Fluxbridge password",
        f"An administrator started a password reset for your Fluxbridge account.\n\n"
        f"Set a new password here (single-use, expires in 24 hours):\n\n{url}\n\n"
        "If you didn't expect this, contact your administrator.")
    return {"user_id": user_id, "url": url, "emailed": emailed, "smtp_configured": alerts.smtp_configured()}


@app.get("/reset", response_class=HTMLResponse)
async def reset_page(request: Request, token: str = "", error: str = "") -> HTMLResponse:
    rec = db.get_password_reset(token) if token else None
    msgs = {"mismatch": "Passwords don't match.", "short": "Password must be at least 8 characters.",
            "token": "This reset link is invalid or has expired."}
    return templates.TemplateResponse(
        "reset.html",
        {"request": request, "token": token, "valid_token": bool(rec), "error": msgs.get(error, "")})


@app.post("/reset")
async def reset_submit(request: Request):
    form = await request.form()
    token = str(form.get("token", ""))
    if not db.get_password_reset(token):
        return RedirectResponse(f"/reset?token={token}&error=token", status_code=302)
    pw = str(form.get("password", ""))
    if pw != str(form.get("password2", "")):
        return RedirectResponse(f"/reset?token={token}&error=mismatch", status_code=302)
    if len(pw) < 8:
        return RedirectResponse(f"/reset?token={token}&error=short", status_code=302)
    uid = db.consume_password_reset(token, pw)
    if uid is None:
        return RedirectResponse(f"/reset?token={token}&error=token", status_code=302)
    user = db.get_user(uid)
    if user:
        state.log_event("info", f"Password reset completed for {user['email']}")
    resp = RedirectResponse("/", status_code=302)
    if uid:
        _set_session_cookie(resp, request, uid)  # log the user straight in
    return resp


@app.get("/favicon.ico")
async def favicon() -> Response:
    """Serve a tiny inline SVG favicon (matches the ◈ brand mark)."""
    svg = (
        "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'>"
        "<rect width='32' height='32' rx='6' fill='#0b0e14'/>"
        "<path d='M16 5l11 11-11 11L5 16z' fill='#4f8cff'/></svg>"
    )
    return Response(content=svg, media_type="image/svg+xml")


@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    """Unauthenticated liveness probe (for Render/uptime checks)."""
    return {"ok": True, "version": config.get_version()}


@app.get("/guide", response_class=HTMLResponse)
async def guide() -> FileResponse:
    """Standalone, self-contained setup guide page."""
    return FileResponse(str(BASE_DIR / "docs" / "setup-guide.html"))


@app.get("/api/extension/token-extractor.zip")
async def extension_zip() -> Response:
    """Serve the browser token-extractor extension as a downloadable .zip so it
    can be installed via chrome://extensions -> Load unpacked (Tools tab)."""
    import io
    import zipfile

    ext_dir = BASE_DIR / "browser-extension" / "token-extractor"
    if not ext_dir.is_dir():
        raise HTTPException(status_code=404, detail="Extension not found")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for path in sorted(ext_dir.rglob("*")):
            if path.is_file():
                # Keep the top-level folder name so unzip yields token-extractor/.
                z.write(path, path.relative_to(ext_dir.parent))
    return Response(
        content=buf.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="token-extractor.zip"'},
    )


# ============================================================== Dashboard view
@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        "dashboard.html",
        {"request": request, "version": config.get_version()},
    )


# ===================================================================== Webhook
def _resolve_webhook(token: str) -> tuple[int | None, dict[str, Any] | None]:
    """Find which area owns a webhook token (webhooks are per area). Returns
    (area_id, webhook) or (None, None)."""
    for area_id in db.all_area_ids():
        for wh in config.load_settings(area_id=area_id).get("webhooks", []):
            if wh.get("token") == token:
                return area_id, wh
    return None, None


async def _process_signal_bg(payload: dict[str, Any], webhook: dict[str, Any]) -> None:
    """Run the signal pipeline in the background so the webhook returns instantly."""
    name = webhook.get("name", "?")
    try:
        result = await signals.process(payload, webhook)
        state.log_signal(payload, result=result.get("status", "ok"))
    except (signals.SignalError, TradovateError) as exc:
        state.log_event("error", f"Signal error: {exc}", payload=payload)
        state.log_signal(payload, result=f"error: {exc}")
        await alerts.webhook_failed(name, str(exc))
    except Exception as exc:  # noqa: BLE001 - never let a background task die silently
        state.log_event("error", f"Signal failed: {exc}", payload=payload)
        state.log_signal(payload, result=f"error: {exc}")
        await alerts.webhook_failed(name, str(exc))


@app.post("/webhook/{token}")
async def webhook(token: str, request: Request) -> JSONResponse:
    """Receive a TradingView alert and route it to Tradovate.

    The ``token`` path segment must match a configured webhook's ``token``; each
    webhook carries its own strategy + routed trade accounts (see
    ``/api/webhooks``). The alert is acknowledged immediately (HTTP 202) and
    processed in the background, so bursts of alerts can't make TradingView time
    out ("request took too long").
    """
    import asyncio

    area_id, wh = _resolve_webhook(token)
    if not wh or not wh.get("enabled") or area_id is None:
        raise HTTPException(status_code=403, detail="Invalid webhook token")

    # Process in the owning user's area context (the background task inherits it).
    tok = context.set_area(area_id)
    try:
        payload = await _parse_payload(request)
        state.log_signal(payload, result="received")
        asyncio.create_task(_process_signal_bg(payload, wh))
    finally:
        context.reset_area(tok)
    return JSONResponse({"status": "accepted"}, status_code=202)


async def _parse_payload(request: Request) -> dict[str, Any]:
    """Accept JSON bodies; tolerate text/plain alerts that contain JSON."""
    raw = await request.body()
    if not raw:
        raise HTTPException(status_code=400, detail="Empty body")
    try:
        import json

        return json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {exc}") from exc


# ========================================================================= API
def _trade_accounts_overview() -> list[dict[str, Any]]:
    """Flat list of every trade account across all logins, with execution toggle
    and live connection status — powers the Trade Accounts overview."""
    out: list[dict[str, Any]] = []
    for idx, t in enumerate(config.load_settings().get("token_accounts") or []):
        tname = t.get("name") or f"account {idx + 1}"
        env = t.get("environment") or "demo"
        tconn = bool(state.session_status(tname).get("connected"))
        accts = t.get("accounts") or []
        if not accts and (t.get("account_spec") or t.get("account_id")):
            accts = [{"spec": t.get("account_spec", ""), "id": t.get("account_id", 0),
                      "enabled": True, "qty_multiplier": t.get("qty_multiplier", 1)}]
        for a in accts:
            out.append({
                "token_idx": idx, "token_name": tname, "environment": env,
                "token_enabled": bool(t.get("enabled")), "connected": tconn,
                "spec": a.get("spec") or a.get("account_spec") or "",
                "id": a.get("id") or a.get("account_id") or 0,
                "enabled": bool(a.get("enabled", True)),
                "qty_multiplier": float(a.get("qty_multiplier", t.get("qty_multiplier", 1)) or 1),
            })
    return out


@app.get("/api/status")
async def api_status() -> dict[str, Any]:
    return {
        "version": config.get_version(),
        "connection": state.aggregate_connection(),
        "sessions": state.session_statuses(),
        "trade_accounts": _trade_accounts_overview(),
        "active_trades": signals.active_trades(),
        "trading_enabled": config.load_settings().get("trading_enabled", False),
    }


@app.get("/api/settings")
async def api_get_settings(request: Request) -> dict[str, Any]:
    s = config.public_settings()
    # Default the alert notify-email to the signed-in user's own address when it
    # hasn't been set, so the field is pre-filled per-user (they can override it).
    if not s.get("alert_email_to"):
        user = getattr(request.state, "user", None)
        if user:
            s["alert_email_to"] = user.get("email", "")
    return s


@app.post("/api/settings")
async def api_save_settings(request: Request) -> dict[str, Any]:
    updates = await request.json()
    # Drop masked secret fields so we don't overwrite stored secrets with "********".
    for field in config.SECRET_FIELDS:
        if updates.get(field) == "********":
            updates.pop(field, None)
    updates.pop("token_accounts", None)  # managed via /api/token-accounts
    config.save_settings(updates)
    state.log_event("info", "Settings updated")
    return config.public_settings()


@app.post("/api/flatten-all")
async def api_flatten_all(request: Request) -> dict[str, Any]:
    """Emergency kill-switch: flatten every position and cancel every working order
    on all trade accounts in the caller's area. Runs even if trading is paused."""
    user = getattr(request.state, "user", None)
    result = await signals.flatten_all()
    if user:
        db.log_action(user["id"], user["email"], "flatten_all", "",
                      f"{result.get('flattened', 0)} flattened, "
                      f"{result.get('cancelled', 0)} cancelled, "
                      f"{result.get('accounts', 0)} account(s)")
    return result


@app.post("/api/alerts/test")
async def api_test_alert() -> dict[str, Any]:
    """Send a test notification on every enabled channel (Discord / email)."""
    channels = await alerts.test_alert()
    if not any(channels.values()):
        return {"status": "none", "channels": channels,
                "detail": "No alert channel is enabled and fully configured."}
    return {"status": "sent", "channels": channels}


@app.get("/api/signals")
async def api_signals() -> list[dict[str, Any]]:
    return state.recent_signals()


@app.get("/api/orders")
async def api_orders() -> list[dict[str, Any]]:
    return state.recent_orders()


@app.get("/api/events")
async def api_events() -> list[dict[str, Any]]:
    return state.recent_events()


@app.get("/api/stream")
async def api_stream(request: Request) -> StreamingResponse:
    """Server-Sent Events: live event-log and signal-log entries (no polling).

    Each message is ``{"kind": "event"|"signal", "data": {...}}``. The connection
    is scoped to the logged-in user's area, captured before the generator starts
    (it runs after the request's area context has been reset)."""
    import asyncio
    import json

    area = context.get_area()
    sub = state.subscribe(area)

    async def gen():
        try:
            yield ": connected\n\n"  # prime so proxies flush headers
            yield "event: ping\ndata: {}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    msg = await asyncio.wait_for(sub.queue.get(), timeout=10.0)
                    yield f"data: {json.dumps(msg)}\n\n"
                except asyncio.TimeoutError:
                    yield "event: ping\ndata: {}\n\n"  # named heartbeat; keeps proxies open
        finally:
            state.unsubscribe(sub, area)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive",
                 "X-Accel-Buffering": "no"},
    )


@app.get("/api/positions")
async def api_positions() -> Any:
    out: list[dict[str, Any]] = []
    for sess in tradovate.manager().enabled():
        try:
            out.extend(await sess.positions())
        except TradovateError:
            pass
    return out


@app.post("/api/connect")
async def api_connect() -> dict[str, Any]:
    """Connect & verify all configured accounts (in parallel)."""
    mgr = tradovate.manager()
    mgr.reload()
    import asyncio
    sessions = mgr.all()
    await asyncio.gather(*(s.connect() for s in sessions), return_exceptions=True)
    return {"sessions": state.session_statuses()}


# =============================================================== Token accounts
@app.get("/api/token-accounts")
async def api_token_accounts() -> list[dict[str, Any]]:
    return config.public_settings().get("token_accounts", [])


@app.post("/api/token-accounts")
async def api_save_token_accounts(request: Request) -> list[dict[str, Any]]:
    """Save the per-account token list. Masked tokens ('********') keep the stored
    value, so editing other fields doesn't wipe the tokens."""
    incoming = await request.json()
    existing = config.load_settings().get("token_accounts") or []
    cleaned: list[dict[str, Any]] = []
    for i, a in enumerate(incoming):
        prev = existing[i] if i < len(existing) else {}
        access = a.get("access_token", "")
        md = a.get("md_token", "")
        cleaned.append({
            "name": (a.get("name") or f"account {i + 1}").strip(),
            "environment": "live" if a.get("environment") == "live" else "demo",
            "access_token": prev.get("access_token", "") if access == "********" else access.strip(),
            "md_token": prev.get("md_token", "") if md == "********" else md.strip(),
            "enabled": bool(a.get("enabled")),
            "qty_multiplier": float(a.get("qty_multiplier", 1) or 1),
            "account_spec": a.get("account_spec") or prev.get("account_spec", ""),
            "account_id": a.get("account_id") or prev.get("account_id", 0),
            "token_expires": prev.get("token_expires", ""),
        })
    config.save_settings({"token_accounts": cleaned})
    tradovate.manager().reload()
    enabled = sum(1 for a in cleaned if a["enabled"])
    state.log_event("info", f"Token accounts updated — {enabled}/{len(cleaned)} enabled")
    return config.public_settings().get("token_accounts", [])


# =============================================================== Trade accounts
@app.get("/api/trade-accounts")
async def api_trade_accounts() -> list[dict[str, Any]]:
    """Overview of every trade account under every login, with on/off toggles."""
    return _trade_accounts_overview()


@app.post("/api/trade-accounts")
async def api_save_trade_accounts(request: Request) -> list[dict[str, Any]]:
    """Save per-account execution toggles & qty multipliers (keyed by login + spec)."""
    incoming = await request.json()
    tokens = list(config.load_settings().get("token_accounts") or [])
    by_token: dict[int, dict[str, Any]] = {}
    for item in incoming:
        try:
            idx = int(item.get("token_idx"))
        except (TypeError, ValueError):
            continue
        by_token.setdefault(idx, {})[item.get("spec", "")] = item

    for idx, updates in by_token.items():
        if not (0 <= idx < len(tokens)):
            continue
        t = dict(tokens[idx])
        existing = {(a.get("spec") or a.get("account_spec") or ""): dict(a)
                    for a in (t.get("accounts") or [])}
        for spec, u in updates.items():
            a = existing.get(spec, {"spec": spec, "id": u.get("id", 0)})
            a["spec"] = spec
            a["enabled"] = bool(u.get("enabled"))
            a["qty_multiplier"] = float(u.get("qty_multiplier", 1) or 1)
            if u.get("id"):
                a["id"] = u["id"]
            existing[spec] = a
        t["accounts"] = list(existing.values())
        tokens[idx] = t

    config.save_settings({"token_accounts": tokens})
    tradovate.manager().reload()
    enabled = sum(1 for a in _trade_accounts_overview() if a["enabled"])
    state.log_event("info", f"Trade-account toggles updated — {enabled} enabled for execution")
    return _trade_accounts_overview()


# =================================================================== Webhooks
def _webhook_or_404(webhook_id: str) -> tuple[list[dict[str, Any]], int]:
    """Return (all webhooks, index of webhook_id) or raise 404."""
    webhooks = config.load_settings().get("webhooks", [])
    for i, wh in enumerate(webhooks):
        if wh.get("id") == webhook_id:
            return webhooks, i
    raise HTTPException(status_code=404, detail="Webhook not found")


@app.get("/api/webhooks")
async def api_list_webhooks() -> list[dict[str, Any]]:
    return config.load_settings().get("webhooks", [])


@app.post("/api/webhooks")
async def api_create_webhook(request: Request) -> dict[str, Any]:
    body = await request.json()
    wh = config.new_webhook(
        name=body.get("name") or "New Webhook",
        strategy=body.get("strategy", "simple"),
        default_qty=body.get("default_qty", 1),
        tp_qty=body.get("tp_qty", 1),
    )
    webhooks = config.load_settings().get("webhooks", [])
    webhooks.append(wh)
    config.save_settings({"webhooks": webhooks})
    state.log_event("info", f"Webhook '{wh['name']}' created ({wh['strategy']})")
    return wh


@app.put("/api/webhooks/{webhook_id}")
async def api_update_webhook(webhook_id: str, request: Request) -> dict[str, Any]:
    body = await request.json()
    webhooks, i = _webhook_or_404(webhook_id)
    wh = webhooks[i]
    if "name" in body:
        wh["name"] = str(body["name"]) or wh["name"]
    if "enabled" in body:
        wh["enabled"] = bool(body["enabled"])
    if "strategy" in body and body["strategy"] in config.STRATEGIES:
        wh["strategy"] = body["strategy"]
    if "default_qty" in body:
        wh["default_qty"] = max(1, int(body["default_qty"] or 1))
    if "tp_qty" in body:
        wh["tp_qty"] = max(1, int(body["tp_qty"] or 1))
    if "accounts" in body:
        wh["accounts"] = [
            {
                "token_idx": int(a["token_idx"]),
                "spec": a.get("spec", ""),
                "enabled": bool(a.get("enabled")),
                "qty_multiplier": float(a.get("qty_multiplier", 1) or 1),
            }
            for a in body["accounts"]
            if a.get("spec") and a.get("token_idx") is not None
        ]
    webhooks[i] = wh
    config.save_settings({"webhooks": webhooks})
    state.log_event("info", f"Webhook '{wh['name']}' updated")
    return wh


@app.delete("/api/webhooks/{webhook_id}")
async def api_delete_webhook(webhook_id: str) -> dict[str, Any]:
    webhooks, i = _webhook_or_404(webhook_id)
    removed = webhooks.pop(i)
    config.save_settings({"webhooks": webhooks})
    state.log_event("info", f"Webhook '{removed.get('name')}' deleted")
    return {"status": "deleted", "id": webhook_id}


@app.post("/api/webhooks/{webhook_id}/regenerate-token")
async def api_regenerate_webhook_token(webhook_id: str) -> dict[str, Any]:
    webhooks, i = _webhook_or_404(webhook_id)
    webhooks[i]["token"] = secrets.token_urlsafe(16)
    config.save_settings({"webhooks": webhooks})
    state.log_event("info", f"Webhook '{webhooks[i]['name']}' token regenerated")
    return webhooks[i]


@app.post("/api/webhooks/{webhook_id}/test")
async def api_test_webhook(webhook_id: str, request: Request) -> dict[str, Any]:
    """Run a payload through the signal pipeline for a specific webhook (real
    execution — respects the trading_enabled switch, same as a live POST)."""
    webhooks, i = _webhook_or_404(webhook_id)
    wh = webhooks[i]
    payload = await request.json()
    state.log_signal(payload, result="test")
    try:
        return await signals.process(payload, wh)
    except (signals.SignalError, TradovateError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# ================================================================== Simulator
@app.get("/api/scenarios")
async def api_scenarios() -> list[dict[str, Any]]:
    return SCENARIOS


@app.post("/api/simulate")
async def api_simulate(request: Request) -> dict[str, Any]:
    """Run a single signal through the pipeline in simulation mode (no broker)."""
    payload = await request.json()
    state.log_signal(payload, result="simulated")
    try:
        return await signals.process(payload, simulate=True)
    except (signals.SignalError, TradovateError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/simulate/state")
async def api_simulate_state() -> dict[str, Any]:
    return {
        "positions": await sim_client.positions(),
        "working_orders": await sim_client.working_orders(),
        "active_trades": signals.active_trades(simulate=True),
    }


@app.post("/api/simulate/reset")
async def api_simulate_reset() -> dict[str, Any]:
    signals.reset_simulation()
    return {"status": "reset"}


# ==================================================================== Updater
@app.get("/api/update/check")
async def api_update_check() -> dict[str, Any]:
    return await updater.check_for_update()


@app.post("/api/update/apply")
async def api_update_apply() -> dict[str, Any]:
    result = await updater.apply_update()
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("message"))
    return result


@app.get("/api/health")
async def api_health() -> dict[str, Any]:
    """On-demand health check of every configured account (current area)."""
    import asyncio
    await asyncio.gather(*(s.health_check() for s in tradovate.manager().all()),
                         return_exceptions=True)
    return {"sessions": state.session_statuses()}


async def _refresh_session(sess) -> float:
    """Proactively renew one session's token and verify it; return next-check delay."""
    interval = int(config.load_settings().get("health_check_interval", 60) or 60)
    try:
        if sess.has_token():
            await sess.proactive_refresh()    # renew well before expiry (never lapse)
        await sess.health_check()
        ok = bool(state.session_status(sess.name).get("connected"))
    except Exception as exc:  # noqa: BLE001 - never let the loop die
        state.log_event("warn", f"[{sess.name}] refresh error: {exc}")
        ok = False
    return sess.seconds_until_refresh(fallback=interval) if ok else 60.0


async def _health_loop() -> None:
    """Keep every area's account tokens alive proactively, in parallel per area."""
    import asyncio
    while True:
        try:
            area_ids = db.all_area_ids()
        except Exception:  # noqa: BLE001
            area_ids = []
        next_delays: list[float] = []
        for area_id in area_ids:
            with context.use_area(area_id):
                # Make sure every area has a Discord supervisor (idempotent).
                try:
                    discord_listener.manager_for(area_id).start()
                except Exception:  # noqa: BLE001
                    pass
                interval = int(config.load_settings(area_id=area_id).get("health_check_interval", 60) or 0)
                if interval <= 0:
                    continue
                mgr = tradovate.manager_for(area_id)
                mgr.reload()
                sessions = mgr.all()
                if not sessions:
                    continue
                delays = await asyncio.gather(*(_refresh_session(s) for s in sessions),
                                              return_exceptions=True)
                next_delays += [d for d in delays if isinstance(d, (int, float))]
        await asyncio.sleep(min(next_delays) if next_delays else 30.0)


async def _discord_health_loop() -> None:
    """Evaluate every area's Discord listener health on a steady cadence and fire
    lost/restored alerts. Kept separate from the token health loop (which paces
    itself to token expiry, sometimes minutes apart) so outages surface quickly."""
    import asyncio
    while True:
        try:
            area_ids = db.all_area_ids()
        except Exception:  # noqa: BLE001
            area_ids = []
        for area_id in area_ids:
            try:
                with context.use_area(area_id):
                    await discord_listener.manager_for(area_id).health_tick()
            except Exception:  # noqa: BLE001 - a health tick must never crash the loop
                pass
        await asyncio.sleep(30.0)


@app.on_event("startup")
async def _startup() -> None:
    import asyncio
    db.init()
    # Default each area's alert "Notify email" to its owner's address where unset.
    try:
        if db.backfill_alert_emails():
            for aid in db.all_area_ids():
                config.invalidate(aid)
    except Exception as exc:  # noqa: BLE001 - never let a migration block startup
        state.log_event("warn", f"alert-email backfill failed: {exc}")
    state.log_event("info", f"Bridge started (v{config.get_version()})")
    asyncio.create_task(_health_loop())
    asyncio.create_task(_discord_health_loop())
    # Start a Discord listener supervisor per existing area (isolated tasks; a
    # Discord failure can never crash order execution). New areas are picked up
    # by the health loop.
    try:
        area_ids = db.all_area_ids()
        for area_id in area_ids:
            discord_listener.manager_for(area_id).start()
        if area_ids and not discord_listener.manager_for(area_ids[0]).library_available():
            state.log_event(
                "warn",
                "[discord] listener library not installed (discord.py-self) — "
                "module idle. Install it to enable the Discord signal listener.",
            )
    except Exception as exc:  # noqa: BLE001 - never let module startup break the app
        state.log_event("warn", f"[discord] listener startup failed: {exc}")


@app.on_event("shutdown")
async def _shutdown() -> None:
    """Stop all Discord listeners and close the shared HTTP client cleanly."""
    for m in discord_listener.all_managers():
        try:
            await m.shutdown()
        except Exception as exc:  # noqa: BLE001
            state.log_event("warn", f"[discord] shutdown error: {exc}")
    await discord_dispatcher.aclose()

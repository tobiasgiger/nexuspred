"""Auth pages: first-run setup, login/logout, invite registration, password reset."""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import auth, config, db, state
from ..web import set_session_cookie, templates

router = APIRouter(tags=["auth"])

_setup_lock = asyncio.Lock()  # two racing first-run POSTs must not both create an admin


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, error: str = "") -> HTMLResponse:
    if db.user_count() == 0:
        return RedirectResponse("/setup", status_code=302)
    if auth.current_user(request):
        return RedirectResponse("/", status_code=302)
    msgs = {"bad": "Wrong email or password."}
    return templates.TemplateResponse(
        "login.html", {"request": request, "error": msgs.get(error, "")})


@router.post("/login")
async def login_submit(request: Request):
    form = await request.form()
    user = await db.authenticate_async(str(form.get("email", "")), str(form.get("password", "")))
    if not user:
        return RedirectResponse("/login?error=bad", status_code=302)
    resp = RedirectResponse("/", status_code=302)
    set_session_cookie(resp, request, user["id"])
    return resp


@router.get("/logout")
async def logout() -> RedirectResponse:
    resp = RedirectResponse("/login", status_code=302)
    resp.delete_cookie(auth.COOKIE, path="/")
    return resp


@router.get("/setup", response_class=HTMLResponse)
async def setup_page(request: Request, error: str = "") -> HTMLResponse:
    if db.user_count() > 0:
        return RedirectResponse("/login", status_code=302)
    msgs = {"mismatch": "Passwords don't match.", "short": "Password must be at least 8 characters.",
            "email": "Enter a valid email."}
    return templates.TemplateResponse(
        "setup.html", {"request": request, "error": msgs.get(error, "")})


@router.post("/setup")
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
    async with _setup_lock:
        if db.user_count() > 0:
            return RedirectResponse("/login", status_code=302)
        # Seed the first admin's area with any pre-multi-tenant settings.json.
        legacy = config.legacy_settings_file() or {}
        user = await db.create_user_async(email, pw, is_admin=True, initial_settings=legacy)
    area = db.user_primary_area(user["id"])
    config.invalidate(area)
    config.migrate_legacy_webhook(area_id=area)
    state.log_event("info", f"Admin account created: {email}")
    resp = RedirectResponse("/", status_code=302)
    set_session_cookie(resp, request, user["id"])
    return resp


@router.get("/register", response_class=HTMLResponse)
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


@router.post("/register")
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
    user = await db.create_user_async(email, pw, is_admin=invite.get("is_admin", False))
    db.consume_invite(code, user["id"])
    area = db.user_primary_area(user["id"])
    config.migrate_legacy_webhook(area_id=area)  # give the new area a Default webhook
    state.log_event("info", f"Account registered: {email}")
    resp = RedirectResponse("/", status_code=302)
    set_session_cookie(resp, request, user["id"])
    return resp


@router.get("/reset", response_class=HTMLResponse)
async def reset_page(request: Request, token: str = "", error: str = "") -> HTMLResponse:
    rec = db.get_password_reset(token) if token else None
    msgs = {"mismatch": "Passwords don't match.", "short": "Password must be at least 8 characters.",
            "token": "This reset link is invalid or has expired."}
    return templates.TemplateResponse(
        "reset.html",
        {"request": request, "token": token, "valid_token": bool(rec), "error": msgs.get(error, "")})


@router.post("/reset")
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
    uid = await db.consume_password_reset_async(token, pw)
    if uid is None:
        return RedirectResponse(f"/reset?token={token}&error=token", status_code=302)
    user = db.get_user(uid)
    if user:
        state.log_event("info", f"Password reset completed for {user['email']}")
    resp = RedirectResponse("/", status_code=302)
    if uid:
        set_session_cookie(resp, request, uid)  # log the user straight in
    return resp

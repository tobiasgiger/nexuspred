"""Auth pages: first-run setup, login/logout, invite registration, password reset."""
from __future__ import annotations

import asyncio
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import auth, config, db, mfa, security, state
from ..security import client_ip
from ..web import render, secure, set_session_cookie


def _signed_in(request: Request, user: dict, how: str) -> None:
    """Stamp + audit a successful sign-in (login, invite registration, reset)."""
    ip = client_ip(request)
    db.record_login(user["id"], ip)
    db.log_action(user["id"], user["email"], "login_ok", ip, how)

_RATE = "Too many attempts — please wait a minute and try again."

router = APIRouter(tags=["auth"])

_setup_lock = asyncio.Lock()  # two racing first-run POSTs must not both create an admin


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, error: str = "") -> HTMLResponse:
    if db.user_count() == 0:
        return RedirectResponse("/setup", status_code=302)
    if auth.current_user(request):
        return RedirectResponse("/", status_code=302)
    msgs = {"bad": "Wrong email or password.", "rate": _RATE}
    return render(request, "login.html", {"error": msgs.get(error, "")})


def _known_address(email: str, ip: str) -> bool:
    """Whether ``ip`` is the address the account last signed in from."""
    if not ip:
        return False
    user = db.get_user_by_email(email)
    return bool(user and user.get("last_login_ip") and user["last_login_ip"] == ip)


@router.post("/login")
async def login_submit(request: Request):
    form = await request.form()
    email = str(form.get("email", "")).strip().lower()
    ip = client_ip(request)
    if not security.login_allowed(email) and not _known_address(email, ip):
        # The per-account / global brakes count failures from *any* address, so
        # a stranger could otherwise lock the owner out of the kill switch by
        # guessing at their (public) marketplace email. The address the account
        # last signed in from stays subject only to the per-IP limiter.
        db.log_action(None, email[:200], "login_blocked", ip, "too many failed logins for this account")
        return RedirectResponse("/login?error=rate", status_code=302, headers={"Retry-After": "600"})
    user = await db.authenticate_async(email, str(form.get("password", "")))
    if not user:
        security.login_failed(email)
        db.log_action(None, email[:200], "login_failed", client_ip(request), "wrong email or password")
        return RedirectResponse("/login?error=bad", status_code=302)
    if user.get("totp_enabled"):
        # the password alone opens nothing: a 5-minute token carries the user to the code page
        resp = RedirectResponse("/login/2fa", status_code=302)
        resp.set_cookie(auth.MFA_COOKIE, auth.make_mfa_token(user["id"]), max_age=auth.MFA_TTL,
                        httponly=True, secure=secure(request), samesite="lax", path="/")
        return resp
    _signed_in(request, user, "password")
    resp = RedirectResponse("/", status_code=302)
    set_session_cookie(resp, request, user["id"])
    return resp


@router.get("/login/2fa", response_class=HTMLResponse)
async def login_2fa_page(request: Request, error: str = "") -> HTMLResponse:
    if auth.read_mfa_token(request.cookies.get(auth.MFA_COOKIE)) is None:
        return RedirectResponse("/login", status_code=302)
    msgs = {"bad": "That code is not valid.", "rate": _RATE, "expired": "The sign-in expired — enter your password again."}
    return render(request, "login_2fa.html", {"error": msgs.get(error, "")})


@router.post("/login/2fa")
async def login_2fa_submit(request: Request):
    uid = auth.read_mfa_token(request.cookies.get(auth.MFA_COOKIE))
    if uid is None:
        return RedirectResponse("/login?error=expired", status_code=302)
    user = db.get_user(uid)
    if not user or not user.get("totp_enabled"):
        return RedirectResponse("/login", status_code=302)
    form = await request.form()
    code = str(form.get("code", "")).strip()
    if not security.login_allowed(user["email"]):
        db.log_action(user["id"], user["email"], "login_blocked", client_ip(request), "too many second-factor attempts")
        return RedirectResponse("/login/2fa?error=rate", status_code=302, headers={"Retry-After": "600"})
    how = _second_factor(user, code)
    if how is None:
        security.login_failed(user["email"])
        db.log_action(user["id"], user["email"], "login_failed", client_ip(request), "second factor rejected")
        return RedirectResponse("/login/2fa?error=bad", status_code=302)
    _signed_in(request, user, how)
    resp = RedirectResponse("/", status_code=302)
    resp.delete_cookie(auth.MFA_COOKIE, path="/")
    set_session_cookie(resp, request, user["id"])
    if how == "backup code":
        left = db.mfa_backup_codes_left(user["id"])
        state.log_event("warn", f"Signed in with a backup code — {left} left. Request a new set under Account when they run low.")
    return resp


def _second_factor(user: dict, code: str) -> str | None:
    """'totp' / 'backup code' when ``code`` is accepted, else None. A TOTP code
    is single-use (counter), a backup code is burned."""
    if mfa.looks_like_backup_code(code):
        return "backup code" if db.mfa_use_backup_code(user["id"], code) else None
    secret = db.mfa_secret(user["id"])
    if not secret:
        return None
    counter = mfa.verify_totp(secret, code, last_counter=db.mfa_counter(user["id"]))
    if counter is None or not db.mfa_touch_counter(user["id"], counter):
        return None
    return "totp"


# --------------------------------------------------- two-factor enrolment
@router.get("/2fa/setup", response_class=HTMLResponse)
async def mfa_setup_page(request: Request, error: str = "", keep: str = "") -> HTMLResponse:
    """Enrolment for a signed-in account that must (or wants to) set up 2FA:
    a fresh secret is issued on every visit until one is confirmed (``keep``
    after a wrong code re-shows the pending one, so the QR already scanned stays valid)."""
    user = request.state.user
    if user.get("totp_enabled"):
        return RedirectResponse("/", status_code=302)
    secret = db.mfa_secret(user["id"]) if keep else ""
    if not secret:
        secret = mfa.new_secret()
        db.mfa_begin(user["id"], secret)
    uri = mfa.provisioning_uri(secret, user["email"])
    msgs = {"bad": "That code is not valid — check the time on your phone and try again.", "rate": _RATE}
    return render(request, "2fa_setup.html", {"qr": mfa.qr_data_uri(uri), "secret": mfa.pretty_secret(secret),
                                              "email": user["email"], "error": msgs.get(error, "")})


@router.post("/2fa/setup")
async def mfa_setup_submit(request: Request):
    user = request.state.user
    form = await request.form()
    secret = db.mfa_secret(user["id"])
    counter = mfa.verify_totp(secret, str(form.get("code", ""))) if secret else None
    if counter is None:
        # keep the same secret: the QR on the page stays valid for a retry
        return RedirectResponse("/2fa/setup?error=bad&keep=1", status_code=302)
    db.mfa_enable(user["id"], counter)
    codes = mfa.new_backup_codes()
    db.mfa_set_backup_codes(user["id"], codes)
    db.log_action(user["id"], user["email"], "mfa_enabled", user["email"], "enrolled")
    state.log_event("info", f"Two-factor authentication enabled for {user['email']}")
    return render(request, "2fa_codes.html", {"codes": codes})


@router.get("/logout")
async def logout_get() -> RedirectResponse:
    """Signing out is a POST (see the topbar); a bare link or a cross-site
    ``<img src="/logout">`` changes nothing, whatever the browser's Fetch
    Metadata support."""
    return RedirectResponse("/", status_code=302)


@router.post("/logout")
async def logout(request: Request) -> RedirectResponse:
    resp = RedirectResponse("/login", status_code=302)
    resp.delete_cookie(auth.COOKIE, path="/")
    return resp


@router.get("/setup", response_class=HTMLResponse)
async def setup_page(request: Request, error: str = "") -> HTMLResponse:
    if db.user_count() > 0:
        return RedirectResponse("/login", status_code=302)
    msgs = {"mismatch": "Passwords don't match.", "short": "Password must be at least 8 characters.",
            "email": "Enter a valid email.", "rate": _RATE}
    return render(request, "setup.html", {"error": msgs.get(error, "")})


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
        user = await db.create_user_async(email, pw, is_admin=True, initial_settings=legacy, totp_required=True)
    area = db.user_primary_area(user["id"])
    config.invalidate(area)
    config.migrate_legacy_webhook(area_id=area)
    state.log_event("info", f"Admin account created: {email}")
    _signed_in(request, user, "setup")
    resp = RedirectResponse("/2fa/setup", status_code=302)          # two-factor enrolment comes first
    set_session_cookie(resp, request, user["id"])
    return resp


@router.get("/register", response_class=HTMLResponse)
async def register_page(request: Request, code: str = "", error: str = "") -> HTMLResponse:
    invite = db.get_invite(code) if code else None
    valid = bool(invite and not invite.get("used_by"))
    msgs = {"mismatch": "Passwords don't match.", "short": "Password must be at least 8 characters.",
            "email": "Enter a valid email.", "exists": "An account with that email already exists.",
            "invite": "This invite is invalid or already used.", "rate": _RATE}
    return render(request, "register.html",
                  {"code": code, "valid_invite": valid,
                   "invite_email": (invite or {}).get("email", ""), "error": msgs.get(error, "")})


@router.post("/register")
async def register_submit(request: Request):
    form = await request.form()
    code = str(form.get("code", ""))
    back = f"/register?code={quote(code, safe='')}&error="
    invite = db.get_invite(code)
    if not invite or invite.get("used_by"):
        return RedirectResponse(back + "invite", status_code=302)
    email = str(form.get("email", "")).strip().lower()
    pw = str(form.get("password", ""))
    if "@" not in email:
        return RedirectResponse(back + "email", status_code=302)
    if invite.get("email") and email != invite["email"]:
        return RedirectResponse(back + "email", status_code=302)  # invite is bound to an address
    if pw != str(form.get("password2", "")):
        return RedirectResponse(back + "mismatch", status_code=302)
    if len(pw) < 8:
        return RedirectResponse(back + "short", status_code=302)
    if db.get_user_by_email(email):
        return RedirectResponse(back + "exists", status_code=302)
    user = await db.create_user_async(email, pw, is_admin=invite.get("is_admin", False), totp_required=True)
    db.consume_invite(code, user["id"])
    area = db.user_primary_area(user["id"])
    config.migrate_legacy_webhook(area_id=area)  # give the new area a Default webhook
    state.log_event("info", f"Account registered: {email}")
    _signed_in(request, user, "invite")
    resp = RedirectResponse("/2fa/setup", status_code=302)          # two-factor enrolment comes first
    set_session_cookie(resp, request, user["id"])
    return resp


@router.get("/reset", response_class=HTMLResponse)
async def reset_page(request: Request, token: str = "", error: str = "") -> HTMLResponse:
    rec = db.get_password_reset(token) if token else None
    msgs = {"mismatch": "Passwords don't match.", "short": "Password must be at least 8 characters.",
            "token": "This reset link is invalid or has expired.", "rate": _RATE}
    return render(request, "reset.html",
                  {"token": token, "valid_token": bool(rec), "error": msgs.get(error, "")})


@router.post("/reset")
async def reset_submit(request: Request):
    form = await request.form()
    token = str(form.get("token", ""))
    back = f"/reset?token={quote(token, safe='')}&error="
    if not db.get_password_reset(token):
        return RedirectResponse(back + "token", status_code=302)
    pw = str(form.get("password", ""))
    if pw != str(form.get("password2", "")):
        return RedirectResponse(back + "mismatch", status_code=302)
    if len(pw) < 8:
        return RedirectResponse(back + "short", status_code=302)
    uid = await db.consume_password_reset_async(token, pw)
    if uid is None:
        return RedirectResponse(back + "token", status_code=302)
    user = db.get_user(uid)
    if user:
        state.log_event("info", f"Password reset completed for {user['email']}")
        _signed_in(request, user, "password reset")
    resp = RedirectResponse("/", status_code=302)
    if uid:
        set_session_cookie(resp, request, uid)  # log the user straight in
    return resp

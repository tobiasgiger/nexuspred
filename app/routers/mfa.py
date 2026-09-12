"""Two-factor authentication from the dashboard (Account page) and the admin
recovery (Users page). Enrolment for new accounts happens on /2fa/setup
(app/routers/auth.py); this router serves accounts that enable it later,
regenerate backup codes, or disable it where allowed."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from .. import db, mfa, state
from ..security import client_ip
from ..web import require_admin

router = APIRouter(prefix="/api", tags=["mfa"])


def _status(user: dict[str, Any]) -> dict[str, Any]:
    fresh = db.get_user(user["id"]) or user
    return mfa.status_of(fresh, db.mfa_backup_codes_left(user["id"]))


async def _check_password(user: dict[str, Any], body: dict[str, Any]) -> None:
    if not await db.authenticate_async(user["email"], str(body.get("password", ""))):
        raise HTTPException(status_code=400, detail="Password is incorrect")


@router.get("/account/2fa")
async def api_mfa_status(request: Request) -> dict[str, Any]:
    return _status(request.state.user)


@router.post("/account/2fa/begin")
async def api_mfa_begin(request: Request) -> dict[str, Any]:
    """Issue a fresh secret (QR + key) for an account without 2FA."""
    user = db.get_user(request.state.user["id"]) or request.state.user
    if user.get("totp_enabled"):
        raise HTTPException(status_code=400, detail="Two-factor authentication is already on")
    secret = mfa.new_secret()
    db.mfa_begin(user["id"], secret)
    uri = mfa.provisioning_uri(secret, user["email"])
    return {"qr": mfa.qr_data_uri(uri), "secret": mfa.pretty_secret(secret)}


@router.post("/account/2fa/confirm")
async def api_mfa_confirm(request: Request) -> dict[str, Any]:
    """Confirm the pending secret with a code → 2FA on, backup codes returned once."""
    user = db.get_user(request.state.user["id"]) or request.state.user
    body = await request.json()
    if user.get("totp_enabled"):
        raise HTTPException(status_code=400, detail="Two-factor authentication is already on")
    secret = db.mfa_secret(user["id"])
    counter = mfa.verify_totp(secret, str(body.get("code", ""))) if secret else None
    if counter is None:
        raise HTTPException(status_code=400, detail="That code is not valid — check the time on your phone and try again")
    db.mfa_enable(user["id"], counter)
    codes = mfa.new_backup_codes()
    db.mfa_set_backup_codes(user["id"], codes)
    db.log_action(user["id"], user["email"], "mfa_enabled", user["email"], "account page")
    state.log_event("info", f"Two-factor authentication enabled for {user['email']}")
    return {**_status(user), "backup_codes": codes}


@router.post("/account/2fa/backup-codes")
async def api_mfa_new_backup_codes(request: Request) -> dict[str, Any]:
    """A fresh set of ten single-use codes (lost, or all used) — replaces the old set.
    Needs the password and a current authenticator code."""
    user = db.get_user(request.state.user["id"]) or request.state.user
    body = await request.json()
    if not user.get("totp_enabled"):
        raise HTTPException(status_code=400, detail="Two-factor authentication is not on")
    await _check_password(user, body)
    secret = db.mfa_secret(user["id"])
    counter = mfa.verify_totp(secret, str(body.get("code", "")), last_counter=db.mfa_counter(user["id"]))
    if counter is None or not db.mfa_touch_counter(user["id"], counter):
        raise HTTPException(status_code=400, detail="That authenticator code is not valid")
    codes = mfa.new_backup_codes()
    db.mfa_set_backup_codes(user["id"], codes)
    db.log_action(user["id"], user["email"], "mfa_backup_codes", user["email"], "new set")
    state.log_event("info", f"New two-factor backup codes issued for {user['email']}")
    return {**_status(user), "backup_codes": codes}


@router.post("/account/2fa/disable")
async def api_mfa_disable(request: Request) -> dict[str, Any]:
    """Turn 2FA off — only for accounts it is not required for."""
    user = db.get_user(request.state.user["id"]) or request.state.user
    body = await request.json()
    if user.get("totp_required"):
        raise HTTPException(status_code=403, detail="Two-factor authentication is required for this account")
    if not user.get("totp_enabled"):
        return _status(user)
    await _check_password(user, body)
    secret = db.mfa_secret(user["id"])
    counter = mfa.verify_totp(secret, str(body.get("code", "")), last_counter=db.mfa_counter(user["id"]))
    if counter is None or not db.mfa_touch_counter(user["id"], counter):
        raise HTTPException(status_code=400, detail="That authenticator code is not valid")     # a code is used once, here too
    db.mfa_reset(user["id"], required=False)
    db.log_action(user["id"], user["email"], "mfa_disabled", user["email"])
    state.log_event("warn", f"Two-factor authentication disabled for {user['email']}")
    return _status(user)


@router.post("/users/{user_id}/2fa/reset")
async def api_admin_mfa_reset(request: Request, user_id: int) -> dict[str, Any]:
    """Admin recovery: the user lost the authenticator and the backup codes.
    Drops both; the user signs in with the password and enrols again."""
    admin = require_admin(request)
    target = db.get_user(user_id)
    if not target:
        raise HTTPException(status_code=404, detail="No such user")
    if target.get("is_admin") and target["id"] != admin["id"] and admin["id"] != 1:
        raise HTTPException(status_code=403, detail="Only the bootstrap admin can reset another administrator's two-factor setup")
    db.mfa_reset(user_id, required=True)
    db.revoke_sessions(user_id)
    db.log_action(admin["id"], admin["email"], "mfa_reset", target["email"], client_ip(request))
    state.log_event("warn", f"Two-factor setup of {target['email']} reset by {admin['email']} — they enrol again at next sign-in")
    return {"status": "ok", "user_id": user_id}

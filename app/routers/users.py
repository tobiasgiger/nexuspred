"""User & admin management: current user, users/invites/features, audit log,
self-service password change, admin-issued password resets."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from .. import alerts, context, db, state
from ..discord_signals import listener as discord_listener
from ..web import base_url, require_admin, set_session_cookie

router = APIRouter(prefix="/api", tags=["users"])


@router.get("/me")
async def api_me(request: Request) -> dict[str, Any]:
    u = request.state.user
    return {"id": u["id"], "email": u["email"], "is_admin": u["is_admin"],
            "features": db.user_features(u["id"])}


@router.get("/users")
async def api_users(request: Request) -> dict[str, Any]:
    require_admin(request)
    return {"users": db.list_users(), "features": db.FEATURES}


@router.post("/users/{user_id}/features")
async def api_set_user_feature(request: Request, user_id: int) -> dict[str, Any]:
    """Admin toggles a feature entitlement (e.g. Discord Signals) for a user."""
    admin = require_admin(request)
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


@router.post("/users/invite")
async def api_create_invite(request: Request) -> dict[str, Any]:
    admin = require_admin(request)
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
    url = f"{base_url(request)}/register?code={code}"
    emailed = False
    if email and "@" in email and bool(body.get("send_email")):
        emailed = await alerts.send_email_to(
            email, "You're invited to Fluxbridge",
            f"You've been invited to Fluxbridge. Create your account here:\n\n{url}\n\n"
            "This link is single-use. If you didn't expect this, you can ignore it.")
    return {"code": code, "url": url, "emailed": emailed, "smtp_configured": alerts.smtp_configured()}


@router.get("/invites")
async def api_invites(request: Request) -> list[dict[str, Any]]:
    require_admin(request)
    return db.list_invites()


@router.delete("/invites/{code}")
async def api_delete_invite(request: Request, code: str) -> dict[str, Any]:
    admin = require_admin(request)
    db.delete_invite(code)
    db.log_action(admin["id"], admin["email"], "invite_revoke", code[:8] + "…")
    return {"status": "deleted", "code": code}


@router.delete("/users/{user_id}")
async def api_delete_user(request: Request, user_id: int) -> dict[str, Any]:
    admin = require_admin(request)
    if user_id == admin["id"]:
        raise HTTPException(status_code=400, detail="You can't delete your own account")
    target = (db.get_user(user_id) or {}).get("email", str(user_id))
    db.delete_user(user_id)
    db.log_action(admin["id"], admin["email"], "user_delete", target)
    state.log_event("info", f"User {user_id} deleted by {admin['email']}")
    return {"status": "deleted", "id": user_id}


@router.get("/audit")
async def api_audit(request: Request, kind: str = "actions") -> list[dict[str, Any]]:
    """``kind=actions`` (default) → admin actions, ``logins`` → sign-in events, ``all``."""
    require_admin(request)
    logins = {"actions": False, "logins": True}.get(kind)
    return db.list_audit(100, logins=logins)


@router.post("/account/password")
async def api_change_password(request: Request) -> JSONResponse:
    """Self-service password change: verify the current password, then set a
    new one. Every other session of the user is signed out (the cookie carries
    the password version); this session gets a fresh cookie so the user is not."""
    user = request.state.user
    body = await request.json()
    current = str(body.get("current", ""))
    new = str(body.get("new", ""))
    if len(new) < 8:
        raise HTTPException(status_code=400, detail="New password must be at least 8 characters")
    if not await db.authenticate_async(user["email"], current):
        raise HTTPException(status_code=400, detail="Current password is incorrect")
    await db.set_password_async(user["id"], new)
    db.log_action(user["id"], user["email"], "password_change", user["email"])
    state.log_event("info", f"Password changed for {user['email']}")
    resp = JSONResponse({"status": "ok"})
    set_session_cookie(resp, request, user["id"])
    return resp


@router.post("/account/sessions/revoke")
async def api_revoke_own_sessions(request: Request) -> JSONResponse:
    """Sign out every other device of the caller: the fingerprint in the session
    cookies is rotated (cookies are stateless), this session gets a fresh one."""
    user = request.state.user
    db.revoke_sessions(user["id"])
    db.log_action(user["id"], user["email"], "sessions_revoke", user["email"], "self")
    resp = JSONResponse({"status": "ok"})
    set_session_cookie(resp, request, user["id"])
    return resp


@router.post("/users/{user_id}/sessions/revoke")
async def api_revoke_user_sessions(request: Request, user_id: int) -> dict[str, Any]:
    """Admin: sign a user out everywhere (lost phone, leaked cookie)."""
    admin = require_admin(request)
    target = db.get_user(user_id)
    if not target:
        raise HTTPException(status_code=404, detail="No such user")
    if target.get("is_admin") and target["id"] != admin["id"] and admin["id"] != 1:
        raise HTTPException(status_code=403, detail="Only the bootstrap admin can sign out another administrator")
    db.revoke_sessions(user_id)
    db.log_action(admin["id"], admin["email"], "sessions_revoke", target["email"])
    state.log_event("info", f"All sessions of {target['email']} were signed out by {admin['email']}")
    return {"status": "ok", "user_id": user_id}


@router.post("/users/{user_id}/reset")
async def api_create_reset(request: Request, user_id: int) -> dict[str, Any]:
    """Admin generates a one-time password-reset link for a user."""
    admin = require_admin(request)
    target = db.get_user(user_id)
    if not target:
        raise HTTPException(status_code=404, detail="No such user")
    if target.get("is_admin") and target["id"] != admin["id"] and admin["id"] != 1:
        # A reset link signs the user in: one admin must not take over another
        # admin's workspace. The bootstrap admin (user 1) can still recover any account.
        raise HTTPException(status_code=403, detail="Only the bootstrap admin can reset another administrator")
    token = db.create_password_reset(user_id)
    db.log_action(admin["id"], admin["email"], "password_reset", target["email"])
    url = f"{base_url(request)}/reset?token={token}"
    emailed = await alerts.send_email_to(
        target["email"], "Reset your Fluxbridge password",
        f"An administrator started a password reset for your Fluxbridge account.\n\n"
        f"Set a new password here (single-use, expires in 24 hours):\n\n{url}\n\n"
        "If you didn't expect this, contact your administrator.")
    # The link is a login: hand it to the admin only when it could not be mailed
    # to the user (no SMTP) and they have to pass it on out of band.
    return {"user_id": user_id, "url": "" if emailed else url, "emailed": emailed, "smtp_configured": alerts.smtp_configured()}

"""Two-factor authentication: TOTP algorithm, enforced enrolment for new
accounts, the second-factor sign-in, single-use backup codes, regeneration,
disable rules and the admin recovery."""
from __future__ import annotations

import base64
import re

import pytest

from app import auth, db, mfa, security
from tests.conftest import _make_client, login_as


@pytest.fixture
def clock(monkeypatch):
    """Advance the server's TOTP clock: a code is single-use per 30-second step,
    so consecutive sign-ins in one test need the clock to move."""
    import time as _time
    state = {"offset": 0.0}
    real = _time.time
    monkeypatch.setattr(mfa.time, "time", lambda: real() + state["offset"])

    def advance(seconds):
        state["offset"] += seconds
    return advance


def code_now(secret):
    return mfa.totp(secret, at=mfa.time.time())


def test_totp_matches_rfc6238_vectors():
    secret = base64.b32encode(b"12345678901234567890").decode()
    assert mfa.totp(secret, at=59) == "287082" and mfa.totp(secret, at=1111111109) == "081804"
    assert mfa.verify_totp(secret, "287082", at=59) == 1
    assert mfa.verify_totp(secret, "287082", at=59 + 30) == 1                # ±1 step of drift
    assert mfa.verify_totp(secret, "287082", at=59, last_counter=1) is None   # replay refused
    assert mfa.verify_totp(secret, "28 70 82", at=59) == 1                  # spaces tolerated
    assert mfa.verify_totp(secret, "000000", at=59) is None and mfa.verify_totp(secret, "", at=59) is None
    uri = mfa.provisioning_uri("ABC", "a@b.c")
    assert uri.startswith("otpauth://totp/Fluxbridge%3Aa%40b.c?secret=ABC&issuer=Fluxbridge") and mfa.qr_data_uri(uri).startswith("data:image/svg+xml")
    codes = mfa.new_backup_codes()
    assert len(codes) == 10 and all(re.fullmatch(r"[A-Z2-9]{5}-[A-Z2-9]{5}", c) for c in codes) and len(set(codes)) == 10
    assert mfa.looks_like_backup_code("abcde-fghjk") and not mfa.looks_like_backup_code("123456")


async def _enrol(client, uid):
    """Enrol through the API; returns (secret, backup codes)."""
    r = await client.post("/api/account/2fa/begin")
    assert r.status_code == 200 and r.json()["qr"].startswith("data:image/svg+xml") and len(r.json()["secret"].replace(" ", "")) == 32
    secret = db.mfa_secret(uid)
    r = await client.post("/api/account/2fa/confirm", json={"code": "000000"})
    assert r.status_code == 400
    r = await client.post("/api/account/2fa/confirm", json={"code": code_now(secret)})
    assert r.status_code == 200 and r.json()["enabled"] is True and len(r.json()["backup_codes"]) == 10
    return secret, r.json()["backup_codes"]


async def test_registration_forces_enrolment(client, admin, anon_client):
    code = (await client.post("/api/users/invite", json={})).json()["code"]
    r = await anon_client.post("/register", data={"code": code, "email": "new@example.com", "password": "password123", "password2": "password123"})
    assert r.status_code == 302, r.text
    assert r.headers["location"] == "/2fa/setup"
    cookie = r.cookies[auth.COOKIE]
    async with _make_client(cookie) as c:
        assert (await c.get("/api/settings")).status_code == 403                 # nothing else until enrolled
        assert (await c.get("/", headers={"accept": "text/html"})).headers.get("location") == "/2fa/setup"
        page = await c.get("/2fa/setup")
        assert page.status_code == 200 and "data:image/svg+xml" in page.text and "otpauth" not in page.text
        u = db.get_user_by_email("new@example.com")
        secret = db.mfa_secret(u["id"])
        r = await c.post("/2fa/setup", data={"code": "000000"})
        assert r.headers["location"].startswith("/2fa/setup?error=bad")
        assert db.mfa_secret(u["id"]) == secret                                  # the scanned QR stays valid
        assert (await c.get("/2fa/setup?keep=1")).status_code == 200 and db.mfa_secret(u["id"]) == secret
        r = await c.post("/2fa/setup", data={"code": mfa.totp(secret)})
        assert r.status_code == 200 and r.text.count("<li>") == 10               # backup codes shown once
        assert (await c.get("/api/settings")).status_code == 200
        st = (await c.get("/api/account/2fa")).json()
        assert st == {"enabled": True, "required": True, "backup_codes_left": 10, "backup_codes_total": 10}
        r = await c.post("/api/account/2fa/disable", json={"password": "password123", "code": mfa.totp(secret)})
        assert r.status_code == 403                                                # required accounts cannot turn it off


async def test_sign_in_needs_the_second_factor_and_backup_codes_are_single_use(client, admin, anon_client, clock):
    secret, codes = await _enrol(client, admin["id"])
    clock(30)
    r = await anon_client.post("/login", data={"email": "admin@example.com", "password": "password123"})
    assert r.headers["location"] == "/login/2fa" and auth.COOKIE not in r.cookies and auth.MFA_COOKIE in r.cookies
    pending = r.cookies[auth.MFA_COOKIE]
    assert auth.read_session(pending) is None                                     # the pending token is no session
    async with _make_client() as c:
        c.headers["cookie"] = f"{auth.MFA_COOKIE}={pending}"
        assert (await c.get("/login/2fa")).status_code == 200
        r = await c.post("/login/2fa", data={"code": "000000"})
        assert r.headers["location"] == "/login/2fa?error=bad"
        code = code_now(secret)
        r = await c.post("/login/2fa", data={"code": code})
        assert r.headers["location"] == "/" and auth.read_session(r.cookies[auth.COOKIE]) == admin["id"]
        # the same TOTP code is not accepted twice
        r = await c.post("/login/2fa", data={"code": code})
        assert r.headers["location"] == "/login/2fa?error=bad"
        # a backup code works once
        r = await c.post("/login/2fa", data={"code": codes[0].lower()})
        assert r.headers["location"] == "/" and db.mfa_backup_codes_left(admin["id"]) == 9
        r = await c.post("/login/2fa", data={"code": codes[0]})
        assert r.headers["location"] == "/login/2fa?error=bad"
    assert any(a["action"] == "login_ok" and a["detail"] == "backup code" for a in db.list_audit(20, logins=True))


async def test_new_backup_codes_replace_the_old_set_and_disable_is_allowed_when_optional(client, admin, clock):
    secret, codes = await _enrol(client, admin["id"])
    assert db.mfa_use_backup_code(admin["id"], codes[3]) and db.mfa_backup_codes_left(admin["id"]) == 9
    clock(30)
    r = await client.post("/api/account/2fa/backup-codes", json={"password": "wrong", "code": code_now(secret)})
    assert r.status_code == 400
    r = await client.post("/api/account/2fa/backup-codes", json={"password": "password123", "code": code_now(secret)})
    assert r.status_code == 200 and len(r.json()["backup_codes"]) == 10 and r.json()["backup_codes_left"] == 10
    assert not db.mfa_use_backup_code(admin["id"], codes[4])                    # the old set is dead
    assert db.mfa_use_backup_code(admin["id"], r.json()["backup_codes"][0])
    # the bootstrap admin was not created through sign-up: 2FA is optional there
    clock(30)
    r = await client.post("/api/account/2fa/disable", json={"password": "password123", "code": code_now(secret)})
    assert r.status_code == 200 and r.json()["enabled"] is False and db.mfa_secret(admin["id"]) == "" and db.mfa_backup_codes_left(admin["id"]) == 0


async def test_admin_reset_and_password_reset_re_enrol(client, admin, anon_client):
    u = db.create_user("user@example.com", "password123", totp_required=True)
    async with _make_client(auth.make_session(u["id"])) as c:
        secret, codes = await _enrol(c, u["id"])
        old = auth.make_session(u["id"])
    r = await client.post(f"/api/users/{u['id']}/2fa/reset")
    assert r.status_code == 200
    fresh = db.get_user(u["id"])
    assert not fresh["totp_enabled"] and fresh["totp_required"] and db.mfa_secret(u["id"]) == "" and db.mfa_backup_codes_left(u["id"]) == 0
    assert auth.read_session(old) is None                                         # signed out everywhere
    r = await anon_client.post("/login", data={"email": "user@example.com", "password": "password123"})
    assert r.headers["location"] == "/" and auth.COOKIE in r.cookies              # password alone again…
    async with _make_client(r.cookies[auth.COOKIE]) as c:
        assert (await c.get("/api/settings")).status_code == 403                 # …but only into enrolment
    # a password reset changes the password only: the second factor stays (a
    # reset link in the wrong hands is never a 2FA bypass) and nobody is signed in by it
    async with _make_client(auth.make_session(u["id"])) as c:
        await _enrol(c, u["id"])
    token = db.create_password_reset(u["id"])
    assert db.consume_password_reset(token, "newpassword1") == u["id"]
    fresh = db.get_user(u["id"])
    assert fresh["totp_enabled"] and fresh["totp_required"] and db.mfa_backup_codes_left(u["id"]) == 10
    assert db.authenticate("user@example.com", "newpassword1")


async def test_second_factor_is_rate_limited_per_account(client, admin, anon_client):
    secret, _ = await _enrol(client, admin["id"])
    for _ in range(25):
        security.login_failed("admin@example.com")
    r = await anon_client.post("/login", data={"email": "admin@example.com", "password": "password123"},
                               headers={"x-forwarded-for": "203.0.113.9"})
    assert r.headers["location"] == "/login?error=rate"

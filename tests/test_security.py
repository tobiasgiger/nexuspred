"""Hardening added in 5.0.0-alpha.4: security headers + CSP nonce, CSRF
origin check, rate limits on the credential endpoints, request-body cap,
session invalidation on password change, settings-key whitelist, admin-only
self-update, Discord feature gate and the outbound-URL (SSRF) guard."""
from __future__ import annotations

import secrets
import socket

import httpx
import pytest

from app import auth, config, context, db, security
from tests.helpers import settle
from app.security import check_outbound_url as real_check_outbound_url  # conftest stubs the module attr
from app.main import app
from tests.conftest import login_as


# ------------------------------------------------------------ headers / CSP
async def test_security_headers_on_dashboard_and_api(client):
    r = await client.get("/")
    assert r.status_code == 200
    csp = r.headers["content-security-policy"]
    assert "frame-ancestors 'none'" in csp and "script-src 'self' 'nonce-" in csp
    nonce = csp.split("'nonce-")[1].split("'")[0]
    assert f'nonce="{nonce}"' in r.text  # inline theme/version scripts carry the nonce
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert "strict-transport-security" not in r.headers  # plain http in tests

    r = await client.get("/api/status")
    assert r.headers["cache-control"] == "no-store"

    r = await client.get("/api/status", headers={"x-forwarded-proto": "https"})
    assert r.headers["strict-transport-security"].startswith("max-age=")


async def test_auth_pages_carry_nonce(anon_client):
    r = await anon_client.get("/setup")
    nonce = r.headers["content-security-policy"].split("'nonce-")[1].split("'")[0]
    assert f'nonce="{nonce}"' in r.text


# ------------------------------------------------------------------- CSRF
async def test_cross_site_post_is_rejected(client):
    r = await client.post("/api/settings", json={"default_qty": 2},
                          headers={"origin": "https://evil.example"})
    assert r.status_code == 403
    r = await client.post("/api/settings", json={"default_qty": 2},
                          headers={"sec-fetch-site": "cross-site"})
    assert r.status_code == 403
    # Same origin (matching Host) and header-less clients (curl, TradingView) pass.
    r = await client.post("/api/settings", json={"default_qty": 2},
                          headers={"origin": "http://testserver", "sec-fetch-site": "same-origin"})
    assert r.status_code == 200 and r.json()["default_qty"] == 2
    r = await client.get("/api/settings", headers={"origin": "https://evil.example"})
    assert r.status_code == 200  # reads are never blocked


async def test_webhook_ingress_ignores_origin(anon_client, admin, webhook_factory):
    from app import context
    with context.use_area(1):
        wh = webhook_factory(name="x")
    r = await anon_client.post(f"/webhook/{wh['token']}", json={"action": "buy", "symbol": "MNQ1!"},
                               headers={"origin": "https://www.tradingview.com"})
    assert r.status_code == 202


# ------------------------------------------------------------ rate limits
async def test_login_is_rate_limited(anon_client, admin):
    for _ in range(10):
        r = await anon_client.post("/login", data={"email": "admin@example.com", "password": "wrong"})
        assert r.headers["location"] == "/login?error=bad"
    r = await anon_client.post("/login", data={"email": "admin@example.com", "password": "password123"})
    assert r.headers["location"] == "/login?error=rate"
    page = await anon_client.get("/login?error=rate")
    assert "Too many attempts" in page.text
    security.reset_limits()
    r = await anon_client.post("/login", data={"email": "admin@example.com", "password": "password123"})
    assert r.headers["location"] == "/"


async def test_rate_limit_is_per_client_ip(anon_client, admin):
    for i in range(10):
        await anon_client.post("/login", data={"email": "a@b.c", "password": "x"},
                               headers={"x-forwarded-for": "203.0.113.5"})
    r = await anon_client.post("/login", data={"email": "a@b.c", "password": "x"},
                               headers={"x-forwarded-for": "203.0.113.5"})
    assert r.headers["location"] == "/login?error=rate"
    r = await anon_client.post("/login", data={"email": "a@b.c", "password": "x"},
                               headers={"x-forwarded-for": "198.51.100.9"})
    assert r.headers["location"] == "/login?error=bad"


async def test_password_change_api_is_rate_limited(client):
    for _ in range(5):
        r = await client.post("/api/account/password", json={"current": "nope", "new": "password456"})
        assert r.status_code == 400
    r = await client.post("/api/account/password", json={"current": "nope", "new": "password456"})
    assert r.status_code == 429


# ------------------------------------------------------------- body limit
async def test_oversized_body_is_rejected(anon_client, admin, webhook_factory):
    from app import context
    with context.use_area(1):
        wh = webhook_factory(name="x")
    big = b'{"action":"buy","symbol":"MNQ1!","pad":"' + b"x" * (security.MAX_BODY_BYTES + 1) + b'"}'
    r = await anon_client.post(f"/webhook/{wh['token']}", content=big,
                               headers={"content-type": "application/json"})
    assert r.status_code == 413
    # Chunked (no Content-Length) bodies are counted as they stream.
    async def chunks():
        for _ in range(3):
            yield b"x" * (security.MAX_BODY_BYTES // 2)
    r = await anon_client.post(f"/webhook/{wh['token']}", content=chunks(),
                               headers={"content-type": "application/json"})
    assert r.status_code == 413


# -------------------------------------------------------- session binding
async def test_password_change_invalidates_other_sessions(admin):
    old = auth.make_session(admin["id"])
    assert auth.read_session(old) == admin["id"]
    db.set_password(admin["id"], "new-password-123")
    assert auth.read_session(old) is None
    assert auth.read_session(auth.make_session(admin["id"])) == admin["id"]


async def test_reset_invalidates_old_sessions(admin):
    old = auth.make_session(admin["id"])
    token = db.create_password_reset(admin["id"])
    assert db.consume_password_reset(token, "another-pass-1") == admin["id"]
    assert auth.read_session(old) is None


async def test_legacy_cookie_without_fingerprint_is_rejected(admin):
    import json
    body = auth._b64e(json.dumps({"uid": admin["id"], "exp": 4102444800}).encode())
    assert auth.read_session(f"{body}.{auth._sign(body)}") is None


async def test_auth_exempt_paths_are_exact(anon_client, admin):
    assert (await anon_client.get("/healthz")).status_code == 200
    r = await anon_client.get("/healthzz", headers={"accept": "application/json"})
    assert r.status_code == 401
    r = await anon_client.get("/loginx", headers={"accept": "application/json"})
    assert r.status_code == 401


# ------------------------------------------------------ settings whitelist
async def test_generic_settings_cannot_write_protected_keys(client, webhook_factory):
    from app import context
    with context.use_area(1):
        wh = webhook_factory(name="keep")
    r = await client.post("/api/settings", json={
        "webhooks": [{"id": "wh_evil", "token": "stolen", "enabled": True}],
        "webhook_secret": "x", "discord_user_token": "tok", "discord_enabled": True,
        "token_accounts": [{"name": "a"}], "default_qty": 7})
    assert r.status_code == 200
    with context.use_area(1):
        s = config.load_settings()
    assert [w["id"] for w in s["webhooks"]] == [wh["id"]]
    assert s["webhook_secret"] == "change-me" and s["discord_user_token"] == ""
    assert s["discord_enabled"] is False and s["token_accounts"] == []
    assert s["default_qty"] == 7


async def test_settings_rejects_non_object(client):
    r = await client.post("/api/settings", json=[1, 2])
    assert r.status_code == 400


async def test_invite_bound_to_email_enforced(anon_client, admin):
    code = db.create_invite(admin["id"], email="invited@example.com")
    r = await anon_client.post("/register", data={"code": code, "email": "other@example.com",
                                                   "password": "password123", "password2": "password123"})
    assert r.headers["location"].endswith("error=email")
    r = await anon_client.post("/register", data={"code": code, "email": "invited@example.com",
                                                   "password": "password123", "password2": "password123"})
    assert r.headers["location"] == "/2fa/setup"


# ------------------------------------------------------------ admin gates
async def test_update_apply_is_admin_only(admin):
    user = db.create_user("user@example.com", "password123", is_admin=False)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        login_as(c, auth.make_session(user["id"]))
        r = await c.post("/api/update/apply")
        assert r.status_code == 403


async def test_discord_routes_require_feature(admin):
    # The bootstrap admin's area has every feature on; an invited user's does not.
    user = db.create_user("user@example.com", "password123", is_admin=False)
    area = db.user_primary_area(user["id"])
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        login_as(c, auth.make_session(user["id"]))
        r = await c.post("/api/discord/config", json={"discord_enabled": True})
        assert r.status_code == 403
        r = await c.post("/api/discord/test", json={"channel_id": "1", "embed": {}})
        assert r.status_code == 403
        assert (await c.get("/api/discord/status")).status_code == 200  # reads stay available
        db.set_area_feature(area, "discord_signals", True)
        r = await c.post("/api/discord/config", json={"discord_enabled": True})
        assert r.status_code == 200


# ------------------------------------------------------------- SSRF guard
def _fake_resolver(mapping):
    def getaddrinfo(host, port, *a, **kw):
        ip = mapping.get(host)
        if ip is None:
            raise socket.gaierror("no such host")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))]
    return getaddrinfo


@pytest.mark.parametrize("url, ok", [
    ("https://discord.com/api/webhooks/1/abc", True),
    ("http://public.example/hook", True),
    ("ftp://discord.com/x", False),
    ("https://user:pw@discord.com/x", False),
    ("http://localhost:9000/api/flatten-all", False),
    ("http://127.0.0.1:9000/webhook/x", False),
    ("http://169.254.169.254/latest/meta-data", False),
    ("http://10.0.0.5/", False),
    ("http://[::1]/", False),
    ("http://intranet.internal/", False),
    ("http://nope.invalid/", False),
    ("not a url", False),
])
def test_check_outbound_url(monkeypatch, url, ok):
    monkeypatch.setattr(socket, "getaddrinfo", _fake_resolver({
        "discord.com": "162.159.128.233", "public.example": "93.184.216.34",
        "10.0.0.5": "10.0.0.5", "127.0.0.1": "127.0.0.1", "169.254.169.254": "169.254.169.254",
        "::1": "::1", "intranet.internal": "10.1.1.1"}))
    assert (real_check_outbound_url(url) is None) is ok


async def test_settings_rejects_internal_discord_alert_url(client, monkeypatch):
    monkeypatch.setattr(security, "check_outbound_url", lambda url: "URL points at an internal address")
    r = await client.post("/api/settings", json={"alert_discord_webhook_url": "http://127.0.0.1/x"})
    assert r.status_code == 400 and "internal" in r.json()["detail"]


async def test_discord_target_url_is_validated(client, monkeypatch):
    db.set_area_feature(1, "discord_signals", True)
    monkeypatch.setattr(security, "check_outbound_url",
                        lambda url: "URL points at an internal address" if "127.0.0.1" in url else None)
    r = await client.post("/api/discord/config", json={"discord_channels": [
        {"id": "1", "targets": [{"label": "bad", "url": "http://127.0.0.1:9000/x"}]}]})
    assert r.status_code == 400 and "bad" in r.json()["detail"]
    r = await client.post("/api/discord/config", json={"discord_channels": [
        {"id": "1", "targets": [{"label": "ok", "url": "https://hooks.example/x"}]}]})
    assert r.status_code == 200


# -------------------------------------------------------------- public URL
async def test_public_url_pins_links_and_status(client, admin, monkeypatch):
    monkeypatch.setattr(config, "PUBLIC_URL", "https://bridge.example.com")
    r = await client.post("/api/users/invite", json={"email": ""},
                          headers={"host": "evil.example", "x-forwarded-host": "evil.example"})
    assert r.json()["url"].startswith("https://bridge.example.com/register?code=")
    assert (await client.get("/api/status")).json()["public_url"] == "https://bridge.example.com"


# ------------------------------------------------------------------- PWA
async def test_pwa_manifest_and_icons(anon_client, admin):
    r = await anon_client.get("/static/manifest.webmanifest")
    assert r.status_code == 200
    m = r.json()
    assert m["display"] == "standalone" and m["start_url"].startswith("/")
    for icon in m["icons"]:
        assert (await anon_client.get(icon["src"])).status_code == 200


async def test_js_is_never_cached_but_css_may_revalidate(anon_client, admin):
    # ES modules import siblings without a version query; a cached bundle would
    # strand an iOS PWA on old code after a deploy.
    js = await anon_client.get("/static/js/push.js")
    assert js.status_code == 200 and js.headers["cache-control"] == "no-store"
    css = await anon_client.get("/static/css/base.css")
    assert css.status_code == 200 and css.headers["cache-control"] == "no-cache"
    page = await anon_client.get("/login")
    assert 'rel="manifest"' in page.text and "apple-touch-icon" in page.text


# ------------------------------------------------------------ login audit
async def test_logins_are_audited(anon_client, admin):
    hdr = {"x-forwarded-for": "203.0.113.7"}
    await anon_client.post("/login", data={"email": "admin@example.com", "password": "wrong"}, headers=hdr)
    r = await anon_client.post("/login", data={"email": "admin@example.com", "password": "password123"}, headers=hdr)
    assert r.headers["location"] == "/"
    rows = db.list_audit(10, logins=True)
    assert [(x["action"], x["target"]) for x in rows] == [("login_ok", "203.0.113.7"), ("login_failed", "203.0.113.7")]
    assert rows[1]["actor_email"] == "admin@example.com"
    assert db.list_audit(10, logins=False) == []  # admin-actions view stays clean
    u = db.get_user(admin["id"])
    assert u["last_login_at"] and u["last_login_ip"] == "203.0.113.7"
    for _ in range(10):
        await anon_client.post("/login", data={"email": "x@y.z", "password": "x"}, headers=hdr)
    assert db.list_audit(1, logins=True)[0]["action"] == "login_blocked"


async def test_audit_api_kinds(client, admin):
    db.log_action(admin["id"], admin["email"], "login_ok", "1.2.3.4")
    db.log_action(admin["id"], admin["email"], "invite_create", "x")
    assert [r["action"] for r in (await client.get("/api/audit")).json()] == ["invite_create"]
    assert [r["action"] for r in (await client.get("/api/audit?kind=logins")).json()] == ["login_ok"]
    assert len((await client.get("/api/audit?kind=all")).json()) == 2
    users = (await client.get("/api/users")).json()["users"]
    assert "last_login_at" in users[0]


# ---------------------------------------------- proxy-aware client IP + login brakes
async def test_client_ip_uses_the_proxy_appended_hop(anon_client, admin):
    """A caller cannot pick its own rate-limit bucket by sending X-Forwarded-For:
    the trusted proxy appends the real address as the *last* hop."""
    spoof = {"x-forwarded-for": "203.0.113.99, 198.51.100.1"}   # "203.0.113.99" was written by the client
    for _ in range(10):
        await anon_client.post("/login", data={"email": "a@b.c", "password": "x"}, headers=spoof)
    r = await anon_client.post("/login", data={"email": "a@b.c", "password": "x"},
                               headers={"x-forwarded-for": "203.0.113.1, 198.51.100.1"})
    assert r.headers["location"] == "/login?error=rate"    # same real address → same bucket
    rows = db.list_audit(5, logins=True)
    assert rows[0]["target"] == "198.51.100.1"


async def test_failed_logins_are_capped_per_account_regardless_of_ip(anon_client, admin):
    from app import security
    for i in range(20):
        r = await anon_client.post("/login", data={"email": "admin@example.com", "password": "wrong"},
                                   headers={"x-forwarded-for": f"198.51.100.{i + 1}"})
        assert r.headers["location"] == "/login?error=bad"
    # 21st failure from yet another address: blocked even with the right password
    r = await anon_client.post("/login", data={"email": "Admin@Example.com", "password": "password123"},
                               headers={"x-forwarded-for": "198.51.100.200"})
    assert r.headers["location"] == "/login?error=rate" and r.headers["retry-after"] == "600"
    assert db.list_audit(1, logins=True)[0]["action"] == "login_blocked"
    # other accounts are unaffected; successes never count against the account
    security.reset_limits()
    for _ in range(5):
        r = await anon_client.post("/login", data={"email": "admin@example.com", "password": "password123"},
                                   headers={"x-forwarded-for": "198.51.100.201"})
        assert r.headers["location"] == "/"
    assert security.login_allowed("admin@example.com")


def test_global_login_brake(admin):
    from app import security
    for _ in range(300):
        security.login_failed(f"user{secrets.token_hex(2)}@example.com")
    assert not security.login_allowed("someone-else@example.com")


def test_proxy_hops_zero_ignores_forwarded_for(monkeypatch):
    from app import security
    monkeypatch.setattr(security, "PROXY_HOPS", 0)
    scope = {"type": "http", "headers": [(b"x-forwarded-for", b"203.0.113.5")], "client": ("10.0.0.9", 1234)}
    from starlette.requests import Request as _R
    assert security.client_ip(_R(scope)) == "10.0.0.9"
    monkeypatch.setattr(security, "PROXY_HOPS", 2)
    scope["headers"] = [(b"x-forwarded-for", b"1.1.1.1, 2.2.2.2, 3.3.3.3")]
    assert security.client_ip(_R(scope)) == "2.2.2.2"
    scope["headers"] = [(b"x-forwarded-for", b"2.2.2.2")]      # fewer hops than trusted proxies
    assert security.client_ip(_R(scope)) == "2.2.2.2"


# ------------------------------------------------ passphrase never leaves the ingress
async def test_webhook_passphrase_is_redacted_from_logs_and_history(client, webhook_factory, admin):
    from app import config, history, state
    config.save_settings({"webhook_passphrase": "s3cret"})
    wh = webhook_factory(name="P")
    entry = state.log_signal({"action": "buy", "symbol": "MNQ", "passphrase": "s3cret", "meta": {"api_secret": "k", "note": "x"}},
                             result="received", webhook="P")
    assert entry["payload"]["passphrase"] == "********" and entry["payload"]["meta"]["api_secret"] == "********"
    assert entry["payload"]["meta"]["note"] == "x" and entry["payload"]["action"] == "buy"
    state.log_event("error", "Signal error", payload={"passphrase": "s3cret", "action": "buy"})
    assert state.recent_events()[0]["payload"]["passphrase"] == "********"
    r = await client.get("/api/signals")
    assert "s3cret" not in r.text
    r = await client.get("/api/history/signals?q=s3cret")
    assert "s3cret" not in r.text
    assert wh  # the fixture created it


async def test_forwarded_marketplace_signals_drop_the_passphrase(monkeypatch, admin, webhook_factory):
    from app import marketplace, signals, state
    wh = webhook_factory(name="Pub")
    seen: list[dict] = []

    async def fake_process(payload, webhook, *, trusted=False):
        seen.append(payload)
        return {"status": "ok"}
    monkeypatch.setattr(signals, "process", fake_process)
    monkeypatch.setattr(marketplace, "sharing_of", lambda w: {"enabled": True})
    monkeypatch.setattr(marketplace, "subscription_view", lambda w, s, a: {**w, "name": "Sub view"})
    monkeypatch.setattr(db, "active_subscriptions", lambda aid, wid: [{"area_id": context.get_area()}])
    n = signals.forward_to_subscribers({"action": "buy", "symbol": "MNQ", "passphrase": "s3cret"}, wh)
    await settle()
    assert n == 1 and seen and "passphrase" not in seen[0] and seen[0]["action"] == "buy"


# --------------------------------------------------------- SMTP target validation
async def test_smtp_host_is_validated_like_any_outbound_target(client, monkeypatch):
    from app import security
    monkeypatch.setattr(security, "check_outbound_url", lambda url: "URL points at an internal address" if "10.0.0" in url else None)
    r = await client.post("/api/settings", json={"alert_smtp_host": "10.0.0.5", "alert_smtp_port": 6379})
    assert r.status_code == 400 and "SMTP host" in r.json()["detail"]
    r = await client.post("/api/settings", json={"alert_smtp_host": "smtp.gmail.com", "alert_smtp_port": "abc"})
    assert r.status_code == 400 and "port" in r.json()["detail"]
    r = await client.post("/api/settings", json={"alert_smtp_host": "smtp.gmail.com", "alert_smtp_port": 587})
    assert r.status_code == 200 and r.json()["alert_smtp_host"] == "smtp.gmail.com" and r.json()["alert_smtp_port"] == 587

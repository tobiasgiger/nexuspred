"""Test harness.

* Points ``NEXUSPRED_DATA_DIR`` at a throw-away directory **before** ``app`` is
  imported, so the SQLite DB and every path derived at import time is isolated.
* ``fresh_env`` (autouse) wipes the DB file and every in-memory registry between
  tests, so each test starts from an empty bridge.
* ``admin`` creates the first user (area 1) — needed by anything that persists
  settings, since ``config.save_settings`` updates the area row.
* ``client`` / ``anon_client`` give an httpx ASGI client with / without a valid
  session cookie.
"""
from __future__ import annotations

import copy
import os
import tempfile
from pathlib import Path

_DATA = Path(tempfile.mkdtemp(prefix="fluxbridge-tests-"))
os.environ["NEXUSPRED_DATA_DIR"] = str(_DATA)
os.environ.setdefault("SESSION_SECRET", "test-session-secret")
os.environ.setdefault("PORT", "9000")

import httpx  # noqa: E402
import pytest  # noqa: E402

from app import auth, config, context, crypto, db, history, http, push, relay, security, signals, state, tradovate, watch  # noqa: E402
from app import copy as copy_mod  # noqa: E402
from app import risk  # noqa: E402
from app import pnl as pnl_mod  # noqa: E402
from app import drawdown as drawdown_mod  # noqa: E402
from app.discord_signals import hub, listener  # noqa: E402
from app.main import app  # noqa: E402


_counter = {"n": 0}
# Pristine copy of the defaults. v4's ``load_settings`` only shallow-copies, so
# routers that ``.append()`` to a fresh area's ``webhooks`` list mutate
# ``DEFAULT_SETTINGS`` itself (see test_routing::test_webhook_create_leaks_into_defaults).
# Restoring it between tests keeps that v4 quirk from cross-contaminating the suite.
_PRISTINE_DEFAULTS = copy.deepcopy(config.DEFAULT_SETTINGS)


def _reset_runtime() -> None:
    """Point the DB at a brand-new file and clear every process-wide registry so
    tests are independent. (A fresh file per test sidesteps Windows' refusal to
    delete a SQLite file while a not-yet-collected connection still holds it.)"""
    _counter["n"] += 1
    db._initialized = False
    db.DB_FILE = _DATA / f"test-{_counter['n']}.db"
    db.reset_caches()
    auth._KEY = None
    crypto.reset()
    config._version = None
    config.DEFAULT_SETTINGS.clear()
    config.DEFAULT_SETTINGS.update(copy.deepcopy(_PRISTINE_DEFAULTS))
    config._cache.clear()
    config._webhook_index = None
    http.reset()
    state._areas.clear()
    signals._active.clear()
    signals._sim_active.clear()
    signals._trade_locks.clear()
    tradovate._managers.clear()
    hub._areas.clear()
    listener._managers.clear()
    signals.sim_client.reset()
    security.reset_limits()
    history.stop()
    relay.reset()
    push.reset()
    watch.reset()
    copy_mod.reset()
    risk.reset()
    pnl_mod.reset()
    drawdown_mod.reset()


@pytest.fixture(autouse=True)
def fresh_env(monkeypatch):
    _reset_runtime()
    # Never let a test spin up a real Discord supervisor task.
    monkeypatch.setattr(listener.ListenerManager, "start", lambda self: None)
    # The SSRF guard resolves DNS; tests run offline (test_security covers it).
    monkeypatch.setattr(security, "check_outbound_url", lambda url: None)
    yield
    _reset_runtime()


@pytest.fixture
def admin():
    """First user → admin, owns area 1 (DEFAULT_AREA_ID)."""
    user = db.create_user("admin@example.com", "password123", is_admin=True)
    config.invalidate()
    return user


@pytest.fixture
def area(admin):
    """Run the test body in the admin's area context."""
    with context.use_area(db.user_primary_area(admin["id"])):
        yield db.user_primary_area(admin["id"])


def login_as(client: httpx.AsyncClient, cookie: str) -> None:
    """Attach a session cookie as a raw header (httpx's cookie jar treats the
    dotless host 'testserver' as 'testserver.local' and would drop it)."""
    client.headers["cookie"] = f"{auth.COOKIE}={cookie}"


def _make_client(cookie: str | None = None) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app)
    c = httpx.AsyncClient(transport=transport, base_url="http://testserver")
    if cookie:
        login_as(c, cookie)
    return c


@pytest.fixture
async def anon_client():
    async with _make_client() as c:
        yield c


@pytest.fixture
async def client(admin):
    async with _make_client(auth.make_session(admin["id"])) as c:
        yield c


@pytest.fixture
def webhook_factory():
    """Create webhooks in the current area's settings and return them."""
    def make(name="Test", strategy="simple", default_qty=1, tp_qty=1, accounts=None, enabled=True):
        wh = config.new_webhook(name=name, strategy=strategy, default_qty=default_qty, tp_qty=tp_qty)
        wh["enabled"] = enabled
        wh["accounts"] = accounts or []
        s = config.load_settings()
        config.save_settings({"webhooks": [*s.get("webhooks", []), wh]})
        return wh
    return make

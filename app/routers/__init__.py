"""HTTP surface, one module per concern. ``ROUTERS`` is what ``app.main`` mounts."""
from __future__ import annotations

from . import accounts, agent, auth, copy, core, extension, journal, marketplace, mfa, news, payments, push, settings_io, simulator, trading, updater, users, webhooks

ROUTERS = [
    auth.router,
    users.router,
    mfa.router,
    core.router,
    accounts.router,
    webhooks.router,
    marketplace.router,
    copy.router,
    journal.router,
    news.router,
    agent.router,
    push.router,
    simulator.router,
    updater.router,
    settings_io.router,
    extension.router,
    trading.router,
    payments.router,
]

__all__ = ["ROUTERS", "accounts", "agent", "auth", "copy", "core", "extension", "journal", "marketplace", "mfa", "news", "payments", "push", "settings_io", "simulator", "trading", "updater", "users", "webhooks"]

"""HTTP surface, one module per concern. ``ROUTERS`` is what ``app.main`` mounts."""
from __future__ import annotations

from . import accounts, agent, auth, copy, core, extension, journal, marketplace, news, push, simulator, updater, users, webhooks

ROUTERS = [
    auth.router,
    users.router,
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
    extension.router,
]

__all__ = ["ROUTERS", "accounts", "agent", "auth", "copy", "core", "extension", "journal", "marketplace", "news", "push", "simulator", "updater", "users", "webhooks"]

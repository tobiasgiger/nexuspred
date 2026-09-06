"""HTTP surface, one module per concern. ``ROUTERS`` is what ``app.main`` mounts."""
from __future__ import annotations

from . import accounts, agent, auth, core, extension, journal, marketplace, push, simulator, updater, users, webhooks

ROUTERS = [
    auth.router,
    users.router,
    core.router,
    accounts.router,
    webhooks.router,
    marketplace.router,
    journal.router,
    agent.router,
    push.router,
    simulator.router,
    updater.router,
    extension.router,
]

__all__ = ["ROUTERS", "accounts", "agent", "auth", "core", "extension", "journal", "marketplace", "push", "simulator", "updater", "users", "webhooks"]

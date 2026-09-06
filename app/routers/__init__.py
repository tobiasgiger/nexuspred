"""HTTP surface, one module per concern. ``ROUTERS`` is what ``app.main`` mounts."""
from __future__ import annotations

from . import accounts, auth, core, extension, simulator, updater, users, webhooks

ROUTERS = [
    auth.router,
    users.router,
    core.router,
    accounts.router,
    webhooks.router,
    simulator.router,
    updater.router,
    extension.router,
]

__all__ = ["ROUTERS", "accounts", "auth", "core", "extension", "simulator", "updater", "users", "webhooks"]

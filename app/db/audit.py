"""Audit log."""
from __future__ import annotations
from typing import Any, Optional
from .core import _connect, _now, init


def log_action(actor_user_id: Optional[int], actor_email: str, action: str,
               target: str = "", detail: str = "") -> None:
    """Record an admin action. Never raises — auditing must not break the action."""
    try:
        init()
        with _connect() as c:
            c.execute(
                "INSERT INTO audit_log(created_at,actor_user_id,actor_email,action,target,detail) "
                "VALUES(?,?,?,?,?,?)",
                (_now(), actor_user_id, actor_email, action, target, detail),
            )
    except Exception:  # noqa: BLE001
        pass


LOGIN_ACTIONS = ("login_ok", "login_failed", "login_blocked")


def list_audit(limit: int = 100, *, logins: Optional[bool] = None) -> list[dict[str, Any]]:
    """Newest audit rows. ``logins=True`` → only sign-in events, ``False`` →
    everything but sign-ins (the admin-actions view), ``None`` → all."""
    init()
    marks = ",".join("?" * len(LOGIN_ACTIONS))
    where = ""
    params: list[Any] = []
    if logins is True:
        where, params = f"WHERE action IN ({marks})", list(LOGIN_ACTIONS)
    elif logins is False:
        where, params = f"WHERE action NOT IN ({marks})", list(LOGIN_ACTIONS)
    with _connect() as c:
        rows = c.execute(
            f"SELECT * FROM audit_log {where} ORDER BY id DESC LIMIT ?", (*params, int(limit))).fetchall()
        return [{"id": r["id"], "created_at": r["created_at"], "actor_email": r["actor_email"],
                 "action": r["action"], "target": r["target"], "detail": r["detail"]}
                for r in rows]

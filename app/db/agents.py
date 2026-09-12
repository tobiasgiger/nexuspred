"""Execution agents and pairing codes."""
from __future__ import annotations
import hashlib
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from .core import _agent_touch_at, _agents_by_hash, _connect, _now, init


AGENT_PAIRING_TTL_S = 15 * 60


def _agent_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _row_to_agent(r: sqlite3.Row) -> dict[str, Any]:
    return {"id": r["id"], "area_id": r["area_id"], "name": r["name"], "version": r["version"],
            "created_at": r["created_at"], "last_seen_at": r["last_seen_at"], "last_ip": r["last_ip"]}


def create_agent_pairing(area_id: int, name: str = "") -> str:
    """A one-time, short-lived pairing code (8 chars, unambiguous alphabet)."""
    init()
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    code = "".join(secrets.choice(alphabet) for _ in range(8))
    now = datetime.now(timezone.utc)
    with _connect() as c:
        c.execute("DELETE FROM agent_pairings WHERE expires_at<? OR used_at IS NOT NULL", (now.isoformat(),))
        c.execute("INSERT INTO agent_pairings(code,area_id,name,created_at,expires_at) VALUES(?,?,?,?,?)",
                  (code, area_id, name, now.isoformat(), (now + timedelta(seconds=AGENT_PAIRING_TTL_S)).isoformat()))
    return code


def consume_agent_pairing(code: str) -> Optional[dict[str, Any]]:
    init()
    now = datetime.now(timezone.utc).isoformat()
    with _connect() as c:
        r = c.execute("SELECT * FROM agent_pairings WHERE code=? AND used_at IS NULL AND expires_at>?",
                      (code, now)).fetchone()
        if not r:
            return None
        # single use, atomically: a second caller with the same code loses the race
        cur = c.execute("UPDATE agent_pairings SET used_at=? WHERE code=? AND used_at IS NULL AND expires_at>?",
                        (now, code, now))
        if cur.rowcount != 1:
            return None
    return {"area_id": r["area_id"], "name": r["name"]}


def create_agent(area_id: int, name: str, *, version: str = "", ip: str = "") -> tuple[str, dict[str, Any]]:
    """Create an agent; returns (plain token — shown once, agent record)."""
    init()
    token = "fba_" + secrets.token_urlsafe(32)
    with _connect() as c:
        cur = c.execute(
            "INSERT INTO agents(area_id,name,token_hash,version,created_at,last_seen_at,last_ip) VALUES(?,?,?,?,?,?,?)",
            (area_id, name, _agent_hash(token), version, _now(), _now(), ip))
        r = c.execute("SELECT * FROM agents WHERE id=?", (cur.lastrowid,)).fetchone()
    _agents_by_hash.clear()
    return token, _row_to_agent(r)


def get_agent_by_token(token: str) -> Optional[dict[str, Any]]:
    h = _agent_hash(token)
    cached = _agents_by_hash.get(h)
    if cached is not None:
        return dict(cached)
    init()
    with _connect() as c:
        r = c.execute("SELECT * FROM agents WHERE token_hash=?", (h,)).fetchone()
    if not r:
        return None  # misses are never cached: unauthenticated guesses must not grow memory
    agent = _row_to_agent(r)
    if len(_agents_by_hash) > 256:
        _agents_by_hash.clear()
    _agents_by_hash[h] = agent
    return dict(agent)


def get_agent(area_id: int, agent_id: int) -> Optional[dict[str, Any]]:
    init()
    with _connect() as c:
        r = c.execute("SELECT * FROM agents WHERE area_id=? AND id=?", (area_id, agent_id)).fetchone()
    return _row_to_agent(r) if r else None


def list_agents(area_id: int) -> list[dict[str, Any]]:
    init()
    with _connect() as c:
        return [_row_to_agent(r) for r in c.execute(
            "SELECT * FROM agents WHERE area_id=? ORDER BY id", (area_id,)).fetchall()]


def touch_agent(agent_id: int, *, ip: str = "", version: str = "") -> None:
    """Record a poll (throttled to one write per 20 s per agent)."""
    import time as _time
    now = _time.monotonic()
    if now - _agent_touch_at.get(agent_id, -1e9) < 20:
        return
    _agent_touch_at[agent_id] = now
    init()
    with _connect() as c:
        if version:
            c.execute("UPDATE agents SET last_seen_at=?, last_ip=?, version=? WHERE id=?", (_now(), ip[:64], version[:40], agent_id))
        else:
            c.execute("UPDATE agents SET last_seen_at=?, last_ip=? WHERE id=?", (_now(), ip[:64], agent_id))


def rename_agent(area_id: int, agent_id: int, name: str) -> bool:
    init()
    with _connect() as c:
        cur = c.execute("UPDATE agents SET name=? WHERE area_id=? AND id=?", (name, area_id, agent_id))
    _agents_by_hash.clear()
    return bool(cur.rowcount)


def delete_agent(area_id: int, agent_id: int) -> bool:
    init()
    with _connect() as c:
        cur = c.execute("DELETE FROM agents WHERE area_id=? AND id=?", (area_id, agent_id))
    _agents_by_hash.clear()
    return bool(cur.rowcount)

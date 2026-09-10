"""Execution agents: route a login's Tradovate calls through a paired helper
so each account trades from its own IP address.

An **agent** is a small script (``agent/fluxbridge_agent.py``) running on a
VPS. It never receives dashboard credentials: the admin creates a one-time
**pairing code** in the bridge, types it into the agent once, and the agent
gets a bearer token that is only good for the relay endpoints under
``/api/agent/``. The token is stored hashed; the bridge login itself never
leaves the bridge.

Transport is plain outbound HTTPS from the agent (no open port on the VPS):

* ``GET /api/agent/jobs?wait=25`` — long-poll; the bridge hands out queued
  HTTP requests (method, url, headers, body, timeout) for that agent.
* ``POST /api/agent/jobs/{id}/result`` — the agent posts status + body back.

:func:`request` is what :class:`app.tradovate.TradovateSession` calls instead
of the pooled HTTP client when the login is assigned to an agent. If no agent
picks the job up within ``DISPATCH_TIMEOUT_S`` the call fails with a clear
"agent offline" error — the bridge never silently falls back to its own IP.
"""
from __future__ import annotations

import asyncio
import secrets
import time
from typing import Any, Optional

from . import db

# Hosts an agent may be asked to call. The agent enforces the same list on its
# side; here it stops a bug (or a compromised bridge process) from turning the
# VPS into a general-purpose proxy.
ALLOWED_HOST_SUFFIXES = (".tradovateapi.com", ".tradovate.com")

DISPATCH_TIMEOUT_S = 12.0     # an agent must have polled within this to be "online"
ONLINE_WINDOW_S = 45.0        # last poll newer than this → online (long-poll is 25 s)
RESULT_TIMEOUT_EXTRA_S = 10.0  # on top of the job's own HTTP timeout


class AgentOffline(Exception):
    pass


class ResultUnknown(AgentOffline):
    """The agent picked the job up but its answer never arrived: the request may
    or may not have been executed at Tradovate."""


def allowed_url(url: str) -> bool:
    """HTTPS to a Tradovate host only."""
    from urllib.parse import urlsplit
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    return parts.scheme == "https" and bool(host) and host.endswith(ALLOWED_HOST_SUFFIXES)


class _Job:
    __slots__ = ("id", "agent_id", "payload", "future", "created", "claimed")

    def __init__(self, agent_id: int, payload: dict[str, Any], loop: asyncio.AbstractEventLoop) -> None:
        self.id = secrets.token_urlsafe(12)
        self.agent_id = agent_id
        self.payload = payload
        self.future: asyncio.Future = loop.create_future()
        self.created = time.monotonic()
        self.claimed = False


_queues: dict[int, asyncio.Queue] = {}       # agent id → jobs waiting to be claimed
_inflight: dict[str, _Job] = {}              # job id → job (claimed or waiting)
_last_seen: dict[int, float] = {}            # agent id → monotonic time of last poll
_lock = asyncio.Lock()


def _queue(agent_id: int) -> asyncio.Queue:
    q = _queues.get(agent_id)
    if q is None:
        q = _queues[agent_id] = asyncio.Queue()
    return q


def touch(agent_id: int) -> None:
    _last_seen[agent_id] = time.monotonic()


def is_online(agent_id: int) -> bool:
    return time.monotonic() - _last_seen.get(agent_id, -1e9) < ONLINE_WINDOW_S


def online_ids() -> set[int]:
    now = time.monotonic()
    return {aid for aid, t in _last_seen.items() if now - t < ONLINE_WINDOW_S}


def reset() -> None:
    _queues.clear()
    _inflight.clear()
    _last_seen.clear()


# ------------------------------------------------------------- bridge side
async def request(agent_id: int, *, method: str, url: str, headers: dict[str, str],
                  json_body: Any = None, params: Optional[dict[str, Any]] = None,
                  timeout: float = 20.0, area_id: Optional[int] = None) -> tuple[int, str]:
    """Run one HTTP request through an agent. Returns ``(status_code, text)``.
    With ``area_id`` the agent must be paired with that workspace."""
    if not allowed_url(url):
        raise ValueError(f"refusing to relay a request to {url!r}: not a Tradovate HTTPS endpoint")
    if area_id is not None and not db.get_agent(area_id, agent_id):
        raise AgentOffline(f"execution agent #{agent_id} is not paired with this workspace")
    if not is_online(agent_id):
        raise AgentOffline(f"execution agent #{agent_id} is offline (no poll in the last {int(ONLINE_WINDOW_S)} s)")
    loop = asyncio.get_running_loop()
    job = _Job(agent_id, {"method": method, "url": url, "headers": headers,
                          "json": json_body, "params": params or None, "timeout": timeout}, loop)
    _inflight[job.id] = job
    await _queue(agent_id).put(job)
    try:
        # phase 1: the agent must pick the job up quickly — an agent that is not
        # polling is "offline" now, not after the whole HTTP timeout
        try:
            return await asyncio.wait_for(asyncio.shield(job.future), timeout=DISPATCH_TIMEOUT_S)
        except asyncio.TimeoutError as exc:
            if not job.claimed:
                raise AgentOffline(f"execution agent #{agent_id} did not pick the request up within {int(DISPATCH_TIMEOUT_S)} s") from exc
        # phase 2: claimed — give it the request's own timeout from the moment it was claimed
        try:
            return await asyncio.wait_for(asyncio.shield(job.future), timeout=timeout + RESULT_TIMEOUT_EXTRA_S)
        except asyncio.TimeoutError as exc:
            raise ResultUnknown(f"execution agent #{agent_id} picked the request up but did not answer within "
                                f"{int(timeout + RESULT_TIMEOUT_EXTRA_S)} s — its outcome is unknown") from exc
    finally:
        _inflight.pop(job.id, None)
        if not job.future.done():
            job.future.cancel()          # a job the caller gave up on must never be executed later (a stale order)


# -------------------------------------------------------------- agent side
async def next_jobs(agent_id: int, wait: float, max_jobs: int = 8) -> list[dict[str, Any]]:
    """Long-poll: block up to ``wait`` seconds for jobs; return their payloads."""
    touch(agent_id)
    q = _queue(agent_id)
    deadline = time.monotonic() + max(0.0, wait)
    out: list[dict[str, Any]] = []
    while not out:
        remaining = deadline - time.monotonic()
        jobs: list[_Job] = []
        try:
            jobs.append(await asyncio.wait_for(q.get(), timeout=max(0.0, remaining)))
        except asyncio.TimeoutError:
            break
        while len(jobs) < max_jobs:
            try:
                jobs.append(q.get_nowait())
            except asyncio.QueueEmpty:
                break
        for j in jobs:
            if j.future.done():  # the caller gave up while the job waited: never run it late
                continue
            j.claimed = True
            out.append({"id": j.id, **j.payload})
        if remaining <= 0:
            break
    touch(agent_id)
    return out


def deliver(agent_id: int, job_id: str, status_code: int, text: str, error: str = "") -> bool:
    """Complete a job with the agent's answer. False when the job is unknown
    (timed out / belongs to another agent)."""
    touch(agent_id)
    job = _inflight.get(job_id)
    if job is None or job.agent_id != agent_id or job.future.done():
        return False
    if error:
        job.future.set_exception(AgentOffline(f"agent request failed: {error}"))
    else:
        job.future.set_result((int(status_code), text))
    return True


def pending(agent_id: int) -> int:
    return _queue(agent_id).qsize()


# ------------------------------------------------------------- auth helper
def authenticate(token: str) -> Optional[dict[str, Any]]:
    """The agent record for a bearer token, or None."""
    if not token or len(token) < 20:
        return None
    return db.get_agent_by_token(token)

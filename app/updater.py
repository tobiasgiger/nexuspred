"""GitHub-backed auto-updater.

Checks the configured GitHub repo for a newer version and, when requested from the
dashboard, pulls the latest code and restarts the process.

Version resolution order for the "latest available" version:
  1. Latest GitHub release tag (e.g. ``v1.2.0``), if any releases exist.
  2. The ``VERSION`` file on the default branch.

The local version is read from the ``VERSION`` file in the working tree.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from typing import Any

import httpx
from packaging.version import InvalidVersion, Version

from . import config, state

API = "https://api.github.com"
RAW = "https://raw.githubusercontent.com"


def _norm(v: str) -> str:
    return v.strip().lstrip("vV")


async def _latest_release_tag() -> str | None:
    url = f"{API}/repos/{config.GITHUB_OWNER}/{config.GITHUB_REPO}/releases/latest"
    try:
        async with httpx.AsyncClient(timeout=15.0) as c:
            resp = await c.get(url, headers={"Accept": "application/vnd.github+json"})
        if resp.status_code == 200:
            return resp.json().get("tag_name")
    except httpx.HTTPError:
        pass
    return None


async def _version_file_on_branch() -> str | None:
    url = (
        f"{RAW}/{config.GITHUB_OWNER}/{config.GITHUB_REPO}/"
        f"{config.GITHUB_BRANCH}/VERSION"
    )
    try:
        async with httpx.AsyncClient(timeout=15.0) as c:
            resp = await c.get(url)
        if resp.status_code == 200:
            return resp.text.strip()
    except httpx.HTTPError:
        pass
    return None


async def check_for_update() -> dict[str, Any]:
    """Compare the local version with the latest available on GitHub."""
    local = config.get_version()
    remote = await _latest_release_tag() or await _version_file_on_branch()

    result: dict[str, Any] = {
        "current_version": local,
        "latest_version": _norm(remote) if remote else None,
        "update_available": False,
        "repo": f"{config.GITHUB_OWNER}/{config.GITHUB_REPO}",
        "branch": config.GITHUB_BRANCH,
        "error": None,
    }

    if not remote:
        result["error"] = "Could not reach GitHub to check for updates"
        return result

    try:
        result["update_available"] = Version(_norm(remote)) > Version(_norm(local))
    except InvalidVersion:
        # Fall back to a plain string comparison if versions aren't semver.
        result["update_available"] = _norm(remote) != _norm(local)

    return result


def _run(cmd: list[str]) -> tuple[bool, str]:
    try:
        out = subprocess.run(
            cmd,
            cwd=str(config.ROOT_DIR),
            capture_output=True,
            text=True,
            timeout=120,
        )
        return out.returncode == 0, (out.stdout + out.stderr).strip()
    except (subprocess.SubprocessError, OSError) as exc:
        return False, str(exc)


async def apply_update() -> dict[str, Any]:
    """Pull the latest code from GitHub and schedule a restart."""
    if not (config.ROOT_DIR / ".git").exists():
        return {"success": False, "message": "Not a git checkout — cannot self-update"}

    state.log_event("info", "Applying update from GitHub…")

    ok, fetch_out = await asyncio.to_thread(_run, ["git", "fetch", "--all", "--prune"])
    if not ok:
        return {"success": False, "message": f"git fetch failed: {fetch_out}"}

    ok, pull_out = await asyncio.to_thread(
        _run, ["git", "reset", "--hard", f"origin/{config.GITHUB_BRANCH}"]
    )
    if not ok:
        return {"success": False, "message": f"git update failed: {pull_out}"}

    # Best-effort dependency refresh; ignore failures so a restart still happens.
    await asyncio.to_thread(
        _run, [sys.executable, "-m", "pip", "install", "-q", "-r", "requirements.txt"]
    )

    new_version = config.get_version()
    state.log_event("info", f"Updated to version {new_version}; restarting…")

    # Restart shortly after responding so the dashboard gets the response first.
    asyncio.get_event_loop().call_later(1.5, _restart)
    return {
        "success": True,
        "message": f"Updated to {new_version}. Restarting…",
        "version": new_version,
        "log": pull_out,
    }


def _restart() -> None:
    """Re-exec the current process so it runs the freshly pulled code."""
    os.execv(sys.executable, [sys.executable, *sys.argv])

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

from . import config, http, state

API = "https://api.github.com"
RAW = "https://raw.githubusercontent.com"


def _norm(v: str) -> str:
    return v.strip().lstrip("vV")


async def _latest_release_tag() -> str | None:
    url = f"{API}/repos/{config.GITHUB_OWNER}/{config.GITHUB_REPO}/releases/latest"
    try:
        resp = await http.client("outbound").get(
            url, headers={"Accept": "application/vnd.github+json"}, timeout=15.0)
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
        resp = await http.client("outbound").get(url, timeout=15.0)
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
    # On managed hosts like Render, deploys are driven by git push, not by us.
    if os.environ.get("RENDER") or os.environ.get("NEXUSPRED_MANAGED_HOST"):
        return {
            "success": False,
            "message": (
                "Managed host (e.g. Render): updates deploy automatically when you "
                "push to GitHub, or click Manual Deploy in the host dashboard."
            ),
        }
    if not (config.ROOT_DIR / ".git").exists():
        return {
            "success": False,
            "message": (
                "Not a git checkout — run connect-git.bat (Windows) or ./connect-git.sh "
                "in the install folder once to enable one-click updates."
            ),
        }

    old_version = config.get_version()
    old_head_ok, old_head_out = await asyncio.to_thread(_run, ["git", "rev-parse", "HEAD"])
    old_head = old_head_out.splitlines()[0].strip() if old_head_ok and old_head_out.strip() else ""
    if not old_head:
        return {"success": False,
                "message": f"Could not determine the current git revision; update was not started: {old_head_out}"}
    state.log_event("info", f"Applying update from GitHub (current v{old_version})…")

    ok, fetch_out = await asyncio.to_thread(
        _run, ["git", "fetch", "--all", "--tags", "--prune"]
    )
    if not ok:
        return {"success": False, "message": f"git fetch failed: {fetch_out}"}

    # Hard-reset to the tracked branch. Settings live in data/ (git-ignored) so
    # they are never touched by the reset.
    ok, pull_out = await asyncio.to_thread(
        _run, ["git", "reset", "--hard", f"origin/{config.GITHUB_BRANCH}"]
    )
    if not ok:
        return {"success": False, "message": f"git update failed: {pull_out}"}

    dep_ok, dep_out = await asyncio.to_thread(
        _run, [sys.executable, "-m", "pip", "install", "-q", "-r", "requirements.txt"]
    )
    if not dep_ok:
        # New source must never be restarted with dependencies that failed to
        # install. Roll the working tree back and restore the previous dependency
        # set best-effort; leave the running process on the known-good code.
        rb_ok, rb_out = await asyncio.to_thread(_run, ["git", "reset", "--hard", old_head])
        rollback_detail = rb_out
        if rb_ok:
            await asyncio.to_thread(
                _run, [sys.executable, "-m", "pip", "install", "-q", "-r", "requirements.txt"]
            )
            config.get_version(force=True)
        else:
            rollback_detail = f"rollback failed: {rb_out}"
        state.log_event("error", f"Update dependency install failed; restart cancelled: {dep_out}; {rollback_detail}")
        return {
            "success": False,
            "message": "Dependency installation failed. The update was rolled back and no restart was scheduled."
                       if rb_ok else "Dependency installation failed and rollback failed. No restart was scheduled; restore the checkout manually.",
            "previous_version": old_version,
            "log": dep_out,
        }

    new_version = config.get_version(force=True)
    state.log_event("info", f"Updated v{old_version} → v{new_version}; restarting…")

    # Restart shortly after responding so the dashboard gets the response first.
    asyncio.get_event_loop().call_later(1.5, _restart)
    return {
        "success": True,
        "message": f"Updated v{old_version} → v{new_version}. Restarting…",
        "previous_version": old_version,
        "version": new_version,
        "log": pull_out,
    }


def _restart() -> None:
    """Restart so the freshly pulled code runs. Under systemd (deploy/install-server.sh,
    ``Restart=always``) a clean shutdown is enough — the unit brings the service back
    with the environment file re-read; elsewhere the process re-execs itself."""
    if os.environ.get("INVOCATION_ID") or os.environ.get("NEXUSPRED_SUPERVISED"):
        import signal
        os.kill(os.getpid(), signal.SIGTERM)      # graceful: loops stop, state is flushed
        return
    os.execv(sys.executable, [sys.executable, *sys.argv])

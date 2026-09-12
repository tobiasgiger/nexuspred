#!/usr/bin/env python3
"""Fluxbridge execution agent.

Runs on a VPS and executes Tradovate HTTP requests on behalf of the bridge, so
the logins assigned to this agent trade from *this* machine's IP address.

* Needs only Python 3.8+ - no packages to install.
* Talks to the bridge with outbound HTTPS only (no open port here).
* Never sees your dashboard login: you pair it once with a code created under
  Settings -> Execution Agents; the bridge hands it a token that is valid for
  the relay endpoints only.

First run (interactive):
    python fluxbridge_agent.py
        -> asks for the bridge URL and the pairing code, saves agent.json

Or non-interactive:
    python fluxbridge_agent.py --bridge https://bridge.example.com --code ABCD-2345 --name "VPS 1"

Linux / macOS, one line (Settings -> Execution Agents -> "Linux one-liner"):
    curl -fsSL https://raw.githubusercontent.com/tobiasgiger/nexuspred/main/deploy/install-agent.sh \
      | sudo bash -s -- --bridge https://bridge.example.com --code ABCD-2345

Preconfigured (Settings -> Execution Agents -> "Download preconfigured agent"):
    the zip already contains agent.json with the bridge URL and this agent's
    token - just start it, nothing to type.

Afterwards just:
    python fluxbridge_agent.py      (or double-click fluxbridge-agent.exe)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

VERSION = "1.3.0"

# The only hosts this agent will ever talk to on the bridge's behalf. Even a
# compromised bridge (or a stolen bridge session) cannot turn this VPS into a
# general-purpose proxy: anything else is refused before a socket is opened.
ALLOWED_HOST_SUFFIXES = (".tradovateapi.com", ".tradovate.com")

# Windows consoles often run cp1252; never let a non-ASCII character crash the agent.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass
# Next to the script - or next to the .exe when packaged with PyInstaller
# (there __file__ points into a temporary extraction folder).
BASE_DIR = os.path.dirname(os.path.abspath(sys.executable if getattr(sys, "frozen", False) else __file__))
CONFIG_FILE = os.path.join(BASE_DIR, "agent.json")
LOG_FILE = os.path.join(BASE_DIR, "agent.log")
POLL_WAIT_S = 25


def log(msg: str) -> None:
    line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


# ------------------------------------------------------------------ config
def load_config() -> dict:
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_config(cfg: dict) -> None:
    tmp = CONFIG_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, CONFIG_FILE)
    try:
        os.chmod(CONFIG_FILE, 0o600)
    except OSError:
        pass


# ------------------------------------------------------------------- http
def bridge_call(cfg: dict, method: str, path: str, body=None, timeout: float = 40.0) -> dict:
    url = cfg["bridge"].rstrip("/") + path
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Accept", "application/json")
    req.add_header("User-Agent", f"fluxbridge-agent/{VERSION}")
    req.add_header("X-Agent-Version", VERSION)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if cfg.get("token"):
        req.add_header("Authorization", f"Bearer {cfg['token']}")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        text = resp.read().decode("utf-8", "replace")
        return json.loads(text) if text else {}


def allowed_url(url: str) -> bool:
    """HTTPS to a Tradovate host only (no private addresses, no other services)."""
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    return parts.scheme == "https" and bool(host) and host.endswith(ALLOWED_HOST_SUFFIXES)


class _SameHostRedirects(urllib.request.HTTPRedirectHandler):
    """Follow redirects only while they stay on an allowed host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not allowed_url(newurl):
            raise urllib.error.HTTPError(newurl, code, "redirect to a non-Tradovate host refused", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_opener = urllib.request.build_opener(_SameHostRedirects)


def run_job(job: dict) -> dict:
    """Execute one relayed request against Tradovate; return the result record."""
    url = str(job.get("url") or "")
    if not allowed_url(url):
        return {"status_code": 0, "text": "", "error": "refused: not a Tradovate HTTPS URL"}
    params = job.get("params")
    if params:
        sep = "&" if "?" in url else "?"
        url = url + sep + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    body = job.get("json")
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=job.get("method", "GET").upper())
    for k, v in (job.get("headers") or {}).items():
        req.add_header(k, v)
    if data is not None and not any(k.lower() == "content-type" for k in (job.get("headers") or {})):
        req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", f"fluxbridge-agent/{VERSION}")
    timeout = float(job.get("timeout") or 20.0)
    try:
        with _opener.open(req, timeout=timeout) as resp:
            return {"status_code": resp.status, "text": resp.read().decode("utf-8", "replace")}
    except urllib.error.HTTPError as exc:  # 4xx/5xx still carry the broker's answer
        return {"status_code": exc.code, "text": exc.read().decode("utf-8", "replace")}
    except Exception as exc:  # noqa: BLE001 - network problems are reported to the bridge
        return {"status_code": 0, "text": "", "error": f"{type(exc).__name__}: {exc}"}


# ------------------------------------------------------------------- pair
def pair(cfg: dict, code: str, name: str) -> dict:
    answer = bridge_call(cfg, "POST", "/api/agent/pair", {"code": code, "name": name, "version": VERSION})
    cfg["token"] = answer["token"]
    cfg["name"] = answer.get("name") or name
    cfg["agent_id"] = answer.get("agent_id")
    save_config(cfg)
    log(f"Paired as '{cfg['name']}' (agent #{cfg['agent_id']}) with {cfg['bridge']}")
    return cfg


def prompt(text: str, default: str = "") -> str:
    try:
        v = input(f"{text}{' [' + default + ']' if default else ''}: ").strip()
    except EOFError:
        v = ""
    return v or default


# ------------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser(description="Fluxbridge execution agent")
    ap.add_argument("--bridge", help="bridge URL, e.g. https://bridge.example.com")
    ap.add_argument("--code", help="pairing code from Settings -> Execution Agents")
    ap.add_argument("--name", help="a name for this agent (e.g. the VPS name)")
    ap.add_argument("--once", action="store_true", help="poll once and exit (for tests)")
    ap.add_argument("--pair-only", action="store_true", help="pair (write agent.json) and exit without polling — used by the installers")
    args = ap.parse_args()

    cfg = load_config()
    if args.bridge:
        cfg["bridge"] = args.bridge
    if not cfg.get("bridge"):
        cfg["bridge"] = prompt("Bridge URL", "https://")
    bridge_host = (urllib.parse.urlsplit(cfg["bridge"]).hostname or "").lower()
    if not cfg["bridge"].startswith("https://") and bridge_host not in ("localhost", "127.0.0.1", "::1"):
        log("The bridge URL must start with https:// (plain http would send the agent token in the clear)")
        return 2
    if args.code or not cfg.get("token"):
        code = args.code or prompt("Pairing code")
        name = args.name or cfg.get("name") or prompt("Agent name", os.environ.get("COMPUTERNAME") or os.uname().nodename if hasattr(os, "uname") else "agent")
        try:
            pair(cfg, code, name)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            log(f"Pairing failed: {exc.code} {detail}")
            return 1
        except Exception as exc:  # noqa: BLE001
            log(f"Pairing failed: {exc}")
            return 1

    if args.pair_only:
        log(f"Agent '{cfg.get('name')}' paired with {cfg['bridge']} — config saved to {CONFIG_FILE}")
        return 0
    log(f"Agent '{cfg.get('name')}' v{VERSION} polling {cfg['bridge']} ...")
    backoff = 2.0
    while True:
        try:
            answer = bridge_call(cfg, "GET", f"/api/agent/jobs?wait={POLL_WAIT_S}", timeout=POLL_WAIT_S + 15)
            backoff = 2.0
            for job in answer.get("jobs") or []:
                started = time.monotonic()
                result = run_job(job)
                ms = round((time.monotonic() - started) * 1000)
                try:
                    bridge_call(cfg, "POST", f"/api/agent/jobs/{job['id']}/result", result, timeout=30)
                except Exception as exc:  # noqa: BLE001
                    log(f"could not deliver result for job {job['id']}: {exc}")
                what = urllib.parse.urlsplit(job.get("url", "")).path
                log(f"{job.get('method', 'GET')} {what} -> {result.get('status_code')} {result.get('error', '')} ({ms} ms)")
            if args.once:
                return 0
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                log("The bridge rejected this agent's token (revoked?). Delete agent.json and pair again.")
                return 1
            log(f"bridge answered {exc.code}; retrying in {backoff:.0f}s")
            time.sleep(backoff)
            backoff = min(60.0, backoff * 2)
        except KeyboardInterrupt:
            log("stopped")
            return 0
        except Exception as exc:  # noqa: BLE001
            log(f"connection problem: {exc}; retrying in {backoff:.0f}s")
            if args.once:
                return 1
            time.sleep(backoff)
            backoff = min(60.0, backoff * 2)


if __name__ == "__main__":
    sys.exit(main())

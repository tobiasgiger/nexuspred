"""Outbound notifications for connection and trade events.

Two channels, each independently toggled in Settings:

* **Discord** — a POST to a webhook URL, optionally prefixed with ``@everyone``.
* **Email** — sent via SMTP (e.g. Gmail with an App Password), connection
  events only (trade executions are Discord-only, per the trigger design).

Each of the three triggers (connection lost, connection restored, trade
executed) has its own on/off switch. A failure sending a notification is
logged and swallowed — a broken webhook URL or bad SMTP login must never
break a health check or a trade.
"""
from __future__ import annotations

import asyncio
import smtplib
from email.mime.text import MIMEText
from typing import Any

import httpx

from . import config, state


async def _send_discord(message: str) -> None:
    s = config.load_settings()
    if not s.get("alert_discord_enabled") or not s.get("alert_discord_webhook_url"):
        return
    content = f"@everyone {message}" if s.get("alert_discord_mention_everyone") else message
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(s["alert_discord_webhook_url"], json={"content": content})
        if resp.status_code >= 400:
            state.log_event("warn", f"Discord alert failed: {resp.status_code} {resp.text}")
    except Exception as exc:  # noqa: BLE001 - never let a notification failure escalate
        state.log_event("warn", f"Discord alert failed: {exc}")


def _send_email_sync(subject: str, body: str) -> None:
    s = config.load_settings()
    to_addr = s.get("alert_email_to")
    username = s.get("alert_smtp_username")
    password = s.get("alert_smtp_password")
    if not s.get("alert_email_enabled") or not to_addr or not username or not password:
        return
    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = username
    msg["To"] = to_addr
    host = s.get("alert_smtp_host") or "smtp.gmail.com"
    port = int(s.get("alert_smtp_port") or 587)
    with smtplib.SMTP(host, port, timeout=15) as server:
        server.starttls()
        server.login(username, password)
        server.send_message(msg)


async def _send_email(subject: str, body: str) -> None:
    try:
        await asyncio.to_thread(_send_email_sync, subject, body)
    except Exception as exc:  # noqa: BLE001
        state.log_event("warn", f"Email alert failed: {exc}")


async def connection_lost(account: str, environment: str, error: str) -> None:
    s = config.load_settings()
    if not s.get("alert_on_connection_lost", True):
        return
    detail = f" — {error}" if error else ""
    message = f"🔴 **Connection lost** — account `{account}` ({environment}, Tradovate){detail}"
    await _send_discord(message)
    await _send_email(f"nexuspred: connection lost ({account})", message)


async def connection_restored(account: str, environment: str) -> None:
    s = config.load_settings()
    if not s.get("alert_on_connection_restored", True):
        return
    message = f"🟢 **Connection restored** — account `{account}` ({environment}, Tradovate)"
    await _send_discord(message)
    await _send_email(f"nexuspred: connection restored ({account})", message)


async def trade_executed(
    webhook_name: str, action: str, contract: str, accounts: list[str]
) -> None:
    s = config.load_settings()
    if not s.get("alert_on_trade_executed", True):
        return
    accts = ", ".join(accounts) if accounts else "none"
    message = (
        f"⚡ **Trade executed** — strategy `{webhook_name}`: {action.upper()} "
        f"{contract} on {accts}"
    )
    await _send_discord(message)

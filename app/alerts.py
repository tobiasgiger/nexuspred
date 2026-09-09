"""Outbound notifications for connection and trade events.

Three channels, each independently toggled in Settings:

* **Discord** — a POST to a webhook URL, optionally prefixed with ``@everyone``.
* **Email** — sent via SMTP (e.g. Gmail with an App Password), connection
  events only (trade executions are Discord-only, per the trigger design).
* **Push** — Web Push to every device that installed the dashboard (desktop
  browsers and the iPhone home-screen app); gets every trigger, trades included.

Each of the three triggers (connection lost, connection restored, trade
executed) has its own on/off switch. A failure sending a notification is
logged and swallowed — a broken webhook URL or bad SMTP login must never
break a health check or a trade.
"""
from __future__ import annotations

import asyncio
import smtplib
import ssl
from email.mime.text import MIMEText
from typing import Any

from . import config, context, db, http, push, state


def account_alerts_on(spec: str, settings: dict[str, Any] | None = None) -> bool:
    """Whether account-level alerts are wanted for this trade account.
    ``alert_accounts`` lists the wanted specs; an empty list means all."""
    s = settings if settings is not None else config.load_settings()
    wanted = s.get("alert_accounts") or []
    return not wanted or str(spec) in set(wanted)


def alert_accounts(accounts: list[str], settings: dict[str, Any] | None = None) -> list[str]:
    s = settings if settings is not None else config.load_settings()
    return [a for a in accounts if account_alerts_on(a, s)]


async def _send_push(title: str, message: str, *, url: str = "/") -> None:
    """Web Push to the area's installed apps (see :mod:`app.push`)."""
    s = config.load_settings()
    if not s.get("alert_push_enabled", True) or not push.available():
        return
    try:
        await push.send_current_area(title, push.strip_markdown(message), url=url)
    except Exception as exc:  # noqa: BLE001 - never let a notification failure escalate
        state.log_event("warn", f"Push alert failed: {exc}")


async def _send_discord(message: str) -> None:
    s = config.load_settings()
    if not s.get("alert_discord_enabled") or not s.get("alert_discord_webhook_url"):
        return
    content = f"@everyone {message}" if s.get("alert_discord_mention_everyone") else message
    try:
        resp = await http.client("outbound").post(
            s["alert_discord_webhook_url"], json={"content": content}, timeout=10.0)
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
        server.starttls(context=ssl.create_default_context())  # verified TLS: credentials never go to an impostor
        server.login(username, password)
        server.send_message(msg)


async def _send_email(subject: str, body: str) -> None:
    try:
        await asyncio.to_thread(_send_email_sync, subject, body)
    except Exception as exc:  # noqa: BLE001
        state.log_event("warn", f"Email alert failed: {exc}")


def smtp_configured() -> bool:
    """True when SMTP is set up well enough to send a message to any recipient."""
    s = config.load_settings()
    return bool(s.get("alert_smtp_username") and s.get("alert_smtp_password"))


def _send_to_sync(to_addr: str, subject: str, body: str) -> None:
    s = config.load_settings()
    username = s.get("alert_smtp_username")
    password = s.get("alert_smtp_password")
    if not to_addr or not username or not password:
        return
    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = username
    msg["To"] = to_addr
    host = s.get("alert_smtp_host") or "smtp.gmail.com"
    port = int(s.get("alert_smtp_port") or 587)
    with smtplib.SMTP(host, port, timeout=15) as server:
        server.starttls(context=ssl.create_default_context())  # verified TLS: credentials never go to an impostor
        server.login(username, password)
        server.send_message(msg)


async def send_email_to(to_addr: str, subject: str, body: str) -> bool:
    """Send a one-off email to an arbitrary recipient via the configured SMTP.

    Returns True if a send was attempted (SMTP configured + recipient present).
    Used for invite / password-reset delivery, independent of the alert toggles.
    """
    if not smtp_configured() or not to_addr:
        return False
    try:
        await asyncio.to_thread(_send_to_sync, to_addr, subject, body)
        return True
    except Exception as exc:  # noqa: BLE001
        state.log_event("warn", f"Email send failed ({to_addr}): {exc}")
        return False


async def connection_lost(account: str, environment: str, error: str) -> None:
    s = config.load_settings()
    if not s.get("alert_on_connection_lost", True):
        return
    detail = f" — {error}" if error else ""
    message = f"🔴 **Connection lost** — account `{account}` ({environment}, Tradovate){detail}"
    await asyncio.gather(_send_discord(message),
                         _send_email(f"Fluxbridge: connection lost ({account})", message),
                         _send_push(f"Connection lost: {account}", message, url="/#/accounts"))


async def connection_restored(account: str, environment: str) -> None:
    s = config.load_settings()
    if not s.get("alert_on_connection_restored", True):
        return
    message = f"🟢 **Connection restored** — account `{account}` ({environment}, Tradovate)"
    await asyncio.gather(_send_discord(message),
                         _send_email(f"Fluxbridge: connection restored ({account})", message),
                         _send_push(f"Connection restored: {account}", message, url="/#/accounts"))


async def trade_executed(
    webhook_name: str, action: str, contract: str, accounts: list[str]
) -> None:
    s = config.load_settings()
    if not s.get("alert_on_trade_executed", True):
        return
    accounts = alert_accounts(accounts, s)
    if not accounts:
        return  # none of the traded accounts is on the alert list
    accts = ", ".join(accounts)
    message = (
        f"⚡ **Trade executed** — strategy `{webhook_name}`: {action.upper()} "
        f"{contract} on {accts}"
    )
    await asyncio.gather(_send_discord(message),
                         _send_push(f"Trade executed: {action.upper()} {contract}", message, url="/#/orders"))


def _money(v: Any) -> str:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return "n/a"
    sign = "+" if x > 0 else ("−" if x < 0 else "")
    return f"{sign}${abs(x):,.2f}"


def _price(v: Any) -> str:
    return f"{float(v):.4f}".rstrip("0").rstrip(".")


async def trade_opened(account: str, symbol: str, direction: str, qty: float, price: Any = None) -> None:
    """A position appeared on the broker side (bridge signal, manual or otherwise)."""
    s = config.load_settings()
    if not s.get("alert_on_trade_opened", True):
        return
    q = f"{qty:g}"
    at = f" @ {_price(price)}" if isinstance(price, (int, float)) and price else ""
    message = f"🟢 **Opened** {direction} {q} × {symbol}{at} · `{account}`"
    await asyncio.gather(_send_discord(message),
                         _send_push(f"Opened {direction} {symbol} · {account}", f"{q} contract{'s' if qty != 1 else ''}{at}", url="/#/"))


async def position_added(account: str, symbol: str, direction: str, added: float, total: float) -> None:
    s = config.load_settings()
    if not s.get("alert_on_trade_opened", True):
        return
    message = f"➕ **Added** {added:g} × {symbol} → {direction} {total:g} · `{account}`"
    await asyncio.gather(_send_discord(message),
                         _send_push(f"Added {added:g} {symbol} · {account}", f"Now {direction} {total:g}", url="/#/"))


async def trade_closed(account: str, symbol: str, direction: str, qty: float, pnl: Any, duration: str = "",
                       *, remaining: float = 0) -> None:
    """A position (or part of it) was closed; ``pnl`` is the account's realised
    change between two polls, i.e. the broker's own figure for the close."""
    s = config.load_settings()
    if not s.get("alert_on_trade_closed", True):
        return
    pnl_txt = _money(pnl) if pnl is not None else "P&L n/a"
    tail = f" ({duration})" if duration else ""
    if remaining:
        icon = "🟡"
        head = f"**Reduced** {direction} {symbol} by {qty:g} → {remaining:g} left"
        title = f"Reduced {direction} {symbol} · {account}"
    else:
        icon = "✅" if (isinstance(pnl, (int, float)) and pnl >= 0) else ("❌" if isinstance(pnl, (int, float)) else "⚪")
        head = f"**Closed** {direction} {qty:g} × {symbol}"
        title = f"Closed {direction} {symbol} · {account}"
    message = f"{icon} {head} · `{account}` · **{pnl_txt}**{tail}"
    await asyncio.gather(_send_discord(message),
                         _send_push(title, f"{pnl_txt}{tail} · {qty:g} contract{'s' if qty != 1 else ''}", url="/#/journal"))


async def agent_lost(name: str, last_ip: str = "") -> None:
    s = config.load_settings()
    if not s.get("alert_on_agent_lost", True):
        return
    where = f" (last seen from {last_ip})" if last_ip else ""
    message = (f"🔴 **Execution agent offline** — `{name}` stopped polling{where}. "
               f"Logins assigned to it cannot trade until it is back.")
    await asyncio.gather(_send_discord(message),
                         _send_email(f"Fluxbridge: execution agent offline ({name})", message),
                         _send_push(f"Agent offline: {name}", message, url="/#/settings/agents"))


async def agent_restored(name: str) -> None:
    s = config.load_settings()
    if not s.get("alert_on_agent_restored", True):
        return
    message = f"🟢 **Execution agent online** — `{name}` is polling again"
    await asyncio.gather(_send_discord(message),
                         _send_email(f"Fluxbridge: execution agent online ({name})", message),
                         _send_push(f"Agent online: {name}", message, url="/#/settings/agents"))


async def copy_alert(title: str, message: str, *, email: bool = False) -> None:
    """Copy trading: a follower order was rejected or a group paused itself."""
    s = config.load_settings()
    if not s.get("alert_on_copy", True):
        return
    body = f"📋 **Copy trading** — {message}"
    sends = [_send_discord(body), _send_push(title, message, url="/#/copy")]
    if email:
        sends.append(_send_email(f"Fluxbridge: {title}", body))
    await asyncio.gather(*sends)


async def daily_summary(pnl: dict[str, Any], closes: list[dict[str, Any]], day: str) -> None:
    """End-of-day recap: realised P&L per account plus the day's closed trades."""
    s = config.load_settings()
    if not s.get("alert_daily_summary", True):
        return
    accounts = [a for a in (pnl.get("accounts") or []) if account_alerts_on(a.get("spec") or a.get("account_id"), s)]
    closes = [c for c in closes if account_alerts_on(c.get("account", ""), s)]
    per = ", ".join(f"{a.get('spec') or a.get('account_id')} {_money(a.get('realized'))}" for a in accounts) or "no accounts polled"
    wins = sum(1 for c in closes if isinstance(c.get("pnl"), (int, float)) and c["pnl"] > 0)
    losses = sum(1 for c in closes if isinstance(c.get("pnl"), (int, float)) and c["pnl"] < 0)
    trades = f"{len(closes)} trade{'s' if len(closes) != 1 else ''} closed" + (f" ({wins} win, {losses} loss)" if closes else "")
    total = _money(sum(float(a.get("realized") or 0) for a in accounts))
    open_pnl = _money(sum(float(a.get("open") or 0) for a in accounts))
    message = f"📊 **Daily summary {day}** — realised **{total}** ({per}) · {trades} · open {open_pnl}"
    await asyncio.gather(_send_discord(message),
                         _send_email(f"Fluxbridge: daily summary {day} ({total})", message),
                         _send_push(f"Daily P&L {total}", f"{trades} · {per}", url="/#/journal"))


async def discord_listener_lost(error: str = "") -> None:
    s = config.load_settings()
    if not s.get("alert_on_discord_lost", True):
        return
    detail = f" — {error}" if error else ""
    message = f"🔴 **Discord listener offline** — the signal listener lost its Gateway connection{detail}"
    await asyncio.gather(_send_discord(message),
                         _send_email("Fluxbridge: Discord listener offline", message),
                         _send_push("Discord listener offline", message, url="/#/discord"))


async def discord_listener_restored(user: str = "") -> None:
    s = config.load_settings()
    if not s.get("alert_on_discord_restored", True):
        return
    who = f" (as `{user}`)" if user else ""
    message = f"🟢 **Discord listener online** — the signal listener reconnected to the Gateway{who}"
    await asyncio.gather(_send_discord(message),
                         _send_email("Fluxbridge: Discord listener online", message),
                         _send_push("Discord listener online", message, url="/#/discord"))


async def webhook_failed(webhook_name: str, reason: str) -> None:
    s = config.load_settings()
    if not s.get("alert_on_webhook_failed", True):
        return
    message = (
        f"⚠️ **Signal not executed** — webhook `{webhook_name}` received a signal but "
        f"execution failed: {reason}"
    )
    await asyncio.gather(_send_discord(message),
                         _send_email(f"Fluxbridge: signal not executed ({webhook_name})", message),
                         _send_push(f"Signal not executed: {webhook_name}", message, url="/#/events"))


async def contract_rollover(message: str) -> None:
    """A mapped contract is near / past its roll date (Discord + email)."""
    s = config.load_settings()
    if not s.get("alert_on_rollover", True):
        return
    await asyncio.gather(_send_discord(message),
                         _send_email("Fluxbridge: contract rollover due", message),
                         _send_push("Contract rollover due", message, url="/#/settings/symbols"))


async def test_alert() -> dict[str, Any]:
    """Send a test notification on every enabled channel; report what was tried."""
    s = config.load_settings()
    message = "🔔 **Test alert** — Fluxbridge notifications are configured correctly."
    channels = {"discord": bool(s.get("alert_discord_enabled") and s.get("alert_discord_webhook_url")),
                "email": bool(s.get("alert_email_enabled") and s.get("alert_email_to")
                              and s.get("alert_smtp_username") and s.get("alert_smtp_password")),
                "push": bool(s.get("alert_push_enabled", True) and push.available()
                             and db.list_push_subscriptions(context.get_area()))}
    sends = []
    if channels["discord"]:
        sends.append(_send_discord(message))
    if channels["email"]:
        sends.append(_send_email("Fluxbridge: test alert", message))
    if channels["push"]:
        sends.append(_send_push("Test alert", message, url="/#/settings/alerts"))
    if sends:
        await asyncio.gather(*sends)
    return channels

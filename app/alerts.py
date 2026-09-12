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


async def _send_push(title: str, message: str, *, url: str = "/", settings: dict[str, Any] | None = None) -> None:
    """Web Push to the area's installed apps (see :mod:`app.push`)."""
    s = settings if settings is not None else config.load_settings()
    if not s.get("alert_push_enabled", True) or not push.available():
        return
    try:
        await push.send_current_area(title, push.strip_markdown(message), url=url)
    except Exception as exc:  # noqa: BLE001 - never let a notification failure escalate
        state.log_event("warn", f"Push alert failed: {exc}")


async def _send_discord(message: str, *, settings: dict[str, Any] | None = None) -> None:
    s = settings if settings is not None else config.load_settings()
    if not s.get("alert_discord_enabled") or not s.get("alert_discord_webhook_url"):
        return
    everyone = bool(s.get("alert_discord_mention_everyone"))
    content = (f"@everyone {message}" if everyone else message)[:2000]          # Discord refuses longer bodies
    try:
        resp = await http.client("outbound").post(
            s["alert_discord_webhook_url"],
            # only the configured @everyone may ping: a webhook name or an error text carrying @here does not
            json={"content": content, "allowed_mentions": {"parse": ["everyone"] if everyone else []}}, timeout=10.0)
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


BROKER_LABELS = {"tradovate": "Tradovate", "rithmic": "Rithmic", "projectx": "ProjectX"}


def _tr(settings: dict[str, Any]):
    """The workspace's alert translator (see app.i18n.alert_translator)."""
    from . import i18n
    return i18n.alert_translator(settings)


async def connection_lost(account: str, environment: str, error: str, broker: str = "tradovate") -> None:
    s = config.load_settings()
    if not s.get("alert_on_connection_lost", True):
        return
    tr = _tr(s)
    detail = f" — {error}" if error else ""
    message = tr("🔴 **Connection lost** — login `{account}` ({environment}, {broker}){detail}", account=account, environment=environment, broker=BROKER_LABELS.get(broker, broker), detail=detail)
    await asyncio.gather(_send_discord(message),
                         _send_email(tr("Fluxbridge: connection lost ({account})", account=account), message),
                         _send_push(tr("Connection lost: {account}", account=account), message, url="/#/accounts"))


async def connection_restored(account: str, environment: str, broker: str = "tradovate") -> None:
    s = config.load_settings()
    if not s.get("alert_on_connection_restored", True):
        return
    tr = _tr(s)
    message = tr("🟢 **Connection restored** — login `{account}` ({environment}, {broker})", account=account, environment=environment, broker=BROKER_LABELS.get(broker, broker))
    await asyncio.gather(_send_discord(message),
                         _send_email(tr("Fluxbridge: connection restored ({account})", account=account), message),
                         _send_push(tr("Connection restored: {account}", account=account), message, url="/#/accounts"))


async def trade_executed(
    webhook_name: str, action: str, contract: str, accounts: list[str], *,
    settings: dict[str, Any] | None = None,
) -> None:
    """``settings`` is the executing signal's settings snapshot (saves three
    settings reads per fill; the alert preferences rarely change mid-signal)."""
    s = settings if settings is not None else config.load_settings()
    if not s.get("alert_on_trade_executed", True):
        return
    accounts = alert_accounts(accounts, s)
    if not accounts:
        return  # none of the traded accounts is on the alert list
    tr = _tr(s)
    accts = ", ".join(accounts)
    message = tr("⚡ **Trade executed** — strategy `{webhook}`: {action} {contract} on {accounts}", webhook=webhook_name, action=action.upper(), contract=contract, accounts=accts)
    await asyncio.gather(_send_discord(message, settings=s),
                         _send_push(tr("Trade executed: {action} {contract}", action=action.upper(), contract=contract), message, url="/#/orders", settings=s))


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
    tr = _tr(s)
    q = f"{qty:g}"
    at = f" @ {_price(price)}" if isinstance(price, (int, float)) and price else ""
    message = tr("🟢 **Opened** {direction} {qty} × {symbol}{at} · `{account}`", direction=direction, qty=q, symbol=symbol, at=at, account=account)
    await asyncio.gather(_send_discord(message),
                         _send_push(tr("Opened {direction} {symbol} · {account}", direction=direction, symbol=symbol, account=account), tr("{qty} contracts{at}" if qty != 1 else "{qty} contract{at}", qty=q, at=at), url="/#/"))


async def position_added(account: str, symbol: str, direction: str, added: float, total: float) -> None:
    s = config.load_settings()
    if not s.get("alert_on_trade_opened", True):
        return
    tr = _tr(s)
    message = tr("➕ **Added** {added} × {symbol} → {direction} {total} · `{account}`", added=f"{added:g}", symbol=symbol, direction=direction, total=f"{total:g}", account=account)
    await asyncio.gather(_send_discord(message),
                         _send_push(tr("Added {added} {symbol} · {account}", added=f"{added:g}", symbol=symbol, account=account), tr("Now {direction} {total}", direction=direction, total=f"{total:g}"), url="/#/"))


async def trade_closed(account: str, symbol: str, direction: str, qty: float, pnl: Any, duration: str = "",
                       *, remaining: float = 0) -> None:
    """A position (or part of it) was closed; ``pnl`` is the account's realised
    change between two polls, i.e. the broker's own figure for the close."""
    s = config.load_settings()
    if not s.get("alert_on_trade_closed", True):
        return
    tr = _tr(s)
    pnl_txt = _money(pnl) if pnl is not None else tr("P&L n/a")
    tail = f" ({duration})" if duration else ""
    if remaining:
        icon = "🟡"
        head = tr("**Reduced** {direction} {symbol} by {qty} → {remaining} left", direction=direction, symbol=symbol, qty=f"{qty:g}", remaining=f"{remaining:g}")
        title = tr("Reduced {direction} {symbol} · {account}", direction=direction, symbol=symbol, account=account)
    else:
        icon = "✅" if (isinstance(pnl, (int, float)) and pnl >= 0) else ("❌" if isinstance(pnl, (int, float)) else "⚪")
        head = tr("**Closed** {direction} {qty} × {symbol}", direction=direction, qty=f"{qty:g}", symbol=symbol)
        title = tr("Closed {direction} {symbol} · {account}", direction=direction, symbol=symbol, account=account)
    message = f"{icon} {head} · `{account}` · **{pnl_txt}**{tail}"
    await asyncio.gather(_send_discord(message),
                         _send_push(title, tr("{pnl}{tail} · {qty} contracts" if qty != 1 else "{pnl}{tail} · {qty} contract", pnl=pnl_txt, tail=tail, qty=f"{qty:g}"), url="/#/journal"))


async def agent_lost(name: str, last_ip: str = "") -> None:
    s = config.load_settings()
    if not s.get("alert_on_agent_lost", True):
        return
    tr = _tr(s)
    where = tr(" (last seen from {ip})", ip=last_ip) if last_ip else ""
    message = tr("🔴 **Execution agent offline** — `{name}` stopped polling{where}. Logins assigned to it cannot trade until it is back.", name=name, where=where)
    await asyncio.gather(_send_discord(message),
                         _send_email(tr("Fluxbridge: execution agent offline ({name})", name=name), message),
                         _send_push(tr("Agent offline: {name}", name=name), message, url="/#/settings/agents"))


async def agent_restored(name: str) -> None:
    s = config.load_settings()
    if not s.get("alert_on_agent_restored", True):
        return
    tr = _tr(s)
    message = tr("🟢 **Execution agent online** — `{name}` is polling again", name=name)
    await asyncio.gather(_send_discord(message),
                         _send_email(tr("Fluxbridge: execution agent online ({name})", name=name), message),
                         _send_push(tr("Agent online: {name}", name=name), message, url="/#/settings/agents"))


async def risk_triggered(spec: str, kind: str, reason: str, pnl: float, errors: list[str]) -> None:
    """Risk guard: an account hit its daily loss / profit limit or flatten time."""
    s = config.load_settings()
    if not s.get("alert_on_risk", True):
        return
    tr = _tr(s)
    icon = {"loss": "🛑", "profit": "🎯", "time": "⏰"}.get(kind, "🔒")
    message = tr("{icon} **Risk guard** — `{spec}` flattened and locked for today: {reason}.{errors}", icon=icon, spec=spec, reason=reason,
                 errors=tr(" Errors: {errors}", errors="; ".join(errors)) if errors else "")
    await asyncio.gather(_send_discord(message),
                         _send_email(tr("Fluxbridge: risk guard {spec} ({kind})", spec=spec, kind=kind), message),
                         _send_push(tr("Risk guard: {spec}", spec=spec), message, url="/#/settings/accounts"))


async def execution_problem(title: str, message: str) -> None:
    """Something the operator must look at now: a position without its stop, an
    order whose outcome is unknown, working orders left behind by a close.
    Always sent (no switch), to every channel."""
    tr = _tr(config.load_settings())
    body = tr("🚨 **{title}** — {message}", title=title, message=message)
    await asyncio.gather(_send_discord(body), _send_email(f"Fluxbridge: {title}", body),
                         _send_push(title, message, url="/#/"))


async def news_lock(title: str, currency: str, until: str, *, flatten: bool = False) -> None:
    """A news-lock window opened: no new entries until ``until`` (and, with
    ``flatten``, open positions are being closed)."""
    tr = _tr(config.load_settings())
    what = f"{title}{' (' + currency + ')' if currency else ''}"
    message = tr("{what}: no new entries until {until}", what=what, until=until) + (tr(" — open positions are being flattened") if flatten else "")
    body = tr("📰 **News lock** — {message}", message=message)
    await asyncio.gather(_send_discord(body), _send_push(tr("News lock"), message, url="/#/settings/news"))


async def copy_alert(title: str, message: str, *, email: bool = False) -> None:
    """Copy trading: a follower order was rejected or a group paused itself."""
    s = config.load_settings()
    if not s.get("alert_on_copy", True):
        return
    body = _tr(s)("📋 **Copy trading** — {message}", message=message)
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
    tr = _tr(s)
    per = ", ".join(f"{a.get('spec') or a.get('account_id')} {_money(a.get('realized'))}" for a in accounts) or tr("no accounts polled")
    wins = sum(1 for c in closes if isinstance(c.get("pnl"), (int, float)) and c["pnl"] > 0)
    losses = sum(1 for c in closes if isinstance(c.get("pnl"), (int, float)) and c["pnl"] < 0)
    trades = tr("{n} trades closed" if len(closes) != 1 else "{n} trade closed", n=len(closes)) + (tr(" ({wins} win, {losses} loss)", wins=wins, losses=losses) if closes else "")
    total = _money(sum(float(a.get("realized") or 0) for a in accounts))
    open_pnl = _money(sum(float(a.get("open") or 0) for a in accounts))
    message = tr("📊 **Daily summary {day}** — realised **{total}** ({per}) · {trades} · open {open}", day=day, total=total, per=per, trades=trades, open=open_pnl)
    await asyncio.gather(_send_discord(message),
                         _send_email(tr("Fluxbridge: daily summary {day} ({total})", day=day, total=total), message),
                         _send_push(tr("Daily P&L {total}", total=total), f"{trades} · {per}", url="/#/journal"))


async def discord_listener_lost(error: str = "") -> None:
    s = config.load_settings()
    if not s.get("alert_on_discord_lost", True):
        return
    tr = _tr(s)
    detail = f" — {error}" if error else ""
    message = tr("🔴 **Discord listener offline** — the signal listener lost its Gateway connection{detail}", detail=detail)
    await asyncio.gather(_send_discord(message),
                         _send_email(tr("Fluxbridge: Discord listener offline"), message),
                         _send_push(tr("Discord listener offline"), message, url="/#/discord"))


async def discord_listener_restored(user: str = "") -> None:
    s = config.load_settings()
    if not s.get("alert_on_discord_restored", True):
        return
    tr = _tr(s)
    who = tr(" (as `{user}`)", user=user) if user else ""
    message = tr("🟢 **Discord listener online** — the signal listener reconnected to the Gateway{who}", who=who)
    await asyncio.gather(_send_discord(message),
                         _send_email(tr("Fluxbridge: Discord listener online"), message),
                         _send_push(tr("Discord listener online"), message, url="/#/discord"))


async def webhook_failed(webhook_name: str, reason: str, *, settings: dict[str, Any] | None = None) -> None:
    s = settings if settings is not None else config.load_settings()
    if not s.get("alert_on_webhook_failed", True):
        return
    tr = _tr(s)
    message = tr("⚠️ **Signal not executed** — webhook `{webhook}` received a signal but execution failed: {reason}", webhook=webhook_name, reason=reason)
    await asyncio.gather(_send_discord(message),
                         _send_email(tr("Fluxbridge: signal not executed ({webhook})", webhook=webhook_name), message),
                         _send_push(tr("Signal not executed: {webhook}", webhook=webhook_name), message, url="/#/events"))


async def contract_rollover(message: str) -> None:
    """A mapped contract is near / past its roll date (Discord + email)."""
    s = config.load_settings()
    if not s.get("alert_on_rollover", True):
        return
    tr = _tr(s)
    await asyncio.gather(_send_discord(message),
                         _send_email(tr("Fluxbridge: contract rollover due"), message),
                         _send_push(tr("Contract rollover due"), message, url="/#/settings/symbols"))


async def test_alert() -> dict[str, Any]:
    """Send a test notification on every enabled channel; report what was tried."""
    s = config.load_settings()
    message = _tr(s)("🔔 **Test alert** — Fluxbridge notifications are configured correctly.")
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


# ------------------------------------------------------------- event bus
# The producers announce, the alert functions above listen. Handlers look the
# alert function up at call time, so a test that patches ``alerts.trade_opened``
# still sees the call.
def _register() -> None:
    from . import events as ev
    ev.subscribe("connection.lost", lambda e: connection_lost(e["account"], e["environment"], e.get("error", ""), broker=e.get("broker", "tradovate")))
    ev.subscribe("connection.restored", lambda e: connection_restored(e["account"], e["environment"], broker=e.get("broker", "tradovate")))
    ev.subscribe("trade.executed", lambda e: trade_executed(e["webhook"], e["action"], e["contract"], e["accounts"], **({"settings": e["settings"]} if e.get("settings") is not None else {})))
    ev.subscribe("position.opened", lambda e: trade_opened(e["account"], e["symbol"], e["direction"], e["qty"], e.get("price")))
    ev.subscribe("position.added", lambda e: position_added(e["account"], e["symbol"], e["direction"], e["added"], e["total"]))
    ev.subscribe("position.closed", lambda e: trade_closed(e["account"], e["symbol"], e["direction"], e["qty"], e.get("pnl"), e.get("duration", ""), **({"remaining": e["remaining"]} if e.get("remaining") else {})))
    ev.subscribe("agent.lost", lambda e: agent_lost(e["name"], e.get("last_ip", "")))
    ev.subscribe("agent.restored", lambda e: agent_restored(e["name"]))
    ev.subscribe("risk.triggered", lambda e: risk_triggered(e["spec"], e["kind"], e["reason"], e["pnl"], e.get("errors") or []))
    ev.subscribe("execution.problem", lambda e: execution_problem(e["title"], e["message"]))
    ev.subscribe("news.lock", lambda e: news_lock(e["title"], e["currency"], e["until"], flatten=bool(e.get("flatten"))))
    ev.subscribe("copy.alert", lambda e: copy_alert(e["title"], e["message"], email=bool(e.get("email"))))
    ev.subscribe("daily.summary", lambda e: daily_summary(e["pnl"], e["closes"], e["day"]))
    ev.subscribe("discord.lost", lambda e: discord_listener_lost(e.get("error", "")))
    ev.subscribe("discord.restored", lambda e: discord_listener_restored(e.get("user", "")))
    ev.subscribe("signal.failed", lambda e: webhook_failed(e["webhook"], e["reason"], **({"settings": e["settings"]} if e.get("settings") is not None else {})))
    ev.subscribe("rollover.due", lambda e: contract_rollover(e["message"]))


_register()

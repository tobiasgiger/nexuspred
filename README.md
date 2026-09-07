# Fluxbridge — TradingView → Tradovate signal router

_(repository: `tobiasgiger/nexuspred`)_

A self-hosted bridge that receives **TradingView** alerts via a webhook and routes them
to **Tradovate** as live/demo orders. Ships with a modern dashboard (dark & light) for
configuration and monitoring, an optional Discord signal listener, **multi-user accounts (invite-only)**
access control, and a built-in GitHub auto-updater.

> **v5** — the refactored, faster platform: same trading logic, same API, same settings
> and database schema as v4.11; restructured code, pooled connections, more concurrency
> and a new build-free UI. `main` carries v5; the previous line is preserved on branch
> **`backup/v4.11.0`** (data is compatible both ways). See
> [Architecture (v5)](#architecture-v5) and [Upgrading from v4 / rollback](#upgrading-from-v4--rollback).

![dashboard](docs/dashboard.png)

---

## Features

- **Bracket strategy order logic** built around the strategy's JSON signals:
  - Initial `buy` / `sell` → **market** order, default **3 contracts** (MNQ / MES).
  - Each `tp1` / `tp2` / `tp3` → **limit** order of **1 contract**.
  - `sl` → protective **stop** order covering the whole position.
  - `move_sl` → moves the protective stop (e.g. to break-even).
  - `trail_active` → acknowledged (the strategy keeps sending `move_sl` updates).
  - `close_all` → cancels working orders and **flattens the whole position**.
- **Trade simulator** that runs full scenarios (winning trade, losing trade, manual
  close…) through the *real* signal logic with an in-memory executor — no credentials,
  no broker calls. Run a whole scenario or step through it.
- **Token-only, multi-account**: each Tradovate account is authenticated by its own
  access token (no username/password). Every signal fans out to all enabled accounts in
  parallel, each with its own quantity multiplier.
- **Token refresh & health monitoring**: renews each account's token via
  `/auth/renewaccesstoken` before expiry (access token → check token, no password), and a
  background loop continuously verifies every connection is up.
- **Dashboard** (dark & light theme, phone-friendly) to manage settings, watch
  positions/orders live and read logs — deep-linkable pages, one live event stream, no
  build step and no CDN.
- **🆘 Flatten all**: a one-click emergency kill-switch that cancels every working order
  and closes every position on all accounts — even while trading is paused.
- **Auto-updater** that checks the GitHub repo and shows an **Update** button when a new
  version is available — one click pulls the latest code and restarts.
- **One dedicated webhook per strategy** (`POST /webhook/{token}`), created/edited/deleted
  from the **Webhooks** tab — each with its own routed trade accounts and per-account qty
  multiplier, so signals never cross strategies. Three selectable strategy types:
  **simple** (buy/sell the payload's qty, no TP/SL — just execution), **bracket** (the
  entry + tp1/tp2/tp3/sl flow described above), and **TS-Hunter** (matches the TS-Hunter
  Pine strategy's own contract: market entry sized from `risk.value`, then percent-based
  partial closes correlated by `trade_id` — see below).
- **Safety first**: trading is **disabled by default**, every webhook URL has its own
  unguessable secret token, and an optional passphrase can be enforced in the alert body.
- **Alerts** (Settings → Alerts): Discord webhook and/or email, each independently
  toggled, for connection lost/restored (which account + broker) and trade executed
  (which accounts + strategy, Discord only).
- **Discord signal listener** (configured under Settings → Discord Listener; live feed on
  the Discord tab): watches Discord channels over the
  Gateway with a personal user token (self-bot) and fans parsed trade signals out to
  configurable webhook targets in parallel — see below.

---

## Quick start

### One-click installers (recommended)

The installers create an isolated virtual environment, install dependencies and
write a launcher — nothing else on your system is touched.

**Linux / macOS**
```bash
git clone https://github.com/tobiasgiger/nexuspred.git
cd nexuspred
chmod +x install.sh
./install.sh            # add --service to auto-start on boot, --port 9000 to change port
./start.sh
```

**Windows**
1. Install [Python 3.9+](https://www.python.org/downloads/windows/) and tick *“Add python.exe to PATH”*.
2. Download the project, then double-click **`install.bat`**.
3. Double-click the generated **`start.bat`**.

### Manual install

```bash
pip install -r requirements.txt
python run.py
```

Open the dashboard at **http://localhost:9000** — then follow the built-in
**Setup Guide** tab, which walks you through Tradovate API setup, connecting, and
wiring up your TradingView alert.

### Deploy on Render (for TradingView webhooks)

TradingView only posts webhooks to **port 80/443**. Render serves every service over
**HTTPS (443)** with a public URL, so it's the easiest way to receive alerts. This repo
includes a `render.yaml` blueprint.

1. In Render: **New → Blueprint**, pick this repo (it reads `render.yaml`: a web service
   + a 1 GB persistent disk at `/var/data`).
2. Set env var `NEXUSPRED_DATA_DIR=/var/data` (persists the SQLite DB + tokens). Render
   injects `PORT` automatically. There's no auth env var — on first load you'll create
   the admin account at `/setup`.
3. Use the **Starter** plan (always-on); the free plan sleeps after ~15 min idle.
4. Deploy → you get `https://YOUR-SERVICE.onrender.com`. The production bridge runs on
   the custom domain **`https://bridge.hurenzone.ch`** (Render → service → *Settings →
   Custom Domains*, plus a CNAME at the DNS provider; Render issues the TLS certificate).
   Set `NEXUSPRED_PUBLIC_URL=https://bridge.hurenzone.ch` (already in `render.yaml`) so
   the dashboard shows every webhook URL on the custom domain and emailed invite /
   reset links use it too. Each strategy's TradingView webhook is
   `https://bridge.hurenzone.ch/webhook/YOUR_TOKEN` (copy it from its card in the
   **Webhooks** tab).
5. Updates deploy automatically on `git push` (the in-app Update button is disabled on
   managed hosts).

> **Build fails compiling `pydantic-core`/`orjson`?** Render chose a too-new Python with
> no prebuilt wheels. The repo pins **Python 3.11** via `.python-version`; if your service
> predates it, set `PYTHON_VERSION=3.11.9` and *Clear build cache & deploy*.

> **Persistence & security on any public host:** point `NEXUSPRED_DATA_DIR` at a
> persistent disk so the SQLite DB + settings survive deploys. The dashboard is
> protected by the **account login** — on first run create the admin at `/setup`, then
> invite users (see [Users, areas & login](#users-areas--login-multi-tenant)). The
> `/webhook/<token>` and `/healthz` paths stay open (`GET /healthz` is an unauthenticated
> liveness probe).

### Execution agents (one IP per account)

Prop firms often frown on several accounts trading from one IP. An **execution agent**
is a tiny helper you run on a VPS (a Windows .exe built by CI, or the plain Python
script): **Settings → Execution Agents → Download preconfigured agent** gives you a zip
with the token already inside — unzip, start, done — or pair the plain agent with a
one-time code. Its token is only valid for the relay endpoints (`/api/agent/…`), and it
long-polls the bridge over outbound HTTPS. Assign a login to it under **Tradovate Accounts → Execute via** and
every Tradovate call of that login — orders, token renewal, health checks, P&L — is
executed by the agent from its IP. An offline agent makes those calls fail loudly (event
log + alert); the bridge never silently falls back to its own address. Setup steps for
a Windows VPS are in `agent/README.md`.

### Trading journal

The **Journal** page turns your Tradovate fills into a P&L journal. Once a day after the
CME close (default **23:30 Europe/Zurich**, configurable under Settings → General →
*Trading journal*) — and whenever you press **Import now** — the bridge reads, per
enabled login, the session's fills, Tradovate's fill pairs (entry ↔ exit), fees, the
contract's value per point and a cash-balance snapshot per account, and stores each
round trip with side, quantity, entry/exit, points, gross P&L, fees and net P&L. Imports
are keyed by Tradovate ids, so running them again never duplicates a trade.

Reporting is bucketed by the trade's exit time in the journal timezone: daily / weekly /
monthly P&L, equity curve, month calendar, breakdowns by symbol, account, weekday and
hour, plus win rate, profit factor, expectancy and max drawdown. Every chart has a table
view; every trade takes a note and tags; the filtered set exports as CSV.

**Past days.** Tradovate's entity lists cover the current session only, but the
platform's Reports tab is served by a separate reporting service that accepts any date
range. Every import requests the **Performance** report (one row per round trip, with
the broker's own realised P&L) per account: the first run reaches back *History to
import (days)* (Settings → General → Trading journal, default 365), later runs fetch
only what is new. Rows are keyed by their fill ids, so a trade that also arrived through
the session import is never duplicated. Reports carry no fees; set *Fee per contract per
side* to have them applied. A CSV export can still be loaded with **Import CSV**. Trades
placed through the simulator are not journaled (they never reach Tradovate).

### Security hardening (built in)

- **Sessions**: HMAC-signed, `HttpOnly`, `Secure` (behind HTTPS), `SameSite=Lax` cookie
  bound to the account's password hash — a password change or reset logs every other
  session out. PBKDF2-SHA256 (200k rounds) password hashing.
- **CSRF**: state-changing requests whose `Origin` / `Sec-Fetch-Site` show another site
  are rejected (the TradingView ingress is exempt — it carries no cookie anyway).
- **Brute force**: per-IP rate limits on `/login`, `/setup`, `/register`, `/reset`,
  the password-change API and agent pairing (with a global per-IP ceiling), plus
  address-independent brakes — 20 failed logins per account in 10 minutes and 300 per
  minute server-wide — so rotating or spoofing IPs buys an attacker nothing. The client
  address is taken from the hop the *trusted* proxy appended to `X-Forwarded-For`
  (`NEXUSPRED_PROXY_HOPS`, default 1 = Render / one nginx; 0 = no proxy, ignore the
  header; 2 = Cloudflare in front of nginx). 256 KB request-body cap.
- **Headers**: strict Content-Security-Policy with a per-request script nonce,
  `frame-ancestors 'none'`, `X-Frame-Options`, `X-Content-Type-Options`,
  `Referrer-Policy`, `Permissions-Policy`, HSTS behind HTTPS, `Cache-Control: no-store`
  on API and auth responses.
- **SSRF**: the Discord alert webhook URL, custom Discord-signal target URLs, the SMTP
  host and every push-notification endpoint are checked on save — `http(s)` only, no
  credentials, and the host must not resolve to a loopback / private / link-local
  address. SMTP uses verified TLS (`starttls` with the system trust store).
- **Execution agents**: the agent only ever opens HTTPS connections to `*.tradovateapi.com`
  / `*.tradovate.com` (redirects elsewhere are refused) and the bridge refuses to relay
  anything else, so neither a bug nor a compromised bridge can turn a VPS into a proxy;
  an agent can only be assigned to logins of the workspace it is paired with; agent
  tokens are stored hashed, unknown tokens are never cached, and deleting a user revokes
  their agents. The bundled Windows `.exe` is verified against the SHA-256 published on
  the same release and the build carries a GitHub provenance attestation.
- **Signal logs** never store the webhook passphrase (or any `secret` / `token` /
  `password` field of the payload): it is masked before the signal is logged, streamed,
  persisted or forwarded to marketplace subscribers.
- **Least privilege**: the generic settings endpoint cannot write webhooks, token
  accounts or the Discord listener config (each has its own validating endpoint); the
  self-updater (`git reset` + restart of the whole process) is admin-only; the Discord
  routes require the admin-granted *Discord Signals* entitlement; an invite bound to an
  email can only be redeemed by that address.
- **Secrets** (Tradovate tokens, Discord user token, SMTP password, alert webhook URL,
  passphrase, Discord-target secrets) are masked in every API response, never logged,
  and **encrypted at rest** in the SQLite DB (Fernet / AES-128-CBC + HMAC). The key
  comes from `NEXUSPRED_ENCRYPTION_KEY`, else `SESSION_SECRET`, else an auto-generated
  key in the DB (weakest — set one of the env vars on any public host). Rotating the
  key makes stored tokens unreadable; re-enter them afterwards.

1. Go to **Settings → Tradovate Accounts** → add one login per Tradovate account with its
   own access token (start in **Demo**), save, then **Connect & Verify** — the trade
   accounts under each login are discovered automatically.
2. Go to **Webhooks** → **Add webhook** for each strategy, pick its strategy type
   (`simple`, `bracket` or `TS-Hunter`) and, on the **Accounts** tab of the drawer, route
   the accounts (with qty multipliers) that strategy should trade.
3. Copy the webhook's URL and alert message from the **Alert template** tab (or the
   **Tools** page) into the matching TradingView alert.
4. Flip **Trading** on (topbar pill or Settings → General & Trading) when you're ready.

---

## TradingView alert setup

Each strategy gets its own webhook — create it in the **Webhooks** tab, then set that
alert's **Webhook URL** to the token shown on its card:

```
http://YOUR_HOST:9000/webhook/YOUR_TOKEN
```

### `simple` strategy webhooks

Just `action`, `symbol` and (optionally) `qty` — no TP/SL:

```json
{"action":"buy","symbol":"MNQ1!","qty":2}
```

Omit `qty` to use the webhook's configured default. `close_all` flattens the position:

```json
{"action":"close_all","symbol":"MNQ1!"}
```

### `bracket` strategy webhooks

Set the alert message to the strategy's JSON. Examples:

**Entry**
```json
{"event":"entry","action":"sell","symbol":"MNQ1!","entry":30267,
 "sl":30285.06839,"tp1":30261.57948,"tp2":30265.19316,"tp3":30247.0425}
```

**Move stop to break-even**
```json
{"event":"tp1_hit","action":"move_sl","symbol":"MNQ1!","new_sl":30266.01}
```

**Trailing active**
```json
{"event":"tp2_hit","action":"trail_active","symbol":"MNQ1!","trail_ema":"ema9"}
```

**Close everything**
```json
{"event":"tp3_hit","action":"close_all","symbol":"MNQ1!","exit_price":30241.7}
```

> If you set a **passphrase** in Settings, include `"passphrase":"..."` in every alert
> (applies to all webhooks).

### `TS-Hunter` strategy webhooks

Matches the TS-Hunter Pine strategy's own alert contract (`contract_version:
at_execution_command_v5`) — point that strategy's alerts straight at this webhook, nothing
to hand-edit. A trade's lifecycle is a `signal` entry followed by zero or more `management`
messages, all correlated by the shared `trade_id`:

**Entry** (`event: "signal"`) — market order sized from `risk.value`, protective stop at `sl.value`:
```json
{"event":"signal","side":"SELL","symbol":"MNQ",
 "risk":{"mode":"fixed_lot","value":4},"sl":{"mode":"fixed_price_from_alert","value":29658.50},
 "trade_id":"TS-HUNTER-SELL-123","tv":{"entry_price":29329.00}}
```

**Partial close** (`event: "management"`, `action: "partial_close_percent"`) — market-closes
`percent`% of whatever remains right now (not of the original size), so three TP hits at
25% / 33.33% / 50% of a 4-lot leave 3 → 2 → 1 (the "runner"). Every partial close resizes
the stop to match the new remaining qty (its price is left unchanged):
```json
{"event":"management","action":"partial_close_percent","lifecycle_stage":"TP2",
 "side":"SELL","symbol":"MNQ","percent":33.33333333,"trade_id":"TS-HUNTER-SELL-123"}
```

**Full close** (`event: "management"`, `action: "full_close"`) — cancels working orders and
liquidates whatever remains, regardless of tracked quantity:
```json
{"event":"management","action":"full_close","side":"SELL","symbol":"MNQ",
 "trade_id":"TS-HUNTER-SELL-123","reason":"Shot ATR-TSL Confirmed Close"}
```

Trades are tracked by `trade_id`, not symbol — several concurrent TS-Hunter trades on the
same symbol never collide. If the bridge restarts and loses track of a trade, `full_close`
still works: it falls back to flattening the symbol on every account the webhook currently
routes to.

---

## How orders are sized

| Strategy | Signal `action` | Order(s) placed | Type | Qty |
|---|---|---|---|---|
| `simple` | `buy` / `sell` | single entry | Market (or Limit if `entry`/`price` given) | payload `qty` (or webhook default) × account multiplier |
| `simple` | `close_all` | cancel working orders + flatten | Market | full position |
| `bracket` | `buy` / `sell` | entry | Market | webhook `default_qty` × account multiplier |
| `bracket` | `buy` / `sell` | tp1, tp2, tp3 (if present) | Limit | webhook `tp_qty` each × account multiplier |
| `bracket` | `buy` / `sell` | sl | Stop | full position |
| `bracket` | `move_sl` | modify the stop order | — | — |
| `bracket` | `trail_active` | resize the stop to the remaining position | — | — |
| any | `set_sl_tp` | place/replace the stop and/or target on the open position | Stop / Limit | current position |
| any | `close_all` | cancel working orders + flatten | Market | full position |
| `ts_hunter` | `signal` | entry | Market | `risk.value` × account multiplier |
| `ts_hunter` | `signal` | sl (if present) | Stop | same as entry qty |
| `ts_hunter` | `partial_close_percent` | market-close `percent`% of what remains + resize sl | Market | `percent`% of current remaining qty |
| `ts_hunter` | `full_close` | cancel working orders + flatten | Market | full position |

Qty defaults/TP qty are set **per webhook** (Webhooks tab); order types (Market/Limit/Stop)
are global, configurable on the **Settings** tab (TS-Hunter is always Market, per its contract).

> **Note on "limit orders":** take-profits are placed as resting **limit** orders. The
> stop-loss is placed as a **stop** order (a limit order at the SL price would fill
> immediately and act as a profit-taker, not protection). The order types are
> configurable if your account/strategy needs different behaviour.

---

## Simulator

The **Simulator** tab lets you rehearse complete trades without sending anything to
Tradovate. Pick a scenario, then **Run all** or **Run next step**:

- *Winning trade — SELL MNQ* — entry → move SL to break-even → trailing → close all
- *Losing trade — BUY MNQ* — entry → stopped out
- *Winning trade — BUY MES* — entry → partial → full take-profit
- *Manual close — SELL MNQ* — entry → manual flatten

Each step runs through the exact same logic as a live webhook, but orders are filled in
an in-memory account shown alongside (simulated positions + working orders). Simulated
orders are tagged **SIM** in the Monitor. No credentials or `trading_enabled` required.

## Accounts, tokens & health

**Token-only, multi-account.** There is no username/password — each **login** is
authenticated by its **own Tradovate access token**. Add logins under
**Settings → Tradovate Accounts**, one row each:

- **Name**, **Environment** (Demo/Live), **Access token** (and optional **Check token**),
  **Enabled** (master switch for that login), and a default **quantity multiplier**.
- Click **Connect & Verify** to authenticate and **discover the trade accounts** under
  that login.

**Multiple trade accounts per login.** A single token often grants access to several trade
accounts. After connecting, every discovered account appears under **Settings → Tradovate
Accounts → Discovered Accounts** — a read-only reference list (Login · Account · Env ·
Status) used to populate each webhook's account picker and to aggregate Open Positions.
**Which accounts actually execute a given strategy's signals, and their qty multiplier, is
chosen per webhook** in the **Webhooks** tab — there is no separate per-account execution
switch (the login-level **Enabled** toggle still disables a whole login).

- The **Dashboard** header shows *Logins* and *Trade accounts* (connected/total);
  **Connection Health** shows each login's token expiry, and **Active Trades** shows one
  row per executing account with its own SL/TP order ids.

Token lifecycle:

- Each account's token expiry is read from its **JWT `exp` claim** (fallback `now + 75 min`).
- A background loop **proactively renews** every token before it expires (≥5 min ahead,
  at least every 25 min) via `renewaccesstoken` — trying the **access token, then the
  check token**. There is no password fallback: if a token can't be renewed, paste a fresh
  one for that account.
- Renewed tokens are **persisted** (best-effort) so they survive a redeploy when a
  persistent disk is attached.
- Configure the loop with *health-check interval* (default 60s; `0` disables). Trigger a
  check on demand with **Check now** or `GET /api/health`.

## Alerts

**Settings → Alerts** — three channels, each with its own on/off switch:

- **Discord** — a webhook URL (Discord channel → *Edit Channel → Integrations → Webhooks*).
  Optionally prefixes every message with `@everyone`.
- **Email** — SMTP, defaults to Gmail (`smtp.gmail.com:587`). Use a Gmail **App Password**
  under your Google Account's security settings, not your normal login password (Gmail
  rejects plain passwords for SMTP). Notify address defaults to your own.
- **Push** — Web Push notifications to your phone or desktop, even when the dashboard is
  closed. Press **Enable on this device** on the Alerts page (the browser asks once for
  permission); each device shows up in *Registered devices* with a test and a remove
  button. **iPhone/iPad:** add the dashboard to the Home Screen first (Share → *Add to
  Home Screen*, iOS 16.4+) and open it from there — Safari only delivers push to
  installed apps. The bridge signs pushes with its own VAPID key pair (generated once,
  stored encrypted); the message text is end-to-end encrypted to the device, so Apple /
  Google never see it. Devices whose subscription expired are pruned automatically.

Seven triggers, each independently toggled (push devices receive every trigger):

| Trigger | Channels | Detail included |
|---|---|---|
| Connection lost | Discord + email + push | which account, environment (demo/live) and error |
| Connection restored | Discord + email + push | which account and environment |
| Trade executed | Discord + push | which webhook/strategy, action, contract, accounts |
| Signal received but not executed | Discord + email + push | which webhook and why execution failed |
| Discord listener offline / back online | Discord + email + push | after a configurable grace period |
| Contract rollover due | Discord + email + push | which contract and when, once per contract |

Connection lost/restored only fires on the actual transition (never on the first
observation of a session, and never twice in a row for the same state) — so you get one
alert when it drops and one when it comes back, not a repeat every health check. A failed
Discord POST or SMTP send is logged as a warning and never blocks a health check or a
trade.

## Discord signal listener

A module (`app/discord_signals/`) that watches one or more Discord channels in which a
signal provider posts trade updates as Discord **embeds**, parses them into structured
signals, and forwards them to configurable webhook targets — typically this bridge, but
any URL works. It runs **inside** the bridge process (same server, port, auth and
deploy), as an isolated supervisor task, so a Discord connection failure can never affect
order execution.

> **Self-bot / ToS note.** Watching a channel you're only a *member* of requires logging
> in with your personal **user token** (a "self-bot"), which violates Discord's Terms of
> Service and can get the account banned. That trade-off is a deliberate choice — the
> module simply implements it. It uses [`discord.py-self`](https://pypi.org/project/discord.py-self/)
> and the **Gateway** (WebSocket push, never polling) so latency stays low.

**Configure it under Settings → Discord Listener** (the live feed is on the Discord tab):

- **Enable listener** and paste your Discord **user token** (stored in your area's
  settings in the SQLite database, masked in the dashboard like every other secret).
- **Global dry-run** — parse and display signals but send to **no** webhook.
- **Channels** — each is a Discord channel ID with a label and one or more **targets**.
  A target is either one of the bridge's **own webhooks** (pick it from a dropdown — the
  signal is handed to that webhook **in-process**, with the same accept/reject semantics
  as a TradingView POST, so it flows straight into your strategy routing) or a **custom
  URL** (for an external logging system or second bridge, with an optional **secret**
  sent as the `X-Webhook-Secret` header). Each target has an on/off toggle. Every
  *enabled* target of a channel receives each signal **in parallel**, each with its own
  5 s timeout and isolated error handling.

All of the above is read **live per event**, so changes take effect without a restart.

**Recognised messages** (by embed title): an **entry** (`… · SELL/BUY <symbol>`), a
**stop/target move** (`… · Stop / target moved · <symbol>`), and a **close**
(`Closed <symbol> · …`). Anything else is shown as **unrecognised** in the live feed and
the event log — never silently dropped — so a change to the provider's format is noticed
immediately.

The **live feed** updates in real time over Server-Sent Events (no polling), showing each
signal, which targets succeeded/failed, the measured latency, and unrecognised raw
messages. The **Send test signal** button pushes a synthetic embed through the full
pipeline to verify fan-out, disabled targets, the secret header and dry-run without a live
Discord connection.

`discord.py-self` is imported lazily: the bridge still boots and the parser/config/test
all work even if it isn't installed (the tab shows "No library"). It's listed in
`requirements.txt`, so a normal install/deploy picks it up.

## Marketplace (share a webhook with other users)

An admin can **publish** one of their webhooks; other users find it on the
**Marketplace** page and **subscribe** — choosing which of *their own* trade accounts
(with a qty multiplier) the signal should trade, and switching the subscription on/off.

- **Publish**: Webhooks → open the webhook → **Sharing** tab → *Publish on the
  marketplace*, with a title, a description and the visibility (**every registered
  user** or **only selected users**). The same tab lists the subscribers (email, on/off,
  routed accounts) with a **Remove** button. The webhook table shows `shared · N`.
- **Subscribe**: Marketplace → *Subscribe* → route accounts + Qty × → save. Your
  subscriptions are listed under Webhooks → *Subscribed signals* (toggle, manage,
  unsubscribe).
- **Execution**: when a TradingView alert hits the published webhook it runs in the
  publisher's area as usual **and** is forwarded to every enabled subscription, each in
  the subscriber's own area — their accounts, their **Trading** switch, their symbol
  mapping, their alert channels and their logs. Subscribers never see the publisher's
  URL, token or accounts; the publisher never sees the subscribers' accounts. A failure
  on one side never affects the other. Subscribers' own webhook passphrase is not
  applied (the publisher's webhook already authenticated the alert).
- **Test signals** stay in the publisher's area unless *Also forward to marketplace
  subscribers* is switched on (confirmation required).
- Unpublishing pauses subscriptions; deleting the webhook removes them. Publish,
  subscribe, unsubscribe and removals are recorded in the admin audit log.

Subscriptions are stored in the `subscriptions` table; the sharing config lives on the
webhook itself (`sharing` key), so v4 data stays compatible.

## Users, areas & login (multi-tenant)

Fluxbridge is **multi-user**. Each user signs in with **email + password** and gets
their own **isolated area** — token accounts, webhooks (each with its own URL token),
Discord listener, symbol map, alerts and logs are all private to that user. Nothing
is shared between areas (shared areas are planned for a later release).

- **First run:** open the dashboard and you're sent to **`/setup`** to create the
  **first admin** account. Any pre-existing single-user `data/settings.json` is
  migrated into that admin's area.
- **Invite-only:** there is no open sign-up. An admin creates **invite links** under
  **Settings → Users** (optionally granting admin, optionally emailed). Share the link;
  the new user registers and gets their own area.
- **Admin** can list users, grant the *Discord Signals* feature per user, create/revoke
  invites, issue password-reset links, delete users (which removes their area and data),
  and review the admin **audit log**.

Storage is a **SQLite** database at `<NEXUSPRED_DATA_DIR>/fluxbridge.db` (users, areas,
memberships, invites). Passwords are salted **PBKDF2** hashes. The login session is a
signed, HTTP-only cookie — no server-side store, no extra dependency. Use **Sign out**
(top-right). Set `SESSION_SECRET` to pin the cookie-signing key across restarts (else
it's generated and stored). The `/webhook/<token>` and `/healthz` paths are never
behind login; a webhook token routes to whichever user's area owns it.

## Symbol mapping

TradingView sends continuous symbols like `MNQ1!`. **Settings → Symbol Mapping**
maps each one to the **exact Tradovate contract** used for orders — update it after every
rollover. Defaults:

| TradingView | Tradovate |
|---|---|
| `NQ1!`  | `NQU6`  |
| `MNQ1!` | `MNQU6` |
| `ES1!`  | `ESU6`  |
| `MES1!` | `MESU6` |
| `GC1!`  | `GCM6`  |
| `MGC1!` | `MGCM6` |

You can also enter a bare root (e.g. `MNQ`) instead of a dated contract — the bridge then
auto-resolves the front month. Symbols not in the mapping fall back to the stripped root
and are gated by the **Allowed symbols** list.

---

## Auto-updates

- The dashboard checks GitHub (`tobiasgiger/nexuspred`) for the latest **release tag**,
  falling back to the `VERSION` file on the default branch.
- When the remote version is newer, the **Update available** button appears in the header.
- Clicking it runs `git fetch` + `git reset --hard origin/<branch>`, refreshes
  dependencies, and **re-execs** the process so it boots on the new code.
- Requires the app to be running from a `git` checkout. Override the tracked branch with
  the `NEXUSPRED_BRANCH` environment variable (default `main`).

To cut a new release, bump `VERSION` (and optionally tag it `vX.Y.Z`). While there are
no GitHub releases, the updater compares against the `VERSION` file on the tracked
branch, so every push to `main` that bumps `VERSION` shows up as an update.

### "Not a git checkout" — connecting a ZIP download

The Update button needs the install folder to be a Git checkout. If you downloaded a
ZIP instead of `git clone`, run the one-time helper in the install folder:

- **Windows:** double-click `connect-git.bat`
- **Linux/macOS:** `./connect-git.sh`

It initialises Git, points the folder at this repo, and resets the **code** to the
latest `main` — your `data/` settings are git-ignored and left untouched. After that,
the dashboard **Update** button works.

---

## API reference

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/webhook/{token}` | Receive a TradingView alert for a specific webhook (202 accepted, processed in the background) |
| `GET`  | `/api/status` | Connection + trading status, trade accounts, active trades |
| `GET/POST` | `/api/settings` | Read / update settings (secrets masked; only the keys you send change) |
| `GET`  | `/api/orders` `/api/signals` `/api/events` | Rolling logs |
| `POST` | `/api/agent/pair` | Exchange a one-time pairing code for an agent token (unauthenticated, rate-limited) |
| `GET`  | `/api/agent/jobs` | Agent long-poll for relay jobs (agent token) · `POST /api/agent/jobs/{id}/result` delivers the answer |
| `GET`  | `/api/agents` | Paired agents with online state · `POST /api/agents/pairing-code`, `PUT`/`DELETE /api/agents/{id}`, `GET /api/agents/download.zip` (admin) |
| `GET`  | `/api/pnl` | Live account P&L: today's realised, open, week, cash per account (`?refresh=1` polls Tradovate now) |
| `GET`  | `/api/journal/overview` | Journal stats, per-period buckets, equity curve for a filter slice (`range`/`frm`/`to`, `account`, `symbol`, `side`, `period`) |
| `GET`  | `/api/journal/calendar` | Daily net P&L for a month (`month=YYYY-MM`) |
| `GET`  | `/api/journal/trades` | Imported round-trip trades (filters as above, `limit`, `before`) |
| `PUT`  | `/api/journal/trades/{id}` | Set a trade's `note` / `tags` |
| `POST` | `/api/journal/import` | Import fills / trades / snapshots from Tradovate now |
| `POST` | `/api/journal/import-csv` | Back-fill from a Tradovate Performance / Orders / Fills CSV export (multipart `file`, `account`, `timezone`, `fee_per_side`) |
| `GET`  | `/api/journal/imports` | Import history |
| `GET`  | `/api/journal/snapshots` | Daily account cash / P&L snapshots |
| `GET`  | `/api/journal/export.csv` | CSV export of the filtered trades |
| `GET`  | `/api/history/signals` | Persisted signals, newest first; `limit`, `before` (cursor), `result`, `q` |
| `GET`  | `/api/history/orders` | Persisted orders; `limit`, `before`, `symbol`, `account` |
| `GET`  | `/api/history/stats` | Per-day signal outcomes + order counts for the last `days` (default 7) |
| `POST` | `/api/rollover/check` | Re-run the contract-rollover check for the caller's area |
| `GET`  | `/api/stream` | Live feed (Server-Sent Events): `event`, `signal`, `order`, `session`, `discord`, `pnl` messages + `ping` heartbeat |
| `POST` | `/api/flatten-all` | 🆘 Cancel every working order and flatten every position on all accounts (ignores the trading switch) |
| `POST` | `/api/alerts/test` | Send a test notification on every enabled alert channel |
| `GET`  | `/api/push/public-key` | VAPID public key for `PushManager.subscribe` · `GET /sw.js` serves the service worker (public) |
| `POST` | `/api/push/subscribe` | Register this device's push subscription · `DELETE` removes it (`{endpoint}` or `{id}`) · `GET /api/push/subscriptions` lists the area's devices · `POST /api/push/test` sends a test push |
| `GET`  | `/api/positions` | Live Tradovate positions |
| `POST` | `/api/connect` | Reload sessions & verify every token account |
| `GET/POST` | `/api/token-accounts` | List / save logins (tokens, enable flags & default multipliers) |
| `GET/POST` | `/api/trade-accounts` | Overview / save per-account execution on-off & multipliers |
| `GET`  | `/api/health` | Check every connection (renews tokens if needed) |
| `GET/POST` | `/api/webhooks` | List all webhooks / create one |
| `PUT/DELETE` | `/api/webhooks/{id}` | Update / delete a webhook (name, strategy, qty, accounts) |
| `POST` | `/api/webhooks/{id}/regenerate-token` | Rotate a webhook's secret token |
| `POST` | `/api/webhooks/{id}/test` | Run a payload through the pipeline for this webhook (`?subscribers=true` also forwards it) |
| `PUT`  | `/api/webhooks/{id}/sharing` | Publish / unpublish on the marketplace (admin): title, description, visibility, allowed users |
| `GET/DELETE` | `/api/webhooks/{id}/subscribers[/{sub_id}]` | List / remove subscribers of a published webhook (admin) |
| `GET`  | `/api/marketplace` | Published webhooks visible to me (with my subscription, if any) |
| `POST` | `/api/marketplace/{area}/{webhook_id}/subscribe` | Subscribe (routed accounts + enabled) |
| `GET/PUT/DELETE` | `/api/subscriptions[/{id}]` | My subscriptions: list / update / unsubscribe |
| `GET`  | `/api/scenarios` | List built-in simulator scenarios |
| `POST` | `/api/simulate` | Run a signal in simulation (no broker) |
| `GET`  | `/api/simulate/state` | Simulated positions & working orders |
| `POST` | `/api/simulate/reset` | Clear the simulated account |
| `GET`  | `/api/update/check` | Check GitHub for a new version |
| `POST` | `/api/update/apply` | Pull latest & restart |
| `GET/POST` | `/api/discord/config` | Read / save the Discord listener config (secrets masked) |
| `GET`  | `/api/discord/status` | Listener state, connection & watched channels |
| `GET`  | `/api/discord/signals` | Recent Discord signal events (ring buffer) |
| `GET`  | `/api/discord/stream` | Live signal feed (Server-Sent Events) |
| `POST` | `/api/discord/test` | Push a synthetic embed through the pipeline |
| `GET`  | `/api/extension/token-extractor.zip` | Download the browser token-extractor extension |
| `GET/POST` | `/setup` · `/login` · `/register` · `/reset` · `/logout` | User auth (first-admin setup, login, invited signup, password reset, logout) |
| `GET`  | `/api/me` · `/api/users` · `/api/invites` · `/api/audit` | Current user / admin user management / admin audit log |
| `POST` | `/api/account/password` · `/api/users/{id}/reset` · `/api/users/{id}/features` | Change own password / admin reset link / feature grant |

---

## Configuration & data

Runtime settings live **per area** in the SQLite database at
`<NEXUSPRED_DATA_DIR>/fluxbridge.db` (default `data/`, git-ignored, never committed). A
pre-multi-tenant `data/settings.json` is migrated into the first admin's area on setup.
Secrets are masked in the dashboard and never sent back to the browser in plain text.
Environment variables are documented in [`.env.example`](.env.example).

## Architecture (v5)

```
run.py                  uvicorn entry point (one worker — runtime state is in-process)
app/main.py             app factory: auth middleware, lifespan (loops, HTTP pool), routers
app/routers/            auth, users, core (status/settings/logs/stream), accounts,
                        webhooks (ingress + CRUD), simulator, updater, extension
app/signals.py          signal entry point: validation, per-trade locks, active-trade tracking
app/engine/             strategy handlers: simple, bracket, manage (close_all/set_sl_tp), ts_hunter
app/tradovate.py        token sessions, per-account executors, diff-based SessionManager
app/http.py             pooled keep-alive httpx clients (tradovate / outbound)
app/health.py           token-renewal + Discord health loops (all areas concurrently)
app/config.py           per-area settings (deep-copied reads, atomic update(), webhook-token index)
app/db.py / auth.py     SQLite (per-thread connection, cached auth lookups), signed-cookie sessions
app/state.py            per-area rolling logs, session status and the live-stream bus
app/discord_signals/    parser → pipeline → dispatcher (in-process for bridge webhooks) + listener
templates/, static/     shell + auth templates; ES-module dashboard (no build step)
tests/                  pytest characterisation suite — run with `pip install -r requirements-dev.txt && pytest`
```

Everything is I/O-bound and runs on one asyncio loop: blocking work (PBKDF2, SMTP,
SQLite) is kept off the loop, and independent broker calls are issued concurrently. Run a
**single uvicorn worker** — sessions, active trades and live subscribers are in-process.

## Upgrading from v4 / rollback

v4.11 (branch `backup/v4.11.0`) and v5 (`main`) have **identical database schema and
settings keys**: a v4 data directory starts unchanged under v5 and stays readable by v4,
logins stay valid, webhook URLs and Tradovate tokens carry over — nothing to re-enter.

- **Render**: keep the service and its disk; set the service's **Branch** to `main` and
  deploy. No `/setup`, no migration. Rollback = set the branch to `backup/v4.11.0`.
- **Self-hosted**: `git fetch && git checkout main && pip install -r requirements.txt`,
  restart. Rollback = `git checkout backup/v4.11.0`.
- Deploy while **flat**: a restart (any version) drops the in-memory trade tracking, so
  stop/partial-close signals for trades opened *before* the restart are skipped.
- **Never run two instances against the same `NEXUSPRED_DATA_DIR` at the same time.**
  Both would renew the same Tradovate tokens and execute the same webhooks twice. For a
  side-by-side comparison, **copy** the data directory and enable **Trading** in only one
  instance.

## Disclaimer

Trading futures involves substantial risk. This software is provided as-is, without
warranty. Test thoroughly on a **demo** account before enabling live trading.

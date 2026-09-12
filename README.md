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
  - `close_all` → cancels that contract's working orders and **flattens the whole position** (stops/targets of other symbols on the same account are left alone).
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
- **Copy trading** (Routing → Copy Trading): mirror one leader trade account onto any
  number of follower accounts in real time — entries, adds, reductions, closes and
  reversals — sized by multiplier or a fixed number of contracts, with a symbol filter,
  a direction filter, a per-follower cap and an automatic *flatten the followers* once
  the leader feed has been lost for X seconds — see below.
- **Discord signal listener** (configured under Settings → Discord Listener; live feed on
  the Discord tab): watches Discord channels over the
  Gateway with a personal user token (self-bot) and fans parsed trade signals out to
  configurable webhook targets in parallel — see below.

---

## Quick start

### ProjectX accounts (TopstepX, Bulenox, Alpha Futures … — beta)

Logins can point at a **ProjectX** gateway: Settings → Broker Accounts → Broker
*ProjectX*, user name, API key and the firm (`topstep`, `bulenox`, `alphaticks`, …).
Webhooks, copy trading, risk guard and P&L work the same; the bridge polls the REST API.
Not yet verified with a real key; see [docs/PROJECTX.md](docs/PROJECTX.md).

### Rithmic accounts (beta)

Logins can point at **Rithmic** (Apex, Topstep, MFFU … on R|Trader) instead of Tradovate:
Settings → Broker Accounts → Broker *Rithmic*, then user, password, the system name your
firm gives and the gateway (`chicago`, `europe`, `paper`). Everything else — webhooks, copy
trading, risk guard, P&L — works the same. Not yet verified against a live Rithmic system;
see [docs/RITHMIC.md](docs/RITHMIC.md) for the checklist and limits.

### Own Linux server (one line, HTTPS included)

```bash
curl -fsSL https://raw.githubusercontent.com/tobiasgiger/nexuspred/main/deploy/install-server.sh | sudo bash -s -- --domain bridge.example.com
```

Installs Caddy (automatic Let's Encrypt certificate), a systemd service, daily backups and
the `fluxbridge` helper command; then open `https://bridge.example.com/setup`. Moving from
Render: **Settings → Updates → Download backup**, then `sudo fluxbridge restore FILE`.
Details: [docs/SELF-HOSTING.md](docs/SELF-HOSTING.md). How the bridge stays fast and what keeps it safe: [docs/PERFORMANCE.md](docs/PERFORMANCE.md), [docs/SECURITY.md](docs/SECURITY.md).

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
long-polls the bridge over outbound HTTPS. Assign a login to it under **Broker Accounts → Execute via** and
every Tradovate call of that login — orders, token renewal, health checks, P&L — is
executed by the agent from its IP. An offline agent makes those calls fail loudly (event
log + alert); the bridge never silently falls back to its own address. Setup steps for
a Windows VPS are in `agent/README.md`.

### Trading journal

The **Journal** page turns your broker fills into a P&L journal. Once a day after the
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

**ProjectX and Rithmic logins** are imported by the same run. Neither exposes Tradovate's
fill pairs, so their executions are read as fills and paired first-in-first-out per
account and contract (the pairing the Tradovate importer also falls back to): ProjectX
through `Trade/search` per account (each row one execution with size, price, side and the
broker's fees), Rithmic through the order plant's fill history (no fees reported — *Fee
per contract per side* applies). An account's first run reaches back *History to import
(days)* (at most a year), later runs re-read the last 7 days; stored fills and trades
are idempotent, so overlap never duplicates. Contract value per point comes from the
ProjectX tick size / tick value, for Rithmic from the built-in table by product root.

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
- **Signal ingress**: at most 60 signals per webhook per 10 seconds (HTTP 429) and 256
  queued signal tasks bridge-wide (HTTP 503), so a leaked webhook URL cannot queue unbounded
  broker work; every price and quantity in a payload is validated before the first broker
  call, quantities are capped at 1000 contracts.
- **Password reset**: a reset link changes the password only — it never wipes the second
  factor and never signs the user in (the new password and the authenticator code are
  asked for at the next sign-in). A lost authenticator is recovered by an admin's *Reset 2FA*.
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

1. Go to **Settings → Broker Accounts** → add one login per Tradovate account with its
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

### Trading window (per webhook)

Each webhook can carry a **trading window** (drawer → General): a local time range and
a set of weekdays inside which *entries* run — `buy` / `sell` and TS-Hunter `signal`
events. Outside it the entry is answered with `skipped` / `trade_window` and logged
(with the window and the current local time), while `close_all`, `set_sl_tp`, `move_sl`,
`trail_active` and TS-Hunter management events always run — the window closes the door
for new risk, it never traps an open position. An end before the start spans midnight
(22:00 → 06:00: the weekday check applies to the evening the window opens on); an empty
timezone follows the journal timezone. The simulator ignores the window. The window
travels in the settings export.

### `simple` strategy webhooks

Just `action`, `symbol` and (optionally) `qty` — no TP/SL:

```json
{"action":"buy","symbol":"MNQ1!","qty":2}
```

Omit `qty` to use the webhook's configured default. `close_all` flattens the position and
cancels only that contract's working orders, so positions in other symbols on the same
account keep their stops (the SOS **Flatten all** button remains account-wide):

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

**Full close** (`event: "management"`, `action: "full_close"`) — closes **this trade**: on every
tracked account its own stop is cancelled and its remaining quantity is closed at market.
Positions of other trades (or manual ones) in the same contract stay; accounts the webhook
routes to that the trade record does not list are never touched — when they hold the
contract, the log and an alert say so:
```json
{"event":"management","action":"full_close","side":"SELL","symbol":"MNQ",
 "trade_id":"TS-HUNTER-SELL-123","reason":"Shot ATR-TSL Confirmed Close"}
```

Trades are tracked by `trade_id`, not symbol — several concurrent TS-Hunter trades on the
same symbol never collide. If the bridge restarts and loses track of a trade, `full_close`
still works: it falls back to flattening the symbol on every account the webhook currently
routes to (there is no tracked quantity to isolate on).

**Protective stop that cannot be placed.** For `bracket` and `ts_hunter` entries the stop is
retried once (waiting out a rate-limit penalty). When it fails again the entry is **closed
again at market**: the trade's own targets are cancelled first (every working order of the
contract when the stop's outcome is unknown, since a stop that did reach the broker would
open a reverse trade), the entered quantity is flattened, the account is not tracked for
that trade and the operator is alerted (*Entry closed again*). A resting limit entry is
cancelled instead and only the part the broker shows as filled is closed. Only when that close fails
too does the position stay live, reported as *Unprotected position* on every channel — it
stays tracked so `close_all` / `full_close` reach it.

---

## How orders are sized

| Strategy | Signal `action` | Order(s) placed | Type | Qty |
|---|---|---|---|---|
| `simple` | `buy` / `sell` | single entry | Market (or Limit if `entry`/`price` given) | payload `qty` (or webhook default) × account multiplier |
| `simple` | `close_all` | cancel *this contract's* working orders + flatten | Market | full position |
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
| `ts_hunter` | `full_close` | cancel the trade's stop + close its remaining qty (isolated) | Market | tracked remaining qty |

Qty defaults/TP qty are set **per webhook** (Webhooks tab); order types (Market/Limit/Stop)
are global, configurable on the **Settings** tab (TS-Hunter is always Market, per its contract).

> **Note on "limit orders":** take-profits are placed as resting **limit** orders. The
> stop-loss is placed as a **stop** order (a limit order at the SL price would fill
> immediately and act as a profit-taker, not protection). The order types are
> configurable if your account/strategy needs different behaviour.

---

### Per-account sizing (webhooks and marketplace subscriptions)

Each routed account on a webhook (Webhooks → Accounts) and each account of a marketplace
subscription has its own **Sizing** rule — the same three as copy trading:

| Mode | Contracts |
|---|---|
| **Same** | exactly what the signal carries (1:1) |
| **Multiplier** | signal × factor, rounded half up, never below 1 |
| **Fixed** | always this many contracts for the entry; bracket take-profit slices are scaled proportionally (fixed 2 for an entry of 3 → a TP slice of 1 stays 1, the stop is 2) |

**Max** caps the result (0 = no cap). Older entries that only carried `qty_multiplier`
keep working: a factor other than 1 is *Multiplier*, 1 is *Same*.

## Language (German / English)

The dashboard speaks German and English. By default it follows the browser's language
(`Accept-Language` / `navigator.languages`: German → Deutsch, everything else → English);
**Settings → General & Trading → Display → Language** forces one for the workspace
(*Browser default*, *Deutsch*, *English*). The page reloads after a change, the sign-in
/ setup / reset pages use the same choice (via the `fb_lang` cookie, else
`Accept-Language`), and dates and numbers are formatted for the chosen language
(`de-CH` for German).

How it works: English is the source language in the code (`t("Save logins")`); the
German dictionary lives in `static/js/locales/de.js` (English text → German) and
`app/i18n.py` for the server-rendered auth pages. A missing entry falls back to English,
`tests/test_i18n.py` fails when a sentence-like UI string has no German entry. Log events
and API error texts stay English (they are also read by tooling and support).

**Alerts** (Discord, email, push, daily summary) are sent in the workspace's language:
the forced setting when one is chosen, otherwise the language the dashboard last ran in
(the browser default the dashboard reports on load). A workspace that has never opened
the dashboard alerts in English.

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

Every login has a permanent id (`lid`). Webhook routes, copy groups and subscriptions
point at logins by that id, so the login table can be reordered or shortened without a
route landing on another login.


**Token-only, multi-account.** There is no username/password — each **login** is
authenticated by its **own Tradovate access token**. Add logins under
**Settings → Broker Accounts**, one row each:

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

## Tradovate request budget

Tradovate rate-limits the API per login (HTTP 429 with a penalty time). Everything the
bridge asks a login for goes through one paced channel per login (at most 5 requests/s,
one at a time) that also honours a running penalty, so no loop can get a login banned on
its own. Who asks what:

| Loop | Calls per login | Cadence |
|---|---|---|
| Live P&L (`app/pnl.py`) | positions ×1, cash snapshot per account **with a position** (flat accounts every 6th tick), risk record (cached 5 min) | `pnl_poll_seconds` (default 5 s) while a dashboard is open, trade alerts or a risk rule are on; else 60 s |
| Health (`app/health.py`) | `/auth/me` ×1 | `health_check_interval` (60 s) |
| Copy trading (`app/copy/`) | positions ×1 (+ orders every 2nd poll) on the **leader** login | 10 s while the socket is synced, 2 s while it is down; slower after a 429 |
| Journal import | fills / orders / cash per account | nightly + the Performance report |
| Rollover | contract lookups | once a day |
| Signals, risk guard, flatten | orders, cancels, liquidations | on demand |

---
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

Eleven triggers, each independently toggled (push devices receive every trigger):

| Trigger | Channels | Detail included |
|---|---|---|
| Connection lost | Discord + email + push | which account, environment (demo/live) and error |
| Connection restored | Discord + email + push | which account and environment |
| Trade executed | Discord + push | which webhook/strategy, action, contract, accounts |
| Signal received but not executed | Discord + email + push | which webhook and why execution failed |
| Discord listener offline / back online | Discord + email + push | after a configurable grace period |
| Contract rollover due | Discord + email + push | which contract and when, once per contract |
| Position opened / added | Discord + push | seen on the broker side (stop/target fills and manual trades included): account, symbol, direction, size, price |
| Position closed / reduced | Discord + push | account, symbol, direction, size, **realised P&L** (the broker's figure for that close) and how long it was open |
| Execution agent offline / back online | Discord + email + push | a paired VPS agent stopped or resumed polling |
| Daily summary | all channels | once a day at a configurable local time: realised P&L per account, trades closed, wins / losses |

The Overview's **Today's P&L** card lists every account with realised / open / weekly
P&L, balance and the **max trailing drawdown**: room left to the liquidation threshold,
the threshold, and EOD vs Intraday trailing. Tradovate only exposes the drawdown size and
the cap, so the bridge tracks the account's peak itself — Intraday: highest equity incl.
open P&L; EOD: highest session close, seeded from the journal's daily balances — and
derives threshold = min(peak, cap) − size. Peaks before the bridge started watching are
unknown, so use ✎ on the row to **pin the threshold your prop firm shows**; from then on
the figures are exact (an ≈ marks unpinned rows). Columns sort on click; *Hide idle*
removes accounts that did nothing today.

**Accounts** on the same page picks which trade accounts may raise account-level alerts
(position opened / closed, signal executed, daily summary) — leave *All accounts* on, or
tick a few when you mirror to many accounts and only want to hear about the leaders.
Connection alerts are per login and always fire.

Position alerts come from polling the broker's position list every few seconds (the same
poll that feeds the Overview's live P&L, `pnl_poll_seconds`), so they fire even when the
trade was not placed by the bridge. Positions already open when the bridge starts are
taken as the baseline and not announced.

Trades that reach the journal from more than one source (live fill pairs, the daily
Performance report, a CSV upload) are stored once: the importer recognises the same round
trip by its broker fill ids or by account / symbol / side / size / prices and exit time.
**Journal → Remove duplicates** collapses anything older imports stored twice, keeping notes.

**Privacy mode** (the eye icon in the top bar) masks account names on screen — the first
six characters stay, the rest become asterisks — handy for screenshots and streaming. It is
a display setting per browser; alerts, exports and the API are unaffected.

Connection lost/restored only fires on the actual transition (never on the first
observation of a session, and never twice in a row for the same state) — so you get one
alert when it drops and one when it comes back, not a repeat every health check. A failed
Discord POST or SMTP send is logged as a warning and never blocks a health check or a
trade.

**External watchdog** (Settings → Alerts → *External watchdog*): the bridge sends a plain
GET to a URL you monitor elsewhere — a healthchecks.io check, an Uptime Kuma *push*
monitor, cronitor — every *Ping interval* seconds (30–3600, default 60). That service alerts
*you* when the pings stop: the one failure the bridge cannot report itself (process gone,
host asleep, network down, Render instance stuck). Anything below HTTP 400 counts as
delivered; the last outcome (time, delivered / failed, error) is shown under the fields and
in `/api/status` (`heartbeat`). Set the monitor's grace period to about twice the interval.
The URL goes through the same outbound-address check as the Discord webhook (no private
or loopback targets) and never leaves the server in a settings export.

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
  applied — the publisher's passphrase is verified **before** anything is forwarded, so a
  signal that fails it reaches no subscriber at all.
- **Test signals** stay in the publisher's area unless *Also forward to marketplace
  subscribers* is switched on (confirmation required).
- Unpublishing pauses subscriptions; deleting the webhook removes them. Publish,
  subscribe, unsubscribe and removals are recorded in the admin audit log.

Subscriptions are stored in the `subscriptions` table; the sharing config lives on the
webhook itself (`sharing` key), so v4 data stays compatible.

---
### Verified track record

Every published signal and copy group carries a **track record** on its marketplace card
(trades, win rate, profit factor, net P&L, last 30 days, max drawdown) and a detail drawer
(equity curve, by month, by symbol, streaks, signal counts). It is built from the
publisher's own **trading journal** — round trips paired from broker fills the bridge
imported itself, never a figure the publisher typed in. A webhook's record covers the
trade accounts it routes to (so it includes anything else those accounts traded); a copy
group's record is the leader account. Trades that came from a CSV upload count towards a
lower *verified share* and the badge turns amber. Subscribers never see the publisher's
account names. Records are cached for two minutes. `GET /api/marketplace/{area}/{webhook_id}/record`,
`…/copy/{group_id}/record`; publishers see their own under `GET /api/webhooks/{id}/record`
and `GET /api/copy/groups/{id}/record`.

### Subscription journal

Routing → Subscription journal: one card per subscription / copy follow with the signals it
delivered and how they ended (executed / skipped / error, last signal), or the mirrored copy
events for your accounts, and your own P&L on the routed accounts since you subscribed.
`GET /api/subscriptions/{id}/journal`. Signal rows now carry the webhook id
(`signal_log.webhook_id`), so a subscription's signals are counted exactly even when the
publisher renames the signal.

### Subscriber controls, publisher controls, discovery, latency fairness

**Subscriber controls** (in the subscribe drawer, *My limits for this signal*): only these
symbol roots, a max number of contracts per signal and account (caps the per-account
sizing), a max number of entries per UTC day (closes are never blocked), a trading window
of your own (entries only) and *switch off after N consecutive errors* — an error is a
failed signal, an `error` result or an entry that reached none of your accounts; the
subscription turns itself off and you get an alert on every channel. Skipped signals are
logged with the reason (`subscription_symbols`, `subscription_daily_cap`, `trade_window`).

**Publisher controls** (webhook / copy-group *Sharing* tab): pause forwarding (the listing
stays, marked as paused), approve new subscribers (they wait as *pending* and receive
nothing until approved), a subscriber limit (409 when reached; existing subscribers stay)
and up to five tags. Per subscriber: approve, pause, resume, remove. `PUT
/api/webhooks/{id}/subscribers/{sub_id} {status}` and `PUT /api/copy/groups/{id}/subscribers/{sub_id}`.

**Discovery**: the marketplace page searches title, description, publisher and tags, sorts
by best last 30 days / net P&L / win rate / trades / subscribers / newest, filters by
kind and *broker-verified only*, and tag chips narrow the list. Listings show the
publish date and `subscribers/limit`.

**Latency fairness**: a published signal is fanned out to its subscribers in a random
order on every signal, so nobody is systematically first in the queue; the publisher's
own execution is never delayed by the fan-out. Every signal row records the wall time
from acceptance to the broker's answer (`signal_log.latency_ms`); the subscription journal
shows p50 / p95 per subscription and the track record shows the publisher's own execution
latency.

### Paid subscriptions (Stripe)

Settings → Payments (admin) connects the **operator's** Stripe account: a secret (or
restricted) key, the webhook signing secret, the currency and a default trial. With the
switch on, a publisher can put a monthly price and a free trial on a listing (Sharing tab).
A subscriber who subscribes to a paid listing is created as `unpaid` — nothing is forwarded
— and sent to Stripe Checkout (subscription mode, the trial applied); the webhook
(`POST /api/payments/webhook`, signature-verified, `checkout.session.completed`,
`customer.subscription.created / updated / deleted`, `invoice.payment_failed`) flips the
subscription to `active` (or `pending` when the publisher approves by hand) and back to
`unpaid` when the Stripe subscription lapses, fails or is cancelled. *Manage billing* opens
the Stripe customer portal. The admin switch off makes every listing free again. Money
lands in the operator's Stripe account; settling with publishers happens outside the
bridge. Stripe is called over plain HTTPS (no SDK); secrets are encrypted at rest.
Selling trading signals may be regulated where you and your subscribers live — check the
rules that apply before switching this on.

## Rollover with confirmation

Dated contracts in the symbol map (`MNQU6`) expire. The daily check flags every mapped
contract within `rollover_warn_days` of its roll date (exchange-convention estimate, or the
broker's expiry when a login is connected) and **proposes the next contract** — taken from
the broker's own listing when connected (`/contract/suggest`, first month after the current
one, with its expiry), otherwise estimated (next quarter for index / FX / treasuries /
crypto, next month otherwise; never a month that is itself already past). The proposal is
shown on the Overview banner and under **Settings → Symbol Mapping → Rollover due**, where
each row can be edited or unticked; **Apply selected rollovers** asks for confirmation and
then rewrites the mapping — nothing changes without that click. New signals trade the new
contract immediately; open positions and working orders on the old contract are left
alone. The rollover alert (Discord / email / push) points to that page.

---
## Automations (per workspace)

Settings → Automations: rules the bridge runs on its own — *when* something happens,
*then* do one thing. Events come from the internal event bus (the same one the alerts
listen on): position closed (with its P&L), position opened, risk guard fired, execution
problem, signal received but not executed, signal executed, connection lost / restored,
news lock, execution agent offline, copy-trading alert, Discord listener offline, daily
summary. Filters: accounts, symbol roots, webhooks, and for closes a minimum loss.
Actions: notify (Discord + email + push), switch trading OFF, flatten the account,
flatten + lock the account for today (like the risk guard), flatten everything, pause the
webhook. Each rule has a cooldown (default 60 s) and fires at most once per cooldown; every
firing is logged under Logs, announced on the alert channels and listed under *Recent
firings*. Rules travel with the settings file export; `GET/PUT /api/automations`.

Example: *Position closed · loss ≥ 200 · → switch trading OFF* stops every strategy after
one bad trade; *Connection lost · → notify* with a custom message; *Execution problem · →
flatten the account* closes what is left when a stop could not be placed.

## Order ticket & exposure (Overview)

The Overview carries an **order ticket**: pick a connected trade account, type a symbol
(the symbol map is applied — `MNQ1!` becomes the mapped contract; anything else goes to
the broker as typed), Buy / Sell, quantity (1–100), Market / Limit / Stop / StopLimit.
A confirmation names the account (red for a live account). The order takes the same path
as a signal: the Trading switch must be on, the account's risk lock holds, and it shows up
under Recent orders and in the history. No stop is attached — you manage the position.
Every open position has a **Close** button: the contract's working orders are cancelled
first, then the position is liquidated; the bridge stops managing that trade on that
account. Both actions are written to the admin audit log.

The **Exposure** card sums the open positions across all accounts per symbol root: long /
short / net contracts, number of accounts, notional at the average entry price
(contracts × price × value per point) and each root's share. It flags a root that is long
on one account and short on another (hedged across accounts) and a root above 60 % of the
notional. `GET /api/exposure` returns the rows and the summary in one broker poll.

## Metrics (Prometheus)

Set `NEXUSPRED_METRICS_TOKEN` and `GET /metrics` answers in the Prometheus text format
with `Authorization: Bearer <token>` (a session cookie is not accepted; without the
variable the path is a 404). Gauges: `fluxbridge_up`, uptime, version, broker connection
per login, active trades and live-feed subscribers per area, queued signals, users.
Counters fed by the event bus: events per kind, signals per outcome, trades, execution
problems, risk triggers, connection changes, automations fired, copy alerts, signal
failures. Histogram `fluxbridge_signal_seconds` — wall time from webhook acceptance to
the broker's answer, per outcome.

## Risk guard (per trade account)

Settings → Broker Accounts → *Discovered trade accounts* → **Risk guard** sets, per
account, a **daily loss limit**, a **daily profit target** and a **flatten time**. The
guard rides on the live P&L poll (a few seconds while a rule is set) and uses the
broker's own figures — today's realised + open P&L. When a rule fires the account is
**flattened** (every working order cancelled, every position closed at market) and
**locked** for the rest of the Tradovate trading day (rolls at 17:00 New York, like the
broker's daily P&L); the flatten time is read in the journal timezone or, per account,
in New York time: the bridge refuses every
order for it — webhooks, Discord signals, marketplace subscriptions and copy-trading
mirrors alike, because the check sits in the one place all of them place orders — and a
position that reappears (a manual trade) is closed again on the next poll. The lock
clears with the next day or with **Unlock for today** (the rules stay). Locked accounts
carry a `locked` tag on the Overview; the *Risk guard fired* alert goes to all channels.
Bridge-placed orders that the guard refuses show up as rejected in the order log.

---
## Copy trading (mirror a leader account onto followers)

**Routing → Copy Trading** mirrors the *positions* of one **leader** trade account onto
any number of **follower** accounts — your own or third-party accounts whose login you
hold. It works on the broker's position, not on signals, so it also copies trades the
leader places by hand in the Tradovate UI, stop / target fills and manual closes.

- **Group** = leader account + optional symbol filter (roots picked from Settings → Symbol
  Mapping, e.g. `MNQ, ES`, or typed by hand) +
  followers. Each follower has a **mode** — *multiplier* (leader size × factor, rounded,
  never below 1 while the leader holds) or *fixed* (N contracts for the leader's entry;
  with *Fixed mode follows adds* on, 2 fixed contracts become 4 when the leader doubles
  up) — plus an optional **max** cap and a **direction** filter (both / long / short).
- **Mirror rule**: on every leader position change the bridge computes each follower's
  target net position and sends **one market order for the difference**. That one rule
  covers opening, adding, reducing, closing and reversing, and it is self-healing: a
  partial fill, a rejected order or a missed event is corrected on the next change or by
  the 10-second **reconcile**, which compares the followers' broker positions with the
  expected ones and fixes drift (the event log shows `drift`).
- **Feed**: the leader's orders and positions are read over REST **once a second** —
  one read per *login*, not per group: groups leading from accounts of the same login
  share each snapshot (a group asking within 0.8 s reuses it, groups asking at the same
  moment wait for the one request in flight), so three groups on one login cost one
  poll against its rate budget; `feed_shared` in the group's diagnostics counts the reuse —
  through the execution agent where the login uses one — and that poll alone decides
  whether the feed is up. Where possible (*Feed: Auto* / *WebSocket*, direct logins)
  Tradovate's WebSocket **user sync** runs beside it as an accelerator: its position events
  are applied the moment they arrive (~100 ms after a fill), but losing the socket is only
  logged (`ws_lost` / `ws_up`), never treated as a lost feed. The table shows the feed state
  (*socket + poll* or *poll*) and the last mirror **latency**.
- **Working orders** (*Mirror working orders*, on by default for new groups): every
  working **limit / stop / stop-limit** order of the leader gets a twin on each follower,
  sized by the same rule (proportional to the leader's position when one exists), following
  the leader's price / size modifications and cancelled the moment the leader's order is no
  longer working. A leader stop / target pair (OCO) becomes **one OCO pair on the
  follower**, so the broker itself cancels the follower's target when its stop fills. When a
  leader order fills, its twins are cancelled first and the follower's **real broker
  position** decides the market order — a twin that already filled is never doubled. Twins
  are persisted (`copy_twins`) and verified against the broker after a restart; the
  10-second reconcile re-creates missing twins, cancels orphans and drops twins the broker
  no longer holds. Not mirrored: market orders, trailing stops and other exotic types,
  orders outside the symbol filter and orders on baseline contracts (`order_skip` in the
  log). Events: `order_mirror`, `order_modify`, `order_cancel`, `order_done`, `order_reject`.
- **Diagnostics**: a change the poll finds before the socket delivered it is logged as
  `ws_miss`; the drawer's **Diagnostics** block shows the leader account id, the sync
  response, event counts and the last raw socket messages — the first place to look when a
  group does not copy.
- **Rate limits**: while the socket is synced the REST poll runs only every 10 s (2 s
  when the socket is down); a 429 from Tradovate is a throttle, not a lost feed — the poll
  waits the broker's penalty, slows down (up to 30 s) and speeds up again after a clean
  minute. The leader's working orders come from the socket too (`order` /
  `orderVersion` events), the REST look at them is a periodic cross-check.
- **Feed loss**: when the REST poll fails (Tradovate unreachable, token dead) the feed is
  lost; after *Flatten followers after feed loss* seconds (default 30) every mirrored
  follower position is **closed at market**, the group **pauses** and an alert goes out
  (Discord, push and email).
  **Resume** clears the pause; **Sync now** copies the leader's current positions.
- **Baseline**: a position the leader already holds when the group starts is *not*
  copied — mirroring of that contract begins with the leader's next entry after it is
  flat, or right away with **Sync now**; the leader closing that baseline position is
  not mirrored either. Mirrored contracts are remembered in the database, so a restart
  (every deploy is one) continues the mirror instead of starting a new baseline.
- **Exclusive followers**: the mirror treats a follower's whole position in a contract as
  its own — do not trade a follower by hand or through another route, and an account can
  follow one leader only (enforced across groups). *After feed loss* is a per-group choice:
  flatten the followers and pause, or pause only. **Flatten followers** closes every mirrored follower position and
  pauses the group. Both are confirmed in the UI.
- The global **Trading** switch applies (mirror orders are skipped while it is off), a
  leader cannot follow itself and chains that would loop (A → B → A) are rejected.
- Every action is written to the **event log** (`copy_events` table, kept 7 days):
  `mirror` with latency, `reject`, `drift`, `feed up` / `feed lost`, `paused`, `resumed`,
  `skipped` (trading off / paused) and `ignored` (baseline). Rejects and pauses raise the
  *Copy trading* alert (Settings → Alerts).

Latency matters: run the bridge close to Tradovate (Render **Virginia / US East**) and
avoid the agent path for the leader where you can — polling adds up to a second.

---
## Two-factor authentication (authenticator app + backup codes)

Every **new** account (first-run setup and invite sign-up) must enrol in two-factor
authentication before it can use the dashboard: after registering, the user lands on
`/2fa/setup`, scans the QR code (or types the key) with Google Authenticator, Microsoft
Authenticator, Authy, 1Password, Aegis …, confirms with a 6-digit code and receives **ten
backup codes** shown once. Existing accounts enable it under **Settings → Account** (there it
can also be turned off again; accounts created through sign-up cannot).

Signing in then asks for the password first and the code second; a backup code works
instead of the app, **each code once**. Under Account the user sees how many are left and
can request a **new set of ten at any time** (lost or all used) with password + current
code — the old set stops working. TOTP codes are single-use too (a code accepted once is
refused within its 30-second step), attempts are rate-limited and audited.

Recovery when the phone *and* the codes are gone: an admin presses **Reset 2FA** on the
Users page (or issues a password reset). Both delete the secret and the codes and sign the
user out everywhere; they sign in with the password and enrol again. The secret is stored
encrypted, backup codes as salted hashes.

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

**Settings file** (Settings → Updates → *Settings file*): **Export settings** downloads the
workspace configuration as one JSON file — webhooks with their routing and sizing, symbol
map, trading rules, alert preferences and triggers, news-lock rules, journal and display
settings. No secret travels: broker logins and tokens, passwords, API keys, the webhook
passphrase, the Discord user token and the heartbeat URL stay behind, as does runtime state
(risk locks, drawdown trackers, copy groups). **Import settings…** replaces those keys with
the file's values after a confirmation (keys absent from the file are left alone). Webhook
ids and tokens travel with the file, so TradingView alerts pointing at the old bridge keep
working on the new one; a token already used by another workspace on the target bridge gets
a fresh one. Routing is kept only for logins that exist on the target (matched by login id) —
re-route the webhook after moving to a bridge with different logins. Every value passes the
same validation as the settings form; both actions are recorded in the audit log. The
database backup above is the full copy including secrets.

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
| `GET`  | `/api/settings/export` | The workspace configuration as a JSON file (no secrets) · `POST /api/settings/import` applies one |
| `GET`  | `/api/orders` `/api/signals` `/api/events` | Rolling logs |
| `POST` | `/api/agent/pair` | Exchange a one-time pairing code for an agent token (unauthenticated, rate-limited) |
| `GET`  | `/api/agent/jobs` | Agent long-poll for relay jobs (agent token) · `POST /api/agent/jobs/{id}/result` delivers the answer |
| `GET`  | `/api/agents` | Paired agents with online state · `POST /api/agents/pairing-code`, `PUT`/`DELETE /api/agents/{id}`, `GET /api/agents/download.zip` (admin) |
| `GET`  | `/api/rollover` | Rollover warnings with proposed next contracts (`?refresh=1` re-checks) · `POST /api/rollover/apply {items:[{tv_symbol, contract}]}` confirms a roll |
| `GET`  | `/api/risk` | Every trade account's risk rules and today's lock · `POST /api/risk/unlock {spec}` clears a lock; rules are saved with `POST /api/trade-accounts` (`risk` key) |
| `GET/POST` | `/api/copy/groups` | Copy-trading groups with live status · `PUT`/`DELETE /api/copy/groups/{id}`, `POST …/{id}/enable|disable|resume|sync|flatten`, `GET /api/copy/status`, `GET /api/copy/events` |
| `GET`  | `/api/pnl` | Live account P&L: today's realised, open, week, cash per account (`?refresh=1` polls Tradovate now) |
| `GET`  | `/api/journal/overview` | Journal stats, per-period buckets, equity curve for a filter slice (`range`/`frm`/`to`, `account`, `symbol`, `side`, `period`) |
| `GET`  | `/api/journal/calendar` | Daily net P&L for a month (`month=YYYY-MM`) |
| `GET`  | `/api/journal/trades` | Imported round-trip trades (filters as above, `limit`, `before`) |
| `PUT`  | `/api/journal/trades/{id}` | Set a trade's `note` / `tags` |
| `POST` | `/api/journal/import` | Import fills / trades / snapshots from every enabled login (Tradovate, ProjectX, Rithmic) now |
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
| `POST` | `/api/orders/manual` | Order ticket: `{lid, spec, symbol, action, qty, order_type, price?, stop_price?}` — Trading switch and risk lock apply |
| `POST` | `/api/positions/close` | Cancel one contract's working orders and close the position at market (`{lid, spec, symbol}`) |
| `GET`  | `/api/exposure` | Open positions (with their login) + per-symbol / per-account exposure summary and warnings |
| `GET/PUT` | `/api/automations` | Rules (`{rules: [...]}`), recent firings, the event and action catalogue |
| `GET`  | `/metrics` | Prometheus text format; `Authorization: Bearer $NEXUSPRED_METRICS_TOKEN` (404 when unset) |
| `POST` | `/api/alerts/test` | Send a test notification on every enabled alert channel |
| `GET`  | `/api/push/public-key` | VAPID public key for `PushManager.subscribe` · `GET /sw.js` serves the service worker (public) |
| `POST` | `/api/push/subscribe` | Register this device's push subscription · `DELETE` removes it (`{endpoint}` or `{id}`) · `GET /api/push/subscriptions` lists the area's devices · `POST /api/push/test` sends a test push |
| `GET`  | `/api/positions` | Live Tradovate positions |
| `POST` | `/api/connect` | Reload sessions & verify every token account |
| `GET/POST` | `/api/token-accounts` | List / save logins (tokens, enable flags & default multipliers) |
| `GET/POST` | `/api/trade-accounts` | Overview / save per-account execution on-off & multipliers |
| `GET`  | `/api/health` | Check every connection (renews tokens if needed) |
| `GET/POST` | `/api/webhooks` | List all webhooks / create one |
| `PUT/DELETE` | `/api/webhooks/{id}` | Update / delete a webhook (name, strategy, qty, accounts, `trade_window`) · `PUT …/sharing` also takes `max_subscribers`, `approval`, `paused`, `tags` |
| `POST` | `/api/webhooks/{id}/regenerate-token` | Rotate a webhook's secret token |
| `POST` | `/api/webhooks/{id}/test` | Run a payload through the pipeline for this webhook (`?subscribers=true` also forwards it) |
| `PUT`  | `/api/webhooks/{id}/sharing` | Publish / unpublish on the marketplace (admin): title, description, visibility, allowed users |
| `GET/DELETE` | `/api/webhooks/{id}/subscribers[/{sub_id}]` | List / remove subscribers of a published webhook (admin) |
| `GET`  | `/api/marketplace` | Published webhooks visible to me (with my subscription, if any) |
| `POST` | `/api/marketplace/{area}/{webhook_id}/subscribe` | Subscribe (routed accounts + enabled) |
| `GET/PUT/DELETE` | `/api/subscriptions[/{id}]` | My subscriptions: list / update / unsubscribe |
| `GET`  | `/api/marketplace/{area}/{webhook_id}/record` | Full track record of a published signal · `…/copy/{group_id}/record` for a copy group · own: `GET /api/webhooks/{id}/record`, `GET /api/copy/groups/{id}/record` |
| `GET`  | `/api/subscriptions/{id}/journal` | The subscriber's journal of one subscription: signals + outcomes (or copy events) and P&L since subscribing |
| `PUT`  | `/api/webhooks/{id}/subscribers/{sub_id}` | Publisher sets a subscriber's status: `active` (approve / resume), `paused` · same under `/api/copy/groups/{id}/subscribers/{sub_id}` |
| `GET/PUT` | `/api/payments/config` | Operator's Stripe connection and the paid-listings switch (admin; secrets masked) |
| `POST` | `/api/payments/checkout` | Stripe Checkout link for a paid listing (`{publisher_area_id, key}`) · `POST /api/payments/portal` billing portal · `GET /api/payments/mine` |
| `GET`  | `/api/payments` | Payment records: all (admin) or those of the caller's listings (publisher) |
| `POST` | `/api/payments/webhook` | Stripe → bridge (public path; `Stripe-Signature` verified) |
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
app/db/ / auth.py      SQLite package (core, users, areas, marketplace, history, copytrade, journal, agents, push, audit), signed-cookie sessions
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

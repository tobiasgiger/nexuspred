# Fluxbridge — TradingView → Tradovate signal router

_(repository: `tobiasgiger/nexuspred`)_

A self-hosted bridge that receives **TradingView** alerts via a webhook and routes them
to **Tradovate** as live/demo orders. Ships with a dark, professional dashboard for
configuration and monitoring, an optional Discord signal listener, **multi-user accounts (invite-only)**
access control, and a built-in GitHub auto-updater.

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
- **Dark-themed dashboard** to manage settings, watch positions/orders, and read logs.
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
4. Deploy → you get `https://YOUR-SERVICE.onrender.com`. Each strategy's TradingView
   webhook is `https://YOUR-SERVICE.onrender.com/webhook/YOUR_TOKEN` (from its card in
   the **Webhooks** tab).
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

1. Go to **Settings → Token Accounts** → add one row per Tradovate account with its own
   access token (start in **Demo**), then **Connect & Verify**.
2. Go to **Settings → Trade Accounts** and switch on the accounts you want available for
   trading.
3. Go to the **Webhooks** tab → **+ Add Webhook** for each strategy, pick its strategy
   type (`simple` or `bracket`), and enable the accounts (with qty multipliers) that
   strategy should trade.
4. Flip **Trading enabled** on (Settings tab) when you're ready to go live.
5. Copy each webhook's URL from its card (or the *Test & Webhook* tab) into the matching
   TradingView alert.

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
| `bracket` | `close_all` | cancel working orders + flatten | Market | full position |
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
**Settings → Token Accounts**, one row each:

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

**Settings → Alerts** — two channels, each with its own on/off switch:

- **Discord** — a webhook URL (Discord channel → *Edit Channel → Integrations → Webhooks*).
  Optionally prefixes every message with `@everyone`.
- **Email** — SMTP, defaults to Gmail (`smtp.gmail.com:587`). Use a Gmail **App Password**
  under your Google Account's security settings, not your normal login password (Gmail
  rejects plain passwords for SMTP). Notify address defaults to your own.

Three triggers, each independently toggled:

| Trigger | Channels | Detail included |
|---|---|---|
| Connection lost | Discord + email | which account, environment (demo/live) and error |
| Connection restored | Discord + email | which account and environment |
| Trade executed | Discord only | which webhook/strategy, action, contract, accounts |

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

- **Enable listener** and paste your Discord **user token** (stored masked in
  `data/settings.json`, like every other secret).
- **Global dry-run** — parse and display signals but send to **no** webhook.
- **Channels** — each is a Discord channel ID with a label and one or more **targets**.
  A target is either one of the bridge's **own webhooks** (pick it from a dropdown — the
  signal is posted to that webhook's URL, so it flows straight into your strategy routing)
  or a **custom URL** (for an external logging system or second bridge, with an optional
  **secret** sent as the `X-Webhook-Secret` header). Each target has an on/off toggle.
  Every *enabled* target of a channel receives each signal **in parallel**, each with its
  own 5 s timeout and isolated error handling.

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

## Users, areas & login (multi-tenant)

Fluxbridge is **multi-user**. Each user signs in with **email + password** and gets
their own **isolated area** — token accounts, webhooks (each with its own URL token),
Discord listener, symbol map, alerts and logs are all private to that user. Nothing
is shared between areas (shared areas are planned for a later release).

- **First run:** open the dashboard and you're sent to **`/setup`** to create the
  **first admin** account. Any pre-existing single-user `data/settings.json` is
  migrated into that admin's area.
- **Invite-only:** there is no open sign-up. An admin creates **invite links** under
  **Settings → Account & Users** (optionally granting admin). Share the link; the new
  user registers and gets their own area.
- **Admin** can list users, create/revoke invites, and delete users (which removes
  their area and data).

Storage is a **SQLite** database at `<NEXUSPRED_DATA_DIR>/fluxbridge.db` (users, areas,
memberships, invites). Passwords are salted **PBKDF2** hashes. The login session is a
signed, HTTP-only cookie — no server-side store, no extra dependency. Use **Sign out**
(top-right). Set `SESSION_SECRET` to pin the cookie-signing key across restarts (else
it's generated and stored). The `/webhook/<token>` and `/healthz` paths are never
behind login; a webhook token routes to whichever user's area owns it.

## Symbol mapping

TradingView sends continuous symbols like `MNQ1!`. **Settings → Current Symbol Mapping**
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

To cut a new release, bump `VERSION` and tag it (`vX.Y.Z`).

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
| `POST` | `/webhook/{token}` | Receive a TradingView alert for a specific webhook |
| `GET`  | `/api/status` | Connection + trading status |
| `GET/POST` | `/api/settings` | Read / update settings |
| `GET`  | `/api/orders` `/api/signals` `/api/events` | Rolling logs |
| `GET`  | `/api/positions` | Live Tradovate positions |
| `POST` | `/api/connect` | Reload sessions & verify every token account |
| `GET/POST` | `/api/token-accounts` | List / save logins (tokens, enable flags & default multipliers) |
| `GET/POST` | `/api/trade-accounts` | Overview / save per-account execution on-off & multipliers |
| `GET`  | `/api/health` | Check every connection (renews tokens if needed) |
| `GET/POST` | `/api/webhooks` | List all webhooks / create one |
| `PUT/DELETE` | `/api/webhooks/{id}` | Update / delete a webhook (name, strategy, qty, accounts) |
| `POST` | `/api/webhooks/{id}/regenerate-token` | Rotate a webhook's secret token |
| `POST` | `/api/webhooks/{id}/test` | Run a payload through the pipeline for this webhook |
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
| `GET/POST` | `/setup` · `/login` · `/register` · `/logout` | User auth (first-admin setup, login, invited signup, logout) |
| `GET`  | `/api/me` · `/api/users` · `/api/invites` | Current user / admin user management |

---

## Configuration & data

Runtime settings are stored in `data/settings.json` (git-ignored, never committed).
Secrets are masked in the dashboard and never sent back to the browser in plain text.

## Disclaimer

Trading futures involves substantial risk. This software is provided as-is, without
warranty. Test thoroughly on a **demo** account before enabling live trading.

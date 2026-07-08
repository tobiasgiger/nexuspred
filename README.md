# nexuspred — Tradovate Webhook Bridge

A self-hosted bridge that receives **TradingView** alerts via a webhook and routes them
to **Tradovate** as live/demo orders. Ships with a dark, professional dashboard for
configuration and monitoring, and a built-in GitHub auto-updater.

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
  multiplier, so signals never cross strategies. Two selectable strategy types:
  **simple** (buy/sell the payload's qty, no TP/SL — just execution) and **bracket** (the
  entry + tp1/tp2/tp3/sl flow described above).
- **Safety first**: trading is **disabled by default**, every webhook URL has its own
  unguessable secret token, and an optional passphrase can be enforced in the alert body.

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
2. Set env vars: `NEXUSPRED_DATA_DIR=/var/data` (persists settings/tokens) and
   `DASHBOARD_PASSWORD=<your-password>` (the dashboard is public — protect it). Render
   injects `PORT` automatically.
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
> persistent disk so settings survive deploys, and always set `DASHBOARD_PASSWORD`
> (HTTP Basic auth on the dashboard + API; the `/webhook/<token>` and `/healthz` paths
> stay open). `GET /healthz` is an unauthenticated liveness probe.

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

Qty defaults/TP qty are set **per webhook** (Webhooks tab); order types (Market/Limit/Stop)
are global, configurable on the **Settings** tab.

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
accounts. After connecting, every discovered account appears in **Settings → Trade
Accounts** — this is the discovery/reference list used to populate each webhook's account
picker (and to aggregate Open Positions). It's not where routing happens anymore: **which
accounts actually execute a given strategy's signals, and their qty multiplier, is chosen
per webhook** in the **Webhooks** tab.

- Newly discovered accounts default to **off** (except the first on a brand-new login), so
  an account never starts trading without an explicit opt-in on some webhook.
- The **Monitor** header shows *Logins connected* and *Accounts executing*;
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

---

## Configuration & data

Runtime settings are stored in `data/settings.json` (git-ignored, never committed).
Secrets are masked in the dashboard and never sent back to the browser in plain text.

## Disclaimer

Trading futures involves substantial risk. This software is provided as-is, without
warranty. Test thoroughly on a **demo** account before enabling live trading.

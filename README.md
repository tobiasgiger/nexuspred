# nexuspred — Tradovate Webhook Bridge

A self-hosted bridge that receives **TradingView** alerts via a webhook and routes them
to **Tradovate** as live/demo orders. Ships with a dark, professional dashboard for
configuration and monitoring, and a built-in GitHub auto-updater.

![dashboard](docs/dashboard.png)

---

## Features

- **Webhook endpoint** for TradingView alerts (`POST /webhook/{secret}`).
- **Order logic** built around the strategy's JSON signals:
  - Initial `buy` / `sell` → **market** order, default **3 contracts** (MNQ / MES).
  - Each `tp1` / `tp2` / `tp3` → **limit** order of **1 contract**.
  - `sl` → protective **stop** order covering the whole position.
  - `move_sl` → moves the protective stop (e.g. to break-even).
  - `trail_active` → acknowledged (the strategy keeps sending `move_sl` updates).
  - `close_all` → cancels working orders and **flattens the whole position**.
- **Trade simulator** that runs full scenarios (winning trade, losing trade, manual
  close…) through the *real* signal logic with an in-memory executor — no credentials,
  no broker calls. Run a whole scenario or step through it.
- **Token refresh & health monitoring**: authenticates once for an API user session
  token, then renews it via `/auth/renewaccesstoken` before expiry (no password resend),
  and a background loop continuously verifies the connection is up.
- **Dark-themed dashboard** to manage settings, watch positions/orders, and read logs.
- **Auto-updater** that checks the GitHub repo and shows an **Update** button when a new
  version is available — one click pulls the latest code and restarts.
- **Safety first**: trading is **disabled by default**, a webhook secret is required, and
  an optional passphrase can be enforced in the alert body.

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

1. Go to **Settings** → enter your Tradovate credentials (start in **Demo**).
2. Click **Connect & Verify** — the account is auto-detected.
3. Set a strong **Webhook secret**.
4. Flip **Trading enabled** on when you're ready to go live.
5. Copy your **Webhook URL** from the *Test & Webhook* tab into your TradingView alert.

---

## TradingView alert setup

Create an alert and set the **Webhook URL** to:

```
http://YOUR_HOST:9000/webhook/YOUR_SECRET
```

Set the alert message to the strategy's JSON. Examples (matching the strategy):

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

> If you set a **passphrase** in Settings, include `"passphrase":"..."` in every alert.

---

## How orders are sized

| Signal `action` | Order(s) placed | Type | Qty |
|---|---|---|---|
| `buy` / `sell` | entry | Market | `default_qty` (3) |
| `buy` / `sell` | tp1, tp2, tp3 (if present) | Limit | `tp_qty` (1) each |
| `buy` / `sell` | sl | Stop | full position |
| `move_sl` | modify the stop order | — | — |
| `close_all` | cancel working orders + flatten | Market | full position |

All quantities and order types are configurable on the **Settings** tab.

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

## Connection, tokens & health

Three authentication methods (Settings → *Authentication method*):

1. **Username & password** (default) — requests an **API user session token**
   (`accesstokenrequest`). If you don't have a paid API key it falls back to the
   **web-trader app identities** (cid 8/2…), so it works **without an API
   subscription**. Supply your own `cid`/`sec` under *Advanced* to use those first.
2. **Paste access token** — paste an existing `accessToken` (and optional market-data
   token). Useful if you already have one; it's kept alive automatically.
3. **OAuth (refresh token)** — authorize once in the browser; the bridge stores a
   **refresh token** and uses it to mint new access tokens (no password stored). Set
   your `oauth_client_id` (defaults to the public web client) and click **Authorize**.

Token lifecycle (all modes):

- Expiry is read from the token's **JWT `exp` claim** (falling back to `expirationTime`),
  so the real lifetime is always respected.
- Before expiry the token is **refreshed without a password** — `renewaccesstoken` for
  password/token modes, the **refresh token** for OAuth.
- Repeated auth failures trigger an **exponential backoff** (and `p-ticket` time
  penalties are honored) so the bridge won't trip Tradovate's IP lockout.
- A background **health check** (default every 60s, configurable; `0` disables) verifies
  the session with `/auth/me`, refreshes as needed, and updates the **Connection Health**
  card (status, user, token expiry, last check, last renew, last error). Trigger it on
  demand with **Check now** or `GET /api/health`.

## Multiple accounts

One Tradovate login can hold several accounts. On **Connect** (or **Refresh from
Tradovate**) the bridge lists them in **Settings → Accounts**, where each account has:

- an **Enabled** toggle — every signal is sent to **all enabled accounts**; disabled
  ones are ignored;
- a **quantity multiplier** — scales the contracts for that account (e.g. `2` turns the
  default 3-contract entry into 6, with TP/SL sized to match).

The same entry/TP/SL/move_sl/close_all logic runs per account, and the **Monitor →
Active Trades** table shows one row per account with its own SL/TP order ids. If no
accounts are configured, the bridge falls back to the single auto-detected account.

## Symbol mapping

TradingView sends continuous symbols like `MNQ1!`. The bridge maps these to a Tradovate
root (`MNQ`, `MES`) via **Settings → Symbol map**, then resolves the tradable front-month
contract automatically through the Tradovate contract API. You can also enter a fully
dated contract (e.g. `MNQU5`) and it will be used as-is.

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

---

## API reference

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/webhook/{secret}` | Receive a TradingView alert |
| `GET`  | `/api/status` | Connection + trading status |
| `GET/POST` | `/api/settings` | Read / update settings |
| `GET`  | `/api/orders` `/api/signals` `/api/events` | Rolling logs |
| `GET`  | `/api/positions` | Live Tradovate positions |
| `POST` | `/api/connect` | Authenticate & verify account(s) |
| `GET/POST` | `/api/accounts` | List / save account enable flags & multipliers |
| `POST` | `/api/accounts/refresh` | Re-fetch accounts from Tradovate |
| `GET`  | `/api/health` | Check connection (renews token if needed) |
| `POST` | `/api/webhook-test` | Run a payload through the pipeline |
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

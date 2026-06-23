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

Open the dashboard at **http://localhost:8000** — then follow the built-in
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
http://YOUR_HOST:8000/webhook/YOUR_SECRET
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
| `POST` | `/api/connect` | Authenticate & verify account |
| `POST` | `/api/webhook-test` | Run a payload through the pipeline |
| `GET`  | `/api/update/check` | Check GitHub for a new version |
| `POST` | `/api/update/apply` | Pull latest & restart |

---

## Configuration & data

Runtime settings are stored in `data/settings.json` (git-ignored, never committed).
Secrets are masked in the dashboard and never sent back to the browser in plain text.

## Disclaimer

Trading futures involves substantial risk. This software is provided as-is, without
warranty. Test thoroughly on a **demo** account before enabling live trading.

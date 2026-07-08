# Changelog

All notable changes to nexuspred. Versions follow [SemVer](https://semver.org/).
Bump `VERSION` on every release — the dashboard compares it against GitHub and
shows the **Update** button when a newer version is available.

## 2.2.1
- **Ready-to-paste alert template** below each webhook's URL (Webhooks tab and
  Test & Webhook tab): TradingView JSON built from its own placeholders
  (`{{strategy.order.action}}`, `{{strategy.order.contracts}}`, `{{ticker}}`,
  `{{strategy.order.price}}`) matching that webhook's strategy — `simple` gets
  action/symbol/qty, `bracket` gets action/symbol/entry plus sl/tp1/tp2/tp3
  placeholders to fill in from the strategy's own levels. One click to copy.

## 2.2.0
- **Multi-webhook routing, one URL per strategy.** New **Webhooks** tab:
  create/edit/delete a dedicated `/webhook/<token>` per strategy, each with its
  own routed trade accounts (picked from the accounts discovered under
  Settings → Trade Accounts) and its own per-account qty multiplier — signals
  from one strategy never cross into another's accounts.
- Two selectable strategy types per webhook: **simple** (buy/sell the qty from
  the payload, or the webhook's default — no TP/SL, just execution) and
  **bracket** (the existing entry + tp1/tp2/tp3/sl flow, with per-webhook
  default/TP qty). A small strategy dispatch, so future logic (e.g. TP/SL
  expressed in points off a close price) can be added later without touching
  routing.
- Existing installs auto-migrate on first startup: the old `webhook_secret` +
  every currently-enabled trade account become a "Default" webhook (strategy
  `bracket`), so existing TradingView alerts keep working unchanged.
- The Test & Webhook tab gained a webhook picker so test signals run through a
  specific webhook's routing; `/api/webhook-test` is replaced by
  `/api/webhooks/{id}/test`.

## 2.1.0
- **Multiple trade accounts per login, with per-account execution on/off.** One
  Tradovate access token often grants access to several trade accounts. Click
  **Connect & Verify** (or *Discover / Refresh*) and the bridge now lists **every**
  account under each login in the new **Settings → Trade Accounts** card. Switch
  execution on/off per account and set a per-account **Qty ×** — each signal fans
  out to exactly the accounts you switched on.
  - The Monitor header now shows *Logins connected* and *Accounts executing*.
  - Newly discovered accounts default to **off** (except the very first on a fresh
    login), so an account never starts trading without an explicit opt-in.
  - Existing single-account setups keep working unchanged until you refresh.

## 2.0.1
- **Fix: dashboard buttons dead after the v2.0.0 upgrade** (e.g. "+ Add account"
  did nothing). The browser was serving the cached v1.5.0 `app.js` against the new
  HTML. Static assets are now cache-busted with `?v=<version>`, so the dashboard JS
  and CSS always match the deployed version. (If you still see it, hard-refresh once.)

## 2.0.0
- **Token-only, multiple Tradovate accounts** (breaking change). Username/password
  login is removed entirely — there is no more single-login "Accounts" model. Each
  account is now its own session authenticated by its **own access token**, configured
  under **Settings → Token Accounts** (Name, Environment, Access token, optional Check
  token, Enabled, Qty × multiplier).
- Every signal fans out to **all enabled accounts in parallel**; each account resolves
  its own contract, places its own bracket, and tracks its own SL/TP order ids.
- Per-account **token refresh & health**: each token is renewed independently
  (access token → check token, no password fallback) and persisted best-effort so it
  survives redeploys. The Monitor shows one status row per account.
- Removed the `TRADOVATE_ACCESS_TOKEN` / `TRADOVATE_CHECK_TOKEN` /
  `TRADOVATE_USERNAME` / `TRADOVATE_PASSWORD` (and `CID/SEC/APP_ID/DEVICE_ID`) env
  vars and the single-login `/api/accounts` endpoints. New `/api/token-accounts`
  manages per-account tokens (secrets masked on read, merged on save).
- **Migration**: re-add each account under Settings → Token Accounts with its access
  token; old credential settings are ignored.

## 1.5.0
- **No more TradingView timeouts on alert bursts**: the webhook now acknowledges
  instantly (HTTP 202) and processes the signal in the background, so many alerts
  firing within milliseconds are handled concurrently instead of blocking.
- **Parallel account execution**: orders for all enabled accounts are placed
  simultaneously (`asyncio.gather`) instead of one-by-one; within an account the
  TP/SL bracket is also placed in parallel. move_sl / trail_active / close_all
  fan out across accounts in parallel too.
- **Contract resolution cached** (1 h) so bursts don't repeat `/contract/find`.

## 1.4.6
- **Break-even = entry price**: a TP1 `move_sl` (or any "breakeven" message) now sets
  the stop to the original **entry price** of the initial buy/sell signal, instead of
  the signal's `new_sl` (which is net-of-fees and slightly off). Trailing `move_sl`
  updates still use `new_sl`. Toggle via *Trading Rules → “Break-even = entry price”*.

## 1.4.5
- Removed the **Open P&L** column from Open Positions. Tradovate's position feed
  has no live P&L (it needs a market-data subscription), so it only ever showed
  0.00 — the column now shows Symbol / Net Pos / Avg Price instead.

## 1.4.4
- **Tokens survive redeploys**: the renewed token persisted on disk now wins over a
  stale `TRADOVATE_ACCESS_TOKEN` env var (the env token is only a seed and expires).
  The loader picks whichever token has the later expiry.
- **Credentials via env vars**: `TRADOVATE_USERNAME` / `TRADOVATE_PASSWORD` (and
  optional `TRADOVATE_CID/SEC/APP_ID/DEVICE_ID/ENVIRONMENT`) — set once on the host
  and the bridge logs in fresh after every deploy, no manual token entry.

## 1.4.3
- **Fix `move_sl` 400 error**: `/order/modifyorder` now sends the required
  `orderQty` and `orderType` (it was failing with “missing required field orderQty”).
- **Stop-loss size now tracks the remaining position**: after TP1 the SL shrinks to
  2 contracts, after TP2 to 1 (scaled by each account's multiplier). The remaining
  qty is derived from the signal's event (`tp1_hit`/`tp2_hit`); `trail_active` (TP2)
  also resizes the stop.

## 1.4.2
- **Proactive token refresh** (adopted from Bridge-Bot-TV): the background loop now
  force-renews the token *before* it expires — at least 5 min ahead and at least
  every 25 min — instead of waiting for it to lapse, with a 60 s retry on failure.
  Adds `proactive_refresh()` and expiry-aware `seconds_until_refresh()`.

## 1.4.1
- Added a **standalone Setup Guide page** (`docs/setup-guide.html`, self-contained,
  inline styles) served at **`/guide`** (public, auth-exempt) with a link from the
  dashboard's Setup Guide tab.

## 1.4.0
- Reworked the in-dashboard **Setup Guide** into a structured how-to (Parts A–H):
  Render deploy, self-host on Linux, configure, TradingView, test, go-live,
  operate/update, and troubleshooting — with sub-steps throughout.
- **Open Positions** now shows the resolved contract symbol (not the numeric id)
  and lists only *open* positions (netPos ≠ 0); flat/closed ones are hidden.
- **Token auto-renewal hardened**: persisting a renewed token is now best-effort,
  so a read-only data dir can no longer break renewal and drop the session
  (caused the "disconnected, signal didn't go through" issue).

## 1.3.3
- Don't 500 when `NEXUSPRED_DATA_DIR` isn't writable (e.g. Render env var set but
  no persistent disk mounted): fall back to the local `data/` dir with a clear
  warning instead of crashing on save.

## 1.3.2
- Pin **Python 3.11** via a `.python-version` file so Render (and other hosts)
  don't pick Python 3.14, which has no prebuilt wheels for `pydantic-core`/`orjson`
  and fails the build trying to compile them. Build-troubleshooting notes added.

## 1.3.1
- `runner_exit` signal (action `close_all`) — already handled by the action-based
  router; added a Test & Webhook preset and a Simulator scenario for it.

## 1.3.0
- **Render.com deployment** for TradingView's port-80/443 requirement: added
  `render.yaml` blueprint (web service + persistent disk), `NEXUSPRED_DATA_DIR`
  to store settings/tokens on a mounted disk, an unauthenticated `/healthz`
  probe, and a Render walkthrough in the Setup Guide + README.
- **Dashboard auth**: optional HTTP Basic auth via `DASHBOARD_PASSWORD` env var
  or the new *Dashboard password* setting — protects the dashboard + API on
  public hosts; `/webhook/<secret>`, `/static`, `/healthz` stay open.
- Self-update button now reports that managed hosts (Render) deploy via git push.

## 1.2.3
- Setup Guide: added a **Quick install** copy-paste block (apt → git clone →
  install → service → firewall), a dedicated **Open / whitelist port 9000** step
  (ss check, ufw/firewalld/iptables, cloud security groups, curl test), and a
  stronger **keep running after SSH disconnect** step (systemd + enable-linger,
  tmux, nohup). Troubleshooting updated.

## 1.2.2
- Rewrote the in-dashboard **Setup Guide** as a beginner-friendly, 15-step
  walkthrough with copy-paste **Linux** commands (using `/home/py/nexuspred`):
  prerequisites, git clone, install, start, run-on-boot (systemd + linger),
  open dashboard, authenticate, connect/accounts, symbol mapping, safe testing,
  exposing to TradingView (Cloudflare Tunnel/ngrok), go-live, updates, and a
  troubleshooting section.

## 1.2.1
- Add `connect-git.bat` / `connect-git.sh` to turn a ZIP-downloaded folder into a
  Git checkout so the dashboard **Update** button works; clearer "not a git
  checkout" message pointing to them.

## 1.2.0
- **Current Symbol Mapping** card in Settings: map each TradingView symbol to the
  exact Tradovate contract (e.g. `MNQ1!` → `MNQU6`) and edit it on rollover.
  Seeded with NQ/MNQ/ES/MES (U6) and GC/MGC (M6).

## 1.1.0
- **Multi-account routing**: enable multiple Tradovate accounts; every signal is
  sent to all enabled accounts (with per-account quantity multiplier).
- **Auth like Bridge-Bot-TV**: `TRADOVATE_ACCESS_TOKEN` / `TRADOVATE_CHECK_TOKEN`
  env vars, JWT-`exp` expiry, renew chain access → check token → credentials login,
  web-trader fallback (no API subscription). OAuth removed.
- **Trade simulator** tab and **connection health** monitoring.
- **Fix**: contract resolution no longer fails with `404 /contract/find`
  (falls back to `/contract/suggest`, front-month selection).
- **Fix**: market entry orders no longer send a `price` (Tradovate rejection).
- Self-updater hardened; default port changed to 9000.

## 1.0.0
- Initial release: TradingView webhook → Tradovate bridge, dark dashboard,
  order logic (market entry + TP limits + SL stop, move_sl, close_all),
  installers, Setup Guide, and GitHub auto-updater.

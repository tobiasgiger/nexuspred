# Changelog

All notable changes to nexuspred. Versions follow [SemVer](https://semver.org/).
Bump `VERSION` on every release — the dashboard compares it against GitHub and
shows the **Update** button when a newer version is available.

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

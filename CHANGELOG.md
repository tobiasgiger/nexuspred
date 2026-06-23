# Changelog

All notable changes to nexuspred. Versions follow [SemVer](https://semver.org/).
Bump `VERSION` on every release — the dashboard compares it against GitHub and
shows the **Update** button when a newer version is available.

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

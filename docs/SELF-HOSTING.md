# Self-hosting Fluxbridge on your own Linux server

One command turns a fresh Debian / Ubuntu server into a running bridge with HTTPS,
a system service, daily backups and the `fluxbridge` helper command. Nothing else
to prepare beyond a server and a DNS name.

## What you need

| | |
|---|---|
| Server | Any VPS with 1 vCPU / 1 GB RAM, Debian 12 or Ubuntu 22.04+, root access. Tradovate's servers are in the US East, so a US-East location gives the lowest order latency. |
| DNS | An A (and optionally AAAA) record, e.g. `bridge.example.com` → the server's IP. TradingView only delivers webhooks to HTTPS on port 443 with a valid certificate; the installer sets that up with Caddy and Let's Encrypt. |
| Ports | 80 and 443 reachable from the internet (the certificate is fetched over 80/443). |

## Install (one line)

```bash
curl -fsSL https://raw.githubusercontent.com/tobiasgiger/nexuspred/main/deploy/install-server.sh \
  | sudo bash -s -- --domain bridge.example.com
```

Then open `https://bridge.example.com/setup` and create the admin account. That's it.

The installer is idempotent: run the same line again to upgrade, or with a different
`--domain` to move to another host name. Secrets are generated once and never overwritten.

Options:

| Flag | Meaning |
|---|---|
| `--domain NAME` | Public host name. Without it the bridge is served over plain HTTP on port 80 (LAN / testing only). Add one later with `sudo fluxbridge domain NAME`. |
| `--session-secret VALUE` | Reuse an existing `SESSION_SECRET` (see *Moving from Render*). |
| `--branch NAME` | Git branch to track (default `main`). |
| `--port N` | Local port behind Caddy (default 9000). |
| `--no-caddy` | Skip Caddy — you run your own reverse proxy (terminate TLS, forward to `127.0.0.1:9000`, set `NEXUSPRED_PROXY_HOPS` accordingly). |
| `--firewall` | Enable `ufw` with OpenSSH, 80 and 443 allowed. |

## What the installer sets up

| Path | Purpose |
|---|---|
| `/opt/fluxbridge` | The git checkout with its virtual environment (`.venv`). The one-click updater on **Settings → Updates** works here. |
| `/var/lib/fluxbridge` | The data directory: `fluxbridge.db` (SQLite, WAL mode). Everything lives in this one file. |
| `/etc/fluxbridge/env` | Environment: `SESSION_SECRET`, data dir, public URL, port. Mode 640, readable by root and the service user only. |
| `/var/backups/fluxbridge` | Daily backups (03:15 local time, kept 14 days) made through SQLite's online backup API, safe while the bridge is trading. |
| `/etc/systemd/system/fluxbridge.service` | Runs as the unprivileged user `fluxbridge`, `Restart=always`, starts on boot, sandboxed (`ProtectSystem=full`, `ProtectHome`, `NoNewPrivileges`). |
| `/etc/caddy/Caddyfile` | `https://DOMAIN` → `127.0.0.1:9000`, gzip, unbuffered server-sent events. Certificates are fetched and renewed automatically. |
| `/usr/local/bin/fluxbridge` | The helper command below. |

`SESSION_SECRET` signs the login cookies **and encrypts the Tradovate tokens stored in
the database**. Back up `/etc/fluxbridge/env` together with the database; with a
different secret the stored tokens are unreadable and must be re-entered.

## The `fluxbridge` command

```
sudo fluxbridge status            # service state + /healthz
sudo fluxbridge logs [-f]         # journal (add -f to follow)
sudo fluxbridge restart
sudo fluxbridge update            # git pull on the tracked branch, pip install, restart
sudo fluxbridge backup            # write a backup now (the timer does this daily)
sudo fluxbridge restore FILE      # replace the database with a backup (the old one is kept next to it)
sudo fluxbridge domain NAME       # change the public host name (Caddy + public URL)
sudo fluxbridge uninstall         # remove service, checkout, Caddy config — data and backups are kept
```

Updates also work from the dashboard: **Settings → Updates → Update & restart**. Under
systemd the bridge shuts down cleanly after the pull and the service brings it back.

## Moving from Render (or any other host)

1. In the old dashboard: **Settings → Updates → Download backup**. You get one
   `fluxbridge-backup-….db` file with every user, webhook, token, journal and copy group.
2. Copy the old `SESSION_SECRET` (Render → service → Environment). The tokens in the backup
   are encrypted with it.
3. Install on the new server with the same secret:
   ```bash
   curl -fsSL https://raw.githubusercontent.com/tobiasgiger/nexuspred/main/deploy/install-server.sh \
     | sudo bash -s -- --domain bridge.example.com --session-secret 'THE_OLD_SECRET'
   ```
4. Upload the backup and restore it:
   ```bash
   scp fluxbridge-backup-20260912.db root@SERVER:/tmp/
   sudo fluxbridge restore /tmp/fluxbridge-backup-20260912.db
   ```
5. Point the DNS record of the old host name at the new server (or keep the new name and
   update the webhook URLs in TradingView). With the same host name nothing changes in
   TradingView, Discord or the marketplace subscriptions.
6. Log in, check **Settings → Tradovate Accounts → Connect & Verify** for every login,
   then stop the Render service.

If you forgot the secret: install without `--session-secret`, restore, and re-enter each
Tradovate token in the dashboard. Everything else in the backup is plain data.

## Backups off the server

The daily backups sit on the same disk as the database. To keep a copy elsewhere, sync the
backup directory with any tool you like, for example once a day:

```bash
rsync -a /var/backups/fluxbridge/ user@other-host:fluxbridge-backups/
```

## Monitoring

`Restart=always` covers crashes and reboots. For "the whole server is gone" use an external
uptime check on `https://bridge.example.com/healthz` (it answers `{"ok": true, "version": …}`).
Tradovate connection loss and copy-trading feed loss are alerted by the bridge itself.

## Uninstall

`sudo fluxbridge uninstall` removes the service, the checkout and the Caddy site. The data
directory, the backups and `/etc/fluxbridge/env` stay until you delete them.

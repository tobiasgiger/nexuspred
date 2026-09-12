#!/usr/bin/env bash
#
# Fluxbridge — one-line server installer (Debian / Ubuntu, run as root)
#
#   curl -fsSL https://raw.githubusercontent.com/tobiasgiger/nexuspred/main/deploy/install-server.sh | sudo bash -s -- --domain bridge.example.com
#
# What it does (idempotent — run it again to upgrade or to change the domain):
#   • installs python3, git, Caddy (automatic HTTPS via Let's Encrypt)
#   • creates the service user `fluxbridge`, the checkout in /opt/fluxbridge,
#     the data dir /var/lib/fluxbridge and the backup dir /var/backups/fluxbridge
#   • generates the secrets once into /etc/fluxbridge/env (never overwritten)
#   • installs the systemd service (restart on crash / reboot), a daily backup
#     timer and the `fluxbridge` command (status, logs, update, backup, restore)
#   • configures Caddy: https://DOMAIN → the bridge (TradingView needs HTTPS)
#
# Options:
#   --domain NAME           public host name (DNS A/AAAA record → this server). Without it
#                           the bridge is served over plain HTTP on port 80 (LAN / testing only)
#   --session-secret VALUE  reuse an existing SESSION_SECRET (moving from Render: copy it from
#                           the Render service's environment, otherwise stored tokens are unreadable)
#   --branch NAME           git branch to track (default main)
#   --port N                local port the app listens on behind Caddy (default 9000)
#   --no-caddy              don't install / configure Caddy (you run your own reverse proxy)
#   --firewall              enable ufw with OpenSSH, 80 and 443 allowed
#
set -euo pipefail

DOMAIN=""; SESSION_SECRET_IN=""; BRANCH="main"; PORT=9000; WITH_CADDY=1; FIREWALL=0
REPO_URL="https://github.com/tobiasgiger/nexuspred.git"
APP_DIR=/opt/fluxbridge; DATA_DIR=/var/lib/fluxbridge; BACKUP_DIR=/var/backups/fluxbridge
ENV_FILE=/etc/fluxbridge/env; SVC_USER=fluxbridge

while [[ $# -gt 0 ]]; do
  case "$1" in
    --domain) DOMAIN="${2:-}"; shift 2 ;;
    --session-secret) SESSION_SECRET_IN="${2:-}"; shift 2 ;;
    --branch) BRANCH="${2:-main}"; shift 2 ;;
    --port) PORT="${2:-9000}"; shift 2 ;;
    --no-caddy) WITH_CADDY=0; shift ;;
    --firewall) FIREWALL=1; shift ;;
    -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
    *) echo "Unknown option: $1"; exit 1 ;;
  esac
done

c_green=$'\033[32m'; c_blue=$'\033[34m'; c_yellow=$'\033[33m'; c_red=$'\033[31m'; c_reset=$'\033[0m'
say()  { printf "%s==>%s %s\n" "$c_blue" "$c_reset" "$1"; }
ok()   { printf "%s ok %s %s\n" "$c_green" "$c_reset" "$1"; }
warn() { printf "%s !! %s %s\n" "$c_yellow" "$c_reset" "$1"; }
die()  { printf "%s error:%s %s\n" "$c_red" "$c_reset" "$1" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "run as root (sudo)"
command -v apt-get >/dev/null 2>&1 || die "this installer supports Debian / Ubuntu (apt-get) only"
[[ -z "$DOMAIN" || "$DOMAIN" =~ ^[A-Za-z0-9.-]+$ ]] || die "invalid --domain '$DOMAIN'"
[[ -n "$DOMAIN" ]] || warn "no --domain given: the bridge will run over plain HTTP. TradingView webhooks need HTTPS — add a domain later with: fluxbridge domain NAME"

echo
echo "  Fluxbridge server installer"
echo "  ---------------------------"
echo

# --- packages ---------------------------------------------------------------
export DEBIAN_FRONTEND=noninteractive
say "Installing system packages"
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip git curl ca-certificates gnupg >/dev/null
if [[ "$WITH_CADDY" == "1" ]] && ! command -v caddy >/dev/null 2>&1; then
  say "Installing Caddy (automatic HTTPS)"
  apt-get install -y -qq debian-keyring debian-archive-keyring apt-transport-https >/dev/null
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -qq && apt-get install -y -qq caddy >/dev/null
fi
ok "Packages ready ($(python3 --version))"

# --- user + directories -----------------------------------------------------
if ! id -u "$SVC_USER" >/dev/null 2>&1; then
  useradd --system --home-dir "$DATA_DIR" --shell /usr/sbin/nologin "$SVC_USER"
fi
mkdir -p "$APP_DIR" "$DATA_DIR" "$BACKUP_DIR" "$(dirname "$ENV_FILE")"
chown "$SVC_USER:$SVC_USER" "$DATA_DIR" "$BACKUP_DIR"
chmod 750 "$DATA_DIR" "$BACKUP_DIR"

# --- checkout ---------------------------------------------------------------
if [[ -d "$APP_DIR/.git" ]]; then
  say "Updating checkout in $APP_DIR (branch $BRANCH)"
  # as the service user: the checkout is theirs (and writable by the web process), so git never runs as root in it
  chown -R "$SVC_USER:$SVC_USER" "$APP_DIR"
  sudo -u "$SVC_USER" -H git -C "$APP_DIR" fetch --quiet --all --tags --prune
  sudo -u "$SVC_USER" -H git -C "$APP_DIR" checkout --quiet "$BRANCH" 2>/dev/null || sudo -u "$SVC_USER" -H git -C "$APP_DIR" checkout --quiet -b "$BRANCH" "origin/$BRANCH"
  sudo -u "$SVC_USER" -H git -C "$APP_DIR" reset --quiet --hard "origin/$BRANCH"
else
  say "Cloning $REPO_URL (branch $BRANCH) into $APP_DIR"
  git clone --quiet --branch "$BRANCH" "$REPO_URL" "$APP_DIR"
fi
chown -R "$SVC_USER:$SVC_USER" "$APP_DIR"

say "Installing Python dependencies"
sudo -u "$SVC_USER" -H bash -c "cd '$APP_DIR' && python3 -m venv .venv && .venv/bin/python -m pip install --quiet --upgrade pip && .venv/bin/python -m pip install --quiet -r requirements.txt"
ok "Dependencies installed"

# --- secrets / environment (generated once) ---------------------------------
if [[ -f "$ENV_FILE" ]]; then
  # shellcheck disable=SC1090
  source "$ENV_FILE"
fi
SECRET="${SESSION_SECRET_IN:-${SESSION_SECRET:-}}"
if [[ -z "$SECRET" ]]; then
  SECRET="$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')"
  say "Generated a new SESSION_SECRET (encrypts the stored broker tokens — keep $ENV_FILE safe)"
fi
if [[ -n "$DOMAIN" ]]; then PUBLIC_URL="https://$DOMAIN"; else PUBLIC_URL="${NEXUSPRED_PUBLIC_URL:-}"; fi
umask 077
cat > "$ENV_FILE" <<ENV
# Fluxbridge environment — written by install-server.sh. SESSION_SECRET is never
# regenerated on re-runs: it encrypts the Tradovate tokens stored in the database.
SESSION_SECRET=$SECRET
NEXUSPRED_DATA_DIR=$DATA_DIR
NEXUSPRED_BRANCH=$BRANCH
NEXUSPRED_PUBLIC_URL=$PUBLIC_URL
NEXUSPRED_PROXY_HOPS=1
HOST=127.0.0.1
PORT=$PORT
ENV
umask 022
chown root:"$SVC_USER" "$ENV_FILE"; chmod 640 "$ENV_FILE"
ok "Environment written to $ENV_FILE"

# --- systemd service, backup timer, CLI -------------------------------------
say "Installing systemd units and the fluxbridge command"
sed -e "s#@APP_DIR@#$APP_DIR#g" -e "s#@ENV_FILE@#$ENV_FILE#g" -e "s#@DATA_DIR@#$DATA_DIR#g" -e "s#@USER@#$SVC_USER#g" \
    "$APP_DIR/deploy/fluxbridge.service" > /etc/systemd/system/fluxbridge.service
sed -e "s#@APP_DIR@#$APP_DIR#g" -e "s#@ENV_FILE@#$ENV_FILE#g" -e "s#@DATA_DIR@#$DATA_DIR#g" -e "s#@USER@#$SVC_USER#g" \
    "$APP_DIR/deploy/fluxbridge-backup.service" > /etc/systemd/system/fluxbridge-backup.service
cp "$APP_DIR/deploy/fluxbridge-backup.timer" /etc/systemd/system/fluxbridge-backup.timer
install -m 755 "$APP_DIR/deploy/fluxbridge" /usr/local/bin/fluxbridge
systemctl daemon-reload
systemctl enable --quiet fluxbridge.service fluxbridge-backup.timer
systemctl restart fluxbridge.service
systemctl start fluxbridge-backup.timer
ok "Service running (fluxbridge status / fluxbridge logs)"

# --- Caddy ------------------------------------------------------------------
if [[ "$WITH_CADDY" == "1" ]]; then
  say "Configuring Caddy"
  if [[ -n "$DOMAIN" ]]; then
    sed -e "s#@DOMAIN@#$DOMAIN#g" -e "s#@PORT@#$PORT#g" "$APP_DIR/deploy/Caddyfile" > /etc/caddy/Caddyfile
  else
    sed -e "s#@DOMAIN@#:80#g" -e "s#@PORT@#$PORT#g" "$APP_DIR/deploy/Caddyfile" > /etc/caddy/Caddyfile
  fi
  caddy validate --config /etc/caddy/Caddyfile >/dev/null
  systemctl enable --quiet caddy && systemctl restart caddy
  ok "Caddy serves ${PUBLIC_URL:-http://<server-ip>} → 127.0.0.1:$PORT"
fi

# --- firewall (opt-in) ------------------------------------------------------
if [[ "$FIREWALL" == "1" ]] && command -v ufw >/dev/null 2>&1; then
  ufw allow OpenSSH >/dev/null; ufw allow 80/tcp >/dev/null; ufw allow 443/tcp >/dev/null
  ufw --force enable >/dev/null
  ok "ufw enabled (OpenSSH, 80, 443)"
fi

# --- done -------------------------------------------------------------------
sleep 2
if curl -fsS "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1; then
  ok "Bridge answers on http://127.0.0.1:$PORT/healthz ($(curl -fsS "http://127.0.0.1:$PORT/healthz"))"
else
  warn "Bridge not answering yet — check: fluxbridge logs"
fi
echo
echo "  Next step: open ${PUBLIC_URL:-http://<server-ip>}/setup and create the admin account."
[[ -n "$DOMAIN" ]] && echo "  (DNS: an A/AAAA record for $DOMAIN must point at this server; Caddy fetches the certificate on the first request.)"
echo "  Moving from Render: download the backup in the old dashboard (Settings → Updates → Download backup),"
echo "  then on this server:  fluxbridge restore /path/to/fluxbridge-backup.db"
echo "  Commands: fluxbridge status | logs | update | backup | restore FILE | domain NAME | uninstall"
echo

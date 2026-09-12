#!/usr/bin/env bash
#
# Fluxbridge execution agent — one-line installer for Linux (systemd) and macOS (launchd)
#
#   curl -fsSL https://raw.githubusercontent.com/tobiasgiger/nexuspred/main/deploy/install-agent.sh \
#     | sudo bash -s -- --bridge https://bridge.example.com --code ABCD-2345
#
# The bridge shows this line ready to copy under Settings → Execution Agents →
# "Linux one-liner" (with a fresh pairing code; valid 15 minutes, single use).
#
# What it does (idempotent — run again to update the agent script):
#   • installs python3 if missing (apt / dnf / yum / apk / pacman / zypper / brew)
#   • downloads fluxbridge_agent.py into /opt/fluxbridge-agent (macOS: ~/fluxbridge-agent)
#   • pairs with the bridge → agent.json (mode 600) — nothing else to type
#   • installs a systemd service (Linux) or a launchd agent (macOS) that starts on boot
#     and restarts on exit, plus the `fluxbridge-agent` helper (status/logs/update/uninstall)
#
# Options:
#   --bridge URL   the bridge (https://…). Required on first install.
#   --code CODE    pairing code. Required on first install; omit on re-runs (keeps the token).
#   --name NAME    agent name shown in the bridge (default: this host's name)
#   --branch NAME  git branch to download the agent from (default main)
#   --dir PATH     install directory (default /opt/fluxbridge-agent, macOS ~/fluxbridge-agent)
#
set -euo pipefail

BRIDGE=""; CODE=""; NAME=""; BRANCH="main"; DIR=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --bridge) BRIDGE="${2:-}"; shift 2 ;;
    --code) CODE="${2:-}"; shift 2 ;;
    --name) NAME="${2:-}"; shift 2 ;;
    --branch) BRANCH="${2:-main}"; shift 2 ;;
    --dir) DIR="${2:-}"; shift 2 ;;
    -h|--help) sed -n '2,24p' "$0"; exit 0 ;;
    *) echo "Unknown option: $1"; exit 1 ;;
  esac
done

c_green=$'\033[32m'; c_blue=$'\033[34m'; c_yellow=$'\033[33m'; c_red=$'\033[31m'; c_reset=$'\033[0m'
say()  { printf "%s==>%s %s\n" "$c_blue" "$c_reset" "$1"; }
ok()   { printf "%s ok %s %s\n" "$c_green" "$c_reset" "$1"; }
warn() { printf "%s !! %s %s\n" "$c_yellow" "$c_reset" "$1"; }
die()  { printf "%s error:%s %s\n" "$c_red" "$c_reset" "$1" >&2; exit 1; }

OS="$(uname -s)"
RAW="https://raw.githubusercontent.com/tobiasgiger/nexuspred/$BRANCH"
SVC_USER=fluxagent
if [[ "$OS" == "Darwin" ]]; then
  DIR="${DIR:-$HOME/fluxbridge-agent}"
else
  [[ $EUID -eq 0 ]] || die "run as root (sudo) on Linux — the agent is installed as a system service"
  DIR="${DIR:-/opt/fluxbridge-agent}"
fi
CFG="$DIR/agent.json"

echo
echo "  Fluxbridge execution agent — installer"
echo "  --------------------------------------"
echo

# --- python3 ----------------------------------------------------------------
if ! command -v python3 >/dev/null 2>&1; then
  say "Installing python3"
  if command -v apt-get >/dev/null 2>&1; then DEBIAN_FRONTEND=noninteractive apt-get update -qq && apt-get install -y -qq python3 >/dev/null
  elif command -v dnf >/dev/null 2>&1; then dnf install -y -q python3
  elif command -v yum >/dev/null 2>&1; then yum install -y -q python3
  elif command -v apk >/dev/null 2>&1; then apk add --quiet python3
  elif command -v pacman >/dev/null 2>&1; then pacman -Sy --noconfirm --quiet python
  elif command -v zypper >/dev/null 2>&1; then zypper --quiet --non-interactive install python3
  elif command -v brew >/dev/null 2>&1; then brew install --quiet python
  else die "python3 not found and no known package manager — install Python 3.8+ and run again"; fi
fi
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' || die "Python 3.8+ required (found $(python3 --version))"
ok "$(python3 --version)"

# --- files ------------------------------------------------------------------
mkdir -p "$DIR"
say "Downloading the agent ($BRANCH)"
if [[ -n "${FLUXBRIDGE_AGENT_SRC:-}" ]]; then cp "$FLUXBRIDGE_AGENT_SRC" "$DIR/fluxbridge_agent.py.new"   # tests / offline installs
else curl -fsSL "$RAW/agent/fluxbridge_agent.py" -o "$DIR/fluxbridge_agent.py.new"; fi
python3 -m py_compile "$DIR/fluxbridge_agent.py.new" || die "downloaded agent does not compile — try again"
mv "$DIR/fluxbridge_agent.py.new" "$DIR/fluxbridge_agent.py"
ok "Agent v$(grep -m1 '^VERSION' "$DIR/fluxbridge_agent.py" | cut -d'"' -f2) in $DIR"

if [[ "$OS" != "Darwin" ]]; then
  id -u "$SVC_USER" >/dev/null 2>&1 || useradd --system --home-dir "$DIR" --shell /usr/sbin/nologin "$SVC_USER" 2>/dev/null || useradd -r -d "$DIR" -s /sbin/nologin "$SVC_USER" \
    || adduser -S -D -H -h "$DIR" -s /sbin/nologin "$SVC_USER"     # Alpine (busybox)
  chown -R "$SVC_USER:$SVC_USER" "$DIR"; chmod 750 "$DIR"
  sudo -u "$SVC_USER" test -r "$DIR/fluxbridge_agent.py" \
    || die "user $SVC_USER cannot read $DIR — a parent directory is not traversable (chmod o+x it, or use --dir /opt/fluxbridge-agent)"
fi

# --- pairing ----------------------------------------------------------------
if [[ -n "$CODE" ]] || ! python3 -c "import json,sys; sys.exit(0 if json.load(open('$CFG')).get('token') else 1)" 2>/dev/null; then
  [[ -n "$BRIDGE" ]] || die "--bridge https://… is required for the first install"
  [[ -n "$CODE" ]] || die "--code is required for the first install (Settings → Execution Agents → New pairing code)"
  [[ "$BRIDGE" == https://* || "$BRIDGE" == http://127.0.0.1* || "$BRIDGE" == http://localhost* ]] || die "the bridge URL must start with https://"
  NAME="${NAME:-$(hostname -s 2>/dev/null || hostname)}"
  say "Pairing with $BRIDGE as '$NAME'"
  if [[ "$OS" == "Darwin" ]]; then
    (cd "$DIR" && python3 fluxbridge_agent.py --bridge "$BRIDGE" --code "$CODE" --name "$NAME" --pair-only)
  else
    sudo -u "$SVC_USER" env HOME="$DIR" "$(command -v python3)" "$DIR/fluxbridge_agent.py" --bridge "$BRIDGE" --code "$CODE" --name "$NAME" --pair-only
  fi
  chmod 600 "$CFG"
  ok "Paired — token stored in $CFG"
else
  ok "Already paired ($CFG kept) — agent updated"
fi

# --- helper command ---------------------------------------------------------
HELPER=/usr/local/bin/fluxbridge-agent
[[ "$OS" == "Darwin" && $EUID -ne 0 ]] && HELPER="$DIR/fluxbridge-agent"
cat > "$HELPER" <<HELP
#!/usr/bin/env bash
# fluxbridge-agent — status | logs [-f] | restart | update | uninstall
set -euo pipefail
DIR="$DIR"
case "\${1:-}" in
  status)
    if [[ -d /run/systemd/system ]]; then systemctl status fluxbridge-agent --no-pager
    else launchctl list | grep fluxbridge || echo "not running"; fi ;;
  logs) shift
    if [[ -d /run/systemd/system ]]; then journalctl -u fluxbridge-agent --no-pager -n 200 "\$@"
    else tail -n 200 "\$@" "\$DIR/agent.log"; fi ;;
  restart)
    if [[ -d /run/systemd/system ]]; then systemctl restart fluxbridge-agent
    else launchctl kickstart -k "gui/\$(id -u)/com.fluxbridge.agent"; fi; echo restarted ;;
  update)
    curl -fsSL "$RAW/deploy/install-agent.sh" | bash -s -- --dir "\$DIR" --branch "$BRANCH" ;;
  uninstall)
    if [[ -d /run/systemd/system ]]; then systemctl disable --now fluxbridge-agent 2>/dev/null || true; rm -f /etc/systemd/system/fluxbridge-agent.service; systemctl daemon-reload
    else launchctl bootout "gui/\$(id -u)/com.fluxbridge.agent" 2>/dev/null || true; rm -f "\$HOME/Library/LaunchAgents/com.fluxbridge.agent.plist"; fi
    rm -rf "\$DIR"; rm -f "\$0"; echo "removed — revoke the agent in the bridge (Settings → Execution Agents) as well" ;;
  *) sed -n '2p' "\$0"; exit 1 ;;
esac
HELP
chmod 755 "$HELPER"

# --- service ----------------------------------------------------------------
if [[ "$OS" == "Darwin" ]]; then
  say "Installing launchd agent (starts at login, restarts on exit)"
  PLIST="$HOME/Library/LaunchAgents/com.fluxbridge.agent.plist"; mkdir -p "$(dirname "$PLIST")"
  cat > "$PLIST" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.fluxbridge.agent</string>
  <key>ProgramArguments</key><array><string>$(command -v python3)</string><string>$DIR/fluxbridge_agent.py</string></array>
  <key>WorkingDirectory</key><string>$DIR</string>
  <key>RunAtLoad</key><true/><key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$DIR/agent.out.log</string><key>StandardErrorPath</key><string>$DIR/agent.out.log</string>
</dict></plist>
PL
  launchctl bootout "gui/$(id -u)/com.fluxbridge.agent" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$PLIST"
  ok "Running (launchctl); helper: $HELPER status|logs|restart|update|uninstall"
elif [[ -d /run/systemd/system ]]; then
  say "Installing systemd service (starts on boot, restarts on exit)"
  cat > /etc/systemd/system/fluxbridge-agent.service <<UNIT
[Unit]
Description=Fluxbridge execution agent
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$SVC_USER
Group=$SVC_USER
WorkingDirectory=$DIR
ExecStart=$(command -v python3) $DIR/fluxbridge_agent.py
Restart=always
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=$DIR
UMask=0077

[Install]
WantedBy=multi-user.target
UNIT
  systemctl daemon-reload
  systemctl enable --quiet fluxbridge-agent
  systemctl restart fluxbridge-agent
  sleep 2
  systemctl is-active --quiet fluxbridge-agent && ok "Running: fluxbridge-agent status | logs -f" || warn "service not active — check: fluxbridge-agent logs"
else
  warn "no systemd here (container / other init) — start the agent with:"
  echo "      cd $DIR && nohup python3 fluxbridge_agent.py >> agent.out.log 2>&1 &"
fi

echo
echo "  The agent appears as online in the bridge within seconds. Assign logins to it under"
echo "  Settings → Tradovate Accounts → Execute via, then Save logins."
echo

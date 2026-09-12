# Fluxbridge execution agent

A tiny helper that runs on a VPS and executes Tradovate requests for the logins you
assign to it — so every account can trade from its **own IP address** while the bridge
itself keeps running wherever it runs.

- Python 3.8+ only, no packages.
- Outbound HTTPS to the bridge only; nothing listens on the VPS.
- Never sees your dashboard login. You pair it once with a short code; it receives a
  token that only works for the relay endpoints (`/api/agent/…`).

## Install (Windows VPS) — preconfigured, nothing to type

1. In the bridge open **Settings → Execution Agents**, enter a name and press
   **Download preconfigured agent**. The zip already contains `agent.json` (bridge URL +
   this agent's token) and `fluxbridge-agent.exe`.
2. Unzip anywhere on the VPS (e.g. `C:\fluxbridge-agent`) and double-click
   `start-agent.bat` (or `fluxbridge-agent.exe`). It starts polling immediately and shows
   up as *online* in the bridge within seconds.
3. Under **Settings → Broker Accounts**, set **Execute via** to the new agent for the
   logins that should use this VPS, then **Save logins**.

Keep the zip private — it contains the agent's token (revoke it in the bridge if it leaks).

## Install (Linux / macOS VPS) — one line

In the bridge open **Settings → Execution Agents**, enter a name and press **Linux
one-liner**. Copy the command it shows and run it on the VPS as root:

```bash
curl -fsSL https://raw.githubusercontent.com/tobiasgiger/nexuspred/main/deploy/install-agent.sh \
  | sudo bash -s -- --bridge https://bridge.example.com --code ABCD-2345 --name "VPS 1"
```

It installs Python 3 if missing, downloads the agent into `/opt/fluxbridge-agent`, pairs
with the code (the token lands in `agent.json`, mode 600, owned by the service user
`fluxagent`) and installs a sandboxed systemd service that starts on boot and restarts on
exit. On macOS the same line (without `sudo`) installs into `~/fluxbridge-agent` with a
launchd agent. The agent is online in the bridge within seconds.

Afterwards: `fluxbridge-agent status | logs -f | restart | update | uninstall`. Re-running
the install line without `--code` updates the agent script and keeps the pairing.

## Alternative: plain agent + pairing code

Download *Plain agent (no token)*, install Python 3 (tick **Add python.exe to PATH**),
create a **pairing code** in the bridge (valid 15 minutes, single use), start
`start-agent.bat` and enter the bridge URL and the code.

To keep it running after you log out of the VPS, register `start-agent.bat` as a
scheduled task ("At startup", run whether user is logged on or not) or wrap it with
[NSSM](https://nssm.cc/) as a service.

Linux: `./start-agent.sh` (same flow), or run `python3 fluxbridge_agent.py` under systemd.

## What the agent will (not) do

The agent only opens **HTTPS connections to Tradovate hosts** (`*.tradovateapi.com`,
`*.tradovate.com`); any other target — including redirects — is refused before a socket
is opened. So even if the bridge were compromised, this VPS could not be used as a
general-purpose proxy. The bridge URL itself must be `https://` (plain http would send
the agent token in the clear); `localhost` is the only exception, for testing.

## What goes through the agent

Everything the assigned login does with Tradovate: order placement, modification and
cancellation, token renewal, health checks, position and P&L reads. If the agent is
offline, those logins **fail loudly** (event log + alert) — the bridge never falls back
to its own IP for them.

## Files

- `agent.json` — bridge URL, agent name and the relay token (keep it private).
- `agent.log` — one line per relayed request.

Revoke an agent any time under Settings → Execution Agents; its token stops working
immediately.

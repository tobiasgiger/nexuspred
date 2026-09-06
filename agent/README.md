# Fluxbridge execution agent

A tiny helper that runs on a VPS and executes Tradovate requests for the logins you
assign to it — so every account can trade from its **own IP address** while the bridge
itself keeps running wherever it runs.

- Python 3.8+ only, no packages.
- Outbound HTTPS to the bridge only; nothing listens on the VPS.
- Never sees your dashboard login. You pair it once with a short code; it receives a
  token that only works for the relay endpoints (`/api/agent/…`).

## Install (Windows VPS)

1. Install Python 3 from python.org and tick **Add python.exe to PATH**.
2. In the bridge open **Settings → Execution Agents → Download agent** and unzip the
   folder anywhere (e.g. `C:\fluxbridge-agent`).
3. Still in the bridge, press **New pairing code** and give the agent a name. The code
   is valid for 15 minutes and works once.
4. Double-click `start-agent.bat`, enter the bridge URL (`https://bridge.hurenzone.ch`)
   and the pairing code. The agent saves `agent.json` and starts polling.
5. Back in the bridge, under **Settings → Tradovate Accounts**, set **Execute via** to
   the new agent for the logins that should use this VPS, then **Save logins**.

To keep it running after you log out of the VPS, register `start-agent.bat` as a
scheduled task ("At startup", run whether user is logged on or not) or wrap it with
[NSSM](https://nssm.cc/) as a service.

Linux: `./start-agent.sh` (same flow), or run `python3 fluxbridge_agent.py` under systemd.

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

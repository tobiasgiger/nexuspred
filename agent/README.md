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
3. Under **Settings → Tradovate Accounts**, set **Execute via** to the new agent for the
   logins that should use this VPS, then **Save logins**.

Keep the zip private — it contains the agent's token (revoke it in the bridge if it leaks).

## Alternative: plain agent + pairing code

Download *Plain agent (no token)*, install Python 3 (tick **Add python.exe to PATH**),
create a **pairing code** in the bridge (valid 15 minutes, single use), start
`start-agent.bat` and enter the bridge URL and the code.

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

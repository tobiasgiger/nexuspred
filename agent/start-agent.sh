#!/usr/bin/env bash
# Fluxbridge execution agent — Linux/macOS launcher (restarts on exit).
cd "$(dirname "$0")"
while true; do
  python3 fluxbridge_agent.py "$@"
  echo "Agent exited ($?). Restarting in 10 s — Ctrl+C to stop."
  sleep 10
done

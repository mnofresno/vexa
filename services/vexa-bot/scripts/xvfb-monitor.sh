#!/bin/bash
# xvfb-monitor.sh — Detect and recover from Xvfb/fluxbox crashes.
#
# Runs as a background daemon. Checks X display health every 5s.
# On failure, kills stale Xvfb/fluxbox and respawns them.

set -u

DISPLAY="${1:-:99}"
RESOLUTION="${2:-1920x1080x24}"
MAX_RESTARTS=10
RESTART_COUNT=0

while true; do
  if ! xdpyinfo -display "$DISPLAY" >/dev/null 2>&1; then
    echo "[xvfb-monitor] X display $DISPLAY is down! Re-spawning... (attempt $((RESTART_COUNT + 1)))"
    if [ "$RESTART_COUNT" -ge "$MAX_RESTARTS" ]; then
      echo "[xvfb-monitor] Max restarts ($MAX_RESTARTS) reached, giving up"
      exit 1
    fi
    pkill -f "Xvfb $DISPLAY" 2>/dev/null || true
    pkill -f fluxbox 2>/dev/null || true
    sleep 1
    Xvfb "$DISPLAY" -screen 0 "$RESOLUTION" -ac +extension RANDR +extension RENDER &
    sleep 2
    mkdir -p /root/.fluxbox
    fluxbox &
    RESTART_COUNT=$((RESTART_COUNT + 1))
    echo "[xvfb-monitor] Xvfb and fluxbox respawned (total restarts: $RESTART_COUNT)"
  else
    RESTART_COUNT=0
  fi
  sleep 5
done

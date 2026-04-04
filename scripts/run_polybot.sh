#!/bin/bash
# Launcher script for Polybot LaunchAgent (ai.polybot.trader)
#
# Features:
#   - PostgreSQL readiness guard (exponential backoff)
#   - Kill switch support
#   - PID file management
#   - caffeinate to prevent sleep during trading
#   - Graceful shutdown via SIGTERM
#   - Crash recovery logging

set -euo pipefail

POLYBOT_DIR="/Users/logannye/polybot"
LOG_DIR="$POLYBOT_DIR/data"
PID_FILE="$LOG_DIR/polybot.pid"
KILL_SWITCH="$LOG_DIR/KILL_SWITCH"

export PYTHONUNBUFFERED=1
cd "$POLYBOT_DIR"

# ── Kill Switch ──────────────────────────────────────────────────────
if [ -f "$KILL_SWITCH" ]; then
    echo "$(date -u +%Y-%m-%dT%H:%M:%S) KILL SWITCH ACTIVE — refusing to start"
    echo "Remove $KILL_SWITCH to resume trading"
    exit 0  # Clean exit so launchd doesn't immediately restart
fi

# ── PostgreSQL Readiness Guard ───────────────────────────────────────
PG_ISREADY="/opt/homebrew/opt/postgresql@16/bin/pg_isready"
PG_HOST="/tmp"
PG_DB="polybot"
PG_WAIT_MAX=60
PG_WAIT_ELAPSED=0
PG_WAIT_INTERVAL=1

echo "$(date -u +%Y-%m-%dT%H:%M:%S) Waiting for PostgreSQL ($PG_DB)..."

while [ $PG_WAIT_ELAPSED -lt $PG_WAIT_MAX ]; do
    if $PG_ISREADY -h "$PG_HOST" -d "$PG_DB" -q 2>/dev/null; then
        echo "$(date -u +%Y-%m-%dT%H:%M:%S) PostgreSQL ready (waited ${PG_WAIT_ELAPSED}s)"
        break
    fi
    sleep $PG_WAIT_INTERVAL
    PG_WAIT_ELAPSED=$((PG_WAIT_ELAPSED + PG_WAIT_INTERVAL))
    if [ $PG_WAIT_INTERVAL -lt 8 ]; then
        PG_WAIT_INTERVAL=$((PG_WAIT_INTERVAL * 2))
    fi
done

if ! $PG_ISREADY -h "$PG_HOST" -d "$PG_DB" -q 2>/dev/null; then
    echo "$(date -u +%Y-%m-%dT%H:%M:%S) TIMEOUT: PostgreSQL not available after ${PG_WAIT_MAX}s"
    echo "  Exiting with code 1 — launchd will retry after ThrottleInterval"
    exit 1
fi

# ── PID File / Orphan Guard ──────────────────────────────────────────
if [ -f "$PID_FILE" ]; then
    OLD_PID=$(cat "$PID_FILE" 2>/dev/null)
    if kill -0 "$OLD_PID" 2>/dev/null; then
        echo "$(date -u +%Y-%m-%dT%H:%M:%S) WARNING: Existing Polybot process $OLD_PID still running"
    else
        echo "$(date -u +%Y-%m-%dT%H:%M:%S) Stale PID file (PID $OLD_PID dead), removing"
        rm -f "$PID_FILE"
    fi
fi

# ── Port Cleanup (prevent EADDRINUSE on restart) ───────────────────
DASHBOARD_PORT=8080
STALE_PID=$(lsof -ti :$DASHBOARD_PORT 2>/dev/null || true)
if [ -n "$STALE_PID" ]; then
    echo "$(date -u +%Y-%m-%dT%H:%M:%S) Killing stale process on port $DASHBOARD_PORT (PID $STALE_PID)"
    kill -TERM $STALE_PID 2>/dev/null || true
    sleep 2
    kill -KILL $STALE_PID 2>/dev/null || true
fi

# ── Pre-flight ───────────────────────────────────────────────────────
echo "$(date -u +%Y-%m-%dT%H:%M:%S) Starting Polybot..."
echo "  uv:    $(uv --version 2>&1)"
echo "  Dir:   $POLYBOT_DIR"
echo "  PID:   $$"

# ── Trap Signals ─────────────────────────────────────────────────────
cleanup() {
    echo "$(date -u +%Y-%m-%dT%H:%M:%S) Received shutdown signal, cleaning up..."
    kill -TERM "$CHILD_PID" 2>/dev/null || true
    sleep 3
    kill -KILL "$CHILD_PID" 2>/dev/null || true
    rm -f "$PID_FILE"
    wait "$CHILD_PID" 2>/dev/null || true
    echo "$(date -u +%Y-%m-%dT%H:%M:%S) Polybot stopped."
    exit 0
}
trap cleanup SIGTERM SIGINT SIGHUP

# ── Launch ───────────────────────────────────────────────────────────
uv run python -m polybot &

CHILD_PID=$!
echo "$CHILD_PID" > "$PID_FILE"
echo "$(date -u +%Y-%m-%dT%H:%M:%S) Polybot running as PID $CHILD_PID"

# ── Prevent Sleep ────────────────────────────────────────────────────
caffeinate -i -s -w "$CHILD_PID" &
echo "$(date -u +%Y-%m-%dT%H:%M:%S) caffeinate preventing sleep (watching PID $CHILD_PID)"

# Wait for child — propagates signals via trap
wait "$CHILD_PID"
EXIT_CODE=$?

rm -f "$PID_FILE"
echo "$(date -u +%Y-%m-%dT%H:%M:%S) Polybot exited with code $EXIT_CODE"
exit $EXIT_CODE

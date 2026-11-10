#!/usr/bin/env bash
# Stop the api-test-server started by start.sh. Safe to call when not running.
set -euo pipefail

STATE="${API_TEST_STATE_DIR:-${TMPDIR:-/tmp}/gough-api-test-server-$(id -u)}"
PIDFILE="$STATE/pid"

if [ ! -f "$PIDFILE" ]; then
  echo "stop.sh: not running"
  exit 0
fi

PID="$(cat "$PIDFILE")"
if kill -0 "$PID" 2>/dev/null; then
  kill "$PID"
  i=0
  while kill -0 "$PID" 2>/dev/null && [ "$i" -lt 20 ]; do
    sleep 0.5
    i=$((i + 1))
  done
  if kill -0 "$PID" 2>/dev/null; then
    kill -9 "$PID"
  fi
fi
rm -f "$PIDFILE" "$STATE/port"
echo "stop.sh: stopped"

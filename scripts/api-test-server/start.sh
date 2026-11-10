#!/usr/bin/env bash
# Start the real api-manager hermetically on 127.0.0.1:<random free port>.
# Prints BASE_URL=... on success; on failure prints the server log and exits 1.
# Env: API_TEST_STATE_DIR (default $TMPDIR/gough-api-test-server-<uid>),
#      API_TEST_PYTHON (default <repo>/.venv/bin/python), API_TEST_TIMEOUT (s, default 60)
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
STATE="${API_TEST_STATE_DIR:-${TMPDIR:-/tmp}/gough-api-test-server-$(id -u)}"
PY="${API_TEST_PYTHON:-$REPO/.venv/bin/python}"
TIMEOUT="${API_TEST_TIMEOUT:-60}"

if [ ! -x "$PY" ]; then
  echo "start.sh: python not found at $PY (create venv: uv venv -p 3.13 .venv)" >&2
  exit 1
fi

# Idempotent: tear down any previous instance first.
API_TEST_STATE_DIR="$STATE" "$HERE/stop.sh" >/dev/null

mkdir -p "$STATE"
chmod 700 "$STATE"
rm -f "$STATE/gough.db" "$STATE/server.log" "$STATE/port" "$STATE/pid"

PORT="$("$PY" -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()')"
ENC_KEY="$("$PY" -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"
RAND="$("$PY" -c 'import secrets; print(secrets.token_hex(32))')"

export DB_TYPE=sqlite
export DB_NAME="$STATE/gough"
export ENCRYPTION_KEY="$ENC_KEY"
export SECRET_KEY="$RAND"
export JWT_SECRET_KEY="$RAND"
export SECURITY_PASSWORD_SALT="$RAND"
export RATE_LIMIT_ENABLED=false
export AUDIT_RECORDING_PATH="$STATE/recordings"
export SECRETS_BACKEND=encrypted_db

nohup "$PY" "$HERE/server.py" --port "$PORT" >"$STATE/server.log" 2>&1 &
PID=$!
echo "$PID" >"$STATE/pid"
echo "$PORT" >"$STATE/port"

i=0
while [ "$i" -lt "$TIMEOUT" ]; do
  if ! kill -0 "$PID" 2>/dev/null; then
    echo "start.sh: server exited during startup; log follows" >&2
    cat "$STATE/server.log" >&2
    rm -f "$STATE/pid"
    exit 1
  fi
  if curl -sf -o /dev/null "http://127.0.0.1:$PORT/healthz"; then
    echo "BASE_URL=http://127.0.0.1:$PORT"
    exit 0
  fi
  sleep 1
  i=$((i + 1))
done

echo "start.sh: not healthy after ${TIMEOUT}s; log follows" >&2
cat "$STATE/server.log" >&2
API_TEST_STATE_DIR="$STATE" "$HERE/stop.sh" >/dev/null
exit 1

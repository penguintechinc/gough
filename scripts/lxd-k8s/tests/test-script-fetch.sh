#!/usr/bin/env bash
# test-script-fetch.sh - tests for gough-script-fetch.sh: lock validation, authenticated HTTPS-only fetch with sha256
# verification, credential handling, redirect and 401/403/404 behaviour, best-effort events and OTLP, and run-in.
# A local python3 fixture server (serves fixture files, logs request headers, simulates 401/403/302) and a PATH-stub lxc
# (logs arguments only, never file contents) drive the cases. Prints "N passed, M failed"; exits non-zero on any failure or when no case ran.
# GOUGH_SCRIPT_UNDER_TEST overrides the script path so mutated copies can be checked against this suite.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT="${GOUGH_SCRIPT_UNDER_TEST:-${SCRIPT_DIR}/../gough-script-fetch.sh}"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/test-script-fetch.XXXXXX")"
SERVE="${WORK}/serve"
LXC_ROOT="${WORK}/lxc-root"
LXC_LOG="${WORK}/lxc.log"
HTTP_LOG="${WORK}/http.log"
REPORT_LOG="${WORK}/reports.log"
OTLP_LOG="${WORK}/otlp.log"
ERR_FILE="${WORK}/stderr.txt"
TOKEN_VALUE="tok-SECRET-XYZ987"
BASIC_VALUE="alice:pw-BASIC-456"
HTTP_PID=""
PORT=""
DEAD_PORT=""
PASSED=0
FAILED=0
RC=0
OUT=""
ERR=""
HASH=""
CASE_DIR=""

# cleanup stops the fixture server and removes the whole work directory, including any copied credentials.
cleanup() {
  if [[ -n $HTTP_PID ]] && kill -0 "$HTTP_PID" 2>/dev/null; then
    kill "$HTTP_PID"
  fi
  rm -rf -- "$WORK"
}
trap cleanup EXIT

mkdir -p "$SERVE" "${WORK}/bin" "$LXC_ROOT"
export LXC_LOG LXC_ROOT
export PATH="${WORK}/bin:${PATH}"

cat > "${WORK}/bin/lxc" <<'STUB'
#!/usr/bin/env bash
# lxc stub: logs each invocation's arguments, maps container paths onto LXC_ROOT, and runs pushed scripts with a clean env.
set -euo pipefail
printf '%s\n' "$*" >> "${LXC_LOG}"
case "$1" in
  file)
    src=$6
    target=$7
    dest="${LXC_ROOT}/${target}"
    mkdir -p "${dest%/*}"
    cp -- "$src" "$dest"
    chmod 0700 "$dest"
    ;;
  exec)
    inst=$2
    shift 2
    envs=()
    while [[ $# -gt 0 && "$1" != "--" ]]; do
      if [[ "$1" == "--env" ]]; then
        envs+=("$2")
        shift 2
      else
        shift
      fi
    done
    shift
    if [[ "$1" == "rm" ]]; then
      rm -f -- "${LXC_ROOT}/${inst}${!#}"
      exit 0
    fi
    exec env -i PATH=/usr/bin:/bin ${envs[@]+"${envs[@]}"} "${LXC_ROOT}/${inst}$1" "${@:2}"
    ;;
  *)
    printf 'stub: unsupported lxc subcommand: %s\n' "$1" >&2
    exit 99
    ;;
esac
STUB
chmod 0755 "${WORK}/bin/lxc"

cat > "${WORK}/server.py" <<'PY'
# Fixture server: serves files from SERVE, logs every request line with its Authorization header, returns 401 under /deny/,
# 403 under /forbid/, 302 under /redir/ (to /landing/), and records POST bodies for /report and /v1/logs.
import http.server
import os
import sys

SERVE, HTTP_LOG, REPORT_LOG, OTLP_LOG, PORT = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4], int(sys.argv[5])


class Handler(http.server.BaseHTTPRequestHandler):
    """Fixture HTTP handler that simulates the scripts host, redirects, and event sinks."""

    def log_message(self, fmt, *args):
        return

    def record(self):
        with open(HTTP_LOG, "a") as fh:
            fh.write("%s %s AUTH=%s\n" % (self.command, self.path, self.headers.get("Authorization", "-")))

    def status_only(self, code):
        self.send_response(code)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        self.record()
        if self.path.startswith("/deny/"):
            return self.status_only(401)
        if self.path.startswith("/forbid/"):
            return self.status_only(403)
        if self.path.startswith("/redir/"):
            self.send_response(302)
            self.send_header("Location", "/landing/" + self.path[len("/redir/"):])
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        name = self.path.split("/")[-1]
        target = os.path.join(SERVE, name)
        if not os.path.isfile(target):
            return self.status_only(404)
        with open(target, "rb") as fh:
            body = fh.read()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        self.record()
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        if self.path == "/report":
            target = REPORT_LOG
        elif self.path == "/v1/logs":
            target = OTLP_LOG
        else:
            return self.status_only(404)
        with open(target, "ab") as fh:
            fh.write(body + b"\n")
        self.status_only(200)


http.server.ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
PY

PORT=$(python3 -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()')
DEAD_PORT=$(python3 -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()')
python3 -I "${WORK}/server.py" "$SERVE" "$HTTP_LOG" "$REPORT_LOG" "$OTLP_LOG" "$PORT" >"${WORK}/server.out" 2>&1 &
HTTP_PID=$!
READY=0
for ((i = 0; i < 50; i++)); do
  if curl -sS -o /dev/null "http://127.0.0.1:${PORT}/"; then
    READY=1
    break
  fi
  sleep 0.1
done
if [[ $READY != 1 ]]; then
  echo "FATAL: fixture server did not start" >&2
  exit 1
fi

# pass records a passing case and prints its label.
pass() {
  PASSED=$((PASSED + 1))
  printf 'PASS: %s\n' "$1"
}

# fail records a failing case with a short reason.
fail() {
  FAILED=$((FAILED + 1))
  printf 'FAIL: %s (%s)\n' "$1" "$2"
}

# check passes when the given test command succeeds, e.g. check "label" [ -f file ].
check() {
  local label=$1
  shift
  if "$@"; then
    pass "$label"
  else
    fail "$label" "condition false: $*"
  fi
}

# expect_ok passes when the last run exited 0.
expect_ok() {
  if (( RC == 0 )); then
    pass "$1"
  else
    fail "$1" "rc=$RC err=${ERR%%$'\n'*}"
  fi
}

# expect_fail passes when the last run exited non-zero and its stderr names the expected reason.
expect_fail() {
  if (( RC != 0 )) && grep -qF -- "$2" <<<"$ERR"; then
    pass "$1"
  else
    fail "$1" "want rc!=0 and stderr '$2'; rc=$RC"
  fi
}

# expect_out passes when the last run's stdout contains the given text.
expect_out() {
  if grep -qF -- "$2" <<<"$OUT"; then
    pass "$1"
  else
    fail "$1" "stdout lacks: $2"
  fi
}

# expect_err passes when the last run's stderr contains the given text.
expect_err() {
  if grep -qF -- "$2" <<<"$ERR"; then
    pass "$1"
  else
    fail "$1" "stderr lacks: $2"
  fi
}

# expect_file_has passes when the file exists and contains the given text.
expect_file_has() {
  if [[ -f $2 ]] && grep -qF -- "$3" "$2"; then
    pass "$1"
  else
    fail "$1" "missing from $2: $3"
  fi
}

# expect_file_lacks passes when the file is absent or does not contain the given text.
expect_file_lacks() {
  if [[ ! -f $2 ]] || ! grep -qF -- "$3" "$2"; then
    pass "$1"
  else
    fail "$1" "unexpectedly present in $2: $3"
  fi
}

# expect_no_token passes when the token value appears in no lxc log, report body, OTLP body, stdout, or stderr.
expect_no_token() {
  local f leaked=0
  for f in "$LXC_LOG" "$REPORT_LOG" "$OTLP_LOG"; do
    if [[ -f $f ]] && grep -qF -- "$TOKEN_VALUE" "$f"; then
      leaked=1
    fi
  done
  if grep -qF -- "$TOKEN_VALUE" <<<"$OUT" || grep -qF -- "$TOKEN_VALUE" <<<"$ERR"; then
    leaked=1
  fi
  if (( leaked == 0 )); then
    pass "$1"
  else
    fail "$1" "token value found in an output or log"
  fi
}

# run executes the script with the given arguments, capturing stdout to OUT, stderr to ERR, and the status to RC.
run() {
  RC=0
  OUT=$("$SCRIPT" "$@" 2>"$ERR_FILE") || RC=$?
  ERR=$(<"$ERR_FILE")
}

# new_case <id> creates a fresh cache, lock path, and token file, and exports the environment every case starts from.
# Cases then override or unset single variables; new_case always resets them.
new_case() {
  CASE_DIR="${WORK}/case-$1"
  mkdir -p "${CASE_DIR}/cache"
  printf '%s\n' "$TOKEN_VALUE" > "${CASE_DIR}/token"
  chmod 0600 "${CASE_DIR}/token"
  printf '%s\n' "$BASIC_VALUE" > "${CASE_DIR}/basic"
  chmod 0600 "${CASE_DIR}/basic"
  export GOUGH_SCRIPTS_LOCK="${CASE_DIR}/scripts.lock"
  export GOUGH_SCRIPTS_CACHE="${CASE_DIR}/cache"
  export GOUGH_SCRIPTS_BASE_URL="http://127.0.0.1:${PORT}"
  export GOUGH_SCRIPTS_TOKEN_FILE="${CASE_DIR}/token"
  export GOUGH_ALLOW_INSECURE_SCRIPTS_HOST=1
  export GOUGH_SCRIPTS_TIMEOUT=10
  export GOUGH_SCRIPTS_MAX_BYTES=1048576
  export TMPDIR="${CASE_DIR}"
  unset GOUGH_SCRIPTS_BASIC_FILE GOUGH_SCRIPTS_CA_FILE GOUGH_SCRIPT_ENV KEEP_PUSHED
  unset GOUGH_REPORT_URL OTEL_EXPORTER_OTLP_ENDPOINT OTEL_EXPORTER_OTLP_HEADERS
  : > "$LXC_LOG"
}

# make_script <name@version> <body> serves a fixture script and sets HASH to its sha256, the value a lock would pin.
make_script() {
  printf '%s' "$2" > "${SERVE}/$1"
  HASH=$(sha256sum "${SERVE}/$1" | awk '{print $1}')
}

# script_body <exit-code> prints a fixture biome script that echoes its arguments and two env vars, then exits with the code.
# The body is a quoted heredoc so its own $ expansions are left for the fixture to evaluate; only @EXIT@ is substituted.
script_body() {
  sed "s/@EXIT@/$1/" <<'BODY'
#!/usr/bin/env bash
printf "ran %s\n" "$*"
printf "FOO_MODE=%s\n" "${FOO_MODE:-unset}"
printf "OTHER_NOT_LISTED=%s\n" "${OTHER_NOT_LISTED:-unset}"
exit @EXIT@
BODY
}

# write_lock <name@version> <hash> writes a one-entry lock with the mode lock-add produces.
write_lock() {
  printf '%s  %s\n' "$2" "$1" > "$GOUGH_SCRIPTS_LOCK"
  chmod 0644 "$GOUGH_SCRIPTS_LOCK"
}

# http_lines prints how many request lines the fixture server has logged so far.
http_lines() {
  awk 'END { print NR }' "$HTTP_LOG"
}

# order_of prints the lxc operations logged for the current case as a sequence of push, exec, and rm tokens.
order_of() {
  awk '
    $1 == "file" { s = s "push " }
    $1 == "exec" && index($0, " -- rm ") > 0 { s = s "rm " }
    $1 == "exec" && index($0, " -- rm ") == 0 { s = s "exec " }
    END { print s }
  ' "$LXC_LOG"
}

# warn_count prints how many event-delivery WARN lines the last run's stderr contains.
# Matches the exact delivery message so the separate insecure-mode WARNING line is not counted.
warn_count() {
  awk '/WARN: event delivery failed/ { n++ } END { print n + 0 }' <<<"$ERR"
}

# ---------------------------------------------------------------- lock and name validation

new_case unknown
make_script demo-u@1.0.0 "$(script_body 0)"
write_lock demo-u@1.0.0 "$HASH"
before=$(http_lines)
run fetch demo-u 2.0.0
expect_fail "name@version not in lock is refused" "not in lock"
check "not-in-lock refusal makes no network request" [ "$(http_lines)" = "$before" ]

new_case emptylock
printf '# no entries\n\n' > "$GOUGH_SCRIPTS_LOCK"
chmod 0644 "$GOUGH_SCRIPTS_LOCK"
run fetch demo-e 1.0.0
expect_fail "empty lock is refused" "zero entries"

new_case duplicate
make_script demo-d@1.0.0 "$(script_body 0)"
printf '%s  %s\n%s  %s\n' "$HASH" demo-d@1.0.0 "$HASH" demo-d@1.0.0 > "$GOUGH_SCRIPTS_LOCK"
chmod 0644 "$GOUGH_SCRIPTS_LOCK"
run fetch demo-d 1.0.0
expect_fail "duplicate lock entries are refused" "duplicates"

new_case groupwrite
make_script demo-gw@1.0.0 "$(script_body 0)"
write_lock demo-gw@1.0.0 "$HASH"
chmod 0664 "$GOUGH_SCRIPTS_LOCK"
run fetch demo-gw 1.0.0
expect_fail "group-writable lock is refused" "group- or world-writable"

new_case symlock
make_script demo-sl@1.0.0 "$(script_body 0)"
write_lock demo-sl@1.0.0 "$HASH"
mv "$GOUGH_SCRIPTS_LOCK" "${CASE_DIR}/real.lock"
ln -s real.lock "$GOUGH_SCRIPTS_LOCK"
run fetch demo-sl 1.0.0
expect_fail "symlinked lock is refused" "symlink"

new_case nolock
run lock-list
expect_fail "missing lock is refused" "missing or not a regular file"

new_case mutable
make_script demo-l@1.0.0 "$(script_body 0)"
write_lock demo-l@1.0.0 "$HASH"
run fetch demo-l latest
expect_fail "version 'latest' is refused" "mutable version refused"
run fetch demo-l HEAD
expect_fail "version 'HEAD' is refused case-insensitively" "mutable version refused"

new_case poisoned
printf '%s  demo-lm@latest\n' "$HASH" > "$GOUGH_SCRIPTS_LOCK"
chmod 0644 "$GOUGH_SCRIPTS_LOCK"
run fetch demo-lm 1.0.0
expect_fail "lock entry with a mutable version fails the whole lock" "mutable version refused"

new_case badnames
for bad in '../evil' '/abs' 'a//b'; do
  run fetch "$bad" 1.0.0
  expect_fail "invalid name refused: $bad" "invalid name"
done

new_case lockcount
make_script demo-lc@1.0.0 "$(script_body 0)"
write_lock demo-lc@1.0.0 "$HASH"
run lock-list
expect_ok "lock-list succeeds on a valid lock"
expect_out "lock-list prints the count" "count: 1"
expect_err "lock read prints the entry count" "lock: 1 entries"

new_case nobase
make_script demo-nb@1.0.0 "$(script_body 0)"
write_lock demo-nb@1.0.0 "$HASH"
unset GOUGH_SCRIPTS_BASE_URL
run fetch demo-nb 1.0.0
expect_fail "fetch without GOUGH_SCRIPTS_BASE_URL is refused" "GOUGH_SCRIPTS_BASE_URL is required"

# ---------------------------------------------------------------- credentials and transport

new_case nocreds
make_script demo-nc@1.0.0 "$(script_body 0)"
write_lock demo-nc@1.0.0 "$HASH"
unset GOUGH_SCRIPTS_TOKEN_FILE GOUGH_ALLOW_INSECURE_SCRIPTS_HOST
before=$(http_lines)
run fetch demo-nc 1.0.0
expect_fail "no credentials and no insecure flag FAILS" "no scripts-host credentials"
check "no-credentials refusal makes no network request" [ "$(http_lines)" = "$before" ]

new_case tokenmode
make_script demo-tm@1.0.0 "$(script_body 0)"
write_lock demo-tm@1.0.0 "$HASH"
chmod 0644 "${CASE_DIR}/token"
run fetch demo-tm 1.0.0
expect_fail "token file with mode 0644 FAILS" "mode must be 0600 or 0400"

new_case tokenlink
make_script demo-tl@1.0.0 "$(script_body 0)"
write_lock demo-tl@1.0.0 "$HASH"
mv "${CASE_DIR}/token" "${CASE_DIR}/real-token"
ln -s real-token "${CASE_DIR}/token"
run fetch demo-tl 1.0.0
expect_fail "symlinked token file FAILS" "symlink"

new_case tokenempty
make_script demo-te@1.0.0 "$(script_body 0)"
write_lock demo-te@1.0.0 "$HASH"
: > "${CASE_DIR}/empty"
chmod 0600 "${CASE_DIR}/empty"
export GOUGH_SCRIPTS_TOKEN_FILE="${CASE_DIR}/empty"
run fetch demo-te 1.0.0
expect_fail "empty credential file FAILS" "credential file is empty"

new_case bearer
make_script demo-b@1.0.0 "$(script_body 0)"
write_lock demo-b@1.0.0 "$HASH"
run fetch demo-b 1.0.0
expect_ok "fetch with bearer token succeeds"
expect_file_has "bearer header reached the host" "$HTTP_LOG" "GET /demo-b@1.0.0 AUTH=Bearer ${TOKEN_VALUE}"
expect_no_token "bearer token absent from stdout, stderr, and logs"

new_case basic
make_script demo-bs@1.0.0 "$(script_body 0)"
write_lock demo-bs@1.0.0 "$HASH"
unset GOUGH_SCRIPTS_TOKEN_FILE
export GOUGH_SCRIPTS_BASIC_FILE="${CASE_DIR}/basic"
b64=$(printf '%s' "$BASIC_VALUE" | base64 -w0)
run fetch demo-bs 1.0.0
expect_ok "basic fallback fetch succeeds"
expect_file_has "basic header reached the host when only the basic file is set" "$HTTP_LOG" "GET /demo-bs@1.0.0 AUTH=Basic ${b64}"

new_case bothcreds
make_script demo-bw@1.0.0 "$(script_body 0)"
write_lock demo-bw@1.0.0 "$HASH"
export GOUGH_SCRIPTS_BASIC_FILE="${CASE_DIR}/basic"
run fetch demo-bw 1.0.0
expect_ok "fetch with both credential files succeeds"
expect_file_has "bearer wins when both are set" "$HTTP_LOG" "GET /demo-bw@1.0.0 AUTH=Bearer ${TOKEN_VALUE}"
expect_file_lacks "basic is not sent when the bearer token is set" "$HTTP_LOG" "GET /demo-bw@1.0.0 AUTH=Basic"

new_case plainremote
make_script demo-pr@1.0.0 "$(script_body 0)"
write_lock demo-pr@1.0.0 "$HASH"
export GOUGH_SCRIPTS_BASE_URL="http://192.0.2.10:8080"
before=$(http_lines)
run fetch demo-pr 1.0.0
expect_fail "credentials over plain http to a non-local host are refused" "plain http refused"
check "refused remote-http fetch makes no network request" [ "$(http_lines)" = "$before" ]

new_case plainlocal
make_script demo-pl@1.0.0 "$(script_body 0)"
write_lock demo-pl@1.0.0 "$HASH"
unset GOUGH_ALLOW_INSECURE_SCRIPTS_HOST
run fetch demo-pl 1.0.0
expect_fail "credentials over plain http are refused without the insecure flag" "plain http refused"

new_case deny
make_script demo-deny@1.0.0 "$(script_body 0)"
write_lock demo-deny@1.0.0 "$HASH"
export GOUGH_SCRIPTS_BASE_URL="http://127.0.0.1:${PORT}/deny"
export GOUGH_REPORT_URL="http://127.0.0.1:${PORT}/report"
run fetch demo-deny 1.0.0
expect_fail "HTTP 401 gives the distinct rejected-credentials error" "scripts host rejected the credentials"
expect_file_has "auth_rejected event is reported with the key" "$REPORT_LOG" '"event":"script.fetch.auth_rejected","severity":"error","name@version":"demo-deny@1.0.0"'
expect_no_token "token absent from the 401 error and report"

new_case forbid
make_script demo-forbid@1.0.0 "$(script_body 0)"
write_lock demo-forbid@1.0.0 "$HASH"
export GOUGH_SCRIPTS_BASE_URL="http://127.0.0.1:${PORT}/forbid"
run fetch demo-forbid 1.0.0
expect_fail "HTTP 403 gives the distinct rejected-credentials error" "scripts host rejected the credentials"

new_case notpub
make_script demo-np@1.0.0 "$(script_body 0)"
write_lock demo-np@1.0.0 "$HASH"
rm -f "${SERVE}/demo-np@1.0.0"
run fetch demo-np 1.0.0
expect_fail "HTTP 404 reports script not published" "script not published"

new_case redirect
make_script demo-redir@1.0.0 "$(script_body 0)"
write_lock demo-redir@1.0.0 "$HASH"
export GOUGH_SCRIPTS_BASE_URL="http://127.0.0.1:${PORT}/redir"
run fetch demo-redir 1.0.0
expect_fail "credentialed redirect is not followed (download fails)" "download failed"
expect_file_lacks "redirect target never received a request" "$HTTP_LOG" "/landing/demo-redir@1.0.0"

# ---------------------------------------------------------------- fetch, verify, cache, lock-add

new_case happy
make_script demo-h@1.0.0 "$(script_body 0)"
write_lock demo-h@1.0.0 "$HASH"
run fetch demo-h 1.0.0
expect_ok "fetch happy path succeeds"
cached="${GOUGH_SCRIPTS_CACHE}/demo-h@1.0.0"
check "fetch prints only the cached path on stdout" [ "$OUT" = "$cached" ]
check "cached file is mode 0500" [ "$(stat -c %a "$cached")" = 500 ]
expect_err "structured ok event line is written to stderr" "event=script.fetch.ok severity=info"
run verify demo-h 1.0.0
expect_ok "verify accepts the untouched cache"

new_case mismatch
make_script demo-mm@1.0.0 "$(script_body 0)"
write_lock demo-mm@1.0.0 "$HASH"
printf 'tampered\n' > "${SERVE}/demo-mm@1.0.0"
actual=$(sha256sum "${SERVE}/demo-mm@1.0.0" | awk '{print $1}')
export GOUGH_REPORT_URL="http://127.0.0.1:${PORT}/report"
run fetch demo-mm 1.0.0
expect_fail "tampered download is refused" "sha256 mismatch"
check "nothing is cached after a mismatch" [ ! -e "${GOUGH_SCRIPTS_CACHE}/demo-mm@1.0.0" ]
expect_file_has "hash_mismatch event carries the expected hash" "$REPORT_LOG" "\"event\":\"script.fetch.hash_mismatch\",\"severity\":\"error\",\"name@version\":\"demo-mm@1.0.0\""
expect_file_has "hash_mismatch event carries the expected sha256" "$REPORT_LOG" "\"expected_sha256\":\"${HASH}\""
expect_file_has "hash_mismatch event carries the actual sha256" "$REPORT_LOG" "\"actual_sha256\":\"${actual}\""

new_case cachetamper
make_script demo-tv@1.0.0 "$(script_body 0)"
write_lock demo-tv@1.0.0 "$HASH"
run fetch demo-tv 1.0.0
expect_ok "fetch before cache tampering succeeds"
cached="${GOUGH_SCRIPTS_CACHE}/demo-tv@1.0.0"
chmod 0600 "$cached"
printf 'evil' >> "$cached"
run verify demo-tv 1.0.0
expect_fail "tampered cache fails verify" "cache mismatch"
check "tampered cache file is deleted" [ ! -e "$cached" ]

new_case lockadd
make_script demo-la@1.1.0 "$(script_body 0)"
check "lock does not exist before lock-add" [ ! -e "$GOUGH_SCRIPTS_LOCK" ]
run lock-add demo-la 1.1.0 "${SERVE}/demo-la@1.1.0"
expect_ok "lock-add creates the lock"
check "created lock is mode 0644" [ "$(stat -c %a "$GOUGH_SCRIPTS_LOCK")" = 644 ]
run lock-list
expect_out "lock-list shows the new entry count" "count: 1"
run fetch demo-la 1.1.0
expect_ok "fetch works after lock-add"
printf 'different\n' > "${CASE_DIR}/other.sh"
run lock-add demo-la 1.1.0 "${CASE_DIR}/other.sh"
expect_fail "lock-add with a different hash FAILS" "different hash"
before=$(sha256sum "$GOUGH_SCRIPTS_LOCK")
run lock-add demo-la 1.1.0 "${SERVE}/demo-la@1.1.0"
expect_ok "lock-add with the same hash succeeds"
expect_err "lock-add with the same hash is a no-op" "no-op"
check "same-hash lock-add leaves the lock unchanged" [ "$before" = "$(sha256sum "$GOUGH_SCRIPTS_LOCK")" ]

new_case oversize
make_script demo-os@1.0.0 "$(printf 'x%.0s' {1..200})"
write_lock demo-os@1.0.0 "$HASH"
export GOUGH_SCRIPTS_MAX_BYTES=100
run fetch demo-os 1.0.0
expect_fail "oversize download is refused" "download failed"
check "oversize download is not cached" [ ! -e "${GOUGH_SCRIPTS_CACHE}/demo-os@1.0.0" ]

new_case deadreport
make_script demo-dr@1.0.0 "$(script_body 0)"
write_lock demo-dr@1.0.0 "$HASH"
export GOUGH_REPORT_URL="http://127.0.0.1:${DEAD_PORT}/report"
run fetch demo-dr 1.0.0
expect_ok "dead report sink does not change the exit status"
expect_out "fetch still prints the cached path" "${GOUGH_SCRIPTS_CACHE}/demo-dr@1.0.0"
check "at most one WARN line when delivery fails" [ "$(warn_count)" -le 1 ]

new_case otlpon
make_script demo-ot@1.0.0 "$(script_body 0)"
write_lock demo-ot@1.0.0 "$HASH"
export OTEL_EXPORTER_OTLP_ENDPOINT="http://127.0.0.1:${PORT}"
run fetch demo-ot 1.0.0
expect_ok "fetch succeeds with OTLP configured"
expect_file_has "OTLP log record is posted when the endpoint is set" "$OTLP_LOG" '"logRecords"'
expect_file_has "OTLP record carries the event" "$OTLP_LOG" '{"key":"event","value":{"stringValue":"script.fetch.ok"}}'
expect_file_has "OTLP record carries name@version" "$OTLP_LOG" '{"key":"name@version","value":{"stringValue":"demo-ot@1.0.0"}}'

new_case otlpoff
make_script demo-noot@1.0.0 "$(script_body 0)"
write_lock demo-noot@1.0.0 "$HASH"
unset OTEL_EXPORTER_OTLP_ENDPOINT
run fetch demo-noot 1.0.0
expect_ok "fetch succeeds with no sinks configured"
expect_file_lacks "nothing is posted to OTLP when the endpoint is unset" "$OTLP_LOG" "demo-noot@1.0.0"
expect_file_lacks "nothing is posted to the report sink when unset" "$REPORT_LOG" "demo-noot@1.0.0"

# ---------------------------------------------------------------- run-in

new_case runok
make_script demo-run@1.0.0 "$(script_body 0)"
write_lock demo-run@1.0.0 "$HASH"
export GOUGH_REPORT_URL="http://127.0.0.1:${PORT}/report"
run run-in inst-ok demo-run 1.0.0 -- hello world
expect_ok "run-in exits 0 with the script status"
expect_out "script output reaches stdout" "ran hello world"
check "push, exec, rm order" [ "$(order_of)" = "push exec rm " ]
check "container copy is removed" [ ! -e "${LXC_ROOT}/inst-ok/var/lib/gough/scripts/demo-run@1.0.0" ]
expect_file_has "run.ok event is reported" "$REPORT_LOG" '"event":"script.run.ok","severity":"info","name@version":"demo-run@1.0.0"'
expect_no_token "token absent from lxc log, stdout, stderr, and reports"

new_case run7
make_script demo-r7@1.0.0 "$(script_body 7)"
write_lock demo-r7@1.0.0 "$HASH"
export GOUGH_REPORT_URL="http://127.0.0.1:${PORT}/report"
run run-in inst-7 demo-r7 1.0.0
check "script exit status 7 propagates" [ "$RC" -eq 7 ]
check "push, exec, rm order on failure" [ "$(order_of)" = "push exec rm " ]
expect_file_has "run.failed event carries the exit code" "$REPORT_LOG" '"event":"script.run.failed","severity":"error","name@version":"demo-r7@1.0.0","node":'
expect_file_has "run.failed event exit_code is 7" "$REPORT_LOG" '"exit_code":"7"'

new_case runtamper
make_script demo-rt@1.0.0 "$(script_body 0)"
write_lock demo-rt@1.0.0 "$HASH"
run fetch demo-rt 1.0.0
expect_ok "fetch before run-in tampering succeeds"
cached="${GOUGH_SCRIPTS_CACHE}/demo-rt@1.0.0"
chmod 0600 "$cached"
printf 'evil' >> "$cached"
: > "$LXC_LOG"
run run-in inst-rt demo-rt 1.0.0
expect_fail "tampered cache is refused before any push" "cache mismatch"
check "no push or exec after tampering" [ -z "$(order_of)" ]

new_case envfwd
make_script demo-env@1.0.0 "$(script_body 0)"
write_lock demo-env@1.0.0 "$HASH"
export FOO_MODE=alpha-mode OTHER_NOT_LISTED=should-not-forward GOUGH_SCRIPT_ENV="FOO_MODE"
run run-in inst-env demo-env 1.0.0
expect_ok "forwarded run exits 0"
expect_out "listed variable reaches the script" "FOO_MODE=alpha-mode"
expect_out "unlisted variable does not reach the script" "OTHER_NOT_LISTED=unset"
expect_file_has "only the listed name is passed to lxc" "$LXC_LOG" "--env FOO_MODE=alpha-mode"
expect_file_lacks "unlisted value never reaches lxc" "$LXC_LOG" "should-not-forward"
unset FOO_MODE OTHER_NOT_LISTED

new_case envjointoken
make_script demo-jt@1.0.0 "$(script_body 0)"
write_lock demo-jt@1.0.0 "$HASH"
export JOIN_TOKEN=join-value-QQ55 GOUGH_SCRIPT_ENV="JOIN_TOKEN"
run run-in inst-jt demo-jt 1.0.0
expect_fail "JOIN_TOKEN is refused" "refusing to forward secret-like"
check "refused JOIN_TOKEN causes no push or exec" [ -z "$(order_of)" ]
expect_file_lacks "JOIN_TOKEN value never reaches lxc" "$LXC_LOG" "join-value-QQ55"
unset JOIN_TOKEN

new_case envmysecret
make_script demo-ms@1.0.0 "$(script_body 0)"
write_lock demo-ms@1.0.0 "$HASH"
export MY_SECRET=my-secret-value-WW77 GOUGH_SCRIPT_ENV="MY_SECRET FOO_MODE"
run run-in inst-ms demo-ms 1.0.0
expect_fail "MY_SECRET is refused even alongside a safe name" "refusing to forward secret-like"
check "refused MY_SECRET causes no push or exec" [ -z "$(order_of)" ]
expect_file_lacks "MY_SECRET value never reaches lxc" "$LXC_LOG" "my-secret-value-WW77"
unset MY_SECRET

new_case keeppushed
make_script demo-kp@1.0.0 "$(script_body 0)"
write_lock demo-kp@1.0.0 "$HASH"
export KEEP_PUSHED=1
run run-in inst-kp demo-kp 1.0.0
expect_ok "KEEP_PUSHED run exits 0"
check "KEEP_PUSHED=1 skips the rm step" [ "$(order_of)" = "push exec " ]

new_case help
run help
expect_ok "help exits 0"
expect_err "help prints the header with the credential variables" "GOUGH_SCRIPTS_TOKEN_FILE"

# ---------------------------------------------------------------- summary

printf '%s passed, %s failed\n' "$PASSED" "$FAILED"
if (( PASSED == 0 || FAILED > 0 )); then
  exit 1
fi

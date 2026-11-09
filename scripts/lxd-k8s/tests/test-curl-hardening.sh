#!/usr/bin/env bash
# test-curl-hardening.sh - tests that every curl in gough-script-fetch.sh (fetch, event report, OTLP) runs with -q first,
# --noproxy '*' and no proxy/CURL_HOME environment, unless GOUGH_SCRIPTS_ALLOW_PROXY=1 (then -q only, with a warning).
# A PATH stub curl logs argv and the proxy-related environment, then runs the real curl against a local python3 server;
# a functional case plants a hostile .curlrc and dead proxy variables and still expects the fetch to succeed.
# GOUGH_SCRIPT_UNDER_TEST overrides the script (mutation checks). Prints "N passed, M failed"; exits non-zero on any
# failure or when no case ran.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT="${GOUGH_SCRIPT_UNDER_TEST:-${SCRIPT_DIR}/../gough-script-fetch.sh}"
REAL_CURL="$(command -v curl)"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/test-curl-hardening.XXXXXX")"
SERVE="${WORK}/serve"
ARGV_LOG="${WORK}/argv.log"
ENV_LOG="${WORK}/env.log"
HTTP_LOG="${WORK}/http.log"
POST_LOG="${WORK}/post.log"
HTTP_PID=""
PASSED=0
FAILED=0
RC=0
ERR=""
HASH=""
US=$'\x1f'

# cleanup stops the fixture server and removes the work directory.
cleanup() {
  if [[ -n ${HTTP_PID} ]] && kill -0 "${HTTP_PID}" 2>/dev/null; then
    kill "${HTTP_PID}"
  fi
  rm -rf -- "${WORK}"
}
trap cleanup EXIT

mkdir -p "${SERVE}" "${WORK}/bin" "${WORK}/home"
: > "${ARGV_LOG}"
: > "${ENV_LOG}"
: > "${HTTP_LOG}"
: > "${POST_LOG}"

# The stub logs one argv line (fields joined by 0x1f) and the names of any proxy-ish variables, then runs the real curl.
cat > "${WORK}/bin/curl" <<STUB
#!/usr/bin/env bash
set -euo pipefail
{ for a in "\$@"; do printf '%s\x1f' "\$a"; done; printf '\n'; } >> "${ARGV_LOG}"
printf '%s|\n' "\$(env | awk -F= 'tolower(\$1) ~ /^(curl_home|http_proxy|https_proxy|all_proxy|no_proxy)\$/ { print \$1 }' | sort | paste -sd, -)" >> "${ENV_LOG}"
exec "${REAL_CURL}" "\$@"
STUB
chmod 0755 "${WORK}/bin/curl"

cat > "${WORK}/server.py" <<'PY'
# Fixture server: serves files from SERVE, logs "GET path X-Planted=<v|-> AUTH=<v|->" per request, accepts POSTs with 200.
import http.server
import os
import sys

SERVE, HTTP_LOG, POST_LOG, PORT = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4])


class Handler(http.server.BaseHTTPRequestHandler):
    """Fixture handler that records the planted header and serves fixture files."""

    def log_message(self, fmt, *args):
        return

    def record(self):
        with open(HTTP_LOG, "a") as fh:
            fh.write("%s %s X-Planted=%s\n" % (self.command, self.path, self.headers.get("X-Planted", "-")))

    def do_GET(self):
        self.record()
        target = os.path.join(SERVE, self.path.split("/")[-1])
        if not os.path.isfile(target):
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        with open(target, "rb") as fh:
            body = fh.read()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        self.record()
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)
        with open(POST_LOG, "a") as fh:
            fh.write("POST %s\n" % self.path)
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()


http.server.ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
PY

PORT=$(python3 -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()')
python3 -I "${WORK}/server.py" "${SERVE}" "${HTTP_LOG}" "${POST_LOG}" "${PORT}" >"${WORK}/server.out" 2>&1 &
HTTP_PID=$!
READY=0
for ((i = 0; i < 50; i++)); do
  if "${REAL_CURL}" -q -s --noproxy '*' -o /dev/null "http://127.0.0.1:${PORT}/"; then
    READY=1
    break
  fi
  sleep 0.1
done
if [[ ${READY} != 1 ]]; then
  echo "FATAL: fixture server did not start" >&2
  exit 1
fi
: > "${HTTP_LOG}"

# pass records a passing case.
pass() {
  PASSED=$((PASSED + 1))
  printf 'PASS: %s\n' "$1"
}

# fail records a failing case with a short reason.
fail() {
  FAILED=$((FAILED + 1))
  printf 'FAIL: %s (%s)\n' "$1" "$2"
}

# new_case <id> resets logs and exports the environment every case starts from (plain-http test host, token file, lock).
new_case() {
  local d="${WORK}/case-$1"
  mkdir -p "${d}/cache"
  printf 'tok-SECRET-XYZ987\n' > "${d}/token"
  chmod 0600 "${d}/token"
  export GOUGH_SCRIPTS_LOCK="${d}/scripts.lock"
  export GOUGH_SCRIPTS_CACHE="${d}/cache"
  export GOUGH_SCRIPTS_BASE_URL="http://127.0.0.1:${PORT}"
  export GOUGH_SCRIPTS_TOKEN_FILE="${d}/token"
  export GOUGH_ALLOW_INSECURE_SCRIPTS_HOST=1
  export GOUGH_SCRIPTS_TIMEOUT=10
  export TMPDIR="${d}"
  unset GOUGH_SCRIPTS_ALLOW_PROXY GOUGH_REPORT_URL OTEL_EXPORTER_OTLP_ENDPOINT OTEL_EXPORTER_OTLP_HEADERS
  : > "${ARGV_LOG}"
  : > "${ENV_LOG}"
  : > "${HTTP_LOG}"
  : > "${POST_LOG}"
}

# publish <name> <version> serves a fixture script and writes a one-entry lock pinning its sha256.
publish() {
  printf '#!/usr/bin/env bash\necho ok\n' > "${SERVE}/$1@$2"
  HASH=$(sha256sum "${SERVE}/$1@$2" | awk '{print $1}')
  printf '%s  %s@%s\n' "${HASH}" "$1" "$2" > "${GOUGH_SCRIPTS_LOCK}"
  chmod 0644 "${GOUGH_SCRIPTS_LOCK}"
}

# run_fetch <name> <version> runs the script's fetch with the stub curl first on PATH.
run_fetch() {
  RC=0
  ERR=$(PATH="${WORK}/bin:${PATH}" "${SCRIPT}" fetch "$1" "$2" 2>&1 >/dev/null) || RC=$?
}

# argv_check <label> <expect-noproxy 1|0> asserts every logged curl call has -q as argv[1] and --noproxy '*' present or absent.
argv_check() {
  local label="$1" want="$2" n=0 bad=0 line first has
  while IFS= read -r line; do
    [[ -n "${line}" ]] || continue
    n=$((n + 1))
    first="${line%%"${US}"*}"
    has=0
    [[ "${line}" == *"${US}--noproxy${US}*${US}"* ]] && has=1
    if [[ "${first}" != "-q" || "${has}" != "${want}" ]]; then
      bad=$((bad + 1))
    fi
  done < "${ARGV_LOG}"
  if (( n >= 1 && bad == 0 )); then
    pass "${label} (${n} curl call(s) checked)"
  else
    fail "${label}" "calls=${n} violating=${bad}"
  fi
}

# count_check <label> <min> asserts at least <min> curl calls were logged (a zero denominator is a failure).
count_check() {
  local n
  n=$(awk 'NF { n++ } END { print n + 0 }' "${ARGV_LOG}")
  if (( n >= $2 )); then pass "$1 (${n} calls)"; else fail "$1" "calls=${n} want>=$2"; fi
}

# env_clean_check <label> asserts the stub saw no CURL_HOME or proxy variables in any call.
env_clean_check() {
  local n bad
  n=$(awk 'NF { n++ } END { print n + 0 }' "${ENV_LOG}")
  bad=$(awk '$0 != "|" { n++ } END { print n + 0 }' "${ENV_LOG}")
  if (( n >= 1 && bad == 0 )); then pass "$1 (${n} call(s))"; else fail "$1" "calls=${n} with_proxy_env=${bad}"; fi
}

# plant_hostile_env creates a .curlrc (header injection plus a dead proxy) as HOME and CURL_HOME and exports dead proxy variables.
plant_hostile_env() {
  printf 'proxy = "http://127.0.0.1:9"\nheader = "X-Planted: 1"\n' > "${WORK}/home/.curlrc"
  export HOME="${WORK}/home" CURL_HOME="${WORK}/home"
  export HTTP_PROXY=http://127.0.0.1:9 HTTPS_PROXY=http://127.0.0.1:9 ALL_PROXY=http://127.0.0.1:9
  export http_proxy=http://127.0.0.1:9 https_proxy=http://127.0.0.1:9 all_proxy=http://127.0.0.1:9
}

# clear_hostile_env removes the planted configuration again.
clear_hostile_env() {
  rm -f "${WORK}/home/.curlrc"
  unset CURL_HOME HTTP_PROXY HTTPS_PROXY ALL_PROXY http_proxy https_proxy all_proxy NO_PROXY no_proxy
}

new_case a
publish demo 1.0.0
export GOUGH_REPORT_URL="http://127.0.0.1:${PORT}/report"
export OTEL_EXPORTER_OTLP_ENDPOINT="http://127.0.0.1:${PORT}"
run_fetch demo 1.0.0
if (( RC == 0 )); then pass "fetch with report and OTLP sinks succeeds"; else fail "fetch with sinks" "rc=${RC} ${ERR}"; fi
count_check "fetch, report and OTLP each invoked curl" 3
argv_check "default: -q first and --noproxy '*' on every fetch/report/OTLP curl" 1
env_clean_check "default: curl never sees CURL_HOME or proxy variables"
if grep -q 'POST /report' "${POST_LOG}" && grep -q 'POST /v1/logs' "${POST_LOG}"; then pass "report and OTLP POSTs reached the server"; else fail "report/OTLP POST" "$(cat "${POST_LOG}")"; fi

new_case b
publish demo 1.0.1
export GOUGH_REPORT_URL="http://127.0.0.1:${PORT}/report"
export OTEL_EXPORTER_OTLP_ENDPOINT="http://127.0.0.1:${PORT}"
export GOUGH_SCRIPTS_ALLOW_PROXY=1
run_fetch demo 1.0.1
if (( RC == 0 )); then pass "GOUGH_SCRIPTS_ALLOW_PROXY=1 fetch succeeds"; else fail "allow-proxy fetch" "rc=${RC} ${ERR}"; fi
count_check "allow-proxy: curl invoked for fetch, report and OTLP" 3
argv_check "allow-proxy: -q first and no --noproxy on every curl" 0
if grep -q 'GOUGH_SCRIPTS_ALLOW_PROXY=1' <<<"${ERR}" && grep -qi 'warn' <<<"${ERR}"; then pass "allow-proxy prints a warning"; else fail "allow-proxy warning" "${ERR}"; fi

new_case c
publish demo 1.0.2
export GOUGH_SCRIPTS_ALLOW_PROXY=2
run_fetch demo 1.0.2
if (( RC != 0 )) && grep -q 'GOUGH_SCRIPTS_ALLOW_PROXY must be 0 or 1' <<<"${ERR}"; then pass "invalid GOUGH_SCRIPTS_ALLOW_PROXY is refused"; else fail "invalid allow-proxy" "rc=${RC} ${ERR}"; fi

new_case d
publish demo 1.0.3
plant_hostile_env
run_fetch demo 1.0.3
if (( RC == 0 )); then pass "hostile .curlrc and dead proxies: fetch still succeeds"; else fail "hostile env fetch" "rc=${RC} ${ERR}"; fi
if grep -q '^GET /demo@1.0.3 X-Planted=-$' "${HTTP_LOG}"; then pass "server logged the request without the planted header"; else fail "server log" "$(cat "${HTTP_LOG}")"; fi
argv_check "hostile env: -q first and --noproxy '*'" 1
env_clean_check "hostile env: curl never sees proxy variables or CURL_HOME"
clear_hostile_env

printf '%d passed, %d failed\n' "${PASSED}" "${FAILED}"
if (( PASSED + FAILED == 0 || FAILED > 0 )); then
  exit 1
fi

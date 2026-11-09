#!/usr/bin/env bash
# gough-script-fetch.sh - fetch biome scripts from the authenticated gough scripts host, verify them against the hypervisor
# sha256 lock, cache them, optionally push one into an LXD container and run it, and report events best-effort.
#
# Environment:
#   GOUGH_SCRIPTS_BASE_URL             REQUIRED for fetch and run-in. Scripts are served at <base>/<name>@<version>.
#   GOUGH_SCRIPTS_TOKEN_FILE           Bearer token file (PREFERRED, managed by gough). Mode 0600 or 0400, owned by you.
#   GOUGH_SCRIPTS_BASIC_FILE           "user:password" file (FALLBACK), used only when GOUGH_SCRIPTS_TOKEN_FILE is unset.
#                                      Basic auth is a static secret for lab or bootstrap use; a short-lived machine JWT is preferred.
#   GOUGH_SCRIPTS_LOCK                 Lock file, the root of trust                                     [/etc/gough/scripts.lock]
#   GOUGH_SCRIPTS_CACHE                Verified-script cache directory                                  [/var/lib/gough/scripts]
#   GOUGH_SCRIPTS_CA_FILE              Internal CA bundle passed to curl --cacert                       [unset]
#   GOUGH_SCRIPTS_MAX_BYTES            Maximum download size in bytes                                   [1048576]
#   GOUGH_SCRIPTS_TIMEOUT              curl --max-time in seconds                                       [30]
#   GOUGH_SCRIPTS_ALLOW_PROXY          1 = let curl honor proxy variables (in-cluster proxy use); warns. Default 0 runs every
#                                      curl as `env -u <proxy vars> curl -q --noproxy '*'`, so ~/.curlrc, CURL_HOME and proxy
#                                      variables can never weaken TLS or redirect credentials. -q is always kept. [0]
#   GOUGH_ALLOW_INSECURE_SCRIPTS_HOST  1 = TEST ONLY: permits http:// hosts and running with no credentials; warns. [0]
#   GOUGH_SCRIPT_ENV                   Space-separated NAMES of non-secret env vars forwarded into the container [unset]
#   KEEP_PUSHED                        1 keeps the pushed script in the container after run-in          [0]
#   GOUGH_REPORT_URL                   Optional event sink: POST one JSON event, 5s timeout             [unset]
#   OTEL_EXPORTER_OTLP_ENDPOINT        Optional: one OTLP/HTTP JSON log record per event to <endpoint>/v1/logs [unset]
#   OTEL_EXPORTER_OTLP_HEADERS         Optional "k=v,k=v" headers for the OTLP POST only, never gough credentials [unset]
#
# Credentials: never on argv. They go into a 0600 header file inside the 0700 work directory, passed as curl -H @file,
# and deleted on exit. Credentialed requests use --max-redirs 0 so they cannot be forwarded by a redirect.
# Plain http never carries credentials unless GOUGH_ALLOW_INSECURE_SCRIPTS_HOST=1 AND the host is 127.0.0.1 or localhost.
#
# Subcommands:
#   lock-add <name> <version> <local-file>           Pin a file's sha256; same hash is a no-op, different hash FAILS
#   lock-list                                        Print lock entries and their count
#   fetch <name> <version>                           Download, verify against the lock, cache; prints the cached path
#   verify <name> <version>                          Re-hash the cached copy against the lock; deletes a mismatch
#   run-in <instance> <name> <version> [-- args...]  Verify, push to /var/lib/gough/scripts/ in the container, exec, remove
#   help                                             Print this header
#
# HTTP: 401 and 403 report "scripts host rejected the credentials"; 404 reports "script not published".
# Events: a structured key=value line on stderr always; also POSTed to GOUGH_REPORT_URL and OTLP when configured.
#   script.fetch.ok (info)            script.fetch.hash_mismatch (error; expected_sha256, actual_sha256)
#   script.fetch.not_in_lock (error)  script.fetch.auth_rejected (error; http_status)
#   script.run.ok (info)              script.run.failed (error; exit_code)
#   Common fields: event, severity, name@version, node (short hostname), timestamp (UTC).
#   Never credentials, script arguments, forwarded env values, or script output. Events never change the exit status.
#
# Lock format: "<64 lowercase hex sha256>  <name>@<version>" per line; blank and '#' lines allowed.
#   name:    ^[a-z0-9][a-z0-9._/-]*$, no "..", no "//", no trailing "/"
#   version: ^[A-Za-z0-9][A-Za-z0-9._-]*$, no "..", not latest, stable, main, master or head
# Exit status is non-zero on any failure. Stdout carries only the requested path; all logs go to stderr.
set -euo pipefail

readonly NAME_RE='^[a-z0-9][a-z0-9._/-]*$'
readonly VERSION_RE='^[A-Za-z0-9][A-Za-z0-9._-]*$'
readonly LOCK_LINE_RE='^([0-9a-f]{64})  (.+)$'
readonly INSTANCE_RE='^[A-Za-z0-9][A-Za-z0-9-]{0,62}$'
readonly ENV_NAME_RE='^[A-Za-z_][A-Za-z0-9_]*$'
readonly SECRET_NAME_RE='(TOKEN|SECRET|PASSWORD|PASSWD|KEY|CREDENTIAL)'
readonly NUMBER_RE='^[1-9][0-9]*$'
readonly VALUE_SAFE_RE='^[A-Za-z0-9._@:/+=-]*$'
readonly HOST_SAFE_RE='^[A-Za-z0-9._-]+$'
readonly CONTAINER_SCRIPT_DIR="/var/lib/gough/scripts"

LOCK_FILE="${GOUGH_SCRIPTS_LOCK:-/etc/gough/scripts.lock}"
CACHE_DIR="${GOUGH_SCRIPTS_CACHE:-/var/lib/gough/scripts}"
LOCK_COUNT=0
RESULT_PATH=""
CRED_VALUE=""
AUTH_MODE="none"
WORK_DIR=""
CLEANUP_INSTANCE=""
CLEANUP_PATH=""
EVENT_KEYS=()
EVENT_VALS=()

# log writes one message line to stderr so stdout stays reserved for the requested path.
log() {
  printf '%s\n' "$*" >&2
}

# die logs a FAIL line and exits non-zero.
die() {
  log "FAIL: $*"
  exit 1
}

# usage prints this script's header block to stderr.
usage() {
  sed -n '2,/^set -euo pipefail/p' "${BASH_SOURCE[0]}" >&2
}

# validate_name dies unless the name matches the charset and has no traversal or empty path segments.
# Names become cache and container path components, so this check is the path-safety boundary.
validate_name() {
  local name=$1
  if ! [[ $name =~ $NAME_RE ]] || [[ $name == *..* ]] || [[ $name == *//* ]] || [[ $name == */ ]]; then
    die "invalid name: '$name'"
  fi
}

# validate_version dies unless the version matches the charset and is not a mutable ref (case-insensitive).
# Mutable refs would let a publisher change what a pinned name resolves to, so they are refused everywhere.
validate_version() {
  local version=$1 rejected=0
  if ! [[ $version =~ $VERSION_RE ]] || [[ $version == *..* ]]; then
    die "invalid version: '$version'"
  fi
  shopt -s nocasematch
  case "$version" in
    latest|stable|main|master|head) rejected=1 ;;
  esac
  shopt -u nocasematch
  if (( rejected == 1 )); then
    die "mutable version refused: '$version'"
  fi
}

# validate_key dies unless the key is exactly one valid name, one "@", and one valid version.
# Applied to every lock line so a poisoned lock entry fails loudly.
validate_key() {
  local key=$1
  if [[ $key != *@* || $key == *@*@* ]]; then
    die "invalid name@version: '$key'"
  fi
  validate_name "${key%@*}"
  validate_version "${key##*@}"
}

# run_curl <curl-args...> runs curl with -q first so no curlrc is read, and by default with --noproxy '*' and all proxy
# variables and CURL_HOME removed. GOUGH_SCRIPTS_ALLOW_PROXY=1 drops only the proxy handling (-q stays).
run_curl() {
  if [[ ${GOUGH_SCRIPTS_ALLOW_PROXY:-0} == 1 ]]; then
    curl -q "$@"
  else
    env -u CURL_HOME -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY -u http_proxy -u https_proxy -u all_proxy -u NO_PROXY -u no_proxy curl -q --noproxy '*' "$@"
  fi
}

# validate_config checks numeric settings and the insecure flag before any network or lock work.
# The insecure flag must be exactly 0 or 1 so a typo cannot silently widen trust.
validate_config() {
  local max=${GOUGH_SCRIPTS_MAX_BYTES:-1048576} timeout=${GOUGH_SCRIPTS_TIMEOUT:-30}
  if ! [[ $max =~ $NUMBER_RE ]]; then
    die "GOUGH_SCRIPTS_MAX_BYTES must be a positive integer"
  fi
  if ! [[ $timeout =~ $NUMBER_RE ]]; then
    die "GOUGH_SCRIPTS_TIMEOUT must be a positive integer"
  fi
  case "${GOUGH_SCRIPTS_ALLOW_PROXY:-0}" in
    0) ;;
    1) log "WARNING: GOUGH_SCRIPTS_ALLOW_PROXY=1 lets curl honor proxy variables; credentials may traverse the proxy." ;;
    *) die "GOUGH_SCRIPTS_ALLOW_PROXY must be 0 or 1" ;;
  esac
  case "${GOUGH_ALLOW_INSECURE_SCRIPTS_HOST:-0}" in
    0) ;;
    1) log "WARNING: GOUGH_ALLOW_INSECURE_SCRIPTS_HOST=1 permits http:// hosts and credential-less runs. Testing only." ;;
    *) die "GOUGH_ALLOW_INSECURE_SCRIPTS_HOST must be 0 or 1" ;;
  esac
}

# parent_dir prints the parent directory of a path without calling dirname.
# Used to create the lock's temp file in the same directory so the final mv is an atomic rename.
parent_dir() {
  local p=$1 d
  if [[ $p != */* ]]; then
    printf '.\n'
    return 0
  fi
  d=${p%/*}
  if [[ -z $d ]]; then
    d=/
  fi
  printf '%s\n' "$d"
}

# lock_validate enforces lock ownership, mode and format, counts entries, and refuses duplicates.
# Pass allow-empty only when bootstrapping through lock-add; otherwise a zero-entry lock FAILS.
lock_validate() {
  local owner mode line key lineno=0 seen=$'\n'
  LOCK_COUNT=0
  if [[ -L $LOCK_FILE ]]; then
    die "lock is a symlink: $LOCK_FILE"
  fi
  if [[ ! -f $LOCK_FILE ]]; then
    die "lock missing or not a regular file: $LOCK_FILE"
  fi
  owner=$(stat -c %u "$LOCK_FILE")
  mode=$(stat -c %a "$LOCK_FILE")
  if [[ $owner != "$EUID" ]]; then
    die "lock not owned by the invoking uid: $LOCK_FILE"
  fi
  if (( (0$mode) & 022 )); then
    die "lock is group- or world-writable (mode $mode): $LOCK_FILE"
  fi
  while IFS= read -r line || [[ -n $line ]]; do
    lineno=$((lineno + 1))
    case "$line" in
      ''|'#'*) continue ;;
    esac
    if ! [[ $line =~ $LOCK_LINE_RE ]]; then
      die "lock line $lineno is malformed"
    fi
    key=${BASH_REMATCH[2]}
    validate_key "$key"
    if [[ $seen == *$'\n'"$key"$'\n'* ]]; then
      die "lock line $lineno duplicates an existing entry: $key"
    fi
    seen+="$key"$'\n'
    LOCK_COUNT=$((LOCK_COUNT + 1))
  done < "$LOCK_FILE"
  if (( LOCK_COUNT == 0 )) && [[ ${1:-} != allow-empty ]]; then
    die "lock has zero entries: $LOCK_FILE"
  fi
  log "lock: $LOCK_COUNT entries"
}

# lock_hash_of <name@version> prints the pinned sha256 for the key, or nothing when absent.
# Callers must run lock_validate first so the file is known to be well-formed.
lock_hash_of() {
  awk -v key="$1" '$0 !~ /^#/ && NF == 2 && $2 == key { print $1 }' "$LOCK_FILE"
}

# file_sha256 <file> prints the lowercase hex sha256 of the file's bytes.
# Uses sha256sum, the only digest tool in the allowed set.
file_sha256() {
  local digest
  digest=$(sha256sum -- "$1")
  printf '%s\n' "${digest%% *}"
}

# hash_matches <expected> <file> succeeds only when the file's sha256 equals the expected value.
# Fails closed: any error while hashing yields a mismatch.
hash_matches() {
  local got
  got=$(file_sha256 "$2") || return 1
  [[ $got == "$1" ]]
}

# check_transport <url> <credentialed 0|1> succeeds only when the scheme and host may carry that payload.
# https is always allowed; http needs GOUGH_ALLOW_INSECURE_SCRIPTS_HOST=1, and with credentials also a local host.
check_transport() {
  local url=$1 creds=$2 hostport host
  case "$url" in
    https://*) return 0 ;;
    http://*) ;;
    *) return 1 ;;
  esac
  if [[ ${GOUGH_ALLOW_INSECURE_SCRIPTS_HOST:-0} != 1 ]]; then
    return 1
  fi
  if (( creds == 0 )); then
    return 0
  fi
  hostport=${url#http://}
  host=${hostport%%[:/]*}
  [[ $host == 127.0.0.1 || $host == localhost ]]
}

# check_credential_file <path> dies unless the file is a regular non-symlink owned by the invoking uid with mode 0600 or 0400.
# Refusing symlinks and foreign ownership stops a planted credential file from being read.
check_credential_file() {
  local path=$1 owner mode
  if [[ -L $path ]]; then
    die "credential file is a symlink: $path"
  fi
  if [[ ! -f $path ]]; then
    die "credential file missing or not a regular file: $path"
  fi
  owner=$(stat -c %u "$path")
  mode=$(stat -c %a "$path")
  if [[ $owner != "$EUID" ]]; then
    die "credential file not owned by the invoking uid: $path"
  fi
  if [[ $mode != 600 && $mode != 400 ]]; then
    die "credential file mode must be 0600 or 0400 (is $mode): $path"
  fi
}

# read_credential <path> sets CRED_VALUE to the file content without trailing newlines.
# Dies when the content is empty or spans several lines, since either would corrupt the header.
read_credential() {
  CRED_VALUE=$(<"$1")
  if [[ -z $CRED_VALUE ]]; then
    die "credential file is empty: $1"
  fi
  if [[ $CRED_VALUE == *$'\n'* ]]; then
    die "credential file must hold a single line: $1"
  fi
}

# resolve_auth selects bearer (preferred) or basic credentials and validates their files.
# With neither set it dies unless the test-only insecure flag is 1, so fetch and run-in never run anonymously by default.
resolve_auth() {
  AUTH_MODE="none"
  if [[ -n ${GOUGH_SCRIPTS_TOKEN_FILE:-} ]]; then
    check_credential_file "$GOUGH_SCRIPTS_TOKEN_FILE"
    AUTH_MODE="bearer"
  elif [[ -n ${GOUGH_SCRIPTS_BASIC_FILE:-} ]]; then
    check_credential_file "$GOUGH_SCRIPTS_BASIC_FILE"
    AUTH_MODE="basic"
  fi
  if [[ $AUTH_MODE == none && ${GOUGH_ALLOW_INSECURE_SCRIPTS_HOST:-0} != 1 ]]; then
    die "no scripts-host credentials: set GOUGH_SCRIPTS_TOKEN_FILE (preferred) or GOUGH_SCRIPTS_BASIC_FILE"
  fi
}

# write_auth_header <file> writes a 0600 "Authorization" header file for curl -H @file.
# The secret reaches the file only, never argv, logs, or output; the subshell keeps umask 077 for the write.
write_auth_header() {
  local out=$1 encoded
  case "$AUTH_MODE" in
    bearer)
      read_credential "$GOUGH_SCRIPTS_TOKEN_FILE"
      ( umask 077; printf 'Authorization: Bearer %s\n' "$CRED_VALUE" > "$out" )
      ;;
    basic)
      read_credential "$GOUGH_SCRIPTS_BASIC_FILE"
      if [[ $CRED_VALUE != *:* ]]; then
        die "basic credential file must hold user:password"
      fi
      encoded=$(printf '%s' "$CRED_VALUE" | base64 -w0)
      ( umask 077; printf 'Authorization: Basic %s\n' "$encoded" > "$out" )
      ;;
    *)
      die "internal error: no credentials to write"
      ;;
  esac
  chmod 0600 "$out"
}

# write_otel_header <file> writes OTEL_EXPORTER_OTLP_HEADERS as a 0600 header file for the OTLP POST only.
# Gough credentials are never added here, so they cannot leak to a collector.
write_otel_header() {
  local out=$1 pair name value
  local -a pairs=()
  : > "$out"
  chmod 0600 "$out"
  IFS=',' read -r -a pairs <<<"${OTEL_EXPORTER_OTLP_HEADERS:-}"
  for pair in ${pairs[@]+"${pairs[@]}"}; do
    name=${pair%%=*}
    value=${pair#*=}
    if [[ $name =~ ^[A-Za-z0-9-]+$ && $value != "$pair" ]]; then
      printf '%s: %s\n' "$name" "$value" >> "$out"
    fi
  done
}

# ensure_work_dir creates the 0700 scratch directory on first use; cleanup_on_exit removes it on every exit path.
ensure_work_dir() {
  if [[ -z $WORK_DIR ]]; then
    WORK_DIR=$(mktemp -d "${TMPDIR:-/tmp}/gough-script-fetch.XXXXXX")
    chmod 0700 "$WORK_DIR"
  fi
}

# collect_fields <event> <severity> <name@version> [k=v...] fills EVENT_KEYS and EVENT_VALS for one event.
# Values outside the safe charset are replaced with "redacted", so no event can carry quotes or secrets into JSON.
collect_fields() {
  local event=$1 severity=$2 key=$3 node ts arg i=0
  shift 3
  node=${HOSTNAME%%.*}
  if ! [[ $node =~ $HOST_SAFE_RE ]]; then
    node=unknown
  fi
  ts=$(date -u +%Y-%m-%dT%H:%M:%SZ) || ts=unknown
  EVENT_KEYS=(event severity "name@version" node timestamp)
  EVENT_VALS=("$event" "$severity" "$key" "$node" "$ts")
  for arg in ${@+"$@"}; do
    EVENT_KEYS+=("${arg%%=*}")
    EVENT_VALS+=("${arg#*=}")
  done
  while (( i < ${#EVENT_VALS[@]} )); do
    if ! [[ ${EVENT_VALS[$i]} =~ $VALUE_SAFE_RE ]]; then
      EVENT_VALS[i]="redacted"
    fi
    i=$((i + 1))
  done
}

# emit_structured writes the collected event to stderr as one key=value line, independent of any network sink.
# This line is the local audit record and is printed even when every network sink is unset.
emit_structured() {
  local line="" i=0
  while (( i < ${#EVENT_KEYS[@]} )); do
    line+="${EVENT_KEYS[$i]}=${EVENT_VALS[$i]} "
    i=$((i + 1))
  done
  log "${line% }"
}

# build_event_json prints the collected event as one flat JSON object.
# Values are charset-checked by collect_fields, so no escaping is needed.
build_event_json() {
  local i=0 out=""
  while (( i < ${#EVENT_KEYS[@]} )); do
    out+="\"${EVENT_KEYS[$i]}\":\"${EVENT_VALS[$i]}\""
    if (( i + 1 < ${#EVENT_KEYS[@]} )); then
      out+=","
    fi
    i=$((i + 1))
  done
  printf '{%s}\n' "$out"
}

# build_otlp_json prints one OTLP/HTTP JSON logs request carrying the collected event as a single log record.
# Severity maps error to ERROR (17) and everything else to INFO (9); attributes mirror the common fields.
build_otlp_json() {
  local i=0 attrs="" sev_num=9 sev_text=INFO ns
  ns=$(date +%s%N) || ns=0
  if ! [[ $ns =~ ^[0-9]+$ ]]; then
    ns=0
  fi
  if [[ ${EVENT_VALS[1]} == error ]]; then
    sev_num=17
    sev_text=ERROR
  fi
  while (( i < ${#EVENT_KEYS[@]} )); do
    attrs+="{\"key\":\"${EVENT_KEYS[$i]}\",\"value\":{\"stringValue\":\"${EVENT_VALS[$i]}\"}}"
    if (( i + 1 < ${#EVENT_KEYS[@]} )); then
      attrs+=","
    fi
    i=$((i + 1))
  done
  printf '{"resourceLogs":[{"resource":{"attributes":[{"key":"service.name","value":{"stringValue":"gough-script-fetch"}}]},"scopeLogs":[{"scope":{"name":"gough-script-fetch"},"logRecords":[{"timeUnixNano":"%s","severityNumber":%s,"severityText":"%s","body":{"stringValue":"%s"},"attributes":[%s]}]}]}]}\n' \
    "$ns" "$sev_num" "$sev_text" "${EVENT_VALS[0]}" "$attrs"
}

# send_gough_report POSTs the collected event to GOUGH_REPORT_URL with the scripts-host credentials; returns non-zero on failure.
# Same auth and transport rules as fetch, --max-redirs 0, 5s timeout; runs inside a failure-tolerant caller.
send_gough_report() {
  local url=${GOUGH_REPORT_URL:-} creds=0
  local -a args=()
  if [[ -z $url ]]; then
    return 0
  fi
  if [[ $AUTH_MODE == none && ${GOUGH_ALLOW_INSECURE_SCRIPTS_HOST:-0} != 1 ]]; then
    return 1
  fi
  if [[ $AUTH_MODE != none ]]; then
    creds=1
  fi
  if ! check_transport "$url" "$creds"; then
    return 1
  fi
  ensure_work_dir
  args=(--fail --silent --max-time 5 --max-redirs 0 -X POST -H 'Content-Type: application/json' -o /dev/null)
  if (( creds == 1 )); then
    if ! (write_auth_header "${WORK_DIR}/report.hdr"); then
      return 1
    fi
    args+=(-H "@${WORK_DIR}/report.hdr")
  fi
  build_event_json > "${WORK_DIR}/event.json"
  run_curl "${args[@]}" --data-binary "@${WORK_DIR}/event.json" "$url"
}

# send_otlp_record POSTs one OTLP/HTTP JSON record to <OTEL_EXPORTER_OTLP_ENDPOINT>/v1/logs; returns non-zero on failure.
# Sends only OTEL_EXPORTER_OTLP_HEADERS, never gough credentials; the same https rule applies when headers are set.
send_otlp_record() {
  local endpoint=${OTEL_EXPORTER_OTLP_ENDPOINT:-} creds=0 hdr
  local -a args=()
  if [[ -z $endpoint ]]; then
    return 0
  fi
  endpoint=${endpoint%/}
  if [[ -n ${OTEL_EXPORTER_OTLP_HEADERS:-} ]]; then
    creds=1
  fi
  if ! check_transport "${endpoint}/v1/logs" "$creds"; then
    return 1
  fi
  ensure_work_dir
  hdr="${WORK_DIR}/otel.hdr"
  write_otel_header "$hdr"
  build_otlp_json > "${WORK_DIR}/otlp.json"
  args=(--fail --silent --max-time 5 --max-redirs 0 -X POST -H 'Content-Type: application/json' -H "@${hdr}" -o /dev/null)
  run_curl "${args[@]}" --data-binary "@${WORK_DIR}/otlp.json" "${endpoint}/v1/logs"
}

# report_event <event> <severity> <name@version> [k=v...] logs the event and sends it to each configured sink.
# Best effort: it always returns 0, prints at most one WARN line, and never changes the caller's exit status.
report_event() {
  local event=$1 severity=$2 key=$3 failed=0
  shift 3
  collect_fields "$event" "$severity" "$key" ${@+"$@"}
  emit_structured
  send_gough_report || failed=1
  send_otlp_record || failed=1
  if (( failed == 1 )); then
    log "WARN: event delivery failed for $event (exit status unchanged)"
  fi
  return 0
}

# require_base_url enforces the transport rules for the scripts host before any download starts.
# Credentials are never sent over plain http unless the test-only insecure flag is set and the host is local.
require_base_url() {
  local base=${GOUGH_SCRIPTS_BASE_URL:-} creds=0
  if [[ -z $base ]]; then
    die "GOUGH_SCRIPTS_BASE_URL is required for fetch and run-in"
  fi
  if [[ $AUTH_MODE != none ]]; then
    creds=1
  fi
  if check_transport "$base" "$creds"; then
    return 0
  fi
  if (( creds == 1 )); then
    die "plain http refused: credentials need https (or GOUGH_ALLOW_INSECURE_SCRIPTS_HOST=1 with 127.0.0.1/localhost)"
  fi
  die "plain http refused (set GOUGH_ALLOW_INSECURE_SCRIPTS_HOST=1 for testing only), or unsupported scheme"
}

# fetch_verified <name> <version> downloads name@version with credentials, verifies it against the lock, then caches it.
# Sets RESULT_PATH to the cached file; nothing reaches the cache unless the hash matches.
fetch_verified() {
  local name=$1 version=$2 key hash url dest proto got size max timeout tmp_file http_code curl_rc=0
  local -a curl_args=()
  validate_name "$name"
  validate_version "$version"
  key="${name}@${version}"
  validate_config
  resolve_auth
  lock_validate
  hash=$(lock_hash_of "$key")
  if [[ -z $hash ]]; then
    report_event script.fetch.not_in_lock error "$key"
    die "not in lock (refused before any network call): $key"
  fi
  require_base_url
  if [[ -n ${GOUGH_SCRIPTS_CA_FILE:-} && ! -f ${GOUGH_SCRIPTS_CA_FILE} ]]; then
    die "GOUGH_SCRIPTS_CA_FILE not found: $GOUGH_SCRIPTS_CA_FILE"
  fi
  max=${GOUGH_SCRIPTS_MAX_BYTES:-1048576}
  timeout=${GOUGH_SCRIPTS_TIMEOUT:-30}
  url="${GOUGH_SCRIPTS_BASE_URL%/}/${key}"
  proto='=https'
  if [[ ${GOUGH_ALLOW_INSECURE_SCRIPTS_HOST:-0} == 1 ]]; then
    proto='=http,https'
  fi
  ensure_work_dir
  tmp_file="${WORK_DIR}/download"
  curl_args=(--fail --silent --show-error --location
    --proto "$proto" --proto-redir "$proto"
    --max-filesize "$max" --max-time "$timeout"
    --write-out '%{http_code}' --output "$tmp_file")
  if [[ $AUTH_MODE != none ]]; then
    write_auth_header "${WORK_DIR}/auth.hdr"
    curl_args+=(-H "@${WORK_DIR}/auth.hdr" --max-redirs 0)
  else
    curl_args+=(--max-redirs 3)
  fi
  if [[ -n ${GOUGH_SCRIPTS_CA_FILE:-} ]]; then
    curl_args+=(--cacert "$GOUGH_SCRIPTS_CA_FILE")
  fi
  log "fetching $key from the scripts host"
  http_code=$(run_curl "${curl_args[@]}" "$url") || curl_rc=$?
  case "$http_code" in
    401|403)
      report_event script.fetch.auth_rejected error "$key" "http_status=$http_code"
      die "scripts host rejected the credentials (HTTP $http_code): $key"
      ;;
    404)
      die "script not published (HTTP 404): $key"
      ;;
  esac
  if (( curl_rc != 0 )); then
    die "download failed (curl exit $curl_rc, HTTP $http_code): $key"
  fi
  size=$(stat -c %s "$tmp_file")
  if (( size > max )); then
    die "download exceeds GOUGH_SCRIPTS_MAX_BYTES ($max): $key"
  fi
  if ! hash_matches "$hash" "$tmp_file"; then
    got=$(file_sha256 "$tmp_file")
    rm -f -- "$tmp_file"
    report_event script.fetch.hash_mismatch error "$key" "expected_sha256=$hash" "actual_sha256=$got"
    die "sha256 mismatch for $key: lock=$hash downloaded=$got (download discarded)"
  fi
  dest="${CACHE_DIR}/${key}"
  mkdir -p -- "$CACHE_DIR" "${dest%/*}"
  chmod 0755 "$CACHE_DIR" "${dest%/*}"
  mv -f -- "$tmp_file" "$dest"
  chmod 0500 "$dest"
  RESULT_PATH=$dest
  report_event script.fetch.ok info "$key" "sha256=$hash"
  log "verified and cached: $key"
}

# verify_cached <name> <version> re-hashes the cached copy against the lock and deletes a mismatching or symlinked entry.
# The cache is never trusted on its own: every use re-runs this check. Sets RESULT_PATH on success.
verify_cached() {
  local name=$1 version=$2 key hash file got
  validate_name "$name"
  validate_version "$version"
  key="${name}@${version}"
  lock_validate
  hash=$(lock_hash_of "$key")
  if [[ -z $hash ]]; then
    die "not in lock: $key"
  fi
  file="${CACHE_DIR}/${key}"
  if [[ -L $file ]]; then
    rm -f -- "$file"
    die "cache entry is a symlink (removed): $key"
  fi
  if [[ ! -f $file ]]; then
    die "not cached: $key (run fetch first)"
  fi
  if ! hash_matches "$hash" "$file"; then
    got=$(file_sha256 "$file")
    rm -f -- "$file"
    die "cache mismatch for $key: lock=$hash cached=$got (file removed)"
  fi
  RESULT_PATH=$file
  log "verified cached copy: $key"
}

# cmd_lock_add <name> <version> <file> pins a local file's sha256 into the lock.
# Re-adding the same hash is a no-op; a different hash FAILS so the operator must edit the lock deliberately.
cmd_lock_add() {
  local name=$1 version=$2 src=$3 key hash existing lock_dir tmp
  validate_name "$name"
  validate_version "$version"
  key="${name}@${version}"
  if [[ -L $src || ! -f $src ]]; then
    die "not a regular file (symlinks refused): $src"
  fi
  hash=$(file_sha256 "$src")
  if [[ -e $LOCK_FILE || -L $LOCK_FILE ]]; then
    lock_validate allow-empty
    existing=$(lock_hash_of "$key")
    if [[ -n $existing ]]; then
      if [[ $existing == "$hash" ]]; then
        log "no-op: $key is already pinned to $hash"
        return 0
      fi
      die "$key is already pinned to a different hash (lock=$existing new=$hash); edit the lock deliberately"
    fi
  fi
  lock_dir=$(parent_dir "$LOCK_FILE")
  if [[ ! -d $lock_dir ]]; then
    die "lock directory missing: $lock_dir"
  fi
  tmp=$(mktemp "${lock_dir}/.scripts-lock.XXXXXX")
  if [[ -f $LOCK_FILE ]]; then
    awk '1' "$LOCK_FILE" > "$tmp"
  fi
  printf '%s  %s\n' "$hash" "$key" >> "$tmp"
  chmod 0644 "$tmp"
  mv -f -- "$tmp" "$LOCK_FILE"
  log "lock: pinned $key ($hash)"
}

# cmd_lock_list prints every lock entry as "<hash>  <name@version>" followed by the entry count.
# Read-only; it still validates the lock so the listing is trustworthy.
cmd_lock_list() {
  lock_validate
  awk '$0 !~ /^#/ && NF == 2 { print $1 "  " $2 }' "$LOCK_FILE"
  printf 'count: %s\n' "$LOCK_COUNT"
}

# cmd_fetch <name> <version> fetches and verifies a script, then prints only its cached path on stdout.
# Stdout is the path alone so callers can capture it safely.
cmd_fetch() {
  fetch_verified "$1" "$2"
  printf '%s\n' "$RESULT_PATH"
}

# cmd_verify <name> <version> re-verifies the cached copy and reports its path on stderr.
# Exit status alone carries the verdict; no secret or content is printed.
cmd_verify() {
  verify_cached "$1" "$2"
  log "OK: $RESULT_PATH"
}

# cmd_run_in <instance> <name> <version> [-- args...] pushes a verified script into the container, runs it, and removes it.
# Forwards only GOUGH_SCRIPT_ENV names (refusing secret-like ones) and exits with the script's own status.
cmd_run_in() {
  local instance=$1 name=$2 version=$3 key target_path target flat cache_file rc=0 env_name matched
  local -a names=() env_args=()
  validate_name "$name"
  validate_version "$version"
  if ! [[ $instance =~ $INSTANCE_RE ]]; then
    die "invalid container instance name: '$instance'"
  fi
  shift 3
  if (( $# > 0 )); then
    if [[ $1 != -- ]]; then
      die "expected -- before script arguments"
    fi
    shift
  fi
  validate_config
  resolve_auth
  read -r -a names <<<"${GOUGH_SCRIPT_ENV:-}"
  for env_name in ${names[@]+"${names[@]}"}; do
    if ! [[ $env_name =~ $ENV_NAME_RE ]]; then
      die "GOUGH_SCRIPT_ENV entry is not a valid variable name: '$env_name'"
    fi
    matched=0
    shopt -s nocasematch
    if [[ $env_name =~ $SECRET_NAME_RE ]]; then
      matched=1
    fi
    shopt -u nocasematch
    if (( matched == 1 )); then
      die "refusing to forward secret-like variable: $env_name"
    fi
    if [[ -z ${!env_name+x} ]]; then
      die "GOUGH_SCRIPT_ENV names a variable that is unset: $env_name"
    fi
    env_args+=(--env "${env_name}=${!env_name}")
  done
  key="${name}@${version}"
  cache_file="${CACHE_DIR}/${key}"
  if [[ -e $cache_file || -L $cache_file ]]; then
    verify_cached "$name" "$version"
  else
    fetch_verified "$name" "$version"
  fi
  verify_cached "$name" "$version"
  cache_file=$RESULT_PATH
  flat=${name//\//_}
  target_path="${CONTAINER_SCRIPT_DIR}/${flat}@${version}"
  target="${instance}${target_path}"
  CLEANUP_INSTANCE=$instance
  CLEANUP_PATH=$target_path
  if ! lxc file push --mode 0700 --create-dirs "$cache_file" "$target"; then
    die "lxc file push failed for $key into $instance"
  fi
  log "running $key in $instance"
  lxc exec "$instance" ${env_args[@]+"${env_args[@]}"} -- "$target_path" "$@" || rc=$?
  if [[ ${KEEP_PUSHED:-0} == 1 ]]; then
    log "KEEP_PUSHED=1: left $target_path in $instance"
  elif ! lxc exec "$instance" -- rm -f -- "$target_path"; then
    log "WARN: could not remove $target_path from $instance"
    if (( rc == 0 )); then
      rc=1
    fi
  fi
  CLEANUP_INSTANCE=""
  if (( rc == 0 )); then
    report_event script.run.ok info "$key"
    log "OK: $key exited 0 in $instance"
  else
    report_event script.run.failed error "$key" "exit_code=$rc"
    log "FAIL: $key exited $rc in $instance"
  fi
  exit "$rc"
}

# cleanup_on_exit removes a pushed container copy left by a failure (unless KEEP_PUSHED=1) and the work directory.
# The work directory holds the 0600 auth header, so removing it on every exit path deletes the credential copy.
cleanup_on_exit() {
  if [[ -n $CLEANUP_INSTANCE && ${KEEP_PUSHED:-0} != 1 ]]; then
    if ! lxc exec "$CLEANUP_INSTANCE" -- rm -f -- "$CLEANUP_PATH"; then
      log "WARN: could not remove $CLEANUP_PATH from $CLEANUP_INSTANCE"
    fi
  fi
  if [[ -n $WORK_DIR ]]; then
    rm -rf -- "$WORK_DIR"
  fi
}

# main dispatches the subcommand, validating argument counts before any work starts.
# The EXIT trap is installed here so sourcing the file for tests has no side effects.
main() {
  local cmd=${1:-help}
  if (( $# > 0 )); then
    shift
  fi
  trap cleanup_on_exit EXIT
  case "$cmd" in
    help|-h|--help)
      usage
      ;;
    lock-add)
      (( $# == 3 )) || die "usage: lock-add <name> <version> <local-file>"
      cmd_lock_add "$@"
      ;;
    lock-list)
      cmd_lock_list
      ;;
    fetch)
      (( $# == 2 )) || die "usage: fetch <name> <version>"
      cmd_fetch "$@"
      ;;
    verify)
      (( $# == 2 )) || die "usage: verify <name> <version>"
      cmd_verify "$@"
      ;;
    run-in)
      (( $# >= 3 )) || die "usage: run-in <instance> <name> <version> [-- args...]"
      cmd_run_in "$@"
      ;;
    *)
      usage
      die "unknown subcommand: $cmd"
      ;;
  esac
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi

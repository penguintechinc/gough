#!/usr/bin/env bash
# test-env-parse.sh - negative and positive tests for the Compose-style env file parser (load_env_file, shared by
# k8s-worker-bootstrap.sh and hypervisor-prep.sh) and the minimal join file builder (build_join_env in hypervisor-prep.sh).
# Functions are extracted by function boundary and every case runs in its own bash process, so no script runs whole.
# Prints "N passed, M failed"; exits non-zero on any failure or when no case ran.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BOOTSTRAP="${SCRIPT_DIR}/../k8s-worker-bootstrap.sh"
HYPERVISOR="${SCRIPT_DIR}/../hypervisor-prep.sh"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/test-env-parse.XXXXXX")"
trap 'rm -rf "${WORK}"' EXIT
EXTRACT="${WORK}/functions.sh"
BODY="${WORK}/body.sh"
PASSED=0
FAILED=0
TOKEN="abcdef.0123456789abcdef"
ENDPOINT="192.168.8.10:6443"
HASH="sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"

# extract_functions <source> <name-alternation> appends the named top-level functions from a script to EXTRACT.
extract_functions() {
  awk -v re="^(${2})[(][)] [{]" '
    $0 ~ re { print; if ($0 ~ /[}][ \t]*$/) next; on = 1; next }
    on { print }
    on && /^[}]/ { on = 0 }
  ' "$1" >> "${EXTRACT}"
}

# write_body writes the per-case driver script: it sources the extracted functions and runs one parser or builder mode.
write_body() {
  cat > "${BODY}" <<'BODYEOF'
#!/usr/bin/env bash
set -euo pipefail
source "$1"
TMP_FILES=()
if [[ "$3" == 1 ]]; then
  valid_endpoint() { printf 'SEEN %s\n' "$1"; }
  valid_hash() { printf 'SEEN %s\n' "$1"; }
  valid_token() { return 0; }
fi
case "$4" in
  join) load_env_file "$2" JOIN_ENDPOINT JOIN_TOKEN CA_CERT_HASH K8S_VERSION K8S_APT_BASE K8S_APT_KEY_FPR CONTAINERD_VERSION ;;
  node) load_env_file "$2" BOND_MEMBER_1 BOND_MEMBER_2 MGMT_IFACE MGMT_IP_CIDR BR0_IP_CIDR WORKER_IP_CIDR GATEWAY DNS NODE_ID WORKER_NAME INSTANCE_TYPE STORAGE_POOL STORAGE_DRIVER IMAGE_REMOTE IMAGE_ALIAS JOIN_ENDPOINT JOIN_TOKEN CA_CERT_HASH K8S_VERSION K8S_APT_BASE K8S_APT_KEY_FPR CONTAINERD_VERSION ;;
  build)
    JOIN_ENDPOINT=192.168.8.10:6443
    JOIN_TOKEN="${TEST_TOKEN}"
    CA_CERT_HASH=sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef
    K8S_APT_BASE=https://pkgs.k8s.io/core:/stable:/v1.35/deb/
    BOND_MEMBER_1=nic-a
    MGMT_IP_CIDR=192.168.4.10/24
    build_join_env OUT
    printf 'KEYS %s|\n' "$(awk -F= '{print $1}' "${OUT}" | paste -sd' ' -)"
    cleanup
    if [[ -e "${OUT}" ]]; then printf 'STILL_PRESENT\n'; else printf 'REMOVED\n'; fi
    ;;
esac
printf 'ENDPOINT=%s\n' "${JOIN_ENDPOINT:-}"
printf 'HASH=%s\n' "${CA_CERT_HASH:-}"
BODYEOF
}

# make_file <path> <mode> <content> writes content with a trailing newline and sets the file mode.
make_file() {
  printf '%s\n' "$3" > "$1"
  chmod "$2" "$1"
}

# write_raw <path> <mode> <printf-format> [args...] writes exact bytes via printf (used for NUL and escape fixtures).
write_raw() {
  local p="$1" m="$2"
  shift 2
  # shellcheck disable=SC2059
  printf "$@" > "${p}"
  chmod "${m}" "${p}"
}

# run_case <name> <pass|fail> <needle> <stub 0|1> <join|node|build> <file|-> [VAR=value...] runs one case and records the result.
# A case passes only when the exit status matches, the needle appears in the output, and the token never appears in the output.
run_case() {
  local name="$1" want="$2" needle="$3" stub="$4" mode="$5" file="$6" rc=0 out reason=""
  shift 6
  out="$(env -u JOIN_TOKEN -u JOIN_ENDPOINT -u CA_CERT_HASH -u GATEWAY -u BOND_MEMBER_1 \
    -u K8S_VERSION -u K8S_APT_BASE -u K8S_APT_KEY_FPR -u CONTAINERD_VERSION \
    TEST_TOKEN="${TOKEN}" ${@+"$@"} bash "${BODY}" "${EXTRACT}" "${file}" "${stub}" "${mode}" 2>&1)" || rc=$?
  if [[ "${want}" == pass ]]; then
    [[ "${rc}" -eq 0 ]] || reason="expected success, exit ${rc}"
  else
    [[ "${rc}" -ne 0 ]] || reason="expected failure, got success"
  fi
  if ! grep -qF -- "${needle}" <<< "${out}"; then
    reason="${reason:-output lacks: ${needle}}"
  fi
  if grep -qF -- "${TOKEN}" <<< "${out}"; then
    reason="token value appeared in output"
  fi
  if [[ -z "${reason}" ]]; then
    PASSED=$((PASSED + 1))
    printf 'PASS %s\n' "${name}"
  else
    FAILED=$((FAILED + 1))
    printf 'FAIL %s: %s\n' "${name}" "${reason}"
  fi
}

: > "${EXTRACT}"
extract_functions "${BOOTSTRAP}" 'log|die|check_value|valid_endpoint|valid_token|valid_hash|load_env_file'
extract_functions "${HYPERVISOR}" 'new_tmp|cleanup|build_join_env'
for fn in log die check_value valid_endpoint valid_token valid_hash load_env_file new_tmp cleanup build_join_env; do
  grep -q "^${fn}() {" "${EXTRACT}" || { printf 'extraction failed: %s not found\n' "${fn}" >&2; exit 1; }
done
write_body

BODY_OK="JOIN_ENDPOINT=${ENDPOINT}"$'\n'"JOIN_TOKEN=${TOKEN}"$'\n'"CA_CERT_HASH=${HASH}"
JOIN_OK="${WORK}/join-ok.env"
make_file "${JOIN_OK}" 600 "${BODY_OK}"
JOIN_OK_0400="${WORK}/join-ok-0400.env"
make_file "${JOIN_OK_0400}" 400 "${BODY_OK}"

NODE_FULL="BOND_MEMBER_1=nic-a
BOND_MEMBER_2=nic-b
MGMT_IFACE=mgmt0
MGMT_IP_CIDR=192.168.4.10/24
BR0_IP_CIDR=192.168.8.10/24
WORKER_IP_CIDR=192.168.8.20/24
GATEWAY=192.168.8.1
DNS=192.168.8.1
NODE_ID=k3
WORKER_NAME=k3-worker
INSTANCE_TYPE=container
STORAGE_POOL=default
STORAGE_DRIVER=dir
IMAGE_REMOTE=ubuntu
IMAGE_ALIAS=24.04
JOIN_ENDPOINT=${ENDPOINT}
JOIN_TOKEN=${TOKEN}
CA_CERT_HASH=${HASH}
K8S_VERSION=1.35.9
K8S_APT_BASE=https://pkgs.k8s.io/core:/stable:/v1.35/deb/
K8S_APT_KEY_FPR=0123456789ABCDEF0123456789ABCDEF01234567
CONTAINERD_VERSION=1.7.24-1"
NODE_OK="${WORK}/node-full.env"
make_file "${NODE_OK}" 600 "${NODE_FULL}"

# Valid files and accepted syntax.
run_case "valid join file loads 3 keys" pass "loaded 3 key(s)" 0 join "${JOIN_OK}"
run_case "mode 0400 accepted" pass "loaded 3 key(s)" 0 join "${JOIN_OK_0400}"
make_file "${WORK}/comments.env" 600 "# header comment

   # indented comment
${BODY_OK}

"
run_case "comments and blank lines skipped" pass "loaded 3 key(s)" 0 join "${WORK}/comments.env"
make_file "${WORK}/single.env" 600 "JOIN_ENDPOINT='${ENDPOINT}'
JOIN_TOKEN='${TOKEN}'
CA_CERT_HASH='${HASH}'"
run_case "single-quoted values accepted" pass "loaded 3 key(s)" 0 join "${WORK}/single.env"
make_file "${WORK}/double.env" 600 "JOIN_ENDPOINT=\"${ENDPOINT}\"
JOIN_TOKEN=\"${TOKEN}\"
CA_CERT_HASH=\"${HASH}\""
run_case "double-quoted values accepted" pass "loaded 3 key(s)" 0 join "${WORK}/double.env"
make_file "${WORK}/inline.env" 600 "JOIN_ENDPOINT=${ENDPOINT}   # the controller
JOIN_TOKEN=${TOKEN} # bootstrap
CA_CERT_HASH=${HASH}  # pinned"
run_case "inline ' #' comment stripped" pass "loaded 3 key(s)" 0 join "${WORK}/inline.env"
run_case "node file loads all 22 keys" pass "loaded 22 key(s)" 0 node "${NODE_OK}"

# Literal values: $ is never expanded, backslash is literal in double quotes. The stub echoes the raw value.
# shellcheck disable=SC2016
write_raw "${WORK}/literal-dollar.env" 600 'JOIN_ENDPOINT=$HOME/$(id)\nJOIN_TOKEN=%s\nCA_CERT_HASH=%s\n' "${TOKEN}" "${HASH}"
# shellcheck disable=SC2016
run_case "unquoted \$HOME and \$(id) kept literal" pass 'SEEN $HOME/$(id)' 1 join "${WORK}/literal-dollar.env"
# shellcheck disable=SC2016
write_raw "${WORK}/literal-dq.env" 600 'JOIN_ENDPOINT="$HOME\\x"\nJOIN_TOKEN=%s\nCA_CERT_HASH=%s\n' "${TOKEN}" "${HASH}"
# shellcheck disable=SC2016
run_case "double-quoted \$HOME and backslash kept literal" pass 'SEEN $HOME\x' 1 join "${WORK}/literal-dq.env"

# Rejected file properties.
ln -s "${JOIN_OK}" "${WORK}/link.env"
run_case "symlink rejected" fail "symbolic link" 0 join "${WORK}/link.env"
make_file "${WORK}/mode644.env" 644 "${BODY_OK}"
run_case "mode 0644 rejected" fail "mode 0600 or 0400" 0 join "${WORK}/mode644.env"
mkdir "${WORK}/dir.env"
run_case "directory rejected" fail "is a directory" 0 join "${WORK}/dir.env"
run_case "missing file rejected" fail "not found" 0 join "${WORK}/does-not-exist.env"
make_file "${WORK}/crlf.env" 600 "$(printf 'JOIN_ENDPOINT=%s\r\nJOIN_TOKEN=%s\r\nCA_CERT_HASH=%s\r' "${ENDPOINT}" "${TOKEN}" "${HASH}")"
run_case "CRLF line endings rejected" fail "CR bytes" 0 join "${WORK}/crlf.env"
write_raw "${WORK}/nul.env" 600 'JOIN_ENDPOINT=%s\n\000JOIN_TOKEN=%s\nCA_CERT_HASH=%s\n' "${ENDPOINT}" "${TOKEN}" "${HASH}"
run_case "NUL byte rejected" fail "NUL bytes" 0 join "${WORK}/nul.env"

# Rejected line syntax and keys.
make_file "${WORK}/unknown.env" 600 "${BODY_OK}
JOIN_FOO=bar"
run_case "unknown key rejected" fail "key 'JOIN_FOO' is not in the allowlist" 0 join "${WORK}/unknown.env"
make_file "${WORK}/dup.env" 600 "${BODY_OK}
JOIN_TOKEN=${TOKEN}"
run_case "duplicate key rejected" fail "duplicate key 'JOIN_TOKEN'" 0 join "${WORK}/dup.env"
make_file "${WORK}/export.env" 600 "export ${BODY_OK}"
run_case "export prefix rejected" fail "'export' prefix" 0 join "${WORK}/export.env"
make_file "${WORK}/ws-before-eq.env" 600 "JOIN_ENDPOINT = ${ENDPOINT}"
run_case "whitespace before '=' rejected" fail "invalid key name" 0 join "${WORK}/ws-before-eq.env"
make_file "${WORK}/ws-after-eq.env" 600 "JOIN_ENDPOINT= ${ENDPOINT}"
run_case "whitespace after '=' rejected" fail "whitespace around '='" 0 join "${WORK}/ws-after-eq.env"
make_file "${WORK}/badname.env" 600 "1BAD=x"
run_case "invalid key name rejected" fail "invalid key name" 0 join "${WORK}/badname.env"
make_file "${WORK}/zero.env" 600 "# only a comment

"
run_case "zero keys loaded is a failure" fail "loaded zero keys" 0 join "${WORK}/zero.env"
make_file "${WORK}/unterminated.env" 600 "JOIN_ENDPOINT='${ENDPOINT}"
run_case "unterminated single quote rejected" fail "unterminated single-quoted" 0 join "${WORK}/unterminated.env"
make_file "${WORK}/junk-after-quote.env" 600 "JOIN_ENDPOINT='${ENDPOINT}'junk"
run_case "text after closing quote rejected" fail "after the closing quote" 0 join "${WORK}/junk-after-quote.env"
make_file "${WORK}/empty-value.env" 600 "JOIN_ENDPOINT=
JOIN_TOKEN=${TOKEN}
CA_CERT_HASH=${HASH}"
run_case "empty value fails validation" fail "JOIN_ENDPOINT must be host:port" 0 join "${WORK}/empty-value.env"

# Allowlist and per-run switches.
make_file "${WORK}/join-with-bond.env" 600 "${BODY_OK}
BOND_MEMBER_1=nic-a"
run_case "node-only key rejected in join file" fail "key 'BOND_MEMBER_1' is not in the allowlist" 0 join "${WORK}/join-with-bond.env"
make_file "${WORK}/join-switch.env" 600 "${BODY_OK}
LAB_PRIVILEGED=1"
run_case "switch rejected in join file" fail "'LAB_PRIVILEGED' is a per-run safety switch" 0 join "${WORK}/join-switch.env"
for sw in LAB_PRIVILEGED I_ACCEPT_HOST_ROOT_RISK APPLY_NETPLAN APPLY_SSH_RESTRICT FORCE_NETPLAN_REPLACE ALLOW_NON_ARM64 ALLOW_UNSUPPORTED_OS DRY_RUN; do
  make_file "${WORK}/node-${sw}.env" 600 "${NODE_FULL}
${sw}=1"
  run_case "switch ${sw} rejected in node file" fail "'${sw}' is a per-run safety switch" 0 node "${WORK}/node-${sw}.env"
done

# Format validation of the join values.
make_file "${WORK}/bad-token.env" 600 "JOIN_ENDPOINT=${ENDPOINT}
JOIN_TOKEN=ABCDEF.0123456789abcdef
CA_CERT_HASH=${HASH}"
run_case "bad token format rejected" fail "not a bootstrap token" 0 join "${WORK}/bad-token.env"
make_file "${WORK}/bad-hash.env" 600 "JOIN_ENDPOINT=${ENDPOINT}
JOIN_TOKEN=${TOKEN}
CA_CERT_HASH=sha256:0123456789abcdef"
run_case "bad CA hash format rejected" fail "CA_CERT_HASH must be sha256" 0 join "${WORK}/bad-hash.env"
for bad in 192.168.8.10 192.168.8.10:70000 bad_host:6443 999.1.1.1:6443; do
  make_file "${WORK}/bad-endpoint.env" 600 "JOIN_ENDPOINT=${bad}
JOIN_TOKEN=${TOKEN}
CA_CERT_HASH=${HASH}"
  run_case "bad endpoint '${bad}' rejected" fail "JOIN_ENDPOINT must be host:port" 0 join "${WORK}/bad-endpoint.env"
done

# Precedence: environment beats the file for endpoint; JOIN_TOKEN never comes from the environment.
run_case "env JOIN_ENDPOINT overrides file" pass "ENDPOINT=10.0.0.5:6443" 0 join "${JOIN_OK}" JOIN_ENDPOINT=10.0.0.5:6443
run_case "invalid env JOIN_ENDPOINT rejected" fail "JOIN_ENDPOINT must be host:port" 0 join "${JOIN_OK}" JOIN_ENDPOINT=bogus
run_case "JOIN_TOKEN in environment rejected" fail "is set in the environment" 0 join "${JOIN_OK}" JOIN_TOKEN="${TOKEN}"
run_case "JOIN_TOKEN in environment rejected (node file)" fail "is set in the environment" 0 node "${NODE_OK}" JOIN_TOKEN="${TOKEN}"
run_case "env GATEWAY overrides node file" pass "GATEWAY taken from the environment" 0 node "${NODE_OK}" GATEWAY=10.0.0.1

# Minimal join file built for the instance: exactly the join keys, never BOND_*/MGMT_* values.
run_case "minimal join file has exactly the expected keys" pass "KEYS JOIN_ENDPOINT JOIN_TOKEN CA_CERT_HASH K8S_APT_BASE|" 0 build -
run_case "minimal join file removed after cleanup" pass "REMOVED" 0 build -

printf '%s passed, %s failed\n' "${PASSED}" "${FAILED}"
if [[ "${PASSED}" -eq 0 ]]; then
  printf 'no test cases ran\n' >&2
  exit 1
fi
if [[ "${FAILED}" -ne 0 ]]; then
  exit 1
fi

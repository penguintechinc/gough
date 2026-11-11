#!/usr/bin/env bash
# test-input-validation.sh - tests that interface names, GATEWAY, DNS and CIDRs are format-validated at load (env file,
# environment, defaults) and again inside render_nft and render_netplan, and that valid input still renders byte for byte
# like the golden files in tests/golden. The script is sourced (main does not run), so the real top-level load path runs.
# HYPERVISOR_SRC overrides the script under test (mutation and before-the-fix checks). Prints "N passed, M failed";
# exits non-zero on any failure or when no case ran.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HYPERVISOR="${HYPERVISOR_SRC:-${SCRIPT_DIR}/../hypervisor-prep.sh}"
GOLDEN="${SCRIPT_DIR}/golden"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/test-input-validation.XXXXXX")"
trap 'rm -rf "${WORK}"' EXIT
PASSED=0
FAILED=0
OUT=""
RC=0
# Names that may come from NODE_ENV_FILE; BOND_NAME and BRIDGE_NAME are environment-only (not in the file allowlist).
FILE_NAMES=(MGMT_IFACE UPLINK_IFACE APP_IFACE BOND_MEMBER_1 BOND_MEMBER_2)
ALL_NAMES=(MGMT_IFACE UPLINK_IFACE APP_IFACE BOND_MEMBER_1 BOND_MEMBER_2 BOND_NAME BRIDGE_NAME)
INJECT=$'br0\n tcp dport 22 accept'
# shellcheck disable=SC2016 # literal metacharacters are the point of these payloads
BAD_NAMES=("${INJECT}" 'br 0' 'a"b' "a'b" 'a;b' 'a{b' '-br0' '0123456789abcdef' '.' '..' 'a$b' 'a`b' 'a/b')
GOOD_NAMES=(enp3s0f0np0 br0 bond0 eno1.100 0123456789abcde)

# check <label> <want-rc 0|nonzero> <output-substring-or-empty> records one result for the last RC/OUT.
check() {
  local label="$1" want="$2" sub="$3" ok=1
  if [[ "${want}" == "0" && "${RC}" != "0" ]]; then ok=0; fi
  if [[ "${want}" != "0" && "${RC}" == "0" ]]; then ok=0; fi
  if [[ -n "${sub}" && "${OUT}" != *"${sub}"* ]]; then ok=0; fi
  if [[ "${ok}" == 1 ]]; then
    PASSED=$((PASSED + 1))
  else
    FAILED=$((FAILED + 1))
    printf 'FAIL: %s (rc=%s)\n' "${label}" "${RC}" >&2
  fi
}

# run_source <env-assignments...> sources the script under test in a fresh bash with the given environment; no main runs.
# shellcheck disable=SC2016 # the single-quoted bash -c body must expand in the child shell
run_source() {
  RC=0
  OUT="$(env -i PATH="${PATH}" HOME="${WORK}" TMPDIR="${WORK}" "$@" bash -c 'set -euo pipefail; source "$1"; printf LOADED' _ "${HYPERVISOR}" 2>&1)" || RC=$?
}

# write_envfile <KEY> <value> writes a 0600 NODE_ENV_FILE with one key, quoting the value so spaces and punctuation survive.
write_envfile() {
  local f="${WORK}/node.env"
  rm -f "${f}"
  if [[ "$2" == *"'"* ]]; then
    printf '%s="%s"\n' "$1" "$2" > "${f}"
  else
    printf "%s='%s'\n" "$1" "$2" > "${f}"
  fi
  chmod 0600 "${f}"
}

# Names from the environment: every bad value is refused for all seven names, every good value accepted.
for n in "${ALL_NAMES[@]}"; do
  for bad in "${BAD_NAMES[@]}"; do
    run_source "${n}=${bad}"
    check "env ${n} bad $(printf '%q' "${bad}")" nonzero ""
    if [[ "${OUT}" == *LOADED* ]]; then printf 'FAIL: loaded %s\n' "${n}" >&2; fi
  done
  for good in "${GOOD_NAMES[@]}"; do
    run_source "${n}=${good}"
    check "env ${n} good ${good}" 0 "LOADED"
  done
done

# Names from the env file (the five file-loadable names), including the empty value.
for n in "${FILE_NAMES[@]}"; do
  for bad in "${BAD_NAMES[@]}" ""; do
    case "${bad}" in *$'\n'*) continue ;; esac
    write_envfile "${n}" "${bad}"
    run_source "NODE_ENV_FILE=${WORK}/node.env"
    check "file ${n} bad $(printf '%q' "${bad}")" nonzero ""
  done
  for good in "${GOOD_NAMES[@]}"; do
    write_envfile "${n}" "${good}"
    run_source "NODE_ENV_FILE=${WORK}/node.env"
    check "file ${n} good ${good}" 0 "LOADED"
  done
done

# GATEWAY, DNS and CIDRs, from the environment and from the file.
BAD_IPS=('1.2.3.256' '1.2.3.4, 5.6.7.8:' '[' $'1.2.3.4\n5.6.7.8' '1.2.3' '01.2.3.4' '1.2.3.4 ' '1.2.3.4/24' 'a.b.c.d' '{1.2.3.4}')
BAD_CIDRS=('1.2.3.4/33' '1.2.3.256/24' $'1.2.3.4/24\nx' '1.2.3.4' '1.2.3.4/08' '1.2.3.4/' '[' '1.2.3.4, 5.6.7.8:' '192.168.8.10/24 ')
for k in GATEWAY DNS; do
  for bad in "${BAD_IPS[@]}"; do
    run_source "${k}=${bad}"
    check "env ${k} bad $(printf '%q' "${bad}")" nonzero ""
    case "${bad}" in *$'\n'*) continue ;; esac
    write_envfile "${k}" "${bad}"
    run_source "NODE_ENV_FILE=${WORK}/node.env"
    check "file ${k} bad $(printf '%q' "${bad}")" nonzero ""
  done
  run_source "${k}=192.168.8.1"
  check "env ${k} good" 0 "LOADED"
done
for k in MGMT_IP_CIDR BR0_IP_CIDR WORKER_IP_CIDR; do
  for bad in "${BAD_CIDRS[@]}"; do
    run_source "${k}=${bad}"
    check "env ${k} bad $(printf '%q' "${bad}")" nonzero ""
    case "${bad}" in *$'\n'*) continue ;; esac
    write_envfile "${k}" "${bad}"
    run_source "NODE_ENV_FILE=${WORK}/node.env"
    check "file ${k} bad $(printf '%q' "${bad}")" nonzero ""
  done
  run_source "${k}=192.168.8.10/24"
  check "env ${k} good" 0 "LOADED"
done

# Render path: extract the real functions and run them with valid values, comparing to the goldens.
EXTRACT="${WORK}/functions.sh"
extract_functions() {
  awk -v re="^(${2})[(][)] [{]" '
    $0 ~ re { print; if ($0 ~ /[}][ \t]*$/) next; on = 1; next }
    on { print
         if (hd != "") { if ($0 == hd) hd = ""; next }
         if (match($0, /<<-?[ ]*'"'"'?[A-Z_]+'"'"'?/)) { t = substr($0, RSTART, RLENGTH); gsub(/<<-?[ ]*|'"'"'/, "", t); hd = t; next }
         if ($0 ~ /^[}]/) on = 0 }
  ' "$1" >> "${EXTRACT}"
}
extract_functions "${HYPERVISOR}" 'log|warn|die|validate_iface_name|valid_ipv4|valid_cidr4|render_nft|render_netplan|uplink_nics|managed_nic_names|bridge_lower'
DRIVER="${WORK}/driver.sh"
cat > "${DRIVER}" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
source "$1"
MGMT_IFACE=enp3s0f0np0 BOND_NAME=bond0 BRIDGE_NAME=br0 BOND_MEMBER_1=enp1s0f0 BOND_MEMBER_2=enp1s0f1 UPLINK_IFACE=eno2
MGMT_IP_CIDR=192.168.4.10/24 BR0_IP_CIDR=192.168.8.10/24 GATEWAY=192.168.8.1 DNS=192.168.8.1
UPLINK_MODE="$2"
for kv in "${@:4}"; do
  printf -v "${kv%%=*}" '%s' "${kv#*=}"
done
case "$3" in nft) render_nft ;; np) render_netplan ;; esac
EOF

# golden_case <mode> <kind> compares the rendered text with the stored golden byte for byte.
golden_case() {
  RC=0
  bash "${DRIVER}" "${EXTRACT}" "$1" "$2" > "${WORK}/rendered.txt" 2>/dev/null || RC=$?
  if [[ "${RC}" == 0 ]] && cmp -s "${WORK}/rendered.txt" "${GOLDEN}/$2-$1.txt"; then
    PASSED=$((PASSED + 1))
  else
    FAILED=$((FAILED + 1))
    printf 'FAIL: golden %s %s\n' "$1" "$2" >&2
  fi
}
for mode in bond single; do
  golden_case "${mode}" nft
  golden_case "${mode}" np
done

# Expected accept-rule counts in the valid rendering: one mgmt accept, one lo accept, one policy accept.
RC=0
OUT="$(bash "${DRIVER}" "${EXTRACT}" bond nft 2>&1)" || RC=$?
check "nft renders" 0 ""
n_ssh="$(printf '%s\n' "${OUT}" | grep -c 'tcp dport 22 accept' || true)"
n_acc="$(printf '%s\n' "${OUT}" | grep -c 'accept' || true)"
if [[ "${n_ssh}" == 1 && "${n_acc}" == 3 ]]; then PASSED=$((PASSED + 1)); else FAILED=$((FAILED + 1)); printf 'FAIL: accept counts ssh=%s all=%s\n' "${n_ssh}" "${n_acc}" >&2; fi

# Last guard: render_nft and render_netplan refuse every bad name even when the variable was set after load, and print nothing.
for n in MGMT_IFACE BRIDGE_NAME BOND_NAME; do
  for bad in "${BAD_NAMES[@]}"; do
    RC=0
    OUT="$(bash "${DRIVER}" "${EXTRACT}" bond nft "${n}=${bad}" 2>/dev/null)" || RC=$?
    check "render_nft ${n} bad $(printf '%q' "${bad}")" nonzero ""
    if [[ -n "${OUT}" ]]; then FAILED=$((FAILED + 1)); printf 'FAIL: render_nft emitted text for bad %s\n' "${n}" >&2; fi
    RC=0
    OUT="$(bash "${DRIVER}" "${EXTRACT}" bond np "${n}=${bad}" 2>/dev/null)" || RC=$?
    check "render_netplan ${n} bad $(printf '%q' "${bad}")" nonzero ""
  done
done
for n in BOND_MEMBER_1 BOND_MEMBER_2; do
  RC=0
  OUT="$(bash "${DRIVER}" "${EXTRACT}" bond np "${n}=${INJECT}" 2>/dev/null)" || RC=$?
  check "render_netplan ${n} injection" nonzero ""
done
RC=0
OUT="$(bash "${DRIVER}" "${EXTRACT}" single np "UPLINK_IFACE=${INJECT}" 2>/dev/null)" || RC=$?
check "render_netplan UPLINK_IFACE injection" nonzero ""
for kv in "GATEWAY=1.2.3.256" "DNS=1.2.3.4, 5.6.7.8:" "BR0_IP_CIDR=1.2.3.4/33" "MGMT_IP_CIDR=[" "GATEWAY=${INJECT}"; do
  RC=0
  OUT="$(bash "${DRIVER}" "${EXTRACT}" bond np "${kv}" 2>/dev/null)" || RC=$?
  check "render_netplan bad ${kv%%=*}" nonzero ""
done

printf '%d passed, %d failed\n' "${PASSED}" "${FAILED}"
if (( PASSED + FAILED == 0 || FAILED > 0 )); then
  exit 1
fi

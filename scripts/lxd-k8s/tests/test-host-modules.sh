#!/usr/bin/env bash
# test-host-modules.sh - tests host kernel module handling in hypervisor-prep.sh: apply_host_kernel_modules (modprobe,
# MODULES_LOAD_DIR/<name>.conf persistence, SYS_MODULE_DIR verification), apply_biome_host_requirements (reads the
# rendered host-requirements.json) and the KERNEL_MODULES default. Each case sources the real script in a child bash with a
# modprobe stub on PATH and fixture directories. Prints "N passed, M failed"; exits non-zero on any failure or no case ran.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HYP="${HYPERVISOR_SCRIPT:-${SCRIPT_DIR}/../hypervisor-prep.sh}"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/test-host-modules.XXXXXX")"
trap 'rm -rf "${WORK}"' EXIT
PASSED=0
FAILED=0
CASE=""
CASE_FAILS=""
CD=""
OUT=""
RC=0

[[ -f "${HYP}" ]] || { printf 'hypervisor script not found: %s\n' "${HYP}" >&2; exit 1; }

mkdir -p "${WORK}/bin"
cat > "${WORK}/bin/modprobe" <<'EOF'
#!/usr/bin/env bash
# modprobe stub: logs the call; STUB_MODPROBE_FAIL names a module that fails like a missing .ko; STUB_NO_SYSFS skips
# creating SYS_MODULE_DIR/<name> (modprobe "succeeds" but nothing is loaded).
printf 'modprobe %s\n' "$*" >> "${STUB_LOG}"
if [[ "$1" == "${STUB_MODPROBE_FAIL:-}" ]]; then
  echo "modprobe: FATAL: Module $1 not found in directory /lib/modules/test" >&2
  exit 1
fi
[[ "${STUB_NO_SYSFS:-0}" == 1 ]] || mkdir -p "${SYS_MODULE_DIR}/$1"
EOF
chmod 0755 "${WORK}/bin/modprobe"

DRIVER="${WORK}/driver.sh"
cat > "${DRIVER}" <<'EOF'
#!/usr/bin/env bash
# Driver: sources the real hypervisor-prep.sh (BASH_SOURCE guard keeps main from running), then runs the given function
# with the remaining arguments. "show_default" prints the effective KERNEL_MODULES instead.
set -euo pipefail
source "$1"
export PATH="${STUB_BIN}:${PATH}"
trap cleanup EXIT
shift
if [[ "$1" == show_default ]]; then
  printf 'KERNEL_MODULES=%s\n' "${KERNEL_MODULES}"
  exit 0
fi
"$@"
EOF

# run_fn [KEY=VALUE...] -- <function> [args...] runs one function of the real script in a clean environment.
run_fn() {
  local -a envs=()
  while [[ $# -gt 0 && "$1" != "--" ]]; do
    envs+=("$1")
    shift
  done
  shift
  if OUT="$(env -i PATH="/usr/bin:/bin" STUB_BIN="${WORK}/bin" STUB_LOG="${CD}/stub.log" \
      MODULES_LOAD_DIR="${CD}/modules-load.d" SYS_MODULE_DIR="${CD}/sys-module" MODULES_BUILTIN_FILE="${CD}/modules.builtin" \
      ${envs[@]+"${envs[@]}"} bash "${DRIVER}" "${HYP}" "$@" 2>&1)"; then
    RC=0
  else
    RC=$?
  fi
}

# case_begin <name> starts a case with a fresh fixture root and an empty stub log.
case_begin() {
  CASE="$1"
  CASE_FAILS=""
  CD="$(mktemp -d "${WORK}/case.XXXXXX")"
  : > "${CD}/stub.log"
}

# check_fail records one failed check in the current case.
check_fail() {
  CASE_FAILS="${CASE_FAILS}; $1"
}

# case_end prints the verdict for the current case and counts it.
case_end() {
  if [[ -z "${CASE_FAILS}" ]]; then
    PASSED=$((PASSED + 1))
    printf 'PASS %s\n' "${CASE}"
  else
    FAILED=$((FAILED + 1))
    printf 'FAIL %s%s\n' "${CASE}" "${CASE_FAILS}"
  fi
}

# want_rc <0|nz> checks the exit status of the last run.
want_rc() {
  if [[ "$1" == "0" ]]; then
    [[ "${RC}" -eq 0 ]] || check_fail "exit ${RC}, expected 0 (${OUT##*$'\n'})"
  else
    [[ "${RC}" -ne 0 ]] || check_fail "exit 0, expected non-zero"
  fi
}

# want_out <ERE> and no_out <ERE> check the captured output of the last run.
want_out() {
  grep -E -q -- "$1" <<< "${OUT}" || check_fail "output lacks /$1/"
}
no_out() {
  if grep -E -q -- "$1" <<< "${OUT}"; then check_fail "output has /$1/"; fi
}

# want_conf <module> checks MODULES_LOAD_DIR/<module>.conf holds exactly the module name with mode 0644.
want_conf() {
  local f="${CD}/modules-load.d/$1.conf" mode
  if [[ ! -f "${f}" ]]; then
    check_fail "$1.conf not written"
    return 0
  fi
  [[ "$(cat "${f}")" == "$1" ]] || check_fail "$1.conf content is not exactly '$1'"
  mode="$(stat -c '%a' "${f}")"
  [[ "${mode}" == "644" ]] || check_fail "$1.conf mode ${mode}, expected 644"
}

# modprobe_calls <module> prints how many times the stub was asked to load the module.
modprobe_calls() {
  grep -c -x -- "modprobe $1" "${CD}/stub.log" || [[ $? -eq 1 ]]
}

# write_req <json> writes a render directory fixture holding host-requirements.json.
write_req() {
  mkdir -p "${CD}/render"
  printf '%s\n' "$1" > "${CD}/render/host-requirements.json"
}

case_begin "module loaded, conf written once, examined count printed"
run_fn -- apply_host_kernel_modules wireguard
want_rc 0
want_conf wireguard
[[ -d "${CD}/sys-module/wireguard" ]] || check_fail "module not loaded in fixture"
[[ "$(modprobe_calls wireguard)" == "1" ]] || check_fail "modprobe wireguard not called exactly once"
want_out "host kernel modules: 1 examined \(wireguard\)"
no_out "unchanged: .*wireguard.conf"
case_end

case_begin "re-run is a no-op for the conf file"
run_fn -- apply_host_kernel_modules wireguard
before="$(stat -c '%Y %i' "${CD}/modules-load.d/wireguard.conf" 2>/dev/null || printf 'missing')"
sleep 1
run_fn -- apply_host_kernel_modules wireguard
want_rc 0
want_conf wireguard
want_out "unchanged: .*/modules-load.d/wireguard.conf"
after="$(stat -c '%Y %i' "${CD}/modules-load.d/wireguard.conf" 2>/dev/null || printf 'missing')"
[[ "${before}" != missing && "${before}" == "${after}" ]] || check_fail "wireguard.conf rewritten on re-run (${before} -> ${after})"
case_end

case_begin "stale conf content is corrected"
mkdir -p "${CD}/modules-load.d"
printf 'wireguard\nextra\n' > "${CD}/modules-load.d/wireguard.conf"
run_fn -- apply_host_kernel_modules wireguard
want_rc 0
want_conf wireguard
case_end

case_begin "modprobe failure fails loudly naming the module and kernel, nothing persisted"
run_fn STUB_MODPROBE_FAIL=wireguard -- apply_host_kernel_modules overlay wireguard
want_rc nz
want_out "ERROR: modprobe wireguard failed on kernel $(uname -r)"
[[ ! -e "${CD}/modules-load.d/wireguard.conf" ]] || check_fail "wireguard.conf written although modprobe failed"
no_out "examined"
case_end

case_begin "modprobe success but module not loaded fails verification"
run_fn STUB_NO_SYSFS=1 -- apply_host_kernel_modules wireguard
want_rc nz
want_out "ERROR: kernel module wireguard is not loaded after modprobe"
case_end

case_begin "built-in module (modules.builtin) passes verification"
printf 'kernel/drivers/net/wireguard/wireguard.ko\n' > "${CD}/modules.builtin"
run_fn STUB_NO_SYSFS=1 -- apply_host_kernel_modules wireguard
want_rc 0
want_conf wireguard
case_end

case_begin "invalid module name refused before modprobe"
run_fn -- apply_host_kernel_modules "../evil"
want_rc nz
want_out "ERROR: invalid kernel module name"
[[ ! -s "${CD}/stub.log" ]] || check_fail "modprobe ran for an invalid name"
case_end

case_begin "DRY_RUN=1 touches nothing and prints the actions"
run_fn DRY_RUN=1 -- apply_host_kernel_modules wireguard
want_rc 0
want_out "\[dry-run\] modprobe wireguard"
want_out "\[dry-run\] write .*/modules-load.d/wireguard.conf \(mode 0644\)"
[[ ! -s "${CD}/stub.log" ]] || check_fail "modprobe ran under DRY_RUN"
[[ ! -e "${CD}/modules-load.d" ]] || check_fail "modules-load.d created under DRY_RUN"
case_end

case_begin "biome host-requirements.json applies its modules"
write_req '{"kernel_modules": ["wireguard"]}'
run_fn -- apply_biome_host_requirements "${CD}/render"
want_rc 0
want_conf wireguard
want_out "host kernel modules: 1 examined \(wireguard\)"
case_end

case_begin "biome with an empty module list applies nothing (0 examined)"
write_req '{"kernel_modules": []}'
run_fn -- apply_biome_host_requirements "${CD}/render"
want_rc 0
want_out "no host kernel modules \(0 examined\)"
[[ ! -s "${CD}/stub.log" ]] || check_fail "modprobe ran for an empty list"
case_end

case_begin "missing host-requirements.json fails loudly"
mkdir -p "${CD}/render"
run_fn -- apply_biome_host_requirements "${CD}/render"
want_rc nz
want_out "ERROR: renderer produced no host-requirements.json"
case_end

for bad in '{"kernel_modules": "wireguard"}' '{"kernel_modules": ["Wire-Guard"]}' '{"kernel_modules": [1]}' '[]' 'not json'; do
  case_begin "malformed host-requirements.json refused: ${bad}"
  write_req "${bad}"
  run_fn -- apply_biome_host_requirements "${CD}/render"
  want_rc nz
  want_out "ERROR: cannot read host-requirements.json"
  [[ ! -s "${CD}/stub.log" ]] || check_fail "modprobe ran for malformed input"
  case_end
done

case_begin "host prereq then biome: same file, written once"
run_fn -- apply_host_kernel_modules overlay wireguard
write_req '{"kernel_modules": ["wireguard"]}'
run_fn -- apply_biome_host_requirements "${CD}/render"
want_rc 0
want_out "unchanged: .*/modules-load.d/wireguard.conf"
want_conf wireguard
want_conf overlay
case_end

case_begin "KERNEL_MODULES default includes wireguard"
run_fn -- show_default
want_rc 0
want_out "^KERNEL_MODULES=ip_tables ip6_tables nf_nat overlay br_netfilter wireguard$"
case_end

if [[ $((PASSED + FAILED)) -eq 0 ]]; then
  printf 'no cases ran\n' >&2
  exit 1
fi
printf '%d passed, %d failed\n' "${PASSED}" "${FAILED}"
if [[ "${FAILED}" -ne 0 ]]; then
  exit 1
fi

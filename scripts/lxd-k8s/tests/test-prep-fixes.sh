#!/usr/bin/env bash
# test-prep-fixes.sh - tests the architecture gate (check_arch) and the renamed ALLOW_UNSUPPORTED_ARCH switch in
# hypervisor-prep.sh. The script is sourced (main does not run) in a clean environment with a PATH stub for uname.
# HYPERVISOR_SRC overrides the script under test. Prints "N passed, M failed"; exits non-zero on any failure or when no case ran.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HYPERVISOR="${HYPERVISOR_SRC:-${SCRIPT_DIR}/../hypervisor-prep.sh}"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/test-prep-fixes.XXXXXX")"
trap 'rm -rf "${WORK}"' EXIT
PASSED=0
FAILED=0
OUT=""
RC=0
mkdir -p "${WORK}/bin"
cat > "${WORK}/bin/uname" <<'STUB'
#!/usr/bin/env bash
# uname stub: -m prints STUB_MACHINE; anything else falls through to the real uname.
if [[ "${1:-}" == "-m" ]]; then printf '%s\n' "${STUB_MACHINE}"; else /usr/bin/uname "$@"; fi
STUB
chmod 0755 "${WORK}/bin/uname"

# check <label> <want-rc 0|nonzero> <output-substring-or-empty> records one result for the last RC/OUT.
check() {
  local ok=1
  if [[ "$2" == "0" && "${RC}" != "0" ]]; then ok=0; fi
  if [[ "$2" != "0" && "${RC}" == "0" ]]; then ok=0; fi
  if [[ -n "$3" && "${OUT}" != *"$3"* ]]; then ok=0; fi
  if [[ "${ok}" == 1 ]]; then
    PASSED=$((PASSED + 1))
  else
    FAILED=$((FAILED + 1))
    printf 'FAIL: %s (rc=%s) out=%s\n' "$1" "${RC}" "${OUT}" >&2
  fi
}

# arch_case <machine> [ENV...] sources the script and runs check_arch with the stubbed machine type.
# shellcheck disable=SC2016 # the single-quoted bash -c body must expand in the child shell
arch_case() {
  local m="$1"
  shift
  RC=0
  OUT="$(env -i PATH="${WORK}/bin:/usr/bin:/bin" HOME="${WORK}" TMPDIR="${WORK}" STUB_MACHINE="${m}" "$@" \
    bash -c 'set -euo pipefail; source "$1"; check_arch; printf "ARCH=%s" "${ARCH}"' _ "${HYPERVISOR}" 2>&1)" || RC=$?
}

arch_case aarch64
check "[M3] arm64 accepted" 0 "ARCH=arm64"
arch_case x86_64
check "[M3] amd64 accepted" 0 "ARCH=amd64"
arch_case riscv64
check "[M3] riscv64 refused" nonzero "ALLOW_UNSUPPORTED_ARCH=1"
arch_case riscv64 ALLOW_UNSUPPORTED_ARCH=1
check "[M3] ALLOW_UNSUPPORTED_ARCH=1 allows riscv64 with a warning" 0 "ARCH=riscv64"
arch_case x86_64 ALLOW_NON_ARM64=1
check "[M3] old ALLOW_NON_ARM64 fails loudly naming the new variable" nonzero "ALLOW_UNSUPPORTED_ARCH"
arch_case aarch64 ALLOW_NON_ARM64=0
check "[M3] old ALLOW_NON_ARM64 fails even when 0" nonzero "ALLOW_UNSUPPORTED_ARCH"
arch_case riscv64 ALLOW_UNSUPPORTED_ARCH=2
check "[M3] ALLOW_UNSUPPORTED_ARCH other than 1 does not override" nonzero "ALLOW_UNSUPPORTED_ARCH=1"


# file_case <KEY=VALUE> sources the script with a 0600 NODE_ENV_FILE holding that one line; the real file loader runs.
# shellcheck disable=SC2016 # the single-quoted bash -c body must expand in the child shell
file_case() {
  local f="${WORK}/node.env"
  printf '%s\n' "$1" > "${f}"
  chmod 0600 "${f}"
  RC=0
  OUT="$(env -i PATH="${WORK}/bin:/usr/bin:/bin" HOME="${WORK}" TMPDIR="${WORK}" NODE_ENV_FILE="${f}" \
    bash -c 'set -euo pipefail; source "$1"; printf "LOADED K8S_PACKAGE_VERSION=%s" "${K8S_PACKAGE_VERSION:-}"' _ "${HYPERVISOR}" 2>&1)" || RC=$?
}
for sw in ALLOW_UNSUPPORTED_ARCH ALLOW_INSECURE_MIRRORS; do
  file_case "${sw}=1"
  check "[M3/M2] ${sw} rejected in the node file" nonzero "'${sw}' is a per-run safety switch"
done
file_case "ALLOW_NON_ARM64=1"
check "[M3] ALLOW_NON_ARM64 in the node file names the new variable" nonzero "ALLOW_UNSUPPORTED_ARCH"
for kv in K8S_PACKAGE_VERSION=1.36.0-1.1 MIRRORED_REGISTRIES=docker.io,quay.io ALLOW_REGISTRY_FALLBACK=1 INTERNAL_CA_URL=https://m.example.lan/ca.pem \
  INTERNAL_CA_SHA256=0000000000000000000000000000000000000000000000000000000000000000; do
  file_case "${kv}"
  check "[M1/M2] ${kv%%=*} is in the node-file allowlist" 0 "LOADED"
done
file_case "K8S_PACKAGE_VERSION=bad version"
check "[M1] bad K8S_PACKAGE_VERSION refused at load" nonzero "K8S_PACKAGE_VERSION"
file_case "K8S_VERSION=1.36"
check "[M1] bad K8S_VERSION refused at load" nonzero "K8S_VERSION"

if grep -q 'ALLOW_NON_ARM64' <(sed -n '1,/^set -euo pipefail/p' "${HYPERVISOR}" | grep -v 'renamed'); then
  FAILED=$((FAILED + 1))
  printf 'FAIL: [M3] header or usage still documents ALLOW_NON_ARM64\n' >&2
else
  PASSED=$((PASSED + 1))
fi

printf '%d passed, %d failed\n' "${PASSED}" "${FAILED}"
if (( PASSED + FAILED == 0 || FAILED > 0 )); then
  exit 1
fi

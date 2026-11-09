#!/usr/bin/env bash
# test-launch-biome.sh - tests the biome-based worker launch in hypervisor-prep.sh: render, LXD profile, STOPPED instance,
# secrets push, marker wait, secrets removal and the TOBOGGANING=1 network path. Each case runs the real script through a
# driver (functions only; root check and NIC detection overridden) with PATH stubs for lxc, ip and netplan. Stubs log the
# subcommand and arguments only, never stdin. Prints "N passed, M failed"; exits non-zero on any failure or when no case ran.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HYP="${HYPERVISOR_SCRIPT:-${SCRIPT_DIR}/../hypervisor-prep.sh}"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../../.." && pwd)"
BIOMES_SRC="${BIOMES_SRC:-${REPO_ROOT}/biomes}"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/test-launch-biome.XXXXXX")"
trap 'rm -rf "${WORK}"' EXIT
TOKEN="abcdef.0123456789abcdef"
ZERO64="0000000000000000000000000000000000000000000000000000000000000000"
PASSED=0
FAILED=0
CASE=""
CASE_FAILS=""
CD=""
LOG=""
OUT=""
RC=0

[[ -f "${HYP}" ]] || { printf 'hypervisor script not found: %s\n' "${HYP}" >&2; exit 1; }
[[ -f "${BIOMES_SRC}/_lib/render.py" && -f "${BIOMES_SRC}/k8s-worker/biome.json" ]] \
  || { printf 'BIOMES_SRC lacks _lib/render.py or k8s-worker/biome.json: %s\n' "${BIOMES_SRC}" >&2; exit 1; }

BIOMES_WORK="${WORK}/biomes"
mkdir -p "${BIOMES_WORK}"
cp -R "${BIOMES_SRC}/_lib" "${BIOMES_SRC}/k8s-worker" "${BIOMES_WORK}/"
rm -rf "${BIOMES_WORK}/_lib/__pycache__"

SYS="${WORK}/sys/class/net"
for n in mgmt0 app0 tap0 appdown; do
  mkdir -p "${SYS}/${n}"
done
printf 'up' > "${SYS}/mgmt0/operstate"
printf 'up' > "${SYS}/app0/operstate"
printf 'up' > "${SYS}/tap0/operstate"
printf 'down' > "${SYS}/appdown/operstate"

mkdir -p "${WORK}/bin"
cat > "${WORK}/bin/lxc" <<'EOF'
#!/usr/bin/env bash
# lxc stub: logs subcommand and arguments (never stdin); answers state queries from STUB_* settings.
printf '%s\n' "$*" >> "${STUB_LOG}"
# image_ok <remote:alias> models the real remotes: ubuntu: has bare release aliases, images: has ubuntu/<ver>; others need STUB_IMAGE_OK.
image_ok() {
  [[ "${STUB_IMAGE_MISSING:-0}" != 1 ]] || return 1
  local r="${1%%:*}" a="${1#*:}"
  case "${r}" in
    ubuntu) [[ "${a}" =~ ^[0-9]{2}\.[0-9]{2}$ ]] ;;
    images) [[ "${a}" == ubuntu/* ]] ;;
    *) [[ -n "${STUB_IMAGE_OK:-}" && "$1" == "${STUB_IMAGE_OK}" ]] ;;
  esac
}
case "$1" in
  image)
    if [[ "$2" == info ]]; then
      image_ok "$3" || { echo "Error: Image not found" >&2; exit 1; }
    fi
    exit 0
    ;;
  init)
    image_ok "$2" || { echo "Error: Failed getting remote image info: Image not found" >&2; exit 1; }
    exit 0
    ;;
  profile)
    case "$2" in
      show) [[ "${STUB_PROFILE_EXISTS:-0}" == 1 ]] && exit 0; echo "Error: not found" >&2; exit 1 ;;
      edit) cat > "${STUB_DIR}/profile-body.json"; exit 0 ;;
    esac
    ;;
  info)
    if [[ "${STUB_INSTANCE_EXISTS:-0}" == 1 ]]; then
      printf 'Name: x\nStatus: %s\n' "${STUB_INSTANCE_STATE:-RUNNING}"
      exit 0
    fi
    echo "Error: not found" >&2
    exit 1
    ;;
  config)
    if [[ "$2" == set && "$4" == cloud-init.* ]]; then cat > "${STUB_DIR}/stdin-$4"; fi
    exit 0
    ;;
  file)
    if [[ "$2" == pull ]]; then
      case "$3" in
        *biome-report.failed)
          if [[ -n "${STUB_FAILED:-}" ]]; then printf '%s\n' "${STUB_FAILED}"; exit 0; fi
          echo "Error: not found" >&2; exit 1 ;;
        *biome-report.done)
          if [[ "${STUB_DONE_MODE:-ok}" == ok ]]; then exit 0; fi
          echo "Error: not found" >&2; exit 1 ;;
        *secrets.env)
          if [[ "${STUB_SECRETS_PRESENT:-0}" == 1 ]]; then echo "secret"; exit 0; fi
          echo "Error: Failed to retrieve file: not found" >&2; exit 1 ;;
      esac
    fi
    ;;
esac
exit 0
EOF
cat > "${WORK}/bin/ip" <<'EOF'
#!/usr/bin/env bash
# ip stub: logs the call; the bridge is present only when STUB_BRIDGE=1 (default).
printf 'ip %s\n' "$*" >> "${STUB_LOG}"
[[ "${STUB_BRIDGE:-1}" == 1 ]]
EOF
cat > "${WORK}/bin/netplan" <<'EOF'
#!/usr/bin/env bash
# netplan stub: logs the call so tests can prove netplan was never invoked.
printf 'netplan %s\n' "$*" >> "${STUB_LOG}"
exit 0
EOF
cat > "${WORK}/bin/snap" <<'EOF'
#!/usr/bin/env bash
# snap stub: the lxd snap is always installed.
exit 0
EOF
cat > "${WORK}/bin/systemctl" <<'EOF'
#!/usr/bin/env bash
# systemctl stub: ssh.socket is active only when STUB_SOCKET=1; everything else succeeds.
printf 'systemctl %s\n' "$*" >> "${STUB_LOG}"
if [[ "$1" == is-active ]]; then [[ "${STUB_SOCKET:-0}" == 1 ]]; else exit 0; fi
EOF
chmod 0755 "${WORK}/bin/lxc" "${WORK}/bin/ip" "${WORK}/bin/netplan" "${WORK}/bin/snap" "${WORK}/bin/systemctl"

DRIVER="${WORK}/driver.sh"
cat > "${DRIVER}" <<'EOF'
#!/usr/bin/env bash
# Driver: sources the real hypervisor-prep.sh (BASH_SOURCE guard keeps main from running), overrides the root check and
# NIC detection (logged), copies the render directory out before cleanup so tests can inspect rendered files, then runs main.
set -euo pipefail
source "$1"
export PATH="${STUB_BIN}:${PATH}"
host_checks() { :; }
if [[ -z "${REAL_DETECT:-}" ]]; then
  detect_network() { printf 'DETECT_CALLED\n' >> "${STUB_LOG}"; }
fi
SSHD_DROPIN="${STUB_DIR}/sshd.conf"
SOCKET_OVERRIDE="${STUB_DIR}/socket.conf"
SERVICE_DROPIN="${STUB_DIR}/service.conf"
NFT_FILE="${STUB_DIR}/ssh.nft"
cleanup() {
  if [[ -n "${RENDER_DIR:-}" && -d "${RENDER_DIR}" ]]; then cp -R "${RENDER_DIR}" "${STUB_DIR}/render"; fi
  if [[ -n "${TMP_DIRS+x}" && ${#TMP_DIRS[@]} -gt 0 ]]; then rm -rf "${TMP_DIRS[@]}"; fi
  if [[ ${#TMP_FILES[@]} -gt 0 ]]; then rm -f "${TMP_FILES[@]}"; fi
}
main "$2"
EOF

COMMON=(UBUNTU_APT_URI=https://mirror.example.lan/ubuntu "K8S_APT_KEY_SHA256=${ZERO64}"
  REGISTRY_MIRROR=https://mirror.example.lan:5000 WAIT_SECONDS=2 POLL_INTERVAL=1)

# base_node prints the node env file shared by every case: placeholders and fixture values only.
base_node() {
  cat <<'EOF'
JOIN_ENDPOINT=192.168.8.10:6443
JOIN_TOKEN=abcdef.0123456789abcdef
CA_CERT_HASH=sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef
WORKER_NAME=k3-worker
WORKER_IP_CIDR=192.168.8.51/24
BR0_IP_CIDR=192.168.8.50/24
GATEWAY=192.168.8.1
DNS=192.168.8.1
MGMT_IFACE=mgmt0
MGMT_IP_CIDR=192.168.4.51/24
K8S_APT_KEY_FPR=0123456789ABCDEF0123456789ABCDEF01234567
CONTAINERD_VERSION=2.2.1-0ubuntu1
K8S_APT_BASE=https://mirror.example.lan/k8s/core/stable/v1.35/deb
EOF
}

# mk_node <file> [KEY=VALUE...] writes a 0600 node env file: base values plus the extra lines.
mk_node() {
  local f="$1" x
  shift
  ( umask 077; { base_node; for x in "$@"; do printf '%s\n' "$x"; done; } > "${f}" )
}

# mk_node_without <file> <KEY> [KEY=VALUE...] writes the base node file with one key removed.
mk_node_without() {
  local f="$1" key="$2" x
  shift 2
  ( umask 077; { base_node | grep -v -- "^${key}="; for x in "$@"; do printf '%s\n' "$x"; done; } > "${f}" )
}

# run_driver <node-file> [KEY=VALUE...] runs the real worker step in a clean environment and records OUT and RC for the current case.
run_driver() {
  local node="$1" cmd="worker" a
  local -a extra=()
  shift
  for a in "$@"; do
    case "${a}" in
      CMD=*) cmd="${a#CMD=}" ;;
      *) extra+=("${a}") ;;
    esac
  done
  if OUT="$(env -i PATH="${WORK}/bin:/usr/bin:/bin" STUB_BIN="${WORK}/bin" NODE_ENV_FILE="${node}" STUB_DIR="${CD}" STUB_LOG="${LOG}" \
      NETPLAN_OUT="${CD}/netplan.yaml" SYSFS_NET="${SYS}" BIOMES_DIR="${BIOMES_WORK}" \
      "${COMMON[@]}" ${extra[@]+"${extra[@]}"} bash "${DRIVER}" "${HYP}" "${cmd}" 2>&1)"; then
    RC=0
  else
    RC=$?
  fi
}

# case_begin <name> starts a case with a fresh directory and an empty lxc log.
case_begin() {
  CASE="$1"
  CASE_FAILS=""
  CD="$(mktemp -d "${WORK}/case.XXXXXX")"
  LOG="${CD}/lxc.log"
  : > "${LOG}"
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
    [[ "${RC}" -eq 0 ]] || check_fail "exit ${RC}, expected 0"
  else
    [[ "${RC}" -ne 0 ]] || check_fail "exit 0, expected non-zero"
  fi
}

# want_out <ERE> and no_out <ERE> check the captured stdout and stderr of the last run.
want_out() {
  grep -E -q -- "$1" <<< "${OUT}" || check_fail "output lacks /$1/"
}
no_out() {
  if grep -E -q -- "$1" <<< "${OUT}"; then check_fail "output has /$1/"; fi
}

# want_log <ERE> and no_log <ERE> check the lxc and stub log, one line per invocation.
want_log() {
  grep -E -q -- "$1" "${LOG}" || check_fail "log lacks /$1/"
}
no_log() {
  if grep -E -q -- "$1" "${LOG}"; then check_fail "log has /$1/"; fi
}

# first_line <ERE> prints the 1-based log line of the first match, or 0 when none matches.
first_line() {
  local n=""
  if n="$(grep -E -n -m 1 -- "$1" "${LOG}")"; then
    printf '%s' "${n%%:*}"
  else
    printf '0'
  fi
}

# want_order <ERE-A> <ERE-B> checks that A's first matching line precedes B's first matching line, both present.
want_order() {
  local a b
  a="$(first_line "$1")"
  b="$(first_line "$2")"
  if [[ "${a}" -eq 0 || "${b}" -eq 0 || "${a}" -ge "${b}" ]]; then
    check_fail "order: /$1/ (line ${a}) must precede /$2/ (line ${b})"
  fi
}

# has_fixed <file> <string> and lacks_fixed <file> <string> check a rendered file for a literal string.
has_fixed() {
  if [[ ! -f "$1" ]] || ! grep -F -q -- "$2" "$1"; then
    check_fail "$(basename "$1") lacks '$2'"
  fi
}
lacks_fixed() {
  if [[ ! -f "$1" ]]; then
    check_fail "missing $(basename "$1") to check for leaks"
  elif grep -F -q -- "$2" "$1"; then
    check_fail "$(basename "$1") contains a forbidden string"
  fi
}

# leak_check checks that the join token is absent from every surface an attacker or log reader could see.
leak_check() {
  lacks_fixed "${LOG}" "${TOKEN}"
  no_out "${TOKEN}"
  for f in params.json profile.json user-data network-config instance.json; do
    lacks_fixed "${CD}/render/${f}" "${TOKEN}"
  done
  lacks_fixed "${CD}/profile-body.json" "${TOKEN}"
  lacks_fixed "${CD}/stdin-cloud-init.user-data" "${TOKEN}"
  has_fixed "${CD}/render/secrets.env" "GOUGH_JOIN_TOKEN="
}

# host_secret_path prints the host-side secrets.env path from the file push log line (second-to-last argument).
host_secret_path() {
  awk '/^file push / { print $(NF-1); exit }' "${LOG}"
}

# --- success path ---------------------------------------------------------------------------------------------

case_begin "success: biome flow, order, 0600 push, no lxc exec, token confined"
mk_node "${CD}/node.env"
run_driver "${CD}/node.env"
want_rc 0
want_order "^profile edit " "^init ubuntu:26.04 k3-worker -p default -p gough-k8s-worker$"
want_order "^init " "^file push --mode 0600"
want_order "^file push --mode 0600" "^start k3-worker$"
want_order "^start k3-worker$" "file pull k3-worker/var/lib/gough/biome-report.done"
want_order "file pull k3-worker/var/lib/gough/biome-report.done" "file pull k3-worker/opt/gough/biome/secrets.env"
no_log "^exec "
want_log "^config set k3-worker limits.cpu 4$"
want_log "^config device override k3-worker root pool=default size=100GiB$"
secret_host="$(host_secret_path)"
if [[ -z "${secret_host}" || -e "${secret_host}" ]]; then check_fail "host secrets.env missing from push line or not removed"; fi
has_fixed "${CD}/profile-body.json" '"nictype": "bridged"'
has_fixed "${CD}/profile-body.json" '"parent": "br0"'
leak_check
case_end

case_begin "profile exists: no create, edit still applied"
mk_node "${CD}/node.env"
run_driver "${CD}/node.env" STUB_PROFILE_EXISTS=1
want_rc 0
no_log "^profile create "
want_log "^profile edit gough-k8s-worker$"
case_end

# --- argument and input failures (fail before any lxc change) -------------------------------------------------

case_begin "missing BIOMES_DIR fails loudly"
mk_node "${CD}/node.env"
run_driver "${CD}/node.env" "BIOMES_DIR=${WORK}/nonexistent"
want_rc nz
want_out "does not contain _lib/render.py"
no_log "^(profile|init|config|file|start) "
case_end

case_begin "missing required mapping names the parameter and its variable"
mk_node "${CD}/node.env"
run_driver "${CD}/node.env" "K8S_APT_KEY_SHA256="
want_rc nz
want_out "k8s_apt_key_sha256 \(set K8S_APT_KEY_SHA256\)"
no_log "^(profile|init|config|file|start) "
case_end

case_begin "invalid biome parameter (DNS octet 999) is refused at load, before the renderer"
mk_node "${CD}/node.env"
run_driver "${CD}/node.env" DNS=999.1.1.1
want_rc nz
want_out "DNS is not a valid IPv4 address"
no_log "^(profile|init|config|file|start) "
case_end

case_begin "missing NODE_ENV_FILE fails loudly"
mk_node "${CD}/node.env"
run_driver "${CD}/node.env" "NODE_ENV_FILE="
want_rc nz
want_out "NODE_ENV_FILE is required"
case_end

case_begin "K8S_APT_KEY_FILE refused (biome serves the key by URL)"
mk_node "${CD}/node.env"
run_driver "${CD}/node.env" K8S_APT_KEY_FILE=/etc/hostname
want_rc nz
want_out "K8S_APT_KEY_FILE is not supported"
no_log "^init "
case_end

# --- marker wait and secrets confirmation -----------------------------------------------------------------------

case_begin "timeout: no done marker fails and leaves the instance"
mk_node "${CD}/node.env"
run_driver "${CD}/node.env" STUB_DONE_MODE=never WAIT_SECONDS=2 POLL_INTERVAL=1
want_rc nz
want_out "timed out after 2s waiting for biome-report.done"
want_out "instance left for inspection"
no_log "^delete "
no_log "^exec "
case_end

case_begin "failed marker: fails at once with the non-secret reason"
mk_node "${CD}/node.env"
run_driver "${CD}/node.env" "STUB_FAILED=k8s-worker biome failed during step kubeadm-join (exit 1)"
want_rc nz
want_out "reported failure in k3-worker: k8s-worker biome failed during step kubeadm-join"
no_log "biome-report.done"
no_log "^exec "
case_end

case_begin "secrets.env still present after done: fails loudly"
mk_node "${CD}/node.env"
run_driver "${CD}/node.env" STUB_SECRETS_PRESENT=1
want_rc nz
want_out "secrets.env is still present"
case_end

# --- dry run ------------------------------------------------------------------------------------------------------

case_begin "DRY_RUN=1: no lxc call at all"
mk_node "${CD}/node.env"
run_driver "${CD}/node.env" DRY_RUN=1
want_rc 0
no_log "^(profile|info|image|init|config|file|start|exec|delete) "
want_out "\[dry-run\] lxc init ubuntu:26.04 k3-worker"
want_out "\[dry-run\] poll k3-worker:/var/lib/gough/biome-report.done"
case_end

# --- existing instance ----------------------------------------------------------------------------------------------

case_begin "existing RUNNING instance: no create or push, marker verified"
mk_node "${CD}/node.env"
run_driver "${CD}/node.env" STUB_INSTANCE_EXISTS=1
want_rc 0
want_out "exists and is RUNNING; skipping create"
no_log "^init "
no_log "^file push "
want_log "file pull k3-worker/var/lib/gough/biome-report.done"
case_end

case_begin "existing STOPPED instance: fails loudly, no change"
mk_node "${CD}/node.env"
run_driver "${CD}/node.env" STUB_INSTANCE_EXISTS=1 STUB_INSTANCE_STATE=STOPPED
want_rc nz
want_out "exists in state STOPPED"
no_log "^init "
no_log "^start "
case_end

# --- bridge path (TOBOGGANING=0 unchanged) -------------------------------------------------------------------------

case_begin "TOBOGGANING=0 bridge absent: refuses before any lxc change"
mk_node "${CD}/node.env"
run_driver "${CD}/node.env" STUB_BRIDGE=0
want_rc nz
want_out "bridge br0 is absent"
no_log "^(profile|init|config|file|start) "
case_end

case_begin "TOBOGGANING=0 detection still runs"
mk_node "${CD}/node.env"
run_driver "${CD}/node.env"
want_log "^DETECT_CALLED$"
case_end

case_begin "APP_NIC_TYPE=macvlan refused without TOBOGGANING=1"
mk_node "${CD}/node.env"
run_driver "${CD}/node.env" APP_NIC_TYPE=macvlan
want_rc nz
want_out "APP_NIC_TYPE=macvlan requires TOBOGGANING=1"
case_end

# --- TOBOGGANING ---------------------------------------------------------------------------------------------------

case_begin "TOBOGGANING=1: no detection, no netplan, parent=APP_IFACE (virtual tap0), no bridge check"
mk_node "${CD}/node.env" TOBOGGANING=1 APP_IFACE=tap0
run_driver "${CD}/node.env" STUB_BRIDGE=0
want_rc 0
want_out "netplan SKIPPED"
no_log "^DETECT_CALLED$"
no_log "^netplan "
no_log "^ip "
[[ ! -e "${CD}/netplan.yaml" ]] || check_fail "netplan file was written"
has_fixed "${CD}/profile-body.json" '"parent": "tap0"'
has_fixed "${CD}/profile-body.json" '"nictype": "bridged"'
leak_check
case_end

case_begin "TOBOGGANING=1 macvlan: nictype applied to eth0 with warning"
mk_node "${CD}/node.env" TOBOGGANING=1 APP_IFACE=tap0 APP_NIC_TYPE=macvlan
run_driver "${CD}/node.env"
want_rc 0
want_out "overriding eth0 nictype on the host"
has_fixed "${CD}/profile-body.json" '"nictype": "macvlan"'
no_log "^netplan "
case_end

case_begin "TOBOGGANING=1 without APP_IFACE fails"
mk_node "${CD}/node.env" TOBOGGANING=1
run_driver "${CD}/node.env"
want_rc nz
want_out "TOBOGGANING=1 requires APP_IFACE"
no_log "^init "
case_end

case_begin "TOBOGGANING=1 without MGMT_IFACE fails"
mk_node_without "${CD}/node.env" MGMT_IFACE TOBOGGANING=1 APP_IFACE=tap0
run_driver "${CD}/node.env"
want_rc nz
want_out "TOBOGGANING=1 requires MGMT_IFACE"
case_end

case_begin "TOBOGGANING=1 APP_IFACE not found fails"
mk_node "${CD}/node.env" TOBOGGANING=1 APP_IFACE=nope0
run_driver "${CD}/node.env"
want_rc nz
want_out "interface nope0 not found"
case_end

case_begin "TOBOGGANING=1 APP_IFACE down fails"
mk_node "${CD}/node.env" TOBOGGANING=1 APP_IFACE=appdown
run_driver "${CD}/node.env"
want_rc nz
want_out "interface appdown operstate is 'down', expected up"
case_end

case_begin "TOBOGGANING=2 rejected"
mk_node "${CD}/node.env"
run_driver "${CD}/node.env" TOBOGGANING=2
want_rc nz
want_out "TOBOGGANING must be exactly 0 or 1"
no_log "^init "
case_end

case_begin "APP_NIC_TYPE=sriov rejected"
mk_node "${CD}/node.env" TOBOGGANING=1 APP_IFACE=tap0
run_driver "${CD}/node.env" APP_NIC_TYPE=sriov
want_rc nz
want_out "APP_NIC_TYPE must be bridged, macvlan or ipvlan"
case_end


# --- correctness fixes (H1-H3, M1, M2, M4, M5, M6) --------------------------------------------------------------------

# render_text prints every rendered file plus the decoded params.env (user-data carries it base64-encoded).
render_text() {
  [[ -f "${CD}/render/user-data" ]] || return 0
  cat "${CD}/render/"*
  awk '/params.env/ { f = 1 } f && /content:/ { gsub(/.*content: "|"$/, ""); print; exit }' "${CD}/render/user-data" | base64 -d
}

# render_has <string> and render_lacks <string> check the rendered text of the current case for a literal string.
render_has() {
  local t
  t="$(render_text)"
  [[ "${t}" == *"$1"* ]] || check_fail "render lacks '$1'"
}
render_lacks() {
  local t
  t="$(render_text)"
  if [[ "${t}" == *"$1"* ]]; then check_fail "render has '$1'"; fi
}

# mk_nic <root> <name> <speed> [master] builds a live physical NIC fixture; master adds the sysfs master link.
mk_nic() {
  local d="$1/$2"
  mkdir -p "${d}/device"
  printf '1\n' > "${d}/type"
  printf 'up\n' > "${d}/operstate"
  printf '1\n' > "${d}/carrier"
  printf '%s\n' "$3" > "${d}/speed"
  if [[ -n "${4:-}" ]]; then ln -s "../$4" "${d}/master"; fi
}

# mk_vnic <root> <virtroot> <name> builds a virtual interface (symlink into devices/virtual, no device link), state up.
mk_vnic() {
  mkdir -p "$2/$3"
  printf '1\n' > "$2/$3/type"
  printf 'up\n' > "$2/$3/operstate"
  printf '1\n' > "$2/$3/carrier"
  printf '10000\n' > "$2/$3/speed"
  ln -s "$2/$3" "$1/$3"
}

FXB="${WORK}/fx"
mkdir -p "${FXB}"
# fx_single_after: mgmt0 + enpa enslaved to br0 (single NIC after netplan applied).
mkdir -p "${FXB}/single/net" "${FXB}/single/virt" "${FXB}/single/bond"
mk_nic "${FXB}/single/net" mgmt0 1000
mk_nic "${FXB}/single/net" enpa 100 br0
mk_vnic "${FXB}/single/net" "${FXB}/single/virt" br0
# fx_bond_after: mgmt0 + bond0 (802.3ad, master br0) with slaves e1 e2.
mkdir -p "${FXB}/bond/net" "${FXB}/bond/virt" "${FXB}/bond/bond"
mk_nic "${FXB}/bond/net" mgmt0 1000
mk_vnic "${FXB}/bond/net" "${FXB}/bond/virt" bond0
ln -s ../br0 "${FXB}/bond/net/bond0/master"
mk_vnic "${FXB}/bond/net" "${FXB}/bond/virt" br0
mk_nic "${FXB}/bond/net" e1 10000 bond0
mk_nic "${FXB}/bond/net" e2 10000 bond0
printf 'Bonding Mode: IEEE 802.3ad Dynamic link aggregation\nSlave Interface: e1\nSlave Interface: e2\n' > "${FXB}/bond/bond/bond0"
# fx_fresh: mgmt0 + one spare NIC, no bridge.
mkdir -p "${FXB}/fresh/net" "${FXB}/fresh/virt" "${FXB}/fresh/bond"
mk_nic "${FXB}/fresh/net" mgmt0 1000
mk_nic "${FXB}/fresh/net" enpb 10000

# --- H1 image remote and alias -----------------------------------------------------------------------------------

case_begin "[H1] default image pair is valid and verified read-only before any mutation"
mk_node "${CD}/node.env"
run_driver "${CD}/node.env"
want_rc 0
want_log "^image info ubuntu:26.04$"
want_order "^image info " "^profile "
want_order "^image info " "^init "
case_end

case_begin "[H1] missing image fails with nothing created"
mk_node "${CD}/node.env"
run_driver "${CD}/node.env" STUB_IMAGE_MISSING=1
want_rc nz
want_out "image ubuntu:26.04 .*not"
no_log "^(profile|init|config|file|start) "
case_end

case_begin "[H1] images remote keeps the ubuntu/ alias form"
mk_node "${CD}/node.env"
run_driver "${CD}/node.env" IMAGE_REMOTE=images
want_rc 0
want_log "^image info images:ubuntu/26.04$"
want_log "^init images:ubuntu/26.04 "
case_end

case_begin "[H1] custom remote and alias stay overridable"
mk_node "${CD}/node.env" IMAGE_REMOTE=lab IMAGE_ALIAS=custom-1
run_driver "${CD}/node.env" STUB_IMAGE_OK=lab:custom-1
want_rc 0
want_log "^init lab:custom-1 "
case_end

case_begin "[H1] ubuntu remote with an ubuntu/ alias is refused before any lxc call"
mk_node "${CD}/node.env" IMAGE_ALIAS=ubuntu/26.04
run_driver "${CD}/node.env"
want_rc nz
want_out "ubuntu/"
no_log "^(image|profile|init|config|file|start) "
case_end

case_begin "[H1] DRY_RUN skips the image check"
mk_node "${CD}/node.env"
run_driver "${CD}/node.env" DRY_RUN=1
want_rc 0
no_log "^image "
case_end

# --- H3 re-run after netplan ----------------------------------------------------------------------------------------

case_begin "[H3] single NIC after netplan: worker preflight passes, uplink derived from br0 member"
mk_node_without "${CD}/node.env" MGMT_IFACE
run_driver "${CD}/node.env" REAL_DETECT=1 "SYSFS_NET=${FXB}/single/net" "PROC_BONDING=${FXB}/single/bond"
want_rc 0
want_out "MGMT_IFACE=mgmt0 UPLINK_MODE=single UPLINK_IFACE=enpa"
case_end

case_begin "[H3] single NIC after netplan with MGMT_IFACE set: worker preflight passes"
mk_node "${CD}/node.env"
run_driver "${CD}/node.env" REAL_DETECT=1 "SYSFS_NET=${FXB}/single/net" "PROC_BONDING=${FXB}/single/bond"
want_rc 0
want_out "UPLINK_MODE=single UPLINK_IFACE=enpa"
case_end

case_begin "[H3] bond after netplan: worker preflight passes, slaves stay slaves"
mk_node "${CD}/node.env"
run_driver "${CD}/node.env" REAL_DETECT=1 "SYSFS_NET=${FXB}/bond/net" "PROC_BONDING=${FXB}/bond/bond"
want_rc 0
want_out "UPLINK_MODE=bond .*BOND_MEMBER_1=e1 BOND_MEMBER_2=e2"
case_end

case_begin "[H3] fresh host unchanged: spare NIC becomes the single uplink"
mk_node "${CD}/node.env"
run_driver "${CD}/node.env" REAL_DETECT=1 "SYSFS_NET=${FXB}/fresh/net" "PROC_BONDING=${FXB}/fresh/bond"
want_rc 0
want_out "UPLINK_MODE=single UPLINK_IFACE=enpb"
case_end

# --- H2 TOBOGGANING=1 without BR0_IP_CIDR -------------------------------------------------------------------------

case_begin "[H2] TOBOGGANING=1 no BR0_IP_CIDR: ssh writes restriction files with the management address"
mk_node_without "${CD}/node.env" BR0_IP_CIDR TOBOGGANING=1 APP_IFACE=tap0
run_driver "${CD}/node.env" CMD=ssh
want_rc 0
no_out "unbound variable"
has_fixed "${CD}/sshd.conf" "ListenAddress 192.168.4.51"
has_fixed "${CD}/ssh.nft" 'iifname "mgmt0" tcp dport 22 accept'
want_out "session on 192.168.4.51"
no_out "BR0"
case_end

case_begin "[H2] TOBOGGANING=1 no BR0_IP_CIDR: all (dry-run) has no unbound variable"
mk_node_without "${CD}/node.env" BR0_IP_CIDR TOBOGGANING=1 APP_IFACE=tap0
run_driver "${CD}/node.env" CMD=all DRY_RUN=1
want_rc 0
no_out "unbound variable"
want_out "\[dry-run\] write ${CD}/sshd.conf"
want_out "session on 192.168.4.51"
case_end

case_begin "[H2] TOBOGGANING=1 gateway outside the worker subnet is refused (ssh)"
mk_node_without "${CD}/node.env" BR0_IP_CIDR TOBOGGANING=1 APP_IFACE=tap0
run_driver "${CD}/node.env" CMD=ssh GATEWAY=192.168.9.1
want_rc nz
want_out "GATEWAY 192.168.9.1 is not inside 192.168.8.51/24"
[[ ! -e "${CD}/sshd.conf" ]] || check_fail "restriction file written despite failure"
case_end

case_begin "[H2] TOBOGGANING=1 overlapping app and management subnets are refused (ssh)"
mk_node_without "${CD}/node.env" BR0_IP_CIDR TOBOGGANING=1 APP_IFACE=tap0
run_driver "${CD}/node.env" CMD=ssh WORKER_IP_CIDR=192.168.4.60/24 GATEWAY=192.168.4.1
want_rc nz
want_out "overlap|inside the management subnet"
case_end

case_begin "[H2] TOBOGGANING=1 provided BR0_IP_CIDR is validated and otherwise ignored"
mk_node "${CD}/node.env" TOBOGGANING=1 APP_IFACE=tap0
run_driver "${CD}/node.env" CMD=ssh BR0_IP_CIDR=10.9.9.9/24
want_rc 0
case_end

case_begin "[H2] TOBOGGANING=1 invalid BR0_IP_CIDR is refused"
mk_node "${CD}/node.env" TOBOGGANING=1 APP_IFACE=tap0
run_driver "${CD}/node.env" CMD=ssh BR0_IP_CIDR=1.2.3.4/33
want_rc nz
want_out "BR0_IP_CIDR is not a valid IPv4 CIDR"
case_end

# --- M1 k8s package version -----------------------------------------------------------------------------------------

case_begin "[M1] K8S_VERSION override derives a consistent package version"
mk_node "${CD}/node.env" K8S_VERSION=1.36.0
run_driver "${CD}/node.env"
want_rc 0
render_has "GOUGH_K8S_VERSION=1.36.0"
render_has "GOUGH_K8S_PACKAGE_VERSION=1.36.0-1.1"
render_lacks "1.35.9-1.1"
case_end

case_begin "[M1] explicit K8S_PACKAGE_VERSION from the node file wins over the derived one"
mk_node "${CD}/node.env" K8S_VERSION=1.36.0 K8S_PACKAGE_VERSION=1.36.0-2.1
run_driver "${CD}/node.env"
want_rc 0
render_has "GOUGH_K8S_PACKAGE_VERSION=1.36.0-2.1"
case_end

case_begin "[M1] no override keeps the biome defaults"
mk_node "${CD}/node.env"
run_driver "${CD}/node.env"
want_rc 0
render_has "GOUGH_K8S_VERSION=1.35.9"
render_has "GOUGH_K8S_PACKAGE_VERSION=1.35.9-1.1"
case_end

case_begin "[M1] package version that does not match K8S_VERSION is refused"
mk_node "${CD}/node.env" K8S_VERSION=1.36.0 K8S_PACKAGE_VERSION=1.35.9-1.1
run_driver "${CD}/node.env"
want_rc nz
want_out "K8S_PACKAGE_VERSION"
no_log "^(profile|init|config|file|start) "
case_end

# --- M2 mirror and registry mappings --------------------------------------------------------------------------------

case_begin "[M2] MIRRORED_REGISTRIES, ALLOW_REGISTRY_FALLBACK and internal CA map from the node file"
mk_node "${CD}/node.env" MIRRORED_REGISTRIES=docker.io,quay.io ALLOW_REGISTRY_FALLBACK=1 \
  INTERNAL_CA_URL=https://mirror.example.lan/ca.pem "INTERNAL_CA_SHA256=${ZERO64}"
run_driver "${CD}/node.env"
want_rc 0
render_has "docker.io,quay.io"
render_has "GOUGH_INTERNAL_CA_URL=https://mirror.example.lan/ca.pem"
render_has "GOUGH_ALLOW_REGISTRY_FALLBACK=true"
case_end

case_begin "[M2] http mirror without ALLOW_INSECURE_MIRRORS is refused by the renderer"
mk_node "${CD}/node.env"
run_driver "${CD}/node.env" REGISTRY_MIRROR=http://mirror.example.lan:5000
want_rc nz
want_out "must use https"
case_end

case_begin "[M2] ALLOW_INSECURE_MIRRORS=1 on the command line permits http and warns loudly"
mk_node "${CD}/node.env"
run_driver "${CD}/node.env" REGISTRY_MIRROR=http://mirror.example.lan:5000 ALLOW_INSECURE_MIRRORS=1
want_rc 0
want_out "WARNING: ALLOW_INSECURE_MIRRORS=1"
case_end

case_begin "[M2] ALLOW_INSECURE_MIRRORS in the node file is refused like the other switches"
mk_node "${CD}/node.env" ALLOW_INSECURE_MIRRORS=1
run_driver "${CD}/node.env"
want_rc nz
want_out "'ALLOW_INSECURE_MIRRORS' is a per-run safety switch"
case_end

case_begin "[M2] invalid mapping values are refused"
mk_node "${CD}/node.env"
run_driver "${CD}/node.env" "MIRRORED_REGISTRIES=Bad Host"
want_rc nz
want_out "MIRRORED_REGISTRIES"
run_driver "${CD}/node.env" ALLOW_REGISTRY_FALLBACK=yes
want_rc nz
want_out "ALLOW_REGISTRY_FALLBACK"
run_driver "${CD}/node.env" ALLOW_INSECURE_MIRRORS=2
want_rc nz
want_out "ALLOW_INSECURE_MIRRORS"
run_driver "${CD}/node.env" INTERNAL_CA_URL=https://mirror.example.lan/ca.pem
want_rc nz
want_out "INTERNAL_CA"
no_log "^(profile|init|config|file|start) "
case_end

# --- M4 duplicate addresses -----------------------------------------------------------------------------------------

case_begin "[M4] BR0 == WORKER == GATEWAY is refused naming a colliding pair"
mk_node "${CD}/node.env"
run_driver "${CD}/node.env" BR0_IP_CIDR=192.168.8.1/24 WORKER_IP_CIDR=192.168.8.1/24
want_rc nz
want_out "address collision: BR0_IP_CIDR and WORKER_IP_CIDR both use 192.168.8.1"
no_log "^(image|profile|init|config|file|start) "
case_end

case_begin "[M4] WORKER == GATEWAY is refused"
mk_node "${CD}/node.env"
run_driver "${CD}/node.env" WORKER_IP_CIDR=192.168.8.1/24
want_rc nz
want_out "address collision: WORKER_IP_CIDR and GATEWAY both use 192.168.8.1"
case_end

case_begin "[M4] BR0 == GATEWAY is refused"
mk_node "${CD}/node.env"
run_driver "${CD}/node.env" BR0_IP_CIDR=192.168.8.1/24
want_rc nz
want_out "address collision: BR0_IP_CIDR and GATEWAY both use 192.168.8.1"
case_end

case_begin "[M4] MGMT == WORKER host address is refused"
mk_node "${CD}/node.env"
run_driver "${CD}/node.env" MGMT_IP_CIDR=192.168.8.51/24
want_rc nz
want_out "address collision: WORKER_IP_CIDR and MGMT_IP_CIDR both use 192.168.8.51"
case_end

case_begin "[M4] TOBOGGANING=1 ignores BR0_IP_CIDR in the collision check"
mk_node "${CD}/node.env" TOBOGGANING=1 APP_IFACE=tap0
run_driver "${CD}/node.env" BR0_IP_CIDR=192.168.8.51/24
want_rc 0
case_end

# --- M5 ssh boot ordering -------------------------------------------------------------------------------------------

case_begin "[M5] ssh.socket override binds with FreeBind after the ListenStream reset"
mk_node_without "${CD}/node.env" BR0_IP_CIDR TOBOGGANING=1 APP_IFACE=tap0
run_driver "${CD}/node.env" CMD=ssh STUB_SOCKET=1
want_rc 0
if [[ "$(cat "${CD}/socket.conf" 2>/dev/null)" != $'[Socket]\nListenStream=\nListenStream=192.168.4.51:22\nFreeBind=true' ]]; then
  check_fail "socket override text differs"
fi
[[ ! -e "${CD}/service.conf" ]] || check_fail "service drop-in written although ssh.socket is active"
want_out "rm -f .*${CD}/service.conf"
case_end

case_begin "[M5] ssh.service case gets a network-online drop-in"
mk_node_without "${CD}/node.env" BR0_IP_CIDR TOBOGGANING=1 APP_IFACE=tap0
run_driver "${CD}/node.env" CMD=ssh STUB_SOCKET=0
want_rc 0
has_fixed "${CD}/service.conf" "[Unit]"
has_fixed "${CD}/service.conf" "After=network-online.target"
has_fixed "${CD}/service.conf" "Wants=network-online.target"
[[ ! -e "${CD}/socket.conf" ]] || check_fail "socket override written although ssh.socket is inactive"
want_out "rm -f .*${CD}/service.conf"
case_end

# --- M6 vm instances ------------------------------------------------------------------------------------------------

case_begin "[M6] INSTANCE_TYPE=vm fails at preflight before any lxc call"
mk_node "${CD}/node.env" INSTANCE_TYPE=vm
run_driver "${CD}/node.env"
want_rc nz
want_out "INSTANCE_TYPE=vm is not supported yet: delivering secrets.env to a stopped VM is unverified; use INSTANCE_TYPE=container"
[[ ! -s "${LOG}" ]] || check_fail "an external command ran before the refusal"
case_end

if [[ $((PASSED + FAILED)) -eq 0 ]]; then
  printf 'no cases ran\n' >&2
  exit 1
fi
printf '%d passed, %d failed\n' "${PASSED}" "${FAILED}"
if [[ "${FAILED}" -ne 0 ]]; then
  exit 1
fi

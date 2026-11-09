#!/usr/bin/env bash
# k8s-worker-bootstrap.sh - turns a fresh Ubuntu 24.04 arm64 LXD container into a kubeadm worker that joins an existing controller.
# Runs as root INSIDE the container; hypervisor-prep.sh (worker subcommand) pushes and executes it. Never invokes sudo.
# Subcommands: all (default), preflight, set_hostname, configure_apt_mirror, install_containerd, node_sysctl,
# kmsg_fix, bpffs, install_kube, disable_ssh, join_cluster, help. Each function is liftable into cloud-init runcmd.
#
# Environment variables (default in brackets; REQUIRED = no default, the step fails loudly without it):
#   WORKER_NAME           REQUIRED for set_hostname  container hostname = Kubernetes node name; lowercase RFC 1123.
#                                                    Convention <node>-worker (k8s3-worker). Must be unique cluster-wide.
#   ALLOW_NON_ARM64       [0]         1 = allow a non-arm64 container (testing only)
#   ALLOW_UNSUPPORTED_OS  [0]         1 = allow an Ubuntu release other than GUEST_UBUNTU_VERSION
#   GUEST_UBUNTU_VERSION  [24.04]
#   CONTAINERD_VERSION    [unset]     exact apt version to install and hold; unset = repo default
#   UBUNTU_APT_URI        [unset]     airgap: archive base URI written into the deb822 URIs: line of ubuntu.sources
#   K8S_VERSION           [1.35.9]    exact kubelet/kubeadm/kubectl version (apt glob <version>-*)
#   K8S_APT_BASE          [https://pkgs.k8s.io/core:/stable:/v1.35/deb/]  apt repo base URL (airgap mirror)
#   K8S_APT_KEY_URL       [${K8S_APT_BASE}Release.key]  armored key URL
#   K8S_APT_KEY_FILE      [unset]     airgap: local armored key file, used instead of K8S_APT_KEY_URL
#   K8S_APT_KEY_FPR       REQUIRED for install_kube  40-hex repo signing-key fingerprint (spaces ignored)
#   JOIN_ENV_FILE         REQUIRED for join_cluster  KEY=VALUE env file (Compose-style), root-owned, mode 0600 or 0400.
#                                                    Parsed, never sourced. Keys: JOIN_ENDPOINT, JOIN_TOKEN, CA_CERT_HASH and
#                                                    optional K8S_VERSION, K8S_APT_BASE, K8S_APT_KEY_FPR, CONTAINERD_VERSION.
#                                                    Removed inside the container after a successful join (kept on failure).
#   JOIN_ENDPOINT         [from file]  optional real environment override of the file value
#   CA_CERT_HASH          [from file]  optional real environment override of the file value
#                                      JOIN_TOKEN is file-only; setting it in the environment is an error.
#
# SSH: disable_ssh masks ssh.service and ssh.socket inside the container. Access is via lxc exec from the hypervisor.
#
# Controller-side kube-proxy conntrack fix (NOT done here): ConfigMap kube-proxy in kube-system with
# conntrack.maxPerCore 0, min 0, tcpEstablishedTimeout 0s, tcpCloseWaitTimeout 0s, then delete pods -l k8s-app=kube-proxy.
# It lives on the controller, which this container script cannot reach or own, and it is a cluster-wide control-plane
# change, so the operator applies it from the runbook after joining rather than having each worker mutate it.
set -euo pipefail

# log prints an informational line to stderr.
log() { printf '[k8s-worker-bootstrap] %s\n' "$*" >&2; }

# die prints an error and exits non-zero; every failure path goes through it.
die() { printf '[k8s-worker-bootstrap] ERROR: %s\n' "$*" >&2; exit 1; }

# valid_endpoint returns 0 when $1 is host:port, host an IPv4 address or RFC 1123 name, port 1-65535.
valid_endpoint() {
  local v="$1" host port o1 o2 o3 o4
  local ip_re='^[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}$'
  local name_re='^[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$'
  [[ "${v}" == *:* ]] || return 1
  host="${v%:*}"
  port="${v##*:}"
  [[ "${port}" =~ ^[0-9]{1,5}$ ]] || return 1
  (( 10#${port} >= 1 && 10#${port} <= 65535 )) || return 1
  if [[ "${host}" =~ ${ip_re} ]]; then
    IFS=. read -r o1 o2 o3 o4 <<< "${host}"
    (( 10#${o1} <= 255 && 10#${o2} <= 255 && 10#${o3} <= 255 && 10#${o4} <= 255 )) || return 1
    return 0
  fi
  [[ "${#host}" -le 253 && "${host}" == *[A-Za-z]* && "${host}" =~ ${name_re} ]]
}

# valid_token returns 0 when $1 has the kubeadm bootstrap token shape: 6 lowercase alphanumerics, a dot, 16 lowercase alphanumerics.
valid_token() {
  local re='^[a-z0-9]{6}\.[a-z0-9]{16}$'
  [[ "$1" =~ ${re} ]]
}

# valid_hash returns 0 when $1 is sha256:<64 lowercase hex>, the kubeadm CA public key pin.
valid_hash() {
  local re='^sha256:[0-9a-f]{64}$'
  [[ "$1" =~ ${re} ]]
}

# check_value <KEY> <VALUE> dies when a validated key fails its format check; other keys pass through.
# Messages name the key and never echo the value, so the bootstrap token cannot leak into logs.
check_value() {
  case "$1" in
    JOIN_ENDPOINT) valid_endpoint "$2" || die "JOIN_ENDPOINT must be host:port (IPv4 or RFC 1123 host, port 1-65535)" ;;
    CA_CERT_HASH) valid_hash "$2" || die "CA_CERT_HASH must be sha256:<64 lowercase hex>" ;;
    JOIN_TOKEN) valid_token "$2" || die "JOIN_TOKEN is not a bootstrap token (format only; value not printed)" ;;
  esac
}

# load_env_file <path> <allowed-key>... parses a Compose-style KEY=VALUE file into the allowed variables; it never sources or executes the file.
# Requires a root-owned regular file, mode 0600 or 0400, free of NUL and CR bytes; rejects per-run safety switches, unlisted or duplicate keys.
# Errors name keys or line numbers, never values. A variable already set in the process environment wins over the file, except JOIN_TOKEN, which is never read from the environment.
load_env_file() {
  local path="$1" line lead key rest value inner after stripped stat_out mode owner size nonnul a fv ok
  local lineno=0 count=0 seen=" " name_re='^[A-Za-z_][A-Za-z0-9_]*$'
  local sq="'" dq='"'
  local switches=" LAB_PRIVILEGED I_ACCEPT_HOST_ROOT_RISK APPLY_NETPLAN APPLY_SSH_RESTRICT FORCE_NETPLAN_REPLACE ALLOW_NON_ARM64 ALLOW_UNSUPPORTED_OS DRY_RUN "
  local -a allowed
  shift
  [[ $# -gt 0 ]] || die "load_env_file: no allowed keys were given"
  allowed=("$@")
  if [[ -n "${JOIN_TOKEN+x}" ]]; then
    die "JOIN_TOKEN is set in the environment; it is read only from the env file. Unset it and put it in the file"
  fi
  [[ -n "${path}" ]] || die "env file path is empty"
  if [[ -L "${path}" ]]; then
    die "env file ${path} is a symbolic link; it must be a regular file"
  fi
  if [[ -d "${path}" ]]; then
    die "env file ${path} is a directory; it must be a regular file"
  fi
  [[ -f "${path}" ]] || die "env file ${path} not found"
  stat_out="$(stat -c '%a %u' -- "${path}")"
  read -r mode owner <<< "${stat_out}"
  case "${mode}" in
    600|400) ;;
    *) die "env file ${path} must be mode 0600 or 0400 (found ${mode})" ;;
  esac
  if [[ "${owner}" != "${EUID}" ]]; then
    die "env file ${path} must be owned by uid ${EUID} (found uid ${owner})"
  fi
  size="$(wc -c < "${path}")"
  nonnul="$(tr -d '\000' < "${path}" | wc -c)"
  [[ "${size}" == "${nonnul}" ]] || die "env file ${path} contains NUL bytes"
  if LC_ALL=C grep -q -- $'\r' "${path}"; then
    die "env file ${path} contains CR bytes (CRLF line endings); save it with LF only"
  fi
  while IFS= read -r line || [[ -n "${line}" ]]; do
    lineno=$((lineno + 1))
    lead="${line%%[![:space:]]*}"
    line="${line#"${lead}"}"
    line="${line%"${line##*[![:space:]]}"}"
    if [[ -z "${line}" || "${line}" == \#* ]]; then
      continue
    fi
    case "${line}" in
      export[[:space:]]*) die "line ${lineno}: the 'export' prefix is not allowed in an env file" ;;
    esac
    if [[ "${line}" != *=* ]]; then
      die "line ${lineno}: expected KEY=VALUE (line content not printed)"
    fi
    key="${line%%=*}"
    rest="${line#*=}"
    [[ "${key}" =~ ${name_re} ]] || die "line ${lineno}: invalid key name (use [A-Za-z_][A-Za-z0-9_]*; no whitespace around '=')"
    case "${rest}" in
      [[:space:]]*) die "line ${lineno}: whitespace around '=' is not allowed" ;;
    esac
    if [[ "${switches}" == *" ${key} "* ]]; then
      die "line ${lineno}: '${key}' is a per-run safety switch and cannot be set in an env file; pass it on the command line"
    fi
    ok=0
    for a in "${allowed[@]}"; do
      if [[ "${a}" == "${key}" ]]; then
        ok=1
      fi
    done
    [[ "${ok}" == 1 ]] || die "line ${lineno}: key '${key}' is not in the allowlist for this file"
    if [[ "${seen}" == *" ${key} "* ]]; then
      die "line ${lineno}: duplicate key '${key}'"
    fi
    case "${rest}" in
      "${sq}"*)
        inner="${rest#"${sq}"}"
        [[ "${inner}" == *"${sq}"* ]] || die "line ${lineno}: unterminated single-quoted value"
        value="${inner%%"${sq}"*}"
        after="${inner#*"${sq}"}"
        ;;
      "${dq}"*)
        inner="${rest#"${dq}"}"
        [[ "${inner}" == *"${dq}"* ]] || die "line ${lineno}: unterminated double-quoted value"
        value="${inner%%"${dq}"*}"
        after="${inner#*"${dq}"}"
        ;;
      *)
        value="${rest%% #*}"
        value="${value%"${value##*[![:space:]]}"}"
        case "${value}" in
          *"${sq}"*|*"${dq}"*) die "line ${lineno}: quote characters are only allowed around a whole value" ;;
        esac
        after=""
        ;;
    esac
    if [[ -n "${after}" ]]; then
      case "${after}" in
        [[:space:]]*) ;;
        *) die "line ${lineno}: text after the closing quote must be whitespace and a comment" ;;
      esac
      lead="${after%%[![:space:]]*}"
      stripped="${after#"${lead}"}"
      [[ "${stripped}" == \#* ]] || die "line ${lineno}: text after the closing quote must be whitespace and a comment"
    fi
    printf -v "F_${key}" '%s' "${value}"
    seen="${seen}${key} "
    count=$((count + 1))
  done < "${path}"
  [[ "${count}" -gt 0 ]] || die "env file ${path} loaded zero keys"
  log "env file loaded ${count} key(s) (values not printed)"
  for a in "${allowed[@]}"; do
    if [[ "${seen}" == *" ${a} "* ]]; then
      fv="F_${a}"
      check_value "${a}" "${!fv}"
    fi
  done
  for a in "${allowed[@]}"; do
    if [[ -n "${!a:-}" ]]; then
      check_value "${a}" "${!a}"
      log "${a} taken from the environment (overrides ${path})"
    elif [[ "${seen}" == *" ${a} "* ]]; then
      fv="F_${a}"
      printf -v "${a}" '%s' "${!fv}"
    fi
  done
}

# Top-level load runs before the defaults below so file values override the built-in defaults.
if [[ -n "${JOIN_TOKEN+x}" ]]; then
  die "JOIN_TOKEN is set in the environment; it is read only from JOIN_ENV_FILE. Unset it and put it in the file"
fi
if [[ -n "${JOIN_ENV_FILE:-}" ]]; then
  load_env_file "${JOIN_ENV_FILE}" JOIN_ENDPOINT JOIN_TOKEN CA_CERT_HASH K8S_VERSION K8S_APT_BASE K8S_APT_KEY_FPR CONTAINERD_VERSION
fi


ALLOW_NON_ARM64="${ALLOW_NON_ARM64:-0}"
ALLOW_UNSUPPORTED_OS="${ALLOW_UNSUPPORTED_OS:-0}"
GUEST_UBUNTU_VERSION="${GUEST_UBUNTU_VERSION:-24.04}"
K8S_VERSION="${K8S_VERSION:-1.35.9}"
K8S_APT_BASE="${K8S_APT_BASE:-https://pkgs.k8s.io/core:/stable:/v1.35/deb/}"
case "${K8S_APT_BASE}" in
  */) ;;
  *) K8S_APT_BASE="${K8S_APT_BASE}/" ;;
esac
K8S_APT_KEY_URL="${K8S_APT_KEY_URL:-${K8S_APT_BASE}Release.key}"
KEYRING_PATH="/etc/apt/keyrings/kubernetes-apt-keyring.gpg"
KUBE_LIST="/etc/apt/sources.list.d/kubernetes.list"
CONTAINERD_CFG="/etc/containerd/config.toml"
TMP_FILES=()
FILE_CHANGED=0

# usage prints the subcommand list.
usage() {
  cat <<'EOF'
Usage: k8s-worker-bootstrap.sh [all|preflight|set_hostname|configure_apt_mirror|install_containerd|node_sysctl|kmsg_fix|bpffs|install_kube|disable_ssh|join_cluster|help]
EOF
}

# require_var checks that one named environment variable is set and non-empty.
require_var() {
  local name="$1"
  if [[ -z "${!name:-}" ]]; then
    die "required environment variable ${name} is not set"
  fi
}

# new_tmp stores a fresh private temp file path in the named variable and registers it for removal.
new_tmp() {
  local t
  t="$(mktemp)"
  TMP_FILES+=("${t}")
  printf -v "$1" '%s' "${t}"
}

# cleanup removes registered temp files on EXIT.
cleanup() {
  if [[ ${#TMP_FILES[@]} -gt 0 ]]; then
    rm -f "${TMP_FILES[@]}"
  fi
}

# scrub_file overwrites and removes a file that held secret material, falling back to rm where shred is absent.
scrub_file() {
  if command -v shred >/dev/null 2>&1; then
    shred -u "$1"
  else
    rm -f "$1"
  fi
}

# write_file_if_changed <path> <mode> reads stdin and installs it only when content differs; sets FILE_CHANGED.
write_file_if_changed() {
  local path="$1" mode="$2" tmp
  new_tmp tmp
  cat > "${tmp}"
  if [[ -f "${path}" ]] && cmp -s "${tmp}" "${path}"; then
    FILE_CHANGED=0
    return 0
  fi
  install -D -m "${mode}" "${tmp}" "${path}"
  FILE_CHANGED=1
}

# ensure_packages installs listed packages that are missing, refreshing apt indexes only when something is absent.
ensure_packages() {
  local pkg
  local -a missing=()
  for pkg in "$@"; do
    if ! dpkg -s "${pkg}" >/dev/null 2>&1; then
      missing+=("${pkg}")
    fi
  done
  if [[ ${#missing[@]} -eq 0 ]]; then
    return 0
  fi
  apt-get update
  DEBIAN_FRONTEND=noninteractive apt-get install -y "${missing[@]}"
}

# detect_arch maps uname -m to the project arch name (arm64 or amd64).
detect_arch() {
  case "$(uname -m)" in
    aarch64) printf 'arm64' ;;
    x86_64) printf 'amd64' ;;
    *) die "unsupported machine architecture: $(uname -m)" ;;
  esac
}

# preflight verifies root, architecture (arm64 unless ALLOW_NON_ARM64=1), Ubuntu release, and required tooling.
preflight() {
  if [[ "${EUID}" -ne 0 ]]; then
    die "must run as root inside the container (no sudo)"
  fi
  local arch os_id os_ver
  arch="$(detect_arch)"
  if [[ "${arch}" != "arm64" && "${ALLOW_NON_ARM64}" != "1" ]]; then
    die "container arch is ${arch}; this target is arm64 only (ALLOW_NON_ARM64=1 overrides)"
  fi
  # shellcheck source=/etc/os-release
  os_id="$(. /etc/os-release && printf '%s' "${ID:-}")"
  # shellcheck source=/etc/os-release
  os_ver="$(. /etc/os-release && printf '%s' "${VERSION_ID:-}")"
  if [[ "${os_id}" != "ubuntu" ]]; then
    die "guest OS is '${os_id}'; expected ubuntu"
  fi
  if [[ "${os_ver}" != "${GUEST_UBUNTU_VERSION}" && "${ALLOW_UNSUPPORTED_OS}" != "1" ]]; then
    die "Ubuntu ${os_ver} found; expected ${GUEST_UBUNTU_VERSION}"
  fi
  command -v apt-get >/dev/null 2>&1 || die "apt-get not found"
  command -v systemctl >/dev/null 2>&1 || die "systemctl not found; systemd is required"
  log "preflight ok: root, arch=${arch}, ubuntu ${os_ver}"
  if [[ -n "${WORKER_NAME:-}" ]]; then
    [[ "${WORKER_NAME}" =~ ^[a-z0-9]([-a-z0-9]*[a-z0-9])?$ ]] || die "WORKER_NAME '${WORKER_NAME}' is not lowercase RFC 1123"
    log "node name that will register in the cluster: ${WORKER_NAME} (confirm it is unique before join)"
  fi
}

# set_hostname sets the container hostname to WORKER_NAME (the Kubernetes node name), only when it differs.
set_hostname() {
  require_var WORKER_NAME
  [[ "${WORKER_NAME}" =~ ^[a-z0-9]([-a-z0-9]*[a-z0-9])?$ ]] || die "WORKER_NAME '${WORKER_NAME}' is not lowercase RFC 1123"
  if [[ "$(hostname)" == "${WORKER_NAME}" ]]; then
    log "hostname already ${WORKER_NAME}"
    return 0
  fi
  hostnamectl set-hostname "${WORKER_NAME}"
  log "hostname set to ${WORKER_NAME}"
}

# configure_apt_mirror points the Ubuntu deb822 source at UBUNTU_APT_URI for airgap builds; no-op when unset.
configure_apt_mirror() {
  if [[ -z "${UBUNTU_APT_URI:-}" ]]; then
    log "UBUNTU_APT_URI unset; using image apt sources"
    return 0
  fi
  local src=/etc/apt/sources.list.d/ubuntu.sources rendered
  [[ -f "${src}" ]] || die "${src} not found; cannot apply UBUNTU_APT_URI"
  new_tmp rendered
  sed -e "s|^URIs:.*|URIs: ${UBUNTU_APT_URI}|" "${src}" > "${rendered}"
  write_file_if_changed "${src}" 0644 < "${rendered}"
  if [[ "${FILE_CHANGED}" == "1" ]]; then
    apt-get update
  fi
}

# containerd_cgroup_count prints "<examined> <under_runc>": SystemdCgroup lines seen, and lines = true under the runc options table.
containerd_cgroup_count() {
  awk '
    /^[[:space:]]*\[/ { sect = $0 }
    /SystemdCgroup/ {
      examined++
      if ($0 ~ /SystemdCgroup[[:space:]]*=[[:space:]]*true/ && sect ~ /runtimes\.runc\.options\]/) runc++
    }
    END { printf "%d %d", examined + 0, runc + 0 }
  ' "$1"
}

# assert_systemd_cgroup fails loudly unless exactly one SystemdCgroup = true sits under the runc options table.
assert_systemd_cgroup() {
  local examined runc
  [[ -f "$1" ]] || die "containerd config $1 missing"
  read -r examined runc <<< "$(containerd_cgroup_count "$1")"
  log "containerd config: examined ${examined} SystemdCgroup line(s); ${runc} set true under runc options (required: 1)"
  [[ "${runc}" == "1" ]] || die "SystemdCgroup = true is not present exactly once under runc options in $1"
}

# install_containerd installs containerd (pinned and held when CONTAINERD_VERSION is set), enforces SystemdCgroup=true, and restarts only after the assertion passes.
install_containerd() {
  local current="" changed=0 rendered examined runc
  if dpkg -s containerd >/dev/null 2>&1; then
    # shellcheck disable=SC2016
    current="$(dpkg-query -W -f='${Version}' containerd)"
  fi
  if [[ -n "${CONTAINERD_VERSION:-}" ]]; then
    if [[ "${current}" != "${CONTAINERD_VERSION}" ]]; then
      apt-get update
      DEBIAN_FRONTEND=noninteractive apt-get install -y --allow-downgrades "containerd=${CONTAINERD_VERSION}"
    fi
    apt-mark hold containerd >/dev/null
  else
    ensure_packages containerd
  fi
  if [[ -f "${CONTAINERD_CFG}" ]]; then
    read -r examined runc <<< "$(containerd_cgroup_count "${CONTAINERD_CFG}")"
  else
    runc=0
  fi
  if [[ "${runc}" != "1" ]]; then
    new_tmp rendered
    containerd config default | sed -e 's/SystemdCgroup = false/SystemdCgroup = true/' > "${rendered}"
    write_file_if_changed "${CONTAINERD_CFG}" 0644 < "${rendered}"
    changed="${FILE_CHANGED}"
  else
    log "containerd config already has SystemdCgroup = true under runc options"
  fi
  assert_systemd_cgroup "${CONTAINERD_CFG}"
  systemctl enable containerd >/dev/null
  if [[ "${changed}" == "1" ]] || ! systemctl is-active --quiet containerd; then
    systemctl restart containerd
  fi
}

# node_sysctl persists ip_forward=1 for pod networking and applies it immediately.
node_sysctl() {
  printf 'net.ipv4.ip_forward = 1\n' | write_file_if_changed /etc/sysctl.d/k8s.conf 0644
  sysctl -p /etc/sysctl.d/k8s.conf >/dev/null
}

# kmsg_fix installs a oneshot unit linking /dev/kmsg to /dev/console, ordered before kubelet, because LXD containers lack /dev/kmsg.
kmsg_fix() {
  write_file_if_changed /etc/systemd/system/kmsg-console-link.service 0644 <<'UNIT'
[Unit]
Description=Link /dev/kmsg to /dev/console for kubelet in LXD
Before=kubelet.service

[Service]
Type=oneshot
ExecStart=/bin/ln -sf /dev/console /dev/kmsg
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
UNIT
  if [[ "${FILE_CHANGED}" == "1" ]]; then
    systemctl daemon-reload
  fi
  systemctl enable --now kmsg-console-link.service
}

# bpffs mounts /sys/fs/bpf when absent and persists that mount in /etc/fstab exactly once.
bpffs() {
  mkdir -p /sys/fs/bpf
  if ! mountpoint -q /sys/fs/bpf; then
    mount -t bpf bpf /sys/fs/bpf
  fi
  if ! grep -qE '^[^#]*[[:space:]]/sys/fs/bpf[[:space:]]' /etc/fstab; then
    printf '%s\n' 'bpf /sys/fs/bpf bpf defaults 0 0' >> /etc/fstab
  fi
}

# install_kube adds the Kubernetes apt repo signed by a key whose fingerprint must match K8S_APT_KEY_FPR, then installs and holds the pinned version.
install_kube() {
  require_var K8S_APT_KEY_FPR
  ensure_packages ca-certificates curl gnupg
  local armored gpgbin fpr want listing have_all=1 p
  new_tmp armored
  new_tmp gpgbin
  new_tmp listing
  if [[ -n "${K8S_APT_KEY_FILE:-}" ]]; then
    [[ -f "${K8S_APT_KEY_FILE}" ]] || die "K8S_APT_KEY_FILE not found: ${K8S_APT_KEY_FILE}"
    cp "${K8S_APT_KEY_FILE}" "${armored}"
  else
    curl -fsSL --tlsv1.2 -o "${armored}" "${K8S_APT_KEY_URL}"
  fi
  fpr="$(gpg --batch --show-keys --with-colons "${armored}" | awk -F: '/^fpr:/ { if (f == "") f = $10 } END { print f }')"
  [[ -n "${fpr}" ]] || die "no fingerprint found in repo key"
  want="$(printf '%s' "${K8S_APT_KEY_FPR}" | tr -d ' ' | tr 'a-f' 'A-F')"
  [[ "${fpr}" == "${want}" ]] || die "repo key fingerprint mismatch (got ${fpr}, expected ${want}); refusing to trust"
  gpg --batch --yes --dearmor -o "${gpgbin}" "${armored}"
  write_file_if_changed "${KEYRING_PATH}" 0644 < "${gpgbin}"
  local keyring_changed="${FILE_CHANGED}"
  printf 'deb [signed-by=%s] %s /\n' "${KEYRING_PATH}" "${K8S_APT_BASE}" > "${listing}"
  write_file_if_changed "${KUBE_LIST}" 0644 < "${listing}"
  local list_changed="${FILE_CHANGED}"
  for p in kubelet kubeadm kubectl; do
    if ! { dpkg -s "${p}" >/dev/null 2>&1 && [[ "$(dpkg-query -W -f='${Version}' "${p}")" == "${K8S_VERSION}-"* ]]; }; then
      have_all=0
    fi
  done
  if [[ "${have_all}" != "1" || "${keyring_changed}" == "1" || "${list_changed}" == "1" ]]; then
    apt-get update
  fi
  if [[ "${have_all}" != "1" ]]; then
    DEBIAN_FRONTEND=noninteractive apt-get install -y "kubelet=${K8S_VERSION}-*" "kubeadm=${K8S_VERSION}-*" "kubectl=${K8S_VERSION}-*"
  fi
  if command -v snap >/dev/null 2>&1; then
    for p in kubelet kubeadm kubectl; do
      if snap list "${p}" >/dev/null 2>&1; then
        die "snap package ${p} is present; remove it. Only apt packages are supported"
      fi
    done
  fi
  apt-mark hold kubelet kubeadm kubectl >/dev/null
  log "kubelet/kubeadm/kubectl ${K8S_VERSION} installed and held"
}

# disable_ssh stops, disables and masks ssh.service and ssh.socket when they exist; access is via lxc exec only.
disable_ssh() {
  local unit
  for unit in ssh.service ssh.socket; do
    if systemctl cat "${unit}" >/dev/null 2>&1; then
      if systemctl is-active --quiet "${unit}" || [[ "$(systemctl is-enabled "${unit}" 2>/dev/null)" != "masked" ]]; then
        systemctl disable "${unit}" >/dev/null
        systemctl mask --now "${unit}" >/dev/null
        log "${unit} stopped and masked"
      else
        log "${unit} already masked"
      fi
    else
      log "${unit} not present; nothing to disable"
    fi
  done
}

# remove_join_env scrubs JOIN_ENV_FILE inside the container once it is no longer needed; fails loudly if it is still present afterwards.
remove_join_env() {
  if [[ -z "${JOIN_ENV_FILE:-}" || ! -e "${JOIN_ENV_FILE}" ]]; then
    return 0
  fi
  scrub_file "${JOIN_ENV_FILE}"
  [[ ! -e "${JOIN_ENV_FILE}" ]] || die "JOIN_ENV_FILE still present after scrub: ${JOIN_ENV_FILE}"
  log "removed JOIN_ENV_FILE ${JOIN_ENV_FILE}"
}

# join_cluster runs kubeadm join from a runtime-built JoinConfiguration in a private temp file; skipped when already joined.
# Credentials come from JOIN_ENV_FILE, loaded at startup; the env file is removed on success and kept on failure for retry.
join_cluster() {
  if [[ -f /etc/kubernetes/kubelet.conf ]]; then
    log "already joined (kubelet.conf present); skipping"
    remove_join_env
    return 0
  fi
  require_var JOIN_ENV_FILE
  require_var JOIN_ENDPOINT
  require_var JOIN_TOKEN
  require_var CA_CERT_HASH
  local cfg rc=0
  new_tmp cfg
  cat > "${cfg}" <<EOF
apiVersion: kubeadm.k8s.io/v1beta4
kind: JoinConfiguration
discovery:
  bootstrapToken:
    token: ${JOIN_TOKEN}
    apiServerEndpoint: ${JOIN_ENDPOINT}
    caCertHashes:
      - ${CA_CERT_HASH}
nodeRegistration:
  criSocket: unix:///run/containerd/containerd.sock
  ignorePreflightErrors:
    - SystemVerification
EOF
  unset JOIN_TOKEN
  kubeadm join --config "${cfg}" || rc=$?
  scrub_file "${cfg}"
  if [[ "${rc}" -ne 0 ]]; then
    log "JOIN_ENV_FILE ${JOIN_ENV_FILE} was kept for retry; delete it once it is no longer needed"
    die "kubeadm join failed (exit ${rc})"
  fi
  remove_join_env
  log "joined cluster as ${WORKER_NAME:-$(hostname)}"
}

# main dispatches the requested subcommand; "all" runs every step in dependency order.
main() {
  local cmd="${1:-all}"
  if [[ "${cmd}" == "help" || "${cmd}" == "-h" || "${cmd}" == "--help" ]]; then
    usage
    exit 0
  fi
  trap cleanup EXIT
  preflight
  case "${cmd}" in
    all)
      set_hostname
      configure_apt_mirror
      install_containerd
      node_sysctl
      kmsg_fix
      bpffs
      install_kube
      disable_ssh
      join_cluster
      ;;
    set_hostname|configure_apt_mirror|install_containerd|node_sysctl|kmsg_fix|bpffs|install_kube|disable_ssh|join_cluster)
      "${cmd}"
      ;;
    preflight) ;;
    *) usage >&2; die "unknown subcommand: ${cmd}" ;;
  esac
  log "done: ${cmd}"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi

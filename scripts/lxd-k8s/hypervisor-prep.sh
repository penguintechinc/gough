#!/usr/bin/env bash
# hypervisor-prep.sh - prepares an arm64 or amd64 Ubuntu 26.04 hypervisor to run one LXD instance as a kubeadm worker.
# Run as root on the hypervisor; this script never invokes sudo. Subcommands: all (default), preflight, detect,
# kernel, netplan, lxd, profile, worker, ssh, help. --dry-run (or DRY_RUN=1) logs actions without running them.
#
# Worker flow: the k8s-worker biome in BIOMES_DIR is the single source of truth for the guest; this script only renders it.
#   render (_lib/render.py) -> LXD profile -> lxc init (STOPPED) -> config + cloud-init -> lxc file push secrets.env
#   -> lxc start -> poll biome-report.done via lxc file pull -> confirm secrets.env gone via lxc file pull
# The only lxc file push is secrets.env (mode 0600). The biome never sees the host. lxc exec is never run on the
# success path. SUPERSEDED: k8s-worker-bootstrap.sh (pushed and exec'd by older revisions) stays on disk untouched
# and is no longer called.
#
# Network roles: MGMT_IFACE/MGMT_IP_CIDR is management only (no gateway). BR0_IP_CIDR and WORKER_IP_CIDR are on
# the application network, bond0 (or one NIC) -> br0 -> LXD worker NIC. SSH is reachable only via management.
# NIC roles are auto-detected when unset (see detect_network); detection only sets variables and applies nothing.
#
# TOBOGGANING=1 (networking/SD-WAN/SD-LAN): interfaces are pre-created by the tobogganing host agent, possibly virtual.
#   detect_network and ensure_host_netplan are SKIPPED entirely (no netplan written, generated or applied). MGMT_IFACE
#   and APP_IFACE are required and must exist under SYSFS_NET with operstate up. The worker NIC attaches with
#   parent=APP_IFACE. SSH restriction still binds to MGMT_IP_CIDR on MGMT_IFACE. BR0_IP_CIDR is not needed (validated and
#   ignored when given); WORKER_IP_CIDR defines the application subnet that GATEWAY must sit in.
#
# Environment variables (default in brackets; REQUIRED = no default, the step fails loudly without it):
#   ALLOW_UNSUPPORTED_ARCH [0]              1 = allow a host that is neither arm64 nor amd64 (both are supported). The old
#                                           name ALLOW_NON_ARM64 was renamed to this: setting the old name fails loudly.
#   ALLOW_UNSUPPORTED_OS   [0]              1 = allow an Ubuntu release other than HOST_UBUNTU_VERSION
#   HOST_UBUNTU_VERSION    [26.04]          expected Ubuntu VERSION_ID
#   DRY_RUN                [0]              1 = log mutating actions instead of running them; no lxc call at all
#   LXD_SNAP_CHANNEL       [latest/stable]  online snap channel (mutable: pin a track for production)
#   LXD_SNAP_FILE          [unset]          airgap: local LXD .snap file (requires LXD_SNAP_ASSERT)
#   LXD_SNAP_ASSERT        [unset]          airgap: matching .assert file
#   STORAGE_POOL           [default]        LXD storage pool for the root disk (the only pool the biome permits)
#   STORAGE_DRIVER         [dir]            LXD storage driver for that pool
#   KERNEL_MODULES         [ip_tables ip6_tables nf_nat overlay br_netfilter]  space-separated
#   BOND_NAME              [bond0]          LACP bond (802.3ad, lacp-rate slow, layer2 hash); no addresses
#   BRIDGE_NAME            [br0]            bridge over bond0; LXD worker NIC attaches here (TOBOGGANING=0)
#   BOND_MEMBER_1          [auto]           first NIC of the application bond (set both or neither; see detection)
#   BOND_MEMBER_2          [auto]           second NIC of the application bond
#   UPLINK_IFACE           [auto]           single application NIC (UPLINK_MODE=single); mutually exclusive with BOND_MEMBER_*
#   MGMT_IFACE             [auto]           management NIC (slowest live physical NIC; REQUIRED under TOBOGGANING=1)
#   UPLINK_MODE            [derived]        bond | single; set by detection or TOBOGGANING, never by hand
#   BR0_IP_CIDR            REQUIRED (TOBOGGANING=0)  host address on br0 (application net), e.g. 192.168.8.x/24
#   WORKER_IP_CIDR         REQUIRED         worker instance address (application net), e.g. 192.168.8.x/24
#   MGMT_IP_CIDR           REQUIRED         management address (management net), e.g. 192.168.4.x/24
#   SYSFS_NET              [/sys/class/net]  interface tree scanned by detection and TOBOGGANING checks (tests use a fixture)
#   PROC_BONDING           [/proc/net/bonding]  bond mode and slave lists read by detection (tests override)
#   GATEWAY                [192.168.8.1]    ASSUMED default gateway (warned; REQUIRED under TOBOGGANING=1)
#   DNS                    [192.168.8.1]    ASSUMED nameserver (warned; REQUIRED under TOBOGGANING=1)
#   NETPLAN_OUT            [/etc/netplan/60-gough-hypervisor.yaml]  authoritative netplan file written by this script
#   FORCE_NETPLAN_REPLACE  [0]              1 = back up and remove existing netplan files that define our interfaces
#   APPLY_NETPLAN          [0]              1 = run netplan apply (can drop connectivity; otherwise reboot or apply)
#   APPLY_SSH_RESTRICT     [0]              1 = apply SSH restriction (sshd, socket, nftables); otherwise files only
#   TOBOGGANING            [0]              0 = physical NIC detection and br0 netplan; 1 = pre-created pseudo interfaces.
#                                           Any other value fails. Allowed in NODE_ENV_FILE.
#   APP_IFACE              REQUIRED (TOBOGGANING=1)  application pseudo interface; the worker NIC parent. May be virtual.
#   APP_NIC_TYPE           [bridged]        worker NIC type: bridged | macvlan | ipvlan. Non-bridged requires TOBOGGANING=1.
#                                           The biome declares nictype bridged; a non-bridged value overrides the rendered
#                                           eth0 nictype on the host, with a warning (biome gap, see report).
#   LAB_PRIVILEGED         [0]              1 = privileged biome profile (allow_privileged=true): container escape to host
#                                           root is possible. Requires I_ACCEPT_HOST_ROOT_RISK=1.
#   I_ACCEPT_HOST_ROOT_RISK [unset]         must be 1 when LAB_PRIVILEGED=1; passed to the biome as accept_host_root_risk
#   INSTANCE_TYPE          [container]      container only. vm is refused at preflight: delivering secrets.env to a stopped
#                                           VM is unverified (needs the guest agent).
#   IMAGE_REMOTE           [ubuntu]         LXD image remote (airgap: a local remote). The ubuntu: remote serves cloud-init
#                                           images under bare release aliases (26.04); the images: remote uses ubuntu/<ver>.
#   IMAGE_ALIAS            [26.04 for the ubuntu remote, else biome default ubuntu/26.04]  image alias on IMAGE_REMOTE.
#                                           Never combine an ubuntu/ alias with the ubuntu remote (refused). The image is
#                                           checked read-only (lxc image info) before the profile is created; skipped under DRY_RUN.
#   NODE_ID                [<short hostname minus site prefix>]  e.g. dal2a-k8s3 -> k8s3
#   WORKER_NAME            [${NODE_ID}-worker]  LXD instance name, guest hostname and k8s node name (biome hostname and
#                          instance_name). Lowercase RFC 1123; unique cluster-wide.
#   BIOMES_DIR             [<script dir>/../../biomes]  must contain _lib/render.py and k8s-worker/biome.json
#   WAIT_SECONDS           [900]            max wait for the biome completion marker
#   POLL_INTERVAL          [10]             seconds between marker polls
#   UBUNTU_APT_URI         REQUIRED (worker)  biome apt_mirror: Ubuntu archive base URL
#   K8S_APT_BASE           REQUIRED (worker)  biome k8s_apt_repo: Kubernetes apt repo base (trailing slash added)
#   K8S_APT_KEY_URL        [${K8S_APT_BASE}Release.key]  biome k8s_apt_key_url
#   K8S_APT_KEY_SHA256     REQUIRED (worker)  biome k8s_apt_key_sha256: sha256 of the armored key file
#   REGISTRY_MIRROR        REQUIRED (worker)  biome registry_mirror: mirror serving upstream repository paths
#   CONTAINERD_VERSION     REQUIRED (worker)  biome containerd_package_version: exact apt version
#   K8S_VERSION            [biome default]    biome k8s_version (must match the controller)
#   K8S_PACKAGE_VERSION    [<K8S_VERSION>-1.1] biome k8s_package_version (apt version of kubelet/kubeadm/kubectl). Derived from
#                                               K8S_VERSION assuming the pkgs.k8s.io revision suffix -1.1; set it to override.
#   MIRRORED_REGISTRIES    [biome default]    biome mirrored_registries: comma-separated upstream registries routed to the mirror
#   ALLOW_REGISTRY_FALLBACK [unset]           0|1; biome allow_registry_fallback (1 lets airgapped nodes dial public registries)
#   INTERNAL_CA_URL        [unset]            biome internal_ca_url: PEM CA for a private mirror (requires INTERNAL_CA_SHA256)
#   INTERNAL_CA_SHA256     [unset]            biome internal_ca_sha256: sha256 of that CA file
#   ALLOW_INSECURE_MIRRORS [unset]            0|1; biome allow_insecure_mirrors: permits plain http:// mirrors (airgapped labs).
#                                           Risk switch: command line only, never in NODE_ENV_FILE; warns loudly when 1.
#   NODE_ENV_FILE          REQUIRED for worker   host KEY=VALUE env file (Compose-style) holding ALL node-unique config.
#                                               Root-owned regular file, mode 0600 or 0400; no CR/NUL, no `export`, no
#                                               interpolation (values are parsed, never sourced or expanded).
#                                               Allowed keys: BOND_MEMBER_1 BOND_MEMBER_2 UPLINK_IFACE MGMT_IFACE MGMT_IP_CIDR
#                                               BR0_IP_CIDR WORKER_IP_CIDR GATEWAY DNS NODE_ID WORKER_NAME INSTANCE_TYPE STORAGE_POOL
#                                               STORAGE_DRIVER IMAGE_REMOTE IMAGE_ALIAS JOIN_ENDPOINT JOIN_TOKEN CA_CERT_HASH
#                                               K8S_VERSION K8S_APT_BASE K8S_APT_KEY_FPR CONTAINERD_VERSION
#                                               TOBOGGANING APP_IFACE APP_NIC_TYPE K8S_PACKAGE_VERSION MIRRORED_REGISTRIES
#                                               ALLOW_REGISTRY_FALLBACK INTERNAL_CA_URL INTERNAL_CA_SHA256
#                                               Sample (placeholders only):
#                                                 BOND_MEMBER_1=<nic-a>
#                                                 BOND_MEMBER_2=<nic-b>
#                                                 MGMT_IFACE=<mgmt-nic>
#                                                 MGMT_IP_CIDR=192.168.4.x/24
#                                                 BR0_IP_CIDR=192.168.8.x/24
#                                                 WORKER_IP_CIDR=192.168.8.y/24
#                                                 GATEWAY=192.168.8.1
#                                                 DNS=192.168.8.1
#                                                 NODE_ID=k3
#                                                 WORKER_NAME=k3-worker
#                                                 JOIN_ENDPOINT=192.168.8.z:6443
#                                                 JOIN_TOKEN=<token-6>.<token-16>
#                                                 CA_CERT_HASH=sha256:<64-hex>
#                                                 K8S_APT_KEY_FPR=<40-hex>
#   Precedence: a real process environment variable wins over the file, except JOIN_TOKEN, which is file-only.
#   Per-run switches are NOT loadable from the file: LAB_PRIVILEGED, I_ACCEPT_HOST_ROOT_RISK, APPLY_NETPLAN,
#   APPLY_SSH_RESTRICT, FORCE_NETPLAN_REPLACE, ALLOW_UNSUPPORTED_ARCH, ALLOW_UNSUPPORTED_OS, ALLOW_INSECURE_MIRRORS, DRY_RUN. A persisted risk
#   acknowledgement or apply flag would silently carry over to every later run; pass them on the command line each time.
#   The node file never leaves the host. JOIN_TOKEN reaches the renderer on stdin only; it lands only in the
#   instance's secrets.env (0600), which the bootstrap removes after join.

# Example (node-unique values in /root/k3.env, mode 0600):
#   NODE_ENV_FILE=/root/k3.env ./hypervisor-prep.sh --dry-run worker
# Risk and apply switches go on the command line only, never in the file:
#   NODE_ENV_FILE=/root/k3.env APPLY_NETPLAN=1 ./hypervisor-prep.sh all
#
# Privileged lab variant (host-root escape risk; both variables required):
#   ... INSTANCE_TYPE=container LAB_PRIVILEGED=1 I_ACCEPT_HOST_ROOT_RISK=1 ./hypervisor-prep.sh all
# Unprivileged containers are unproven for kubelet/kube-proxy. INSTANCE_TYPE=vm is not supported yet (see INSTANCE_TYPE).
#
# Node naming: the Kubernetes node name equals the guest hostname, set from WORKER_NAME. It must be unique
# cluster-wide. Default WORKER_NAME is <NODE_ID>-worker where NODE_ID is the short hostname minus its site
# prefix (dal2a-k8s3 -> k8s3 -> k8s3-worker). Preflight prints the name that will register.
#
# Controller-side kube-proxy conntrack fix is not done by this script; see the bootstrap header.
set -euo pipefail

# log prints an informational line to stderr so stdout stays free for rendered content.
log() { printf '[hypervisor-prep] %s\n' "$*" >&2; }

# warn prints a non-fatal warning to stderr.
warn() { printf '[hypervisor-prep] WARNING: %s\n' "$*" >&2; }

# die prints an error and exits non-zero; every failure path goes through it so messages stay explicit.
die() { printf '[hypervisor-prep] ERROR: %s\n' "$*" >&2; exit 1; }

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

# validate_iface_name <label> <name> dies unless name is a safe interface name: 1-15 chars of [A-Za-z0-9._-], not "." or "..",
# no leading dash. Names reach the nft heredoc and netplan YAML verbatim, so anything else (newline, space, quote, brace) is refused.
# Dots are allowed on purpose so VLAN sub-interface names such as eno1.100 stay valid. The value is never echoed.
validate_iface_name() {
  local label="$1" name="$2" re='^[A-Za-z0-9._-]{1,15}$'
  [[ "${name}" =~ ${re} ]] || die "${label} is not a valid interface name (1-15 chars of A-Za-z0-9._-; value not printed)"
  [[ "${name}" != "." && "${name}" != ".." ]] || die "${label} must not be . or .."
  [[ "${name}" != -* ]] || die "${label} must not start with a dash"
}

# valid_ipv4 returns 0 when $1 is a strict dotted-quad IPv4 address: four octets 0-255 with no leading zeros and nothing else.
# Leading zeros are refused because some parsers read them as octal.
valid_ipv4() {
  local re='^(0|[1-9][0-9]{0,2})\.(0|[1-9][0-9]{0,2})\.(0|[1-9][0-9]{0,2})\.(0|[1-9][0-9]{0,2})$' o
  local -a oct
  [[ "$1" =~ ${re} ]] || return 1
  IFS=. read -r -a oct <<< "$1"
  for o in "${oct[@]}"; do
    (( o <= 255 )) || return 1
  done
  return 0
}

# valid_cidr4 returns 0 when $1 is a strict IPv4 CIDR: a valid_ipv4 address, a slash, a prefix 0-32 with no leading zero.
valid_cidr4() {
  local re='^[^/]+/(0|[1-9]|[12][0-9]|3[0-2])$'
  [[ "$1" =~ ${re} ]] && valid_ipv4 "${1%/*}"
}

# validate_net_values dies when any set interface name, GATEWAY, DNS or *_IP_CIDR is malformed; unset (empty) values are skipped.
# Called after defaults load and after detection, so every name or address is checked before any use.
validate_net_values() {
  local k v
  for k in BOND_NAME BRIDGE_NAME BOND_MEMBER_1 BOND_MEMBER_2 UPLINK_IFACE MGMT_IFACE APP_IFACE; do
    v="${!k:-}"
    [[ -z "${v}" ]] || validate_iface_name "${k}" "${v}"
  done
  for k in GATEWAY DNS; do
    v="${!k:-}"
    [[ -z "${v}" ]] || valid_ipv4 "${v}" || die "${k} is not a valid IPv4 address (value not printed)"
  done
  for k in MGMT_IP_CIDR BR0_IP_CIDR WORKER_IP_CIDR; do
    v="${!k:-}"
    [[ -z "${v}" ]] || valid_cidr4 "${v}" || die "${k} is not a valid IPv4 CIDR (value not printed)"
  done
}

# check_value <KEY> <VALUE> dies when a validated key fails its format check; other keys pass through.
# Messages name the key and never echo the value, so the bootstrap token cannot leak into logs.
check_value() {
  case "$1" in
    JOIN_ENDPOINT) valid_endpoint "$2" || die "JOIN_ENDPOINT must be host:port (IPv4 or RFC 1123 host, port 1-65535)" ;;
    CA_CERT_HASH) valid_hash "$2" || die "CA_CERT_HASH must be sha256:<64 lowercase hex>" ;;
    JOIN_TOKEN) valid_token "$2" || die "JOIN_TOKEN is not a bootstrap token (format only; value not printed)" ;;
    BOND_MEMBER_1|BOND_MEMBER_2|UPLINK_IFACE|MGMT_IFACE|APP_IFACE) validate_iface_name "$1" "$2" ;;
    GATEWAY|DNS) valid_ipv4 "$2" || die "$1 is not a valid IPv4 address (value not printed)" ;;
    MGMT_IP_CIDR|BR0_IP_CIDR|WORKER_IP_CIDR) valid_cidr4 "$2" || die "$1 is not a valid IPv4 CIDR (value not printed)" ;;
    K8S_VERSION) [[ "$2" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "K8S_VERSION must look like 1.35.9 (digits.digits.digits)" ;;
    K8S_PACKAGE_VERSION) [[ "$2" =~ ^[0-9][A-Za-z0-9.+~:-]*$ ]] || die "K8S_PACKAGE_VERSION is not an apt version (e.g. 1.35.9-1.1)" ;;
    MIRRORED_REGISTRIES) [[ "$2" =~ ^[a-z0-9.-]+(,[a-z0-9.-]+)*$ ]] || die "MIRRORED_REGISTRIES must be comma-separated lowercase registry hosts" ;;
    ALLOW_REGISTRY_FALLBACK) [[ "$2" == 0 || "$2" == 1 ]] || die "ALLOW_REGISTRY_FALLBACK must be 0 or 1" ;;
    INTERNAL_CA_URL) [[ "$2" =~ ^https?://[^[:space:]]+$ ]] || die "INTERNAL_CA_URL must be an http(s) URL (value not printed)" ;;
    INTERNAL_CA_SHA256) [[ "$2" =~ ^[0-9a-f]{64}$ ]] || die "INTERNAL_CA_SHA256 must be 64 lowercase hex characters" ;;
  esac
}

# load_env_file <path> <allowed-key>... parses a Compose-style KEY=VALUE file into the allowed variables; it never sources or executes the file.
# Requires a root-owned regular file, mode 0600 or 0400, free of NUL and CR bytes; rejects per-run safety switches, unlisted or duplicate keys.
# Errors name keys or line numbers, never values. A variable already set in the process environment wins over the file, except JOIN_TOKEN, which is never read from the environment.
load_env_file() {
  local path="$1" line lead key rest value inner after stripped stat_out mode owner size nonnul a fv ok
  local lineno=0 count=0 seen=" " name_re='^[A-Za-z_][A-Za-z0-9_]*$'
  local sq="'" dq='"'
  local switches=" LAB_PRIVILEGED I_ACCEPT_HOST_ROOT_RISK APPLY_NETPLAN APPLY_SSH_RESTRICT FORCE_NETPLAN_REPLACE ALLOW_UNSUPPORTED_ARCH ALLOW_UNSUPPORTED_OS ALLOW_INSECURE_MIRRORS DRY_RUN "
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
    if [[ "${key}" == "ALLOW_NON_ARM64" ]]; then
      die "line ${lineno}: ALLOW_NON_ARM64 was renamed to ALLOW_UNSUPPORTED_ARCH, and per-run switches cannot be set in an env file"
    fi
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
  die "JOIN_TOKEN is set in the environment; it is read only from NODE_ENV_FILE. Unset it and put it in the file"
fi
if [[ -n "${ALLOW_NON_ARM64+x}" ]]; then
  die "ALLOW_NON_ARM64 was renamed to ALLOW_UNSUPPORTED_ARCH (arm64 and amd64 are both supported now); unset it and use the new name only for other architectures"
fi
if [[ -n "${NODE_ENV_FILE:-}" ]]; then
  load_env_file "${NODE_ENV_FILE}" BOND_MEMBER_1 BOND_MEMBER_2 UPLINK_IFACE MGMT_IFACE MGMT_IP_CIDR BR0_IP_CIDR WORKER_IP_CIDR GATEWAY DNS NODE_ID WORKER_NAME INSTANCE_TYPE STORAGE_POOL STORAGE_DRIVER IMAGE_REMOTE IMAGE_ALIAS JOIN_ENDPOINT JOIN_TOKEN CA_CERT_HASH K8S_VERSION K8S_APT_BASE K8S_APT_KEY_FPR CONTAINERD_VERSION TOBOGGANING APP_IFACE APP_NIC_TYPE K8S_PACKAGE_VERSION MIRRORED_REGISTRIES ALLOW_REGISTRY_FALLBACK INTERNAL_CA_URL INTERNAL_CA_SHA256
fi


SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PATH="/snap/bin:${PATH}"
ALLOW_UNSUPPORTED_ARCH="${ALLOW_UNSUPPORTED_ARCH:-0}"
ALLOW_UNSUPPORTED_OS="${ALLOW_UNSUPPORTED_OS:-0}"
HOST_UBUNTU_VERSION="${HOST_UBUNTU_VERSION:-26.04}"
DRY_RUN="${DRY_RUN:-0}"
LXD_SNAP_CHANNEL="${LXD_SNAP_CHANNEL:-latest/stable}"
STORAGE_POOL="${STORAGE_POOL:-default}"
STORAGE_DRIVER="${STORAGE_DRIVER:-dir}"
KERNEL_MODULES="${KERNEL_MODULES:-ip_tables ip6_tables nf_nat overlay br_netfilter}"
BOND_NAME="${BOND_NAME:-bond0}"
BRIDGE_NAME="${BRIDGE_NAME:-br0}"
SYSFS_NET="${SYSFS_NET:-/sys/class/net}"
PROC_BONDING="${PROC_BONDING:-/proc/net/bonding}"
BOND_MEMBER_1="${BOND_MEMBER_1:-}"
BOND_MEMBER_2="${BOND_MEMBER_2:-}"
UPLINK_IFACE="${UPLINK_IFACE:-}"
MGMT_IFACE="${MGMT_IFACE:-}"
UPLINK_MODE="${UPLINK_MODE:-}"
GATEWAY_ASSUMED=0
[[ -n "${GATEWAY:-}" ]] || GATEWAY_ASSUMED=1
GATEWAY="${GATEWAY:-192.168.8.1}"
DNS_ASSUMED=0
[[ -n "${DNS:-}" ]] || DNS_ASSUMED=1
DNS="${DNS:-192.168.8.1}"
NETPLAN_OUT="${NETPLAN_OUT:-/etc/netplan/60-gough-hypervisor.yaml}"
NETPLAN_DIR="/etc/netplan"
NETPLAN_BACKUP_DIR="/var/backups/gough-netplan"
FORCE_NETPLAN_REPLACE="${FORCE_NETPLAN_REPLACE:-0}"
APPLY_NETPLAN="${APPLY_NETPLAN:-0}"
APPLY_SSH_RESTRICT="${APPLY_SSH_RESTRICT:-0}"
LAB_PRIVILEGED="${LAB_PRIVILEGED:-0}"
INSTANCE_TYPE="${INSTANCE_TYPE:-container}"
IMAGE_REMOTE="${IMAGE_REMOTE:-ubuntu}"
# Default alias on the ubuntu: remote (cloud-init images, bare release aliases); matches the biome's ubuntu/26.04 release.
DEFAULT_UBUNTU_IMAGE_ALIAS="26.04"
IMAGE_ALIAS="${IMAGE_ALIAS:-}"
TOBOGGANING="${TOBOGGANING:-0}"
APP_IFACE="${APP_IFACE:-}"
APP_NIC_TYPE="${APP_NIC_TYPE:-bridged}"
validate_net_values
BIOMES_DIR="${BIOMES_DIR:-${SCRIPT_DIR}/../../biomes}"
WAIT_SECONDS="${WAIT_SECONDS:-900}"
POLL_INTERVAL="${POLL_INTERVAL:-10}"
UBUNTU_APT_URI="${UBUNTU_APT_URI:-}"
K8S_APT_BASE="${K8S_APT_BASE:-}"
K8S_APT_KEY_URL="${K8S_APT_KEY_URL:-}"
K8S_APT_KEY_SHA256="${K8S_APT_KEY_SHA256:-}"
K8S_APT_KEY_FPR="${K8S_APT_KEY_FPR:-}"
REGISTRY_MIRROR="${REGISTRY_MIRROR:-}"
CONTAINERD_VERSION="${CONTAINERD_VERSION:-}"
K8S_VERSION="${K8S_VERSION:-}"
K8S_PACKAGE_VERSION="${K8S_PACKAGE_VERSION:-}"
MIRRORED_REGISTRIES="${MIRRORED_REGISTRIES:-}"
ALLOW_REGISTRY_FALLBACK="${ALLOW_REGISTRY_FALLBACK:-}"
INTERNAL_CA_URL="${INTERNAL_CA_URL:-}"
INTERNAL_CA_SHA256="${INTERNAL_CA_SHA256:-}"
ALLOW_INSECURE_MIRRORS="${ALLOW_INSECURE_MIRRORS:-}"
_short_host="$(hostname -s)"
NODE_ID="${NODE_ID:-${_short_host##*-}}"
WORKER_NAME="${WORKER_NAME:-${NODE_ID}-worker}"
NFT_FILE="/etc/nftables.d/60-ssh-mgmt-only.nft"
SSHD_DROPIN="/etc/ssh/sshd_config.d/60-mgmt-only.conf"
SOCKET_OVERRIDE="/etc/systemd/system/ssh.socket.d/60-mgmt-only.conf"
SERVICE_DROPIN="/etc/systemd/system/ssh.service.d/60-mgmt-only.conf"
TMP_FILES=()
TMP_DIRS=()
RENDER_DIR=""
ARCH=""


# usage prints the subcommand list.
usage() {
  cat <<'EOF'
Usage: hypervisor-prep.sh [--dry-run] [all|preflight|detect|kernel|netplan|lxd|profile|worker|ssh|help]
  detect: read-only NIC role detection (MGMT_IFACE, UPLINK_MODE, UPLINK_IFACE, BOND_MEMBER_1/2); changes nothing. Skipped under TOBOGGANING=1.
  profile: render the k8s-worker biome and apply its LXD profile only (no instance).
  worker: render the biome, create the STOPPED instance, push secrets.env, start, wait for biome-report.done (WAIT_SECONDS).
  profile/worker/all need NODE_ENV_FILE. BIOMES_DIR points at the biomes directory (default <script dir>/../../biomes).
EOF
}

# require_var checks that one named environment variable is set and non-empty.
require_var() {
  local name="$1"
  if [[ -z "${!name:-}" ]]; then
    die "required environment variable ${name} is not set"
  fi
}

# run executes a command, or only logs it when DRY_RUN=1. Never pass secrets as arguments.
run() {
  if [[ "${DRY_RUN}" == "1" ]]; then
    log "[dry-run] $*"
    return 0
  fi
  "$@"
}

# run_from_file runs a command with stdin redirected from a file, or logs it under DRY_RUN=1.
run_from_file() {
  local src="$1"
  shift
  if [[ "${DRY_RUN}" == "1" ]]; then
    log "[dry-run] $* < ${src}"
    return 0
  fi
  "$@" < "${src}"
}

# new_tmp stores a fresh private temp file path in the named variable and registers it for removal.
new_tmp() {
  local t
  t="$(mktemp)"
  TMP_FILES+=("${t}")
  printf -v "$1" '%s' "${t}"
}

# new_tmp_dir stores a fresh private (0700) temp directory path in the named variable and registers it for removal on EXIT.
new_tmp_dir() {
  local d
  d="$(mktemp -d)"
  TMP_DIRS+=("${d}")
  printf -v "$1" '%s' "${d}"
}

# cleanup removes every temp file and temp directory registered by new_tmp and new_tmp_dir; runs on EXIT.
# Removing the render directory also removes the rendered secrets.env. Guards keep it safe when TMP_DIRS is undeclared.
cleanup() {
  if [[ ${#TMP_FILES[@]} -gt 0 ]]; then
    rm -f "${TMP_FILES[@]}"
  fi
  if [[ -n "${TMP_DIRS+x}" && ${#TMP_DIRS[@]} -gt 0 ]]; then
    rm -rf "${TMP_DIRS[@]}"
  fi
}

# write_file_if_changed <path> <mode> reads stdin and installs it only when content differs.
# Logs whether the file was written (or would be, under dry-run) or was already identical.
write_file_if_changed() {
  local path="$1" mode="$2" tmp
  new_tmp tmp
  cat > "${tmp}"
  if [[ -f "${path}" ]] && cmp -s "${tmp}" "${path}"; then
    log "unchanged: ${path}"
    return 0
  fi
  if [[ "${DRY_RUN}" == "1" ]]; then
    log "[dry-run] write ${path} (mode ${mode})"
    return 0
  fi
  install -D -m "${mode}" "${tmp}" "${path}"
}

# ensure_line appends an exact line to a file unless it is already present.
ensure_line() {
  local file="$1" line="$2"
  if [[ -f "${file}" ]] && grep -qxF -- "${line}" "${file}"; then
    return 0
  fi
  if [[ "${DRY_RUN}" == "1" ]]; then
    log "[dry-run] append to ${file}: ${line}"
    return 0
  fi
  printf '%s\n' "${line}" >> "${file}"
}

# detect_arch maps uname -m to the project arch name (arm64 or amd64); any other machine type is printed as reported.
detect_arch() {
  case "$(uname -m)" in
    aarch64) printf 'arm64' ;;
    x86_64) printf 'amd64' ;;
    *) uname -m ;;
  esac
}

# check_arch sets ARCH and dies unless it is arm64 or amd64; ALLOW_UNSUPPORTED_ARCH=1 allows any other architecture with a warning.
# Split from host_checks so the architecture policy is testable without root.
check_arch() {
  ARCH="$(detect_arch)"
  case "${ARCH}" in
    arm64|amd64) ;;
    *)
      if [[ "${ALLOW_UNSUPPORTED_ARCH}" != "1" ]]; then
        die "host arch is ${ARCH}; supported architectures are arm64 and amd64 (ALLOW_UNSUPPORTED_ARCH=1 overrides for lab use)"
      fi
      warn "host arch ${ARCH} is unsupported; continuing because ALLOW_UNSUPPORTED_ARCH=1"
      ;;
  esac
}

# host_checks verifies root, architecture (arm64 or amd64 unless ALLOW_UNSUPPORTED_ARCH=1) and Ubuntu release; read-only.
# Split from preflight so the read-only detect subcommand can run host checks without worker config checks.
host_checks() {
  if [[ "${EUID}" -ne 0 ]]; then
    die "must run as root; this script never invokes sudo. Re-run from a root shell."
  fi
  check_arch
  local os_id os_ver
  # shellcheck source=/etc/os-release
  os_id="$(. /etc/os-release && printf '%s' "${ID:-}")"
  # shellcheck source=/etc/os-release
  os_ver="$(. /etc/os-release && printf '%s' "${VERSION_ID:-}")"
  if [[ "${os_id}" != "ubuntu" ]]; then
    die "host OS is '${os_id}'; expected ubuntu"
  fi
  if [[ "${os_ver}" != "${HOST_UBUNTU_VERSION}" && "${ALLOW_UNSUPPORTED_OS}" != "1" ]]; then
    die "Ubuntu ${os_ver} found; expected ${HOST_UBUNTU_VERSION} (ALLOW_UNSUPPORTED_OS=1 overrides)"
  fi
  log "host checks ok: root, arch=${ARCH}, ubuntu ${os_ver}"
}

# validate_modes fails unless TOBOGGANING is exactly 0 or 1 and APP_NIC_TYPE is bridged, macvlan or ipvlan; non-bridged needs TOBOGGANING=1.
validate_modes() {
  case "${TOBOGGANING}" in
    0|1) ;;
    *) die "TOBOGGANING must be exactly 0 or 1 (got '${TOBOGGANING}')" ;;
  esac
  case "${APP_NIC_TYPE}" in
    bridged|macvlan|ipvlan) ;;
    *) die "APP_NIC_TYPE must be bridged, macvlan or ipvlan (got '${APP_NIC_TYPE}')" ;;
  esac
  if [[ "${TOBOGGANING}" == "0" && "${APP_NIC_TYPE}" != "bridged" ]]; then
    die "APP_NIC_TYPE=${APP_NIC_TYPE} requires TOBOGGANING=1; with TOBOGGANING=0 the worker NIC attaches to ${BRIDGE_NAME} (bridged)"
  fi
}

# is_toboggan returns success when TOBOGGANING=1 (interfaces pre-created by the tobogganing host agent).
is_toboggan() {
  [[ "${TOBOGGANING}" == "1" ]]
}

# toboggan_inputs requires MGMT_IFACE and APP_IFACE, checks both exist under SYSFS_NET with operstate up, and sets the uplink to APP_IFACE.
# Virtual interfaces are allowed: physical-NIC rules apply only to detection, which TOBOGGANING=1 skips entirely.
toboggan_inputs() {
  local name state
  [[ -n "${MGMT_IFACE:-}" ]] || die "TOBOGGANING=1 requires MGMT_IFACE (management pseudo interface); set it in NODE_ENV_FILE or the environment"
  [[ -n "${APP_IFACE:-}" ]] || die "TOBOGGANING=1 requires APP_IFACE (application pseudo interface); set it in NODE_ENV_FILE or the environment"
  [[ "${MGMT_IFACE}" != "${APP_IFACE}" ]] || die "MGMT_IFACE and APP_IFACE must differ (both ${MGMT_IFACE})"
  [[ "${GATEWAY_ASSUMED}" == "0" ]] || die "TOBOGGANING=1 requires GATEWAY to be set (no default)"
  [[ "${DNS_ASSUMED}" == "0" ]] || die "TOBOGGANING=1 requires DNS to be set (no default)"
  for name in "${MGMT_IFACE}" "${APP_IFACE}"; do
    [[ -e "${SYSFS_NET}/${name}" ]] || die "interface ${name} not found under ${SYSFS_NET}"
    state="$(read_attr "${SYSFS_NET}/${name}/operstate")"
    [[ "${state}" == "up" ]] || die "interface ${name} operstate is '${state:-unknown}', expected up"
  done
  UPLINK_MODE="single"
  UPLINK_IFACE="${APP_IFACE}"
  BOND_MEMBER_1=""
  BOND_MEMBER_2=""
  DETECT_MGMT_WHY="TOBOGGANING pseudo interface (MGMT_IFACE)"
  DETECT_UPLINK_WHY="TOBOGGANING pseudo interface (APP_IFACE)"
  log "TOBOGGANING=1: MGMT_IFACE=${MGMT_IFACE} APP_IFACE=${APP_IFACE} (both up; virtual interfaces allowed)"
}

# preflight runs host checks, mode and config checks before any change. Under TOBOGGANING=1 it validates the pseudo interfaces;
# under TOBOGGANING=0 detection runs here too, so an ambiguous host fails before any netplan is written.
preflight() {
  host_checks
  validate_worker_name
  validate_modes
  case "${INSTANCE_TYPE}" in
    container) ;;
    vm) die "INSTANCE_TYPE=vm is not supported yet: delivering secrets.env to a stopped VM is unverified; use INSTANCE_TYPE=container" ;;
    *) die "INSTANCE_TYPE must be container (got '${INSTANCE_TYPE}'); vm is not supported yet" ;;
  esac
  if is_toboggan; then
    log "TOBOGGANING=1: detect_network and netplan SKIPPED; host NICs are pre-created (no netplan is written or applied)"
    toboggan_inputs
  else
    detect_network
  fi
  warn "node name that will register in the cluster: ${WORKER_NAME} (confirm no existing node has this name before join)"
}

# validate_worker_name fails unless WORKER_NAME is a lowercase RFC 1123 label (also used as the k8s node name).
validate_worker_name() {
  [[ "${WORKER_NAME}" =~ ^[a-z0-9]([-a-z0-9]*[a-z0-9])?$ ]] \
    || die "WORKER_NAME '${WORKER_NAME}' is not lowercase RFC 1123 (^[a-z0-9]([-a-z0-9]*[a-z0-9])?$); set WORKER_NAME or NODE_ID"
}

# read_attr <file> prints the file content without its trailing newline, or nothing when the file is absent or unreadable.
# Sysfs and proc attributes are single values; a missing file is an expected state, so it reads as empty.
read_attr() {
  local v
  if v="$(cat -- "$1" 2>/dev/null)"; then
    printf '%s' "${v}"
  fi
}

# bond_mode <name> prints the "Bonding Mode:" value from ${PROC_BONDING}/<name>, or nothing when that file is absent.
# Only 802.3ad bonds are offered as the application uplink; an absent proc file means not a bond master.
bond_mode() {
  local f="${PROC_BONDING}/$1"
  [[ -r "${f}" ]] || return 0
  awk -F': ' '/^Bonding Mode:/ { print $2; exit }' "${f}"
}

# bond_slaves <name> prints the slave interface names listed in ${PROC_BONDING}/<name>, one per line.
# The bond's own proc entry is the only source for which NICs it really aggregates.
bond_slaves() {
  local f="${PROC_BONDING}/$1"
  [[ -r "${f}" ]] || return 0
  awk -F': ' '/^Slave Interface:/ { print $2 }' "${f}"
}

# name_is_virtual returns success for our own bridge and for container, VM, tunnel, overlay and VLAN sub-interface names.
# Name rules catch our bridge and container, VM, tunnel and overlay devices that can look physical.
name_is_virtual() {
  case "$1" in
    br0|"${BRIDGE_NAME}"|br-*|docker*|lxdbr*|lxc*|virbr*|veth*|tap*|tun*|cni*|flannel*|cilium*|kube*|*.*) return 0 ;;
    *) return 1 ;;
  esac
}

# classify_iface <name> sets CL_KIND (physical|bond|wireless|virtual|slave|brmember) and CL_SKIP (empty means selectable).
# Order: loopback, wireless, existing bond master (exempt from virtual rules), virtual names/links, enslaved NICs, type.
# A NIC whose master is our own BRIDGE_NAME is the bridge's member (brmember, selectable as the uplink only); a NIC enslaved to anything else (a bond) is a slave.
classify_iface() {
  local name="$1" dir="${SYSFS_NET}/$1" target master type
  CL_KIND="physical"
  CL_SKIP=""
  if [[ "${name}" == "lo" ]]; then
    CL_KIND="virtual"
    CL_SKIP="loopback"
    return 0
  fi
  case "${name}" in
    wlp*|wlan*|wlx*)
      CL_KIND="wireless"
      CL_SKIP="wireless (name pattern)"
      return 0
      ;;
  esac
  if [[ -e "${dir}/wireless" || -L "${dir}/phy80211" ]]; then
    CL_KIND="wireless"
    CL_SKIP="wireless device"
    return 0
  fi
  if [[ -f "${PROC_BONDING}/${name}" ]]; then
    CL_KIND="bond"
    return 0
  fi
  if name_is_virtual "${name}"; then
    CL_KIND="virtual"
    CL_SKIP="virtual name pattern"
    return 0
  fi
  target="$(readlink -f -- "${dir}")"
  if [[ ! -e "${dir}/device" || "${target}" == */devices/virtual/* ]]; then
    CL_KIND="virtual"
    CL_SKIP="virtual (no device link or devices/virtual target)"
    return 0
  fi
  if [[ -e "${dir}/master" || -L "${dir}/master" ]]; then
    master="$(readlink -- "${dir}/master")"
    if [[ "${master##*/}" == "${BRIDGE_NAME}" ]]; then
      CL_KIND="brmember"
    else
      CL_KIND="slave"
      CL_SKIP="slave of ${master##*/}"
      return 0
    fi
  fi
  type="$(read_attr "${dir}/type")"
  if [[ "${type}" != "1" ]]; then
    CL_SKIP="non-Ethernet type ${type:-unknown}"
  fi
}

# scan_interfaces walks ${SYSFS_NET}, classifies every entry and fills the R_* table and C_* candidate arrays.
# Dies when zero interfaces were examined, so an empty or wrong tree can never pass as "nothing to choose".
scan_interfaces() {
  local path name state carrier speed mode reason
  local speed_re='^[0-9]+$'
  R_NAME=()
  R_STATE=()
  R_SPEED=()
  R_KIND=()
  R_DECISION=()
  C_NAME=()
  C_SPEED=()
  C_KIND=()
  N_EXAMINED=0
  for path in "${SYSFS_NET}"/*; do
    [[ -e "${path}" || -L "${path}" ]] || continue
    name="${path##*/}"
    classify_iface "${name}"
    state="$(read_attr "${path}/operstate")"
    carrier="$(read_attr "${path}/carrier")"
    speed="$(read_attr "${path}/speed")"
    mode=""
    if [[ "${CL_KIND}" == "bond" ]]; then
      mode="$(bond_mode "${name}")"
    fi
    if [[ -n "${CL_SKIP}" ]]; then
      reason="skip: ${CL_SKIP}"
    elif [[ "${CL_KIND}" == "bond" && "${mode}" != *802.3ad* ]]; then
      reason="skip: bond mode '${mode:-unknown}' is not 802.3ad"
    elif [[ "${state}" != "up" ]]; then
      reason="not live: operstate ${state:-unknown}"
    elif [[ "${carrier}" != "1" ]]; then
      reason="not live: no carrier"
    elif [[ ! "${speed}" =~ ${speed_re} ]]; then
      reason="not live: no speed"
    elif (( speed == 0 )); then
      reason="not live: no speed"
    else
      reason="candidate"
    fi
    R_NAME+=("${name}")
    R_STATE+=("${state:-?}")
    R_SPEED+=("${speed:-n/a}")
    R_KIND+=("${CL_KIND}")
    R_DECISION+=("${reason}")
    if [[ "${reason}" == "candidate" ]]; then
      C_NAME+=("${name}")
      C_SPEED+=("${speed}")
      C_KIND+=("${CL_KIND}")
    fi
    N_EXAMINED=$((N_EXAMINED + 1))
  done
  N_CAND=${#C_NAME[@]}
  [[ "${N_EXAMINED}" -gt 0 ]] || die "examined 0 interfaces under ${SYSFS_NET}; refusing to choose NICs without a scan"
}

# list_candidates prints every live candidate as "name(kind speed Mb/s)" for failure messages.
# Failure messages list every live candidate so the operator sees exactly what the scan found.
list_candidates() {
  local i out=""
  for (( i = 0; i < ${#C_NAME[@]}; i++ )); do
    out="${out} ${C_NAME[i]}(${C_KIND[i]} ${C_SPEED[i]} Mb/s)"
  done
  printf '%s' "${out:- none}"
}

# check_override_kind <name> <role> dies unless an override names an existing NIC usable for that role.
# Bonds, virtual and wireless interfaces are never valid; a slave is valid only as an uplink member.
check_override_kind() {
  local name="$1" role="$2"
  [[ -e "${SYSFS_NET}/${name}" ]] || die "${role}=${name} not found under ${SYSFS_NET}"
  classify_iface "${name}"
  case "${CL_KIND}" in
    bond|virtual|wireless) die "${role}=${name} is a ${CL_KIND} interface; ${role} must name a physical Ethernet NIC" ;;
  esac
  if [[ -n "${CL_SKIP}" && "${CL_KIND}" != "slave" ]]; then
    die "${role}=${name} is not usable: ${CL_SKIP}"
  fi
  if [[ "${role}" == "MGMT_IFACE" && "${CL_KIND}" == "slave" ]]; then
    die "MGMT_IFACE=${name} is ${CL_SKIP}; management must be a physical NIC that is not enslaved"
  fi
  if [[ "${role}" == "MGMT_IFACE" && "${CL_KIND}" == "brmember" ]]; then
    die "MGMT_IFACE=${name} is a member of bridge ${BRIDGE_NAME}; management must be a physical NIC outside the application bridge"
  fi
}

# select_mgmt sets MGMT_IFACE: the operator override, else the slowest live physical NIC not used by the uplink.
# A tie for slowest is refused because a wrong management guess locks the operator out of SSH.
select_mgmt() {
  local i min="" count=0 names=""
  if [[ -n "${MGMT_IFACE:-}" ]]; then
    check_override_kind "${MGMT_IFACE}" MGMT_IFACE
    DETECT_MGMT_WHY="operator override (MGMT_IFACE)"
    return 0
  fi
  for (( i = 0; i < ${#C_NAME[@]}; i++ )); do
    [[ "${C_KIND[i]}" == "physical" ]] || continue
    case "${C_NAME[i]}" in
      "${UPLINK_IFACE:-}"|"${BOND_MEMBER_1:-}"|"${BOND_MEMBER_2:-}") continue ;;
    esac
    if [[ -z "${min}" ]] || (( C_SPEED[i] < min )); then
      min="${C_SPEED[i]}"
      names="${C_NAME[i]}"
      count=1
    elif (( C_SPEED[i] == min )); then
      names="${names} ${C_NAME[i]}"
      count=$((count + 1))
    fi
  done
  [[ "${count}" -gt 0 ]] || die "no live physical NIC left for management; live candidates:$(list_candidates). Set MGMT_IFACE=<nic>."
  if [[ "${count}" -gt 1 ]]; then
    die "management NIC is ambiguous: ${names} all run at ${min} Mb/s (slowest). A wrong guess locks you out of SSH (SSH is restricted to management). Set MGMT_IFACE=<nic> in NODE_ENV_FILE or the environment."
  fi
  MGMT_IFACE="${names}"
  DETECT_MGMT_WHY="slowest live physical NIC at ${min} Mb/s"
}

# select_uplink sets UPLINK_MODE and its NICs: operator overrides, then one live 802.3ad bond, then one spare NIC.
# It never creates a bond from unbonded NICs, because switch-side LACP may not be configured.
select_uplink() {
  local i m nb=0 nn=0 nu=0 bond_name="" nic_name="" brm_name=""
  local -a members=()
  if [[ -n "${BOND_MEMBER_1:-}" || -n "${BOND_MEMBER_2:-}" ]]; then
    [[ -n "${BOND_MEMBER_1:-}" && -n "${BOND_MEMBER_2:-}" ]] || die "set BOND_MEMBER_1 and BOND_MEMBER_2 together, or neither"
    [[ -z "${UPLINK_IFACE:-}" ]] || die "set either UPLINK_IFACE or BOND_MEMBER_1/2, not both"
    [[ "${BOND_MEMBER_1}" != "${BOND_MEMBER_2}" ]] || die "BOND_MEMBER_1 and BOND_MEMBER_2 must differ"
    check_override_kind "${BOND_MEMBER_1}" BOND_MEMBER_1
    check_override_kind "${BOND_MEMBER_2}" BOND_MEMBER_2
    UPLINK_MODE="bond"
    DETECT_UPLINK_WHY="operator override (BOND_MEMBER_1/2)"
    return 0
  fi
  if [[ -n "${UPLINK_IFACE:-}" ]]; then
    check_override_kind "${UPLINK_IFACE}" UPLINK_IFACE
    UPLINK_MODE="single"
    DETECT_UPLINK_WHY="operator override (UPLINK_IFACE)"
    return 0
  fi
  for (( i = 0; i < ${#C_NAME[@]}; i++ )); do
    if [[ "${C_KIND[i]}" == "bond" ]]; then
      nb=$((nb + 1))
      bond_name="${C_NAME[i]}"
    elif [[ "${C_KIND[i]}" == "physical" && "${C_NAME[i]}" != "${MGMT_IFACE}" ]]; then
      nn=$((nn + 1))
      nic_name="${C_NAME[i]}"
    elif [[ "${C_KIND[i]}" == "brmember" && "${C_NAME[i]}" != "${MGMT_IFACE}" ]]; then
      nu=$((nu + 1))
      brm_name="${C_NAME[i]}"
    fi
  done
  if [[ "${nb}" -eq 0 && "${nu}" -ge 1 ]]; then
    [[ "${nu}" -eq 1 ]] || die "bridge ${BRIDGE_NAME} has ${nu} member NICs and no 802.3ad bond; cannot derive the uplink. Set UPLINK_IFACE=<nic>."
    UPLINK_IFACE="${brm_name}"
    UPLINK_MODE="single"
    DETECT_UPLINK_WHY="member of existing bridge ${BRIDGE_NAME} (netplan already applied)"
    return 0
  fi
  if [[ "${nb}" -eq 1 ]]; then
    while IFS= read -r m; do
      if [[ -n "${m}" ]]; then
        members+=("${m}")
      fi
    done < <(bond_slaves "${bond_name}")
    [[ "${#members[@]}" -eq 2 ]] || die "bond ${bond_name} has ${#members[@]} member(s); the uplink needs exactly 2. Set BOND_MEMBER_1 and BOND_MEMBER_2 explicitly."
    for m in "${members[@]}"; do
      [[ "${m}" != "${MGMT_IFACE}" ]] || die "bond ${bond_name} member ${m} is MGMT_IFACE; management must not be a bond member"
    done
    BOND_MEMBER_1="${members[0]}"
    BOND_MEMBER_2="${members[1]}"
    UPLINK_MODE="bond"
    DETECT_BOND="${bond_name}"
    DETECT_UPLINK_WHY="existing live 802.3ad bond ${bond_name}"
    return 0
  fi
  if [[ "${nb}" -eq 0 && "${nn}" -eq 1 ]]; then
    UPLINK_IFACE="${nic_name}"
    UPLINK_MODE="single"
    DETECT_UPLINK_WHY="only remaining live NIC"
    return 0
  fi
  die "cannot choose the application uplink (${nb} live 802.3ad bond(s), ${nn} spare NIC(s)); live candidates:$(list_candidates). Set UPLINK_IFACE=<nic> for one NIC, or BOND_MEMBER_1 and BOND_MEMBER_2 to reuse an existing live 802.3ad bond. This script never creates a bond from unbonded NICs (switch-side LACP may not be configured)."
}

# print_decision_table logs one row per examined interface, then the counts and resolved roles; read-only, to stderr.
# The table is the audit trail of why each NIC was chosen or skipped; it prints on every detect and preflight run.
print_decision_table() {
  local i name decision
  log "NIC decision table (detection only; nothing is applied):"
  log "$(printf '%-12s | %-9s | %-7s | %-9s | %s' name state speed kind decision)"
  for (( i = 0; i < ${#R_NAME[@]}; i++ )); do
    name="${R_NAME[i]}"
    decision="${R_DECISION[i]}"
    if [[ "${decision}" == "candidate" ]]; then
      decision="candidate, not selected"
    fi
    if [[ "${name}" == "${MGMT_IFACE}" ]]; then
      decision="MGMT: ${DETECT_MGMT_WHY}"
    elif [[ "${name}" == "${UPLINK_IFACE:-}" || "${name}" == "${BOND_MEMBER_1:-}" || "${name}" == "${BOND_MEMBER_2:-}" || "${name}" == "${DETECT_BOND:-}" ]]; then
      decision="UPLINK: ${DETECT_UPLINK_WHY}"
      if [[ "${R_DECISION[i]}" != "candidate" && "${R_DECISION[i]}" != "candidate, not selected" ]]; then
        decision="${decision} (${R_DECISION[i]})"
      fi
    fi
    log "$(printf '%-12s | %-9s | %-7s | %-9s | %s' "${name}" "${R_STATE[i]}" "${R_SPEED[i]}" "${R_KIND[i]}" "${decision}")"
  done
  log "examined ${N_EXAMINED}, candidates ${N_CAND}, skipped $((N_EXAMINED - N_CAND))"
  log "resolved: MGMT_IFACE=${MGMT_IFACE} UPLINK_MODE=${UPLINK_MODE} UPLINK_IFACE=${UPLINK_IFACE:-} BOND_MEMBER_1=${BOND_MEMBER_1:-} BOND_MEMBER_2=${BOND_MEMBER_2:-}"
}

# detect_network resolves MGMT_IFACE and the uplink (operator overrides win), refuses ambiguous or impossible choices,
# and prints the decision table. It only sets variables; nothing on the host is changed.
detect_network() {
  local both=0
  DETECT_MGMT_WHY=""
  DETECT_UPLINK_WHY=""
  DETECT_BOND=""
  scan_interfaces
  if [[ -n "${MGMT_IFACE:-}" ]] && { [[ -n "${UPLINK_IFACE:-}" ]] || [[ -n "${BOND_MEMBER_1:-}" && -n "${BOND_MEMBER_2:-}" ]]; }; then
    both=1
  fi
  if [[ "${N_CAND}" -lt 2 && "${both}" -eq 0 ]]; then
    die "only ${N_CAND} live candidate(s) found, need 2:$(list_candidates). A single live NIC cannot be both management and uplink. Set MGMT_IFACE and UPLINK_IFACE (or BOND_MEMBER_1/2) to override."
  fi
  select_mgmt
  select_uplink
  case " ${BOND_MEMBER_1:-} ${BOND_MEMBER_2:-} ${UPLINK_IFACE:-} " in
    *" ${MGMT_IFACE} "*) die "MGMT_IFACE (${MGMT_IFACE}) must not be an uplink NIC" ;;
  esac
  validate_net_values
  print_decision_table
}

# ip2int converts a dotted-quad IPv4 address to a 32-bit integer.
ip2int() {
  local a b c d n
  IFS=. read -r a b c d <<< "$1"
  n=$(( (10#$a << 24) | (10#$b << 16) | (10#$c << 8) | 10#$d ))
  printf '%d' "${n}"
}

# cidr_ok returns success when the argument is a syntactically valid IPv4 CIDR (octets 0-255, prefix 0-32).
cidr_ok() {
  local cidr="$1" ip o
  local -a oct
  [[ "${cidr}" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}/([0-9]|[12][0-9]|3[0-2])$ ]] || return 1
  ip="${cidr%/*}"
  IFS=. read -r -a oct <<< "${ip}"
  for o in "${oct[@]}"; do
    (( 10#${o} <= 255 )) || return 1
  done
  return 0
}

# ip_ok returns success when the argument is a valid plain IPv4 address.
ip_ok() {
  cidr_ok "$1/32"
}

# cidr_mask prints the 32-bit netmask for the prefix of a CIDR.
cidr_mask() {
  local p="${1#*/}" m
  m=$(( (0xFFFFFFFF << (32 - p)) & 0xFFFFFFFF ))
  printf '%d' "${m}"
}

# ip_in_cidr returns success when plain IP $1 lies inside the CIDR $2.
ip_in_cidr() {
  local ipi net mask
  ipi="$(ip2int "$1")"
  net="$(ip2int "${2%/*}")"
  mask="$(cidr_mask "$2")"
  [[ $(( ipi & mask )) -eq $(( net & mask )) ]]
}

# cidr_overlap returns success when two valid CIDRs share any address (the shorter prefix contains the other network).
cidr_overlap() {
  local pa="${1#*/}" pb="${2#*/}" p mask
  p=$(( pa < pb ? pa : pb ))
  mask=$(( (0xFFFFFFFF << (32 - p)) & 0xFFFFFFFF ))
  [[ $(( $(ip2int "${1%/*}") & mask )) -eq $(( $(ip2int "${2%/*}") & mask )) ]]
}

# check_distinct_addresses dies naming the first pair of BR0, WORKER, MGMT host addresses and GATEWAY that are equal.
# Unset values are skipped; BR0_IP_CIDR is ignored under TOBOGGANING=1 (no host bridge address exists there).
check_distinct_addresses() {
  local i j
  local -a names=() vals=()
  if ! is_toboggan && [[ -n "${BR0_IP_CIDR:-}" ]]; then
    names+=(BR0_IP_CIDR)
    vals+=("${BR0_IP_CIDR%/*}")
  fi
  if [[ -n "${WORKER_IP_CIDR:-}" ]]; then
    names+=(WORKER_IP_CIDR)
    vals+=("${WORKER_IP_CIDR%/*}")
  fi
  if [[ -n "${MGMT_IP_CIDR:-}" ]]; then
    names+=(MGMT_IP_CIDR)
    vals+=("${MGMT_IP_CIDR%/*}")
  fi
  names+=(GATEWAY)
  vals+=("${GATEWAY}")
  for (( i = 0; i < ${#names[@]}; i++ )); do
    for (( j = i + 1; j < ${#names[@]}; j++ )); do
      if [[ "${vals[i]}" == "${vals[j]}" ]]; then
        die "address collision: ${names[i]} and ${names[j]} both use ${vals[i]}; every host address and the gateway must be distinct"
      fi
    done
  done
}

# require_net_inputs fails loudly naming every unresolved network input. TOBOGGANING=1 needs no BR0_IP_CIDR (no host bridge) and no detected uplink.
require_net_inputs() {
  local name missing=""
  local -a required
  if is_toboggan; then
    required=(WORKER_IP_CIDR MGMT_IP_CIDR MGMT_IFACE)
  else
    required=(BR0_IP_CIDR WORKER_IP_CIDR MGMT_IP_CIDR MGMT_IFACE)
  fi
  for name in "${required[@]}"; do
    if [[ -z "${!name:-}" ]]; then
      missing="${missing} ${name}"
    fi
  done
  case "${UPLINK_MODE:-}" in
    bond)
      [[ -n "${BOND_MEMBER_1:-}" && -n "${BOND_MEMBER_2:-}" ]] || missing="${missing} BOND_MEMBER_1/2"
      ;;
    single)
      [[ -n "${UPLINK_IFACE:-}" ]] || missing="${missing} UPLINK_IFACE"
      ;;
    *)
      missing="${missing} UPLINK_MODE (run detection: detect)"
      ;;
  esac
  if [[ -n "${missing}" ]]; then
    die "required network inputs not set or not resolved:${missing}"
  fi
}


# validate_network_inputs checks interfaces, distinctness, CIDR syntax, subnet placement and gateway/DNS; refuses on any failure.
# Under TOBOGGANING=1 the application subnet is WORKER_IP_CIDR; BR0_IP_CIDR is validated when given and otherwise ignored.
validate_network_inputs() {
  require_net_inputs
  local iface app_cidr
  local -a nics=("${MGMT_IFACE}")
  local -a ups
  read -r -a ups <<< "$(uplink_nics)"
  nics+=("${ups[@]}")
  for iface in "${nics[@]}"; do
    [[ -e "${SYSFS_NET}/${iface}" ]] || die "interface ${iface} not found under ${SYSFS_NET}"
  done
  if [[ "${UPLINK_MODE}" == "bond" ]]; then
    [[ "${BOND_MEMBER_1}" != "${BOND_MEMBER_2}" ]] || die "BOND_MEMBER_1 and BOND_MEMBER_2 must differ"
  fi
  for iface in "${ups[@]}"; do
    if [[ "${MGMT_IFACE}" == "${iface}" ]]; then
      die "MGMT_IFACE must not be an uplink NIC (bond member or UPLINK_IFACE)"
    fi
  done
  cidr_ok "${WORKER_IP_CIDR}" || die "WORKER_IP_CIDR is not a valid IPv4 CIDR: ${WORKER_IP_CIDR}"
  cidr_ok "${MGMT_IP_CIDR}" || die "MGMT_IP_CIDR is not a valid IPv4 CIDR: ${MGMT_IP_CIDR}"
  if is_toboggan; then
    [[ -z "${BR0_IP_CIDR:-}" ]] || cidr_ok "${BR0_IP_CIDR}" || die "BR0_IP_CIDR is not a valid IPv4 CIDR: ${BR0_IP_CIDR}"
    app_cidr="${WORKER_IP_CIDR}"
  else
    cidr_ok "${BR0_IP_CIDR}" || die "BR0_IP_CIDR is not a valid IPv4 CIDR: ${BR0_IP_CIDR}"
    if [[ "${BR0_IP_CIDR#*/}" != "${WORKER_IP_CIDR#*/}" ]] \
      || [[ $(( $(ip2int "${BR0_IP_CIDR%/*}") & $(cidr_mask "${BR0_IP_CIDR}") )) -ne \
            $(( $(ip2int "${WORKER_IP_CIDR%/*}") & $(cidr_mask "${WORKER_IP_CIDR}") )) ]]; then
      die "BR0_IP_CIDR (${BR0_IP_CIDR}) and WORKER_IP_CIDR (${WORKER_IP_CIDR}) must share one subnet"
    fi
    if ip_in_cidr "${BR0_IP_CIDR%/*}" "${MGMT_IP_CIDR}"; then
      die "application addresses must not be inside the management subnet ${MGMT_IP_CIDR}"
    fi
    app_cidr="${BR0_IP_CIDR}"
  fi
  if ip_in_cidr "${WORKER_IP_CIDR%/*}" "${MGMT_IP_CIDR}"; then
    die "application addresses must not be inside the management subnet ${MGMT_IP_CIDR}"
  fi
  if cidr_overlap "${app_cidr}" "${MGMT_IP_CIDR}"; then
    die "application subnet ${app_cidr} and management subnet ${MGMT_IP_CIDR} overlap"
  fi
  ip_ok "${GATEWAY}" || die "GATEWAY is not a valid IPv4 address: ${GATEWAY}"
  ip_ok "${DNS}" || die "DNS is not a valid IPv4 address: ${DNS}"
  ip_in_cidr "${GATEWAY}" "${app_cidr}" || die "GATEWAY ${GATEWAY} is not inside ${app_cidr}"
  check_distinct_addresses
  if [[ "${GATEWAY_ASSUMED}" == "1" || "${DNS_ASSUMED}" == "1" ]]; then
    warn "GATEWAY/DNS not set in env; ASSUMED ${GATEWAY}/${DNS} for the application net. Verify before apply."
  fi
  log "network inputs validated (app net ${app_cidr}, worker ${WORKER_IP_CIDR}, mgmt ${MGMT_IP_CIDR} on ${MGMT_IFACE})"
}

# host_kernel_prereqs loads and persists the kernel modules, enables ip_forward, and ensures bpffs.
host_kernel_prereqs() {
  local mod
  for mod in ${KERNEL_MODULES}; do
    run modprobe "${mod}"
  done
  render_modules | write_file_if_changed /etc/modules-load.d/k8s-lxd.conf 0644
  if [[ "$(cat /proc/sys/net/ipv4/ip_forward)" != "1" ]]; then
    run sysctl -w net.ipv4.ip_forward=1
  fi
  printf 'net.ipv4.ip_forward = 1\n' | write_file_if_changed /etc/sysctl.d/99-k8s-lxd-host.conf 0644
  bpffs
}

# render_modules prints one kernel module per line for modules-load.d.
render_modules() {
  local mod
  for mod in ${KERNEL_MODULES}; do
    printf '%s\n' "${mod}"
  done
}

# bpffs mounts /sys/fs/bpf when absent and persists that mount in /etc/fstab exactly once.
bpffs() {
  if ! mountpoint -q /sys/fs/bpf; then
    run mkdir -p /sys/fs/bpf
    run mount -t bpf bpf /sys/fs/bpf
  fi
  if ! grep -qE '^[^#]*[[:space:]]/sys/fs/bpf[[:space:]]' /etc/fstab; then
    ensure_line /etc/fstab 'bpf /sys/fs/bpf bpf defaults 0 0'
  fi
}

# install_lxd installs the LXD snap (online channel, or airgap from LXD_SNAP_FILE plus its assert) unless present.
install_lxd() {
  command -v snap >/dev/null 2>&1 || die "snap command not found; snapd is required"
  if snap list lxd >/dev/null 2>&1; then
    log "lxd snap already installed; skipping"
    return 0
  fi
  if [[ -n "${LXD_SNAP_FILE:-}" ]]; then
    require_var LXD_SNAP_ASSERT
    [[ -f "${LXD_SNAP_FILE}" ]] || die "LXD_SNAP_FILE not found: ${LXD_SNAP_FILE}"
    [[ -f "${LXD_SNAP_ASSERT}" ]] || die "LXD_SNAP_ASSERT not found: ${LXD_SNAP_ASSERT}"
    run snap ack "${LXD_SNAP_ASSERT}"
    run snap install "${LXD_SNAP_FILE}"
  else
    run snap install lxd --channel="${LXD_SNAP_CHANNEL}"
  fi
  run lxd waitready --timeout 300
}

# render_preseed prints a minimal lxd init preseed: one storage pool, no managed network, root-disk default profile.
render_preseed() {
  cat <<EOF
config: {}
networks: []
storage_pools:
  - name: ${STORAGE_POOL}
    driver: ${STORAGE_DRIVER}
profiles:
  - name: default
    description: Default LXD profile
    devices:
      root:
        path: /
        pool: ${STORAGE_POOL}
        type: disk
EOF
}

# lxd_init creates the storage pool via preseed (no managed network: br0 is used) unless the pool already exists.
lxd_init() {
  if lxc storage show "${STORAGE_POOL}" >/dev/null 2>&1; then
    log "storage pool ${STORAGE_POOL} exists; skipping lxd init"
    return 0
  fi
  local preseed
  new_tmp preseed
  render_preseed > "${preseed}"
  run_from_file "${preseed}" lxd init --preseed
}

# bridge_present reports whether BRIDGE_NAME exists on the host.
bridge_present() {
  ip link show "${BRIDGE_NAME}" >/dev/null 2>&1
}

# uplink_nics prints the uplink NIC names, space-separated: BOND_MEMBER_1 and BOND_MEMBER_2 (bond) or UPLINK_IFACE (single).
# Single source for uplink NIC names, so netplan rendering, conflict checks and validation cannot disagree.
uplink_nics() {
  if [[ "${UPLINK_MODE}" == "bond" ]]; then
    printf '%s %s' "${BOND_MEMBER_1}" "${BOND_MEMBER_2}"
  else
    printf '%s' "${UPLINK_IFACE}"
  fi
}

# managed_nic_names prints every NIC this script manages (management first), space-separated, with no trailing space.
# Management plus uplink NICs: the set covered by netplan conflict detection and existence checks.
managed_nic_names() {
  printf '%s %s' "${MGMT_IFACE}" "$(uplink_nics)"
}

# bridge_lower prints the device enslaved to BRIDGE_NAME: BOND_NAME in bond mode, the single uplink NIC otherwise.
# The bridge's lower device is the bond in bond mode and the uplink NIC itself in single mode.
bridge_lower() {
  if [[ "${UPLINK_MODE}" == "bond" ]]; then
    printf '%s' "${BOND_NAME}"
  else
    printf '%s' "${UPLINK_IFACE}"
  fi
}

# render_netplan prints the authoritative netplan: uplink NICs (no addresses), optional bond0, br0 (app net), MGMT_IFACE (mgmt, no gateway).
render_netplan() {
  local -a ups
  local m
  read -r -a ups <<< "$(uplink_nics)"
  validate_iface_name MGMT_IFACE "${MGMT_IFACE}"
  validate_iface_name BRIDGE_NAME "${BRIDGE_NAME}"
  validate_iface_name BOND_NAME "${BOND_NAME}"
  if [[ "${UPLINK_MODE}" == "bond" ]]; then
    validate_iface_name BOND_MEMBER_1 "${BOND_MEMBER_1}"
    validate_iface_name BOND_MEMBER_2 "${BOND_MEMBER_2}"
  else
    validate_iface_name UPLINK_IFACE "${UPLINK_IFACE}"
  fi
  valid_cidr4 "${MGMT_IP_CIDR}" || die "MGMT_IP_CIDR is not a valid IPv4 CIDR (value not printed)"
  valid_cidr4 "${BR0_IP_CIDR}" || die "BR0_IP_CIDR is not a valid IPv4 CIDR (value not printed)"
  valid_ipv4 "${GATEWAY}" || die "GATEWAY is not a valid IPv4 address (value not printed)"
  valid_ipv4 "${DNS}" || die "DNS is not a valid IPv4 address (value not printed)"
  cat <<EOF
network:
  version: 2
  renderer: networkd
  ethernets:
EOF
  for m in "${ups[@]}"; do
    printf '    %s:\n      dhcp4: false\n      dhcp6: false\n' "${m}"
  done
  cat <<EOF
    ${MGMT_IFACE}:
      dhcp4: false
      dhcp6: false
      addresses: [${MGMT_IP_CIDR}]
EOF
  if [[ "${UPLINK_MODE}" == "bond" ]]; then
    cat <<EOF
  bonds:
    ${BOND_NAME}:
      interfaces: [${BOND_MEMBER_1}, ${BOND_MEMBER_2}]
      parameters:
        mode: 802.3ad
        lacp-rate: slow
        transmit-hash-policy: layer2
        mii-monitor-interval: 100
EOF
  fi
  cat <<EOF
  bridges:
    ${BRIDGE_NAME}:
      interfaces: [$(bridge_lower)]
      addresses: [${BR0_IP_CIDR}]
      routes:
        - to: default
          via: ${GATEWAY}
      nameservers:
        addresses: [${DNS}]
      parameters:
        stp: false
        forward-delay: 0
EOF
}

# list_other_ifaces prints section/name for interfaces in a netplan file that this script does not manage.
list_other_ifaces() {
  awk -v ours=" ${BOND_NAME} ${BRIDGE_NAME} $(managed_nic_names) " '
    /^  [a-z]+:/ { sec = $1; next }
    /^    [^ ]+:/ {
      name = $1; sub(/:$/, "", name)
      if (index(ours, " " name " ") == 0 && (sec == "ethernets:" || sec == "bonds:" || sec == "bridges:" || sec == "vlans:" || sec == "wifis:"))
        print "  " sec, name
    }
  ' "$1"
}

# find_conflicting_netplan prints netplan files (other than an identical NETPLAN_OUT) that define our interfaces.
find_conflicting_netplan() {
  local rendered="$1" f names
  names="$(managed_nic_names)"
  local pattern="^[[:space:]]*(${BOND_NAME}|${BRIDGE_NAME}|${names// /|}):"
  for f in "${NETPLAN_DIR}"/*.yaml; do
    [[ -f "${f}" ]] || continue
    if cmp -s "${rendered}" "${f}"; then
      continue
    fi
    if grep -qE "${pattern}" "${f}"; then
      printf '%s\n' "${f}"
    fi
  done
}

# replace_netplan_files backs up each conflicting file outside /etc/netplan, lists orphaned interfaces, then removes it.
replace_netplan_files() {
  local epoch f base bk
  epoch="$(date +%s)"
  run mkdir -p "${NETPLAN_BACKUP_DIR}"
  for f in "$@"; do
    base="$(basename "${f}")"
    bk="${NETPLAN_BACKUP_DIR}/${base}.bak-${epoch}"
    warn "replacing ${f}; interfaces it defines that will NO longer be configured:"
    list_other_ifaces "${f}" >&2
    run cp -p "${f}" "${bk}"
    if [[ "${DRY_RUN}" != "1" ]]; then
      cmp -s "${f}" "${bk}" || die "backup verification failed for ${f}"
    fi
    run rm -f "${f}"
    log "rollback for ${base}: cp -p ${bk} ${f}"
  done
}

# ensure_host_netplan writes ONE authoritative netplan (replacing conflicting files only with FORCE_NETPLAN_REPLACE=1), runs generate, and applies only with APPLY_NETPLAN=1.
ensure_host_netplan() {
  if is_toboggan; then
    log "TOBOGGANING=1: netplan SKIPPED; no netplan file is written, generated or applied"
    return 0
  fi
  if bridge_present; then
    log "bridge ${BRIDGE_NAME} exists; skipping netplan generation"
    return 0
  fi
  validate_network_inputs
  local m rendered
  local -a conflicts=() nics
  read -r -a nics <<< "$(managed_nic_names)"
  for m in "${nics[@]}"; do
    ip link show "${m}" >/dev/null 2>&1 || die "NIC ${m} not found"
  done
  new_tmp rendered
  render_netplan > "${rendered}"
  while IFS= read -r m; do
    [[ -n "${m}" ]] && conflicts+=("${m}")
  done < <(find_conflicting_netplan "${rendered}")
  if [[ ${#conflicts[@]} -gt 0 ]]; then
    if [[ "${FORCE_NETPLAN_REPLACE}" != "1" ]]; then
      die "existing netplan file(s) define managed interfaces: ${conflicts[*]}. Review, then re-run with FORCE_NETPLAN_REPLACE=1 (files are backed up to ${NETPLAN_BACKUP_DIR})"
    fi
    replace_netplan_files "${conflicts[@]}"
  fi
  if [[ -f "${NETPLAN_OUT}" ]] && cmp -s "${rendered}" "${NETPLAN_OUT}"; then
    log "netplan ${NETPLAN_OUT} already current"
  else
    run install -m 600 "${rendered}" "${NETPLAN_OUT}"
  fi
  run netplan generate
  if [[ "${APPLY_NETPLAN}" == "1" ]]; then
    run netplan apply
  else
    warn "NETPLAN NOT APPLIED. ${BRIDGE_NAME} stays absent until 'netplan apply' or reboot. Operator must be on ${MGMT_IP_CIDR%/*} (mgmt) when applying."
    warn "rollback: rm -f ${NETPLAN_OUT}; restore backups from ${NETPLAN_BACKUP_DIR} into ${NETPLAN_DIR}; netplan generate; netplan apply"
  fi
}




# build_join_env <outvar> (not on the launch path; retained only because tests/test-env-parse.sh extracts it) writes a minimal join env file (join values plus any set K8S_* optionals) to a new temp file and stores its path in outvar.
# Only the listed keys are written, so node-unique BOND_*/MGMT_* values never enter the instance; the result is re-parsed to prove it is valid.
build_join_env() {
  local out k
  new_tmp out
  {
    printf 'JOIN_ENDPOINT=%s\n' "${JOIN_ENDPOINT}"
    printf 'JOIN_TOKEN=%s\n' "${JOIN_TOKEN}"
    printf 'CA_CERT_HASH=%s\n' "${CA_CERT_HASH}"
    for k in K8S_VERSION K8S_APT_BASE K8S_APT_KEY_FPR CONTAINERD_VERSION; do
      if [[ -n "${!k:-}" ]]; then
        printf '%s=%s\n' "${k}" "${!k}"
      fi
    done
  } > "${out}"
  chmod 0600 "${out}"
  ( unset JOIN_ENDPOINT JOIN_TOKEN CA_CERT_HASH K8S_VERSION K8S_APT_BASE K8S_APT_KEY_FPR CONTAINERD_VERSION
    load_env_file "${out}" JOIN_ENDPOINT JOIN_TOKEN CA_CERT_HASH K8S_VERSION K8S_APT_BASE K8S_APT_KEY_FPR CONTAINERD_VERSION ) \
    || die "minimal join env file failed its own parse check"
  printf -v "$1" '%s' "${out}"
}


# json_quote <value> sets JSON_LIT to a JSON string literal. It refuses quotes, backslashes and control bytes so no escaping is needed.
# It sets a variable rather than printing, so a refusal exits the main shell instead of a command substitution.
json_quote() {
  case "$1" in
    *\"*|*\\*) die "biome parameter value contains a quote or backslash; refused (value not printed)" ;;
  esac
  if [[ "$1" =~ [[:cntrl:]] ]]; then
    die "biome parameter value contains a control character; refused (value not printed)"
  fi
  JSON_LIT="\"$1\""
}

# add_param <param> <source-label> <value> <required 0|1> appends a JSON member for a non-empty value.
# A missing required value is recorded in MISSING_PARAMS so every gap is reported in one failure.
add_param() {
  if [[ -z "$3" ]]; then
    if [[ "$4" == "1" ]]; then
      MISSING_PARAMS="${MISSING_PARAMS} $1 (set $2)"
    fi
    return 0
  fi
  json_quote "$3"
  PARAM_JSON="${PARAM_JSON:+${PARAM_JSON},}\"$1\":${JSON_LIT}"
}

# add_raw_param <param> <json-literal> appends a member whose value is already a JSON literal (booleans only).
add_raw_param() {
  PARAM_JSON="${PARAM_JSON:+${PARAM_JSON},}\"$1\":$2"
}

# resolve_image_alias validates IMAGE_REMOTE and fills IMAGE_ALIAS when unset so remote and alias always form a valid pair.
# The ubuntu: remote has bare aliases (26.04) and the biome default ubuntu/26.04 only exists on images:, so the two are never mixed.
resolve_image_alias() {
  [[ "${IMAGE_REMOTE}" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] || die "IMAGE_REMOTE is not a valid LXD remote name (value not printed)"
  if [[ -z "${IMAGE_ALIAS}" && "${IMAGE_REMOTE}" == "ubuntu" ]]; then
    IMAGE_ALIAS="${DEFAULT_UBUNTU_IMAGE_ALIAS}"
    log "IMAGE_ALIAS not set: using ${IMAGE_REMOTE}:${IMAGE_ALIAS} (bare release alias on the ubuntu remote)"
  fi
  if [[ "${IMAGE_REMOTE}" == "ubuntu" && "${IMAGE_ALIAS}" == ubuntu/* ]]; then
    die "IMAGE_ALIAS ${IMAGE_ALIAS} does not exist on the ubuntu remote (it uses bare aliases such as ${DEFAULT_UBUNTU_IMAGE_ALIAS}); use IMAGE_REMOTE=images for ubuntu/<release> aliases"
  fi
}

# build_worker_params fills PARAM_JSON with the non-secret k8s-worker parameters from the node environment; never the join token.
# Each biome parameter maps to the host variable that supplies it. Names every required parameter with no value in one failure.
build_worker_params() {
  local bridge_label bridge_value allow_priv=false accept=false k
  PARAM_JSON=""
  MISSING_PARAMS=""
  if [[ -n "${K8S_APT_BASE}" && "${K8S_APT_BASE}" != */ ]]; then
    K8S_APT_BASE="${K8S_APT_BASE}/"
  fi
  if [[ -z "${K8S_APT_KEY_URL}" && -n "${K8S_APT_BASE}" ]]; then
    K8S_APT_KEY_URL="${K8S_APT_BASE}Release.key"
  fi
  if is_toboggan; then
    bridge_label="APP_IFACE"
    bridge_value="${APP_IFACE}"
  else
    bridge_label="BRIDGE_NAME"
    bridge_value="${BRIDGE_NAME}"
  fi
  add_param hostname WORKER_NAME "${WORKER_NAME}" 1
  add_param instance_name WORKER_NAME "${WORKER_NAME}" 1
  add_param static_ip WORKER_IP_CIDR "${WORKER_IP_CIDR:-}" 1
  add_param gateway GATEWAY "${GATEWAY}" 1
  add_param dns_servers DNS "${DNS}" 1
  add_param join_endpoint JOIN_ENDPOINT "${JOIN_ENDPOINT}" 1
  add_param join_ca_cert_hash CA_CERT_HASH "${CA_CERT_HASH}" 1
  add_param bridge "${bridge_label}" "${bridge_value}" 1
  add_param apt_mirror UBUNTU_APT_URI "${UBUNTU_APT_URI}" 1
  add_param k8s_apt_repo K8S_APT_BASE "${K8S_APT_BASE}" 1
  add_param k8s_apt_key_url K8S_APT_KEY_URL "${K8S_APT_KEY_URL}" 1
  add_param k8s_apt_key_sha256 K8S_APT_KEY_SHA256 "${K8S_APT_KEY_SHA256}" 1
  add_param k8s_apt_key_fingerprint K8S_APT_KEY_FPR "${K8S_APT_KEY_FPR}" 1
  add_param containerd_package_version CONTAINERD_VERSION "${CONTAINERD_VERSION}" 1
  add_param registry_mirror REGISTRY_MIRROR "${REGISTRY_MIRROR}" 1
  for k in K8S_VERSION K8S_PACKAGE_VERSION MIRRORED_REGISTRIES ALLOW_REGISTRY_FALLBACK INTERNAL_CA_URL INTERNAL_CA_SHA256; do
    if [[ -n "${!k}" ]]; then
      check_value "${k}" "${!k}"
    fi
  done
  if [[ -z "${K8S_PACKAGE_VERSION}" && -n "${K8S_VERSION}" ]]; then
    # Assumption: the pkgs.k8s.io apt revision suffix is -1.1; set K8S_PACKAGE_VERSION to override it.
    K8S_PACKAGE_VERSION="${K8S_VERSION}-1.1"
    log "K8S_PACKAGE_VERSION not set: derived ${K8S_PACKAGE_VERSION} from K8S_VERSION (assumes the -1.1 revision suffix)"
  fi
  if [[ -n "${K8S_VERSION}" && "${K8S_PACKAGE_VERSION}" != "${K8S_VERSION}" && "${K8S_PACKAGE_VERSION}" != "${K8S_VERSION}"-* ]]; then
    die "K8S_PACKAGE_VERSION does not match K8S_VERSION ${K8S_VERSION} (expected ${K8S_VERSION}-<revision>); the bootstrap would fail its kubeadm version check"
  fi
  if [[ -n "${INTERNAL_CA_URL}" && -z "${INTERNAL_CA_SHA256}" ]] || [[ -z "${INTERNAL_CA_URL}" && -n "${INTERNAL_CA_SHA256}" ]]; then
    die "INTERNAL_CA_URL and INTERNAL_CA_SHA256 must be set together"
  fi
  add_param k8s_version K8S_VERSION "${K8S_VERSION}" 0
  add_param k8s_package_version K8S_PACKAGE_VERSION "${K8S_PACKAGE_VERSION}" 0
  add_param mirrored_registries MIRRORED_REGISTRIES "${MIRRORED_REGISTRIES}" 0
  add_param internal_ca_url INTERNAL_CA_URL "${INTERNAL_CA_URL}" 0
  add_param internal_ca_sha256 INTERNAL_CA_SHA256 "${INTERNAL_CA_SHA256}" 0
  case "${ALLOW_REGISTRY_FALLBACK}" in
    1) add_raw_param allow_registry_fallback true ;;
    0) add_raw_param allow_registry_fallback false ;;
  esac
  case "${ALLOW_INSECURE_MIRRORS}" in
    1)
      add_raw_param allow_insecure_mirrors true
      warn "ALLOW_INSECURE_MIRRORS=1: plain http:// mirror URLs are permitted; packages, registry images and the CA can be read or altered on the network. Trusted airgapped lab only."
      ;;
    0|"") ;;
    *) die "ALLOW_INSECURE_MIRRORS must be 0 or 1" ;;
  esac
  resolve_image_alias
  add_param image_alias IMAGE_ALIAS "${IMAGE_ALIAS}" 0
  add_param storage_pool STORAGE_POOL "${STORAGE_POOL}" 0
  case "${LAB_PRIVILEGED}" in
    0|1) ;;
    *) die "LAB_PRIVILEGED must be 0 or 1" ;;
  esac
  if [[ "${LAB_PRIVILEGED}" == "1" ]]; then
    [[ "${I_ACCEPT_HOST_ROOT_RISK:-0}" == "1" ]] || die "LAB_PRIVILEGED=1 makes the container privileged (apparmor unconfined, cgroup devices allow all, rw proc/sys/cgroup); a workload can escape to host root. Set I_ACCEPT_HOST_ROOT_RISK=1 to acknowledge, or use LAB_PRIVILEGED=0"
    allow_priv=true
    warn "PRIVILEGED WORKER ${WORKER_NAME}: container can escape to HOST ROOT. Lab only. Pending approval (ROOT EXCEPTION)."
  fi
  if [[ "${I_ACCEPT_HOST_ROOT_RISK:-0}" == "1" ]]; then
    accept=true
  fi
  add_raw_param allow_privileged "${allow_priv}"
  add_raw_param accept_host_root_risk "${accept}"
  if [[ "${GATEWAY_ASSUMED}" == "1" || "${DNS_ASSUMED}" == "1" ]]; then
    warn "GATEWAY/DNS not set in env; ASSUMED ${GATEWAY}/${DNS} for the worker. Verify before launch."
  fi
  if [[ -n "${MISSING_PARAMS}" ]]; then
    die "k8s-worker biome has required parameters with no value:${MISSING_PARAMS}"
  fi
  cidr_ok "${WORKER_IP_CIDR}" || die "WORKER_IP_CIDR is not a valid IPv4 CIDR: ${WORKER_IP_CIDR}"
  ip_in_cidr "${GATEWAY}" "${WORKER_IP_CIDR}" || die "GATEWAY ${GATEWAY} is not inside ${WORKER_IP_CIDR}"
}

# json_field <file> <key> prints one top-level scalar from a rendered JSON file; exits non-zero when the key is missing.
json_field() {
  python3 -I -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))[sys.argv[2]])' "$1" "$2"
}

# render_worker_biome renders the k8s-worker biome into RENDER_DIR (a fresh 0700 temp dir removed on EXIT).
# params.json (0600) holds non-secret parameters only. The join token is appended to the renderer's stdin, so it reaches
# secrets.env and never params.json, argv, the environment or the profile.
render_worker_biome() {
  local full
  [[ -f "${BIOMES_DIR}/_lib/render.py" ]] || die "BIOMES_DIR ${BIOMES_DIR} does not contain _lib/render.py (set BIOMES_DIR to the biomes directory)"
  [[ -f "${BIOMES_DIR}/k8s-worker/biome.json" ]] || die "BIOMES_DIR ${BIOMES_DIR} does not contain k8s-worker/biome.json"
  new_tmp_dir RENDER_DIR
  build_worker_params
  printf '{%s}\n' "${PARAM_JSON}" > "${RENDER_DIR}/params.json"
  chmod 0600 "${RENDER_DIR}/params.json"
  full="{${PARAM_JSON},\"join_token\":\"${JOIN_TOKEN}\"}"
  log "rendering k8s-worker biome for ${WORKER_NAME} (parameter values not printed)"
  if ! printf '%s' "${full}" | python3 -I "${BIOMES_DIR}/_lib/render.py" --biome "${BIOMES_DIR}/k8s-worker" --params - --out "${RENDER_DIR}" --allowed-pools "${STORAGE_POOL}"; then
    die "biome render failed (see message above)"
  fi
  unset full
  [[ -f "${RENDER_DIR}/secrets.env" ]] || die "renderer produced no secrets.env; the join token was not delivered"
  log "biome rendered into the private render directory (params.json, user-data, network-config, profile.json, instance.json, secrets.env)"
}

# check_worker_image <dir> verifies read-only (lxc image info) that the rendered image exists on IMAGE_REMOTE.
# It runs before the profile is created so a bad remote or alias fails before any change; skipped under DRY_RUN.
check_worker_image() {
  local image
  image="$(json_field "$1/profile.json" image)" || die "cannot read the image from the rendered profile.json"
  if [[ "${DRY_RUN}" == "1" ]]; then
    log "[dry-run] lxc image info ${IMAGE_REMOTE}:${image} (read-only existence check skipped)"
    return 0
  fi
  lxc image info "${IMAGE_REMOTE}:${image}" >/dev/null 2>&1 \
    || die "image ${IMAGE_REMOTE}:${image} not found (lxc image info failed); check IMAGE_REMOTE and IMAGE_ALIAS. Nothing was created"
  log "image ${IMAGE_REMOTE}:${image} exists"
}

# apply_worker_profile <dir> creates or replaces the biome LXD profile from profile.json. It is idempotent: lxc profile edit replaces the whole profile.
# A non-bridged APP_NIC_TYPE overrides the rendered eth0 nictype on the host, with a warning, because the biome declares bridged.
apply_worker_profile() {
  local dir="$1" name nic="" body
  body="${dir}/lxd-profile.json"
  name="$(json_field "${dir}/profile.json" name)" || die "cannot read the profile name from the rendered profile.json"
  if is_toboggan && [[ "${APP_NIC_TYPE}" != "bridged" ]]; then
    nic="${APP_NIC_TYPE}"
    warn "APP_NIC_TYPE=${nic}: the k8s-worker biome declares nictype bridged; overriding eth0 nictype on the host (biome gap)"
  fi
  python3 -I - "${dir}/profile.json" "${body}" "${nic}" <<'PY' || die "cannot build the LXD profile body from profile.json"
import json, os, sys
src, dst, nic = sys.argv[1], sys.argv[2], sys.argv[3]
with open(src, encoding="utf-8") as fh:
    prof = json.load(fh)
devices = prof.get("devices") or {}
if nic:
    eth = devices.get("eth0")
    if not isinstance(eth, dict):
        sys.exit("profile has no eth0 device to set nictype on")
    eth["nictype"] = nic
body = {"config": prof.get("config") or {}, "description": "gough biome k8s-worker", "devices": devices}
fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w", encoding="utf-8") as fh:
    json.dump(body, fh, indent=2, sort_keys=True)
PY
  if [[ "${DRY_RUN}" == "1" ]]; then
    log "[dry-run] lxc profile show ${name} (existence check skipped); create if absent"
  elif ! lxc profile show "${name}" >/dev/null 2>&1; then
    run lxc profile create "${name}"
  fi
  run_from_file "${body}" lxc profile edit "${name}"
  log "profile ${name} applied from the biome"
}

# ensure_worker_instance <dir> creates the (container) instance STOPPED from the biome profile, applies instance.json limits, cloud-init user-data and
# network-config (config keys fed on stdin), then pushes secrets.env (0600) before start. An existing RUNNING instance is left untouched
# (cloud-init runs once); a non-running one fails loudly.
ensure_worker_instance() {
  local dir="$1" profile image info state instance_lines kind key val rootpool="" rootsize=""
  profile="$(json_field "${dir}/profile.json" name)" || die "cannot read the profile name from profile.json"
  image="$(json_field "${dir}/profile.json" image)" || die "cannot read the image from profile.json"
  if [[ "${DRY_RUN}" == "1" ]]; then
    log "[dry-run] lxc info ${WORKER_NAME} (existence check skipped)"
  elif info="$(lxc info "${WORKER_NAME}" 2>/dev/null)"; then
    state="$(awk '/^Status:/ {print $2; exit}' <<< "${info}")"
    if [[ "${state}" != "RUNNING" ]]; then
      die "instance ${WORKER_NAME} exists in state ${state:-unknown}; cloud-init runs once per instance. Start it, or delete it / choose another WORKER_NAME"
    fi
    log "instance ${WORKER_NAME} exists and is RUNNING; skipping create, config and secrets push; waiting on its marker"
    return 0
  fi
  run lxc init "${IMAGE_REMOTE}:${image}" "${WORKER_NAME}" -p default -p "${profile}"
  instance_lines="$(python3 -I -c 'import json,sys; i=json.load(open(sys.argv[1], encoding="utf-8")); [print("config", k, v) for k, v in sorted((i.get("config") or {}).items())]; r=(i.get("devices") or {}).get("root") or {}; print("device pool", r["pool"]); print("device size", r["size"])' "${dir}/instance.json")" \
    || die "cannot read limits and root disk from instance.json"
  while read -r kind key val; do
    case "${kind}" in
      config) run lxc config set "${WORKER_NAME}" "${key}" "${val}" ;;
      device)
        if [[ "${key}" == "pool" ]]; then
          rootpool="${val}"
        else
          rootsize="${val}"
        fi
        ;;
    esac
  done <<< "${instance_lines}"
  run lxc config device override "${WORKER_NAME}" root "pool=${rootpool}" "size=${rootsize}"
  run_from_file "${dir}/user-data" lxc config set "${WORKER_NAME}" cloud-init.user-data -
  run_from_file "${dir}/network-config" lxc config set "${WORKER_NAME}" cloud-init.network-config -
  run lxc file push --mode 0600 --uid 0 --gid 0 --create-dirs "${dir}/secrets.env" "${WORKER_NAME}/opt/gough/biome/secrets.env"
  run lxc start "${WORKER_NAME}"
  log "instance ${WORKER_NAME} started; secrets.env pushed before first boot"
}

# wait_worker_done polls for the biome outcome marker with lxc file pull (never lxc exec). biome-report.failed fails at once with its reason;
# biome-report.done succeeds. Neither within WAIT_SECONDS fails and leaves the instance for inspection.
wait_worker_done() {
  local errf reason first
  if [[ "${DRY_RUN}" == "1" ]]; then
    log "[dry-run] poll ${WORKER_NAME}:/var/lib/gough/biome-report.done every ${POLL_INTERVAL}s up to ${WAIT_SECONDS}s (no lxc call)"
    return 0
  fi
  new_tmp errf
  SECONDS=0
  while :; do
    if reason="$(lxc file pull "${WORKER_NAME}/var/lib/gough/biome-report.failed" - 2>/dev/null)"; then
      die "biome bootstrap reported failure in ${WORKER_NAME}: ${reason:0:200} (instance left for inspection)"
    fi
    if lxc file pull "${WORKER_NAME}/var/lib/gough/biome-report.done" - >/dev/null 2>"${errf}"; then
      log "biome bootstrap completed in ${WORKER_NAME}"
      return 0
    fi
    if [[ "${SECONDS}" -ge "${WAIT_SECONDS}" ]]; then
      first="$(head -n 1 "${errf}")"
      die "timed out after ${WAIT_SECONDS}s waiting for biome-report.done in ${WORKER_NAME} (last lxc: ${first:-no output}); instance left for inspection"
    fi
    sleep "${POLL_INTERVAL}"
  done
}

# confirm_secrets_removed fails loudly unless the instance's secrets.env is gone: lxc file pull must report not-found.
confirm_secrets_removed() {
  local errf rc=0 first
  if [[ "${DRY_RUN}" == "1" ]]; then
    log "[dry-run] confirm ${WORKER_NAME}:/opt/gough/biome/secrets.env absent (no lxc call)"
    return 0
  fi
  new_tmp errf
  lxc file pull "${WORKER_NAME}/opt/gough/biome/secrets.env" - >/dev/null 2>"${errf}" || rc=$?
  first="$(head -n 1 "${errf}")"
  if [[ "${rc}" -eq 0 ]]; then
    die "secrets.env is still present in ${WORKER_NAME} after the biome completed; delete it"
  fi
  if [[ "${first}" == *"not found"* || "${first}" == *"No such file"* || "${first}" == *"does not exist"* ]]; then
    log "confirmed secrets.env removed from ${WORKER_NAME}"
    return 0
  fi
  die "could not verify secrets.env removal in ${WORKER_NAME} (lxc exit ${rc}: ${first})"
}

# launch_worker renders the k8s-worker biome, applies its profile, creates the STOPPED instance, pushes secrets.env and starts it,
# then waits for the biome completion marker and confirms secrets.env is gone. The join is never run through lxc exec.
launch_worker() {
  [[ -n "${NODE_ENV_FILE:-}" ]] || die "NODE_ENV_FILE is required for the worker step (holds JOIN_ENDPOINT, JOIN_TOKEN, CA_CERT_HASH)"
  require_var JOIN_ENDPOINT
  require_var JOIN_TOKEN
  require_var CA_CERT_HASH
  validate_modes
  check_distinct_addresses
  if [[ -n "${K8S_APT_KEY_FILE:-}" ]]; then
    die "K8S_APT_KEY_FILE is not supported by the k8s-worker biome; serve the key at K8S_APT_KEY_URL and pin K8S_APT_KEY_SHA256"
  fi
  if is_toboggan; then
    toboggan_inputs
  else
    bridge_present || die "bridge ${BRIDGE_NAME} is absent; apply netplan (or reboot) before launching the worker"
  fi
  render_worker_biome
  check_worker_image "${RENDER_DIR}"
  apply_worker_profile "${RENDER_DIR}"
  ensure_worker_instance "${RENDER_DIR}"
  wait_worker_done
  confirm_secrets_removed
}

# restrict_ssh_to_mgmt writes the sshd drop-in, socket override (FreeBind, when ssh.socket is active) or ssh.service network-online drop-in, and nftables file; applies them only with APPLY_SSH_RESTRICT=1.
restrict_ssh_to_mgmt() {
  validate_network_inputs
  local mgmt_ip="${MGMT_IP_CIDR%/*}"
  printf '# Managed by hypervisor-prep.sh: sshd listens on management only\nListenAddress %s\n' "${mgmt_ip}" \
    | write_file_if_changed "${SSHD_DROPIN}" 0644
  if systemctl is-active --quiet ssh.socket; then
    printf '[Socket]\nListenStream=\nListenStream=%s:22\nFreeBind=true\n' "${mgmt_ip}" \
      | write_file_if_changed "${SOCKET_OVERRIDE}" 0644
  else
    log "ssh.socket not active; socket override not needed (ssh.service path); ordering sshd after network-online"
    printf '# Managed by hypervisor-prep.sh: start sshd only once the management address can exist\n[Unit]\nAfter=network-online.target\nWants=network-online.target\n' \
      | write_file_if_changed "${SERVICE_DROPIN}" 0644
  fi
  validate_net_values
  validate_iface_name MGMT_IFACE "${MGMT_IFACE}"
  validate_iface_name BRIDGE_NAME "${BRIDGE_NAME}"
  validate_iface_name BOND_NAME "${BOND_NAME}"
  render_nft | write_file_if_changed "${NFT_FILE}" 0644
  if [[ "${APPLY_SSH_RESTRICT}" == "1" ]]; then
    apply_ssh_restrict
  else
    warn "SSH RESTRICTION NOT APPLIED (files written only). Apply with APPLY_SSH_RESTRICT=1 from a session on ${mgmt_ip} (management), never from an application-network address."
    print_ssh_rollback
  fi
}

# render_nft prints an idempotent nftables file: add+delete+re-add of table inet gough_ssh_mgmt, dropping SSH except on MGMT_IFACE.
render_nft() {
  validate_iface_name MGMT_IFACE "${MGMT_IFACE}"
  validate_iface_name BRIDGE_NAME "${BRIDGE_NAME}"
  validate_iface_name BOND_NAME "${BOND_NAME}"
  cat <<EOF
# Managed by hypervisor-prep.sh: SSH only via ${MGMT_IFACE}; explicit drops on ${BRIDGE_NAME} and ${BOND_NAME}.
add table inet gough_ssh_mgmt
delete table inet gough_ssh_mgmt
table inet gough_ssh_mgmt {
  chain input {
    type filter hook input priority -10; policy accept;
    iifname "lo" accept
    iifname "${MGMT_IFACE}" tcp dport 22 accept
    iifname "${BRIDGE_NAME}" tcp dport 22 drop
    iifname "${BOND_NAME}" tcp dport 22 drop
    tcp dport 22 drop
  }
}
EOF
}

# print_ssh_rollback prints the commands that undo the SSH restriction.
print_ssh_rollback() {
  cat >&2 <<EOF
[hypervisor-prep] ROLLBACK (run as root from the management network):
  rm -f ${SSHD_DROPIN} ${SOCKET_OVERRIDE} ${SERVICE_DROPIN} ${NFT_FILE}
  systemctl daemon-reload
  nft delete table inet gough_ssh_mgmt
  systemctl restart ssh.socket   # or: systemctl restart ssh.service when ssh.socket is not active
EOF
}

# apply_ssh_restrict enforces preconditions, validates sshd, loads the nft rules and restarts sshd, then verifies listeners.
apply_ssh_restrict() {
  local mgmt_ip="${MGMT_IP_CIDR%/*}" addr_out link_out flags conn_server
  if [[ -n "${SSH_CONNECTION:-}" ]]; then
    conn_server="$(awk '{print $3}' <<< "${SSH_CONNECTION}")"
    [[ "${conn_server}" == "${mgmt_ip}" ]] || die "this SSH session arrived on ${conn_server}, not ${mgmt_ip}; reconnect via management before applying"
  else
    warn "no SSH_CONNECTION in environment; ensure you are on the console or management network"
  fi
  addr_out="$(ip -o addr show dev "${MGMT_IFACE}")"
  [[ "${addr_out}" == *" ${mgmt_ip}/"* ]] || die "${mgmt_ip} is not assigned to ${MGMT_IFACE}; refusing to apply"
  link_out="$(ip -o link show dev "${MGMT_IFACE}")"
  flags="${link_out#*<}"
  flags="${flags%%>*}"
  case ",${flags}," in
    *,UP,*) ;;
    *) die "${MGMT_IFACE} is not UP; refusing to apply" ;;
  esac
  if ! run sshd -t; then
    die "sshd -t failed; refusing to restart ssh"
  fi
  run systemctl daemon-reload
  run nft -f "${NFT_FILE}"
  ensure_line /etc/nftables.conf "include \"${NFT_FILE}\""
  run systemctl enable nftables.service
  if systemctl is-active --quiet ssh.socket; then
    run systemctl restart ssh.socket
  else
    run systemctl restart ssh.service
  fi
  if [[ "${DRY_RUN}" == "1" ]]; then
    log "[dry-run] listener verification skipped"
    return 0
  fi
  verify_ssh_listeners
}

# verify_ssh_listeners asserts every :22 listener is bound to MGMT_IP and that at least one exists; prints the count examined.
verify_ssh_listeners() {
  local listeners line local_addr addr count=0 bad=0 mgmt_ip="${MGMT_IP_CIDR%/*}"
  local _st _rq _sq _peer
  listeners="$(ss -ltnH 'sport = :22')"
  while IFS= read -r line; do
    [[ -z "${line}" ]] && continue
    count=$((count + 1))
    read -r _st _rq _sq local_addr _peer <<< "${line}"
    addr="${local_addr%:*}"
    addr="${addr#[}"
    addr="${addr%]}"
    if [[ "${addr}" != "${mgmt_ip}" ]]; then
      bad=$((bad + 1))
      warn "listener ${local_addr} is not ${mgmt_ip}"
    fi
  done <<< "${listeners}"
  log "ssh listeners examined: ${count}; not bound to ${mgmt_ip}: ${bad}"
  if [[ "${count}" -eq 0 ]]; then
    print_ssh_rollback
    die "FAILURE: zero listeners on :22 after apply"
  fi
  if [[ "${bad}" -ne 0 ]]; then
    print_ssh_rollback
    die "FAILURE: ${bad} of ${count} listener(s) not bound to ${mgmt_ip}"
  fi
}

# main parses arguments, runs preflight, and dispatches the requested subcommand.
main() {
  local cmd="all" arg
  for arg in "$@"; do
    case "${arg}" in
      --dry-run) DRY_RUN=1 ;;
      -h|--help|help) usage; exit 0 ;;
      *) cmd="${arg}" ;;
    esac
  done
  trap cleanup EXIT
  if [[ "${cmd}" == "detect" ]]; then
    host_checks
    if is_toboggan; then
      log "detect: skipped under TOBOGGANING=1 (interfaces are pre-created)"
      return 0
    fi
    detect_network
    log "done: detect (read-only; nothing changed)"
    return 0
  fi
  preflight
  case "${cmd}" in
    all)
      host_kernel_prereqs
      ensure_host_netplan
      install_lxd
      lxd_init
      if is_toboggan || bridge_present || [[ "${DRY_RUN}" == "1" ]]; then
        launch_worker
      else
        warn "bridge ${BRIDGE_NAME} absent (netplan not applied); worker NOT launched. After applying netplan run: $0 worker"
      fi
      restrict_ssh_to_mgmt
      ;;
    preflight) ;;
    kernel) host_kernel_prereqs ;;
    netplan) ensure_host_netplan ;;
    lxd) install_lxd; lxd_init ;;
    profile) render_worker_biome; check_worker_image "${RENDER_DIR}"; apply_worker_profile "${RENDER_DIR}" ;;
    worker) launch_worker ;;
    ssh) restrict_ssh_to_mgmt ;;
    *) usage >&2; die "unknown subcommand: ${cmd}" ;;
  esac
  log "done: ${cmd}"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi

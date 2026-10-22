#!/usr/bin/env bash
# gough biome k8s-worker: node prep + kubeadm join. Cilium is cluster-wide and installed by k8s-controller.
# Runs once from cloud-init inside the LXD instance as root. Re-runnable: completed steps are skipped.
set -euo pipefail

ROLE=k8s-worker
BIOME_DIR=/opt/gough/biome
STATE_DIR=/var/lib/gough/biome/state
JOIN_CONFIG=/var/lib/gough/biome/join-config.yaml
LOG_FILE=/var/log/gough-biome.log
PARAMS_FILE="$BIOME_DIR/params.env"

install -d -m 0755 "$STATE_DIR"
touch "$LOG_FILE"
exec > >(tee -a "$LOG_FILE") 2>&1

[[ -f "$PARAMS_FILE" ]] || { echo "missing $PARAMS_FILE" >&2; exit 1; }
set -a
# shellcheck source=/dev/null
. "$PARAMS_FILE"
set +a

# BEGIN node-prep (identical in k8s-controller and k8s-worker; tests/test_static.py fails on drift)

# Print a timestamped log line; output is captured into the biome log by the tee redirect.
log() { printf '%s [%s] %s\n' "$(date -u +%FT%TZ)" "$ROLE" "$*"; }

# Log a fatal error and exit non-zero so cloud-init records the run as failed.
die() { log "FATAL: $*"; exit 1; }

# Download a URL to a destination and fail unless its sha256 matches the expected value.
fetch_verified() {
  local url="$1" sha="$2" dest="$3"
  curl --fail --silent --show-error --location --retry 3 -o "$dest" "$url" || die "download failed: $url"
  echo "${sha}  ${dest}" | sha256sum --check --strict - >/dev/null || die "checksum mismatch: $url"
}

# Point Ubuntu apt at the operator mirror; the original deb822 file is kept once as .gough-orig.
prep_apt_sources() {
  . /etc/os-release
  local codename="$VERSION_CODENAME"
  if [[ -f /etc/apt/sources.list.d/ubuntu.sources && ! -f /etc/apt/sources.list.d/ubuntu.sources.gough-orig ]]; then
    cp /etc/apt/sources.list.d/ubuntu.sources /etc/apt/sources.list.d/ubuntu.sources.gough-orig
  fi
  cat > /etc/apt/sources.list.d/ubuntu.sources <<SRC
Types: deb
URIs: ${GOUGH_APT_MIRROR}
Suites: ${codename} ${codename}-updates ${codename}-security
Components: main universe
Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg
SRC
  : > /etc/apt/sources.list
}

# Install the Kubernetes apt repo from the mirror, verifying the signing key by checksum.
prep_k8s_apt() {
  install -d -m 0755 /etc/apt/keyrings
  local armored=/etc/apt/keyrings/gough-kubernetes.asc
  fetch_verified "$GOUGH_K8S_APT_KEY_URL" "$GOUGH_K8S_APT_KEY_SHA256" "$armored"
  gpg --batch --yes --dearmor -o /etc/apt/keyrings/gough-kubernetes.gpg "$armored"
  rm -f "$armored"
  cat > /etc/apt/sources.list.d/gough-kubernetes.sources <<SRC
Types: deb
URIs: ${GOUGH_K8S_APT_REPO}
Suites: /
Signed-By: /etc/apt/keyrings/gough-kubernetes.gpg
SRC
}

# Install an operator-supplied internal CA into the system trust store when one is configured.
prep_internal_ca() {
  [[ -n "${GOUGH_INTERNAL_CA_URL:-}" ]] || { log "no internal CA configured"; return 0; }
  fetch_verified "$GOUGH_INTERNAL_CA_URL" "$GOUGH_INTERNAL_CA_SHA256" /usr/local/share/ca-certificates/gough-internal.crt
  update-ca-certificates
}

# Install containerd and kubeadm/kubelet/kubectl at exact pinned versions, then hold them.
prep_packages() {
  export DEBIAN_FRONTEND=noninteractive
  apt-get update
  apt-get install -y --no-install-recommends ca-certificates curl gpg conntrack socat ebtables ethtool iproute2
  apt-get install -y --no-install-recommends "containerd=${GOUGH_CONTAINERD_PACKAGE_VERSION}"
  apt-mark hold containerd
  apt-get install -y --no-install-recommends \
    "kubelet=${GOUGH_K8S_PACKAGE_VERSION}" "kubeadm=${GOUGH_K8S_PACKAGE_VERSION}" "kubectl=${GOUGH_K8S_PACKAGE_VERSION}"
  apt-mark hold kubelet kubeadm kubectl
  systemctl enable kubelet
  local reported
  reported="$(kubeadm version -o short)"
  [[ "$reported" == "v${GOUGH_K8S_VERSION}" ]] || die "kubeadm reports ${reported}, expected v${GOUGH_K8S_VERSION}"
}

# Write a containerd hosts.toml per upstream registry so every pull goes through the mirror.
write_registry_mirrors() {
  local reg upstream dir
  local -a regs
  IFS=',' read -r -a regs <<< "$GOUGH_MIRRORED_REGISTRIES"
  for reg in "${regs[@]}"; do
    upstream="$reg"
    [[ "$reg" == "docker.io" ]] && upstream="registry-1.docker.io"
    dir="/etc/containerd/certs.d/${reg}"
    install -d -m 0755 "$dir"
    cat > "${dir}/hosts.toml" <<TOML
server = "https://${upstream}"

[host."${GOUGH_REGISTRY_MIRROR}"]
  capabilities = ["pull", "resolve"]
TOML
  done
}

# Configure containerd with the systemd cgroup driver and the mirror config path, then restart it.
prep_containerd() {
  install -d -m 0755 /etc/containerd
  containerd config default > /etc/containerd/config.toml
  sed -i -E 's/SystemdCgroup = false/SystemdCgroup = true/' /etc/containerd/config.toml
  grep -q 'SystemdCgroup = true' /etc/containerd/config.toml || die "containerd SystemdCgroup not enabled"
  sed -i -E "s|config_path = ['\"]['\"]|config_path = \"/etc/containerd/certs.d\"|" /etc/containerd/config.toml
  grep -q 'config_path = "/etc/containerd/certs.d"' /etc/containerd/config.toml || die "containerd config_path not set"
  write_registry_mirrors
  systemctl enable containerd
  systemctl restart containerd
}

# Enable IPv4 forwarding and the kmsg shim, and mount bpffs for Cilium (all inside the instance).
prep_kernel_bits() {
  sysctl -w net.ipv4.ip_forward=1 >/dev/null
  printf 'net.ipv4.ip_forward=1\n' > /etc/sysctl.d/99-gough-k8s.conf
  cat > /etc/systemd/system/kmsg-fix.service <<'UNIT'
[Unit]
Description=Link /dev/kmsg to /dev/console for kubelet in LXC
Before=kubelet.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/bin/ln -sf /dev/console /dev/kmsg

[Install]
WantedBy=multi-user.target
UNIT
  systemctl daemon-reload
  systemctl enable --now kmsg-fix.service
  mountpoint -q /sys/fs/bpf || mount -t bpf bpffs /sys/fs/bpf
  grep -q '^bpffs /sys/fs/bpf ' /etc/fstab || echo 'bpffs /sys/fs/bpf bpf defaults 0 0' >> /etc/fstab
}

# Run every node-prep sub-step in dependency order; each is idempotent on re-run.
prepare_node() {
  prep_apt_sources
  prep_internal_ca
  prep_k8s_apt
  prep_packages
  prep_containerd
  prep_kernel_bits
}
# END node-prep

# Return success if a named step already completed on a previous run.
step_done() { [[ -f "$STATE_DIR/$1" ]]; }

# Record a named step as complete so re-runs skip it.
mark_done() { touch "$STATE_DIR/$1"; }

# Run one step function once, recording completion only when it succeeds.
run_step() {
  local name="$1"; shift
  if step_done "$name"; then log "skip $name (already done)"; return 0; fi
  log "step $name"
  "$@"
  mark_done "$name"
}

# Fail loudly if any required GOUGH_ parameter is empty.
require_params() {
  local var
  for var in "$@"; do
    [[ -n "${!var:-}" ]] || die "missing parameter ${var}"
  done
}

# Join the cluster using a config file so the bootstrap token never appears in process arguments.
worker_join() {
  if [[ -f /etc/kubernetes/kubelet.conf ]]; then
    log "node already joined; not re-joining"
    return 0
  fi
  [[ "$GOUGH_JOIN_TOKEN" =~ ^[a-z0-9]{6}\.[a-z0-9]{16}$ ]] || die "join token has invalid format"
  [[ "$GOUGH_JOIN_CA_CERT_HASH" =~ ^sha256:[0-9a-f]{64}$ ]] || die "join CA cert hash has invalid format"
  if [[ -e /var/lib/kubelet/config.yaml ]]; then
    log "resetting partial join state"
    kubeadm reset -f
  fi
  install -m 0600 /dev/null "$JOIN_CONFIG"
  cat > "$JOIN_CONFIG" <<YAML
apiVersion: kubeadm.k8s.io/v1beta4
kind: JoinConfiguration
discovery:
  bootstrapToken:
    token: "${GOUGH_JOIN_TOKEN}"
    apiServerEndpoint: "${GOUGH_JOIN_ENDPOINT}"
    caCertHashes:
      - "${GOUGH_JOIN_CA_CERT_HASH}"
nodeRegistration:
  name: "$(hostname)"
  ignorePreflightErrors:
    - SystemVerification
YAML
  kubeadm join --config "$JOIN_CONFIG"
  rm -f "$JOIN_CONFIG"
}

# Confirm kubelet is running after join; node Ready is verified by the controller side.
worker_verify() {
  systemctl is-active --quiet kubelet || die "kubelet is not active after join"
  log "kubelet active; node registration complete (Ready is confirmed from the control plane)"
}

# Entry point: prepare the node, join the cluster, verify kubelet.
main() {
  require_params GOUGH_HOSTNAME GOUGH_APT_MIRROR GOUGH_K8S_APT_REPO GOUGH_K8S_APT_KEY_URL \
    GOUGH_K8S_APT_KEY_SHA256 GOUGH_K8S_VERSION GOUGH_K8S_PACKAGE_VERSION \
    GOUGH_CONTAINERD_PACKAGE_VERSION GOUGH_REGISTRY_MIRROR GOUGH_MIRRORED_REGISTRIES \
    GOUGH_JOIN_ENDPOINT GOUGH_JOIN_TOKEN GOUGH_JOIN_CA_CERT_HASH
  run_step node-prep prepare_node
  run_step kubeadm-join worker_join
  run_step kubelet-verify worker_verify
  log "k8s-worker biome complete"
}

main "$@"

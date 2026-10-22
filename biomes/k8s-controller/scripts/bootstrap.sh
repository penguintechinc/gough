#!/usr/bin/env bash
# gough biome k8s-controller: kubeadm control plane + Cilium (kube-proxy replacement).
# Runs once from cloud-init inside the LXD instance as root. Re-runnable: completed steps are skipped.
set -euo pipefail

ROLE=k8s-controller
BIOME_DIR=/opt/gough/biome
STATE_DIR=/var/lib/gough/biome/state
JOIN_FILE=/var/lib/gough/biome/join.env
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

# Reset any partial kubeadm state so init can start clean.
kubeadm_reset_if_partial() {
  if [[ -e /etc/kubernetes/admin.conf || -e /etc/kubernetes/manifests/kube-apiserver.yaml ]]; then
    log "resetting partial control plane state"
    kubeadm reset -f
  fi
}

# Initialise the control plane with kube-proxy skipped, since Cilium replaces it.
controller_init() {
  if [[ -f /etc/kubernetes/admin.conf ]] && kubectl --kubeconfig /etc/kubernetes/admin.conf get --raw=/readyz >/dev/null 2>&1; then
    log "control plane already healthy; not re-initialising"
  else
    kubeadm_reset_if_partial
    kubeadm init \
      --apiserver-advertise-address="$GOUGH_APISERVER_ADVERTISE_ADDRESS" \
      --control-plane-endpoint="$GOUGH_CONTROL_PLANE_ENDPOINT" \
      --pod-network-cidr="$GOUGH_POD_CIDR" \
      --service-cidr="$GOUGH_SERVICE_CIDR" \
      --node-name="$(hostname)" \
      --skip-phases=addon/kube-proxy \
      --ignore-preflight-errors=SystemVerification
  fi
  install -d -m 0700 /root/.kube
  install -m 0600 /etc/kubernetes/admin.conf /root/.kube/config
}

# Install the pinned Cilium CLI and chart from the mirror, pre-apply CRDs, then install Cilium.
controller_cilium() {
  export KUBECONFIG=/etc/kubernetes/admin.conf
  local work=/var/lib/gough/biome/cilium
  local api_host="${GOUGH_CONTROL_PLANE_ENDPOINT%%:*}"
  rm -rf "$work"
  install -d -m 0700 "$work"
  fetch_verified "$GOUGH_CHART_MIRROR_URL/cilium-${GOUGH_CILIUM_VERSION}.tgz" "$GOUGH_CILIUM_CHART_SHA256" "$work/chart.tgz"
  tar -xzf "$work/chart.tgz" -C "$work" --no-same-owner
  fetch_verified "$GOUGH_CILIUM_CLI_URL" "$GOUGH_CILIUM_CLI_SHA256" "$work/cilium-cli.tgz"
  tar -xzf "$work/cilium-cli.tgz" -C /usr/local/bin cilium
  local crd_dir="$work/cilium/crds"
  shopt -s nullglob
  local crds=("$crd_dir"/*.yaml)
  shopt -u nullglob
  (( ${#crds[@]} > 0 )) || die "no Cilium CRDs found in chart at $crd_dir"
  kubectl apply --server-side --force-conflicts -f "$crd_dir"
  kubectl wait --for=condition=Established --timeout=180s -f "$crd_dir"
  cilium install --chart-directory "$work/cilium" \
    --set kubeProxyReplacement=true \
    --set k8sServiceHost="$api_host" \
    --set k8sServicePort=6443 \
    --set ipam.mode=kubernetes \
    --set ipv4.enabled=true \
    --set ipv6.enabled=false \
    --set l2announcements.enabled=true \
    --set externalIPs.enabled=true \
    --set loadBalancer.algorithm=maglev \
    --set loadBalancer.mode=snat \
    --set bpf.masquerade=true \
    --set socketLB.enabled=true \
    --set nodePort.enabled=true \
    --set hostServices.enabled=true \
    --set enableCiliumEndpointSlice=true \
    --set envoyConfig.enabled=true \
    --set operator.replicas=1 \
    --set hubble.enabled=false \
    --set gatewayAPI.enabled=false
  cilium status --wait --wait-duration 15m
  kubectl wait --for=condition=Ready "node/$(hostname)" --timeout=300s
}

# Create a bounded-TTL join token, store join material locally (mode 600), and report it to the hub.
controller_publish_join() {
  local out token hash body hdr
  out="$(kubeadm token create --ttl "$GOUGH_JOIN_TOKEN_TTL" --print-join-command)"
  [[ "$out" =~ --token\ ([a-z0-9]{6}\.[a-z0-9]{16}) ]] || die "could not parse join token"
  token="${BASH_REMATCH[1]}"
  [[ "$out" =~ --discovery-token-ca-cert-hash\ (sha256:[0-9a-f]{64}) ]] || die "could not parse CA cert hash"
  hash="${BASH_REMATCH[1]}"
  install -m 0600 /dev/null "$JOIN_FILE"
  printf 'GOUGH_JOIN_ENDPOINT=%s\nGOUGH_JOIN_TOKEN=%s\nGOUGH_JOIN_CA_CERT_HASH=%s\n' \
    "$GOUGH_CONTROL_PLANE_ENDPOINT" "$token" "$hash" > "$JOIN_FILE"
  body=$(printf '{"biome":"%s","hostname":"%s","join_endpoint":"%s","join_token":"%s","ca_cert_hash":"%s","cilium_version":"%s"}' \
    "$ROLE" "$(hostname)" "$GOUGH_CONTROL_PLANE_ENDPOINT" "$token" "$hash" "$GOUGH_CILIUM_VERSION")
  umask 077
  hdr="$(mktemp)"
  trap 'rm -f "$hdr"' RETURN
  printf 'header = "Authorization: Bearer %s"\n' "$GOUGH_REPORT_BOOTSTRAP_TOKEN" > "$hdr"
  curl --fail --silent --show-error --retry 5 --retry-delay 5 --max-time 30 \
    --config "$hdr" -H 'Content-Type: application/json' -X POST --data "$body" "$GOUGH_REPORT_URL" >/dev/null
  log "join material reported to hub"
}

# Entry point: prepare the node, bring up the control plane, install Cilium, publish join material.
main() {
  require_params GOUGH_HOSTNAME GOUGH_APT_MIRROR GOUGH_K8S_APT_REPO GOUGH_K8S_APT_KEY_URL \
    GOUGH_K8S_APT_KEY_SHA256 GOUGH_K8S_VERSION GOUGH_K8S_PACKAGE_VERSION \
    GOUGH_CONTAINERD_PACKAGE_VERSION GOUGH_REGISTRY_MIRROR GOUGH_MIRRORED_REGISTRIES \
    GOUGH_APISERVER_ADVERTISE_ADDRESS GOUGH_CONTROL_PLANE_ENDPOINT GOUGH_POD_CIDR GOUGH_SERVICE_CIDR \
    GOUGH_CHART_MIRROR_URL GOUGH_CILIUM_VERSION GOUGH_CILIUM_CHART_SHA256 GOUGH_CILIUM_CLI_URL \
    GOUGH_CILIUM_CLI_SHA256 GOUGH_JOIN_TOKEN_TTL GOUGH_REPORT_URL GOUGH_REPORT_BOOTSTRAP_TOKEN
  run_step node-prep prepare_node
  run_step control-plane-init controller_init
  run_step cilium controller_cilium
  run_step join-material controller_publish_join
  log "k8s-controller biome complete"
}

main "$@"

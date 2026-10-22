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
[[ -f "$LOG_FILE" ]] || install -m 0600 /dev/null "$LOG_FILE"
chmod 0600 "$LOG_FILE"
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

# Download to a 0600 temp file, verify sha256, and only then install to the destination (never trust unverified bytes).
fetch_verified() {
  local url="$1" sha="$2" dest="$3" tmp
  tmp="$(mktemp)"
  if ! curl --fail --silent --show-error --location --retry 3 -o "$tmp" "$url"; then
    rm -f "$tmp"
    die "download failed: $url"
  fi
  if ! echo "${sha}  ${tmp}" | sha256sum --check --strict - >/dev/null; then
    rm -f "$tmp"
    die "checksum mismatch: $url"
  fi
  install -m 0644 "$tmp" "$dest"
  rm -f "$tmp"
}

# Log a warning when plain-HTTP mirrors were explicitly allowed; artifacts stay sha256-verified.
warn_insecure_mirrors() {
  if [[ "${GOUGH_ALLOW_INSECURE_MIRRORS:-false}" == "true" ]]; then
    log "WARNING: allow_insecure_mirrors=true; mirror traffic may be plaintext HTTP (artifacts are still sha256-verified)"
  fi
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
# Mirror-only by default: no upstream server line, so airgapped nodes never dial the public registry.
# allow_registry_fallback=true adds the public upstream as a last-resort server.
write_registry_mirrors() {
  local reg upstream dir
  local -a regs
  IFS=',' read -r -a regs <<< "$GOUGH_MIRRORED_REGISTRIES"
  for reg in "${regs[@]}"; do
    upstream="$reg"
    [[ "$reg" == "docker.io" ]] && upstream="registry-1.docker.io"
    dir="/etc/containerd/certs.d/${reg}"
    install -d -m 0755 "$dir"
    {
      if [[ "$GOUGH_ALLOW_REGISTRY_FALLBACK" == "true" ]]; then
        printf 'server = "https://%s"\n\n' "$upstream"
      fi
      printf '[host."%s"]\n  capabilities = ["pull", "resolve"]\n' "$GOUGH_REGISTRY_MIRROR"
    } > "${dir}/hosts.toml"
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
  warn_insecure_mirrors
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

# Write the kubeadm config file: InitConfiguration, ClusterConfiguration and, when kube-proxy is kept, a KubeProxyConfiguration with conntrack zeroed (runbook-verified LXC fix applied before creation).
write_kubeadm_config() {
  local cfg=/var/lib/gough/biome/kubeadm-config.yaml
  install -m 0600 /dev/null "$cfg"
  cat > "$cfg" <<YAML
apiVersion: kubeadm.k8s.io/v1beta4
kind: InitConfiguration
localAPIEndpoint:
  advertiseAddress: "${GOUGH_APISERVER_ADVERTISE_ADDRESS}"
nodeRegistration:
  name: "$(hostname)"
  ignorePreflightErrors:
    - SystemVerification
---
apiVersion: kubeadm.k8s.io/v1beta4
kind: ClusterConfiguration
controlPlaneEndpoint: "${GOUGH_CONTROL_PLANE_ENDPOINT}"
networking:
  podSubnet: "${GOUGH_POD_CIDR}"
  serviceSubnet: "${GOUGH_SERVICE_CIDR}"
---
apiVersion: kubelet.config.k8s.io/v1beta1
kind: KubeletConfiguration
systemReserved:
  cpu: "200m"
  memory: "512Mi"
kubeReserved:
  cpu: "100m"
  memory: "256Mi"
evictionHard:
  memory.available: "500Mi"
  nodefs.available: "10%"
YAML
  if [[ "$GOUGH_KUBE_PROXY_REPLACEMENT" != "true" ]]; then
    cat >> "$cfg" <<YAML
---
apiVersion: kubeproxy.config.k8s.io/v1alpha1
kind: KubeProxyConfiguration
conntrack:
  maxPerCore: 0
  min: 0
  tcpEstablishedTimeout: 0s
  tcpCloseWaitTimeout: 0s
YAML
  fi
  printf '%s' "$cfg"
}

# Initialise the control plane; kube-proxy is skipped only when Cilium replaces it.
controller_init() {
  if [[ -f /etc/kubernetes/admin.conf ]] && kubectl --kubeconfig /etc/kubernetes/admin.conf get --raw=/readyz >/dev/null 2>&1; then
    log "control plane already healthy; not re-initialising"
  else
    kubeadm_reset_if_partial
    local cfg out=/var/lib/gough/biome/kubeadm-init.out
    cfg="$(write_kubeadm_config)"
    install -m 0600 /dev/null "$out"
    local -a skip=()
    if [[ "$GOUGH_KUBE_PROXY_REPLACEMENT" == "true" ]]; then
      skip=(--skip-phases=addon/kube-proxy)
      log "kube-proxy replacement: Cilium will own service load balancing"
    else
      log "kube-proxy kept (conntrack zeroed via KubeProxyConfiguration)"
    fi
    # Output goes to a root-only file: --skip-token-print keeps the default token out of it, and
    # any failure tail is redacted before it reaches the biome log.
    if ! kubeadm init --config "$cfg" "${skip[@]}" --skip-token-print >"$out" 2>&1; then
      sed -E 's/[a-z0-9]{6}\.[a-z0-9]{16}/<redacted-token>/g' "$out" | tail -n 40
      die "kubeadm init failed; full output is in $out (mode 0600)"
    fi
    log "kubeadm init complete (full output in $out, mode 0600)"
  fi
  install -d -m 0700 /root/.kube
  install -m 0600 /etc/kubernetes/admin.conf /root/.kube/config
}

# Install the pinned Cilium CLI and chart from the mirror, pre-apply CRDs, then install Cilium with the selected features.
controller_cilium() {
  export KUBECONFIG=/etc/kubernetes/admin.conf
  local work=/var/lib/gough/biome/cilium
  local api_host="${GOUGH_CONTROL_PLANE_ENDPOINT%%:*}"
  local kpr="$GOUGH_KUBE_PROXY_REPLACEMENT"
  local hubble="$GOUGH_CILIUM_HUBBLE"
  local gateway="$GOUGH_CILIUM_GATEWAY_API"
  # Images come only from the operator mirror; digests pin the Cilium 1.20.1 release.
  local mirror_host="${GOUGH_REGISTRY_MIRROR#*://}"
  mirror_host="${mirror_host%/}"
  local CILIUM_IMAGE_DIGEST="sha256:ae9ea21f7427fe24bc6ea7247eb552157a1b0a431744045d3f641545ca71d11b"
  local CILIUM_OPERATOR_DIGEST="sha256:6c3885fc7b629099fdbe2a5c87869c86feb825fa18fae299eac0f61918d16ecf"
  local CILIUM_HUBBLE_RELAY_DIGEST="sha256:59be0ae7d475ab9011a5e954618c0f27b5778b17140381425b308b55ba4917f4"
  rm -rf "$work"
  install -d -m 0700 "$work"
  fetch_verified "$GOUGH_CHART_MIRROR_URL/cilium-${GOUGH_CILIUM_VERSION}.tgz" "$GOUGH_CILIUM_CHART_SHA256" "$work/chart.tgz"
  tar -xzf "$work/chart.tgz" -C "$work" --no-same-owner
  fetch_verified "$GOUGH_CILIUM_CLI_URL" "$GOUGH_CILIUM_CLI_SHA256" "$work/cilium-cli.tgz"
  tar -xzf "$work/cilium-cli.tgz" -C /usr/local/bin --no-same-owner cilium
  [[ -n "$GOUGH_CILIUM_CRDS_URL" && -n "$GOUGH_CILIUM_CRDS_SHA256" ]] \
    || die "cilium_crds_url and cilium_crds_sha256 are required"
  fetch_verified "$GOUGH_CILIUM_CRDS_URL" "$GOUGH_CILIUM_CRDS_SHA256" "$work/cilium-crds.yaml"
  kubectl apply --server-side --force-conflicts -f "$work/cilium-crds.yaml"
  kubectl wait --for=condition=Established --timeout=180s -f "$work/cilium-crds.yaml"
  if [[ "$gateway" == "true" ]]; then
    [[ -n "$GOUGH_GATEWAY_API_CRDS_URL" && -n "$GOUGH_GATEWAY_API_CRDS_SHA256" ]] \
      || die "cilium_gateway_api=true requires gateway_api_crds_url and gateway_api_crds_sha256"
    fetch_verified "$GOUGH_GATEWAY_API_CRDS_URL" "$GOUGH_GATEWAY_API_CRDS_SHA256" "$work/gateway-api-crds.yaml"
    kubectl apply --server-side --force-conflicts -f "$work/gateway-api-crds.yaml"
    kubectl wait --for=condition=Established --timeout=180s -f "$work/gateway-api-crds.yaml"
  fi
  local -a sets=(
    --set "kubeProxyReplacement=${kpr}"
    --set "k8sServiceHost=${api_host}"
    --set k8sServicePort=6443
    --set ipam.mode=kubernetes
    --set ipv4.enabled=true
    --set ipv6.enabled=false
    --set "l2announcements.enabled=${kpr}"
    --set "externalIPs.enabled=${kpr}"
    --set "socketLB.enabled=${kpr}"
    --set "nodePort.enabled=${kpr}"
    --set "hostServices.enabled=${kpr}"
    --set "bpf.masquerade=${kpr}"
    --set loadBalancer.algorithm=maglev
    --set loadBalancer.mode=snat
    --set enableCiliumEndpointSlice=true
    --set envoyConfig.enabled=true
    --set operator.replicas=1
    --set "hubble.enabled=${hubble}"
    --set "hubble.relay.enabled=${hubble}"
    --set "gatewayAPI.enabled=${gateway}"
    --set "image.repository=${mirror_host}/cilium/cilium"
    --set "image.digest=${CILIUM_IMAGE_DIGEST}"
    --set image.useDigest=true
    --set "operator.image.repository=${mirror_host}/cilium/operator-generic"
    --set "operator.image.digest=${CILIUM_OPERATOR_DIGEST}"
    --set operator.image.useDigest=true
    --set "hubble.relay.image.repository=${mirror_host}/cilium/hubble-relay"
    --set "hubble.relay.image.digest=${CILIUM_HUBBLE_RELAY_DIGEST}"
    --set hubble.relay.image.useDigest=true
  )
  cilium install --chart-directory "$work/cilium" "${sets[@]}"
  cilium status --wait --wait-duration 15m
  kubectl wait --for=condition=Ready "node/$(hostname)" --timeout=300s
}

# Delete every bootstrap token except the report token, so only the short-TTL token survives.
prune_bootstrap_tokens() {
  local keep="${1%%.*}" id
  while read -r id; do
    [[ -n "$id" && "$id" != "$keep" ]] || continue
    kubeadm token delete "$id" >/dev/null
  done < <(kubeadm token list | awk 'NR>1 {print $1}' | sed 's/\..*$//')
}

# Deliver the join-material JSON to the hub over https with a bearer token (hub mode).
report_to_hub() {
  local json="$1" body hdr
  [[ -n "$GOUGH_REPORT_URL" ]] || die "report_mode hub requires report_url"
  body="$(mktemp)"
  hdr="$(mktemp)"
  trap 'rm -f "$body" "$hdr"; trap - RETURN' RETURN
  printf '%s' "$json" > "$body"
  printf 'header = "Authorization: Bearer %s"\ndata-binary = "@%s"\n' "$GOUGH_REPORT_BOOTSTRAP_TOKEN" "$body" > "$hdr"
  curl --fail --silent --show-error --retry 5 --retry-delay 5 --max-time 30 \
    --config "$hdr" -H 'Content-Type: application/json' -X POST "$GOUGH_REPORT_URL" >/dev/null
  log "join material reported to hub"
}

# Write the join-material JSON atomically to a root-only file, then the done marker last (local mode).
report_locally() {
  local json="$1" report=/var/lib/gough/biome-report.json done=/var/lib/gough/biome-report.done tmp
  tmp="$(mktemp -p /var/lib/gough)"
  printf '%s\n' "$json" > "$tmp"
  chmod 0600 "$tmp"
  chown root:root "$tmp"
  mv -f "$tmp" "$report"
  tmp="$(mktemp -p /var/lib/gough)"
  printf 'written %s\n' "$(date -u +%FT%TZ)" > "$tmp"
  chmod 0600 "$tmp"
  mv -f "$tmp" "$done"
  log "join report written to $report (consumer must read, then delete it and $done)"
}

# Mint a short-TTL report token, store join material locally (mode 600), deliver it per report_mode, then prune other bootstrap tokens.
controller_publish_join() {
  local out token hash json
  out="$(kubeadm token create --ttl "$GOUGH_JOIN_TOKEN_TTL" --print-join-command)"
  [[ "$out" =~ --token\ ([a-z0-9]{6}\.[a-z0-9]{16}) ]] || die "could not parse join token"
  token="${BASH_REMATCH[1]}"
  [[ "$out" =~ --discovery-token-ca-cert-hash\ (sha256:[0-9a-f]{64}) ]] || die "could not parse CA cert hash"
  hash="${BASH_REMATCH[1]}"
  install -m 0600 /dev/null "$JOIN_FILE"
  printf 'GOUGH_JOIN_ENDPOINT=%s\nGOUGH_JOIN_TOKEN=%s\nGOUGH_JOIN_CA_CERT_HASH=%s\n' \
    "$GOUGH_CONTROL_PLANE_ENDPOINT" "$token" "$hash" > "$JOIN_FILE"
  json=$(printf '{"biome":"%s","hostname":"%s","join_endpoint":"%s","join_token":"%s","ca_cert_hash":"%s","cilium_version":"%s"}' \
    "$ROLE" "$(hostname)" "$GOUGH_CONTROL_PLANE_ENDPOINT" "$token" "$hash" "$GOUGH_CILIUM_VERSION")
  case "$GOUGH_REPORT_MODE" in
    hub) report_to_hub "$json" ;;
    local) report_locally "$json" ;;
    *) die "unknown report_mode: $GOUGH_REPORT_MODE" ;;
  esac
  prune_bootstrap_tokens "$token"
  log "default bootstrap tokens pruned; only the report token remains"
}

# Entry point: prepare the node, bring up the control plane, install Cilium, publish join material.
main() {
  require_params GOUGH_HOSTNAME GOUGH_APT_MIRROR GOUGH_K8S_APT_REPO GOUGH_K8S_APT_KEY_URL \
    GOUGH_K8S_APT_KEY_SHA256 GOUGH_K8S_VERSION GOUGH_K8S_PACKAGE_VERSION \
    GOUGH_CONTAINERD_PACKAGE_VERSION GOUGH_REGISTRY_MIRROR GOUGH_MIRRORED_REGISTRIES \
    GOUGH_APISERVER_ADVERTISE_ADDRESS GOUGH_CONTROL_PLANE_ENDPOINT GOUGH_POD_CIDR GOUGH_SERVICE_CIDR \
    GOUGH_CHART_MIRROR_URL GOUGH_CILIUM_VERSION GOUGH_CILIUM_CHART_SHA256 GOUGH_CILIUM_CRDS_URL GOUGH_CILIUM_CRDS_SHA256 GOUGH_CILIUM_CLI_URL \
    GOUGH_CILIUM_CLI_SHA256 GOUGH_JOIN_TOKEN_TTL GOUGH_REPORT_MODE \
    GOUGH_KUBE_PROXY_REPLACEMENT GOUGH_CILIUM_GATEWAY_API GOUGH_CILIUM_HUBBLE
  run_step node-prep prepare_node
  run_step control-plane-init controller_init
  run_step cilium controller_cilium
  run_step join-material controller_publish_join
  log "k8s-controller biome complete"
}

main "$@"

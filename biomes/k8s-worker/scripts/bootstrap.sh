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
SECRETS_FILE="$BIOME_DIR/secrets.env"
REPORT_DIR=/var/lib/gough
CURRENT_STEP=startup

# Delete a file with shred when available (overwrite then unlink), else rm; a missing file is fine.
remove_file_securely() {
  local path="$1"
  [[ -e "$path" ]] || return 0
  if command -v shred >/dev/null 2>&1; then
    shred -u "$path"
  else
    rm -f "$path"
  fi
}

# Write the host-visible outcome marker: biome-report.done on success (written last), biome-report.failed with a non-secret reason otherwise.
report_biome_outcome() {
  local rc="$1" marker
  install -d -m 0755 "$REPORT_DIR"
  umask 077
  if [[ "$rc" == 0 ]]; then
    marker="$REPORT_DIR/biome-report.done"
    printf 'k8s-worker biome complete\n' > "$marker"
    rm -f "$REPORT_DIR/biome-report.failed"
  else
    marker="$REPORT_DIR/biome-report.failed"
    printf 'k8s-worker biome failed during step %s (exit %s); see /var/log/gough-biome.log\n' "$CURRENT_STEP" "$rc" > "$marker"
    rm -f "$REPORT_DIR/biome-report.done"
  fi
}

# EXIT trap: remove the secrets file and any rendered join config on success and on every failure path, then write the outcome marker.
cleanup_join_material() {
  local rc=$?
  remove_file_securely "$SECRETS_FILE"
  remove_file_securely "$JOIN_CONFIG"
  report_biome_outcome "$rc"
}

install -d -m 0755 "$STATE_DIR"
[[ -f "$LOG_FILE" ]] || install -m 0600 /dev/null "$LOG_FILE"
chmod 0600 "$LOG_FILE"
exec > >(tee -a "$LOG_FILE") 2>&1

trap cleanup_join_material EXIT
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
  if ! curl -q --fail --silent --show-error --location --retry 3 -o "$tmp" "$url"; then
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

# Verify the primary GPG fingerprint of the armored apt key against the pinned value (case-insensitive, full match).
verify_apt_key_fingerprint() {
  local keyfile="$1" want fprs primary count
  want="$(printf '%s' "$GOUGH_K8S_APT_KEY_FINGERPRINT" | tr '[:lower:]' '[:upper:]')"
  [[ "$want" =~ ^[0-9A-F]{40}$ ]] || die "k8s_apt_key_fingerprint is not 40 hex characters"
  fprs="$(gpg --batch --no-default-keyring --with-colons --show-keys "$keyfile" | awk -F: '$1 == "fpr" { print toupper($10) }')" \
    || die "gpg could not read the apt key at $keyfile"
  [[ -n "$fprs" ]] || die "no fingerprint found in the apt key"
  primary="$(printf '%s\n' "$fprs" | awk 'NR == 1')"
  count="$(printf '%s\n' "$fprs" | awk 'END { print NR }')"
  [[ "$primary" == "$want" ]] || die "apt key fingerprint mismatch: expected ${want}, got ${primary}"
  log "apt key fingerprint verified (${count} fingerprint(s) examined)"
}

# Install the Kubernetes apt repo from the mirror, verifying the signing key by checksum and fingerprint.
prep_k8s_apt() {
  install -d -m 0755 /etc/apt/keyrings
  local armored=/etc/apt/keyrings/gough-kubernetes.asc
  fetch_verified "$GOUGH_K8S_APT_KEY_URL" "$GOUGH_K8S_APT_KEY_SHA256" "$armored"
  verify_apt_key_fingerprint "$armored"
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
      # server names the default host containerd dials; pinning it to the mirror keeps pulls off the public registry.
      if [[ "$GOUGH_ALLOW_REGISTRY_FALLBACK" == "true" ]]; then
        printf 'server = "https://%s"\n\n' "$upstream"
      else
        printf 'server = "%s"\n\n' "$GOUGH_REGISTRY_MIRROR"
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
  local cgroup_count
  cgroup_count="$(awk '/SystemdCgroup = true/ { n++ } END { print n + 0 }' /etc/containerd/config.toml)"
  log "containerd SystemdCgroup = true matches: ${cgroup_count}"
  [[ "$cgroup_count" == "1" ]] || die "expected exactly one SystemdCgroup = true in containerd config, found ${cgroup_count}"
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
  CURRENT_STEP="$name"
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

# Read one GOUGH_ value from the secrets file into SECRET_VALUE by parsing (never sourcing), so it is never exported.
# Not run in a subshell: a die here must exit the script, not a command substitution.
read_secret() {
  local key="$1" line value mode
  [[ -f "$SECRETS_FILE" ]] || die "secrets file missing: ${SECRETS_FILE}; the launcher must push secrets.env (mode 0600) before first boot"
  mode="$(stat -c %a "$SECRETS_FILE")"
  [[ "$mode" == "600" ]] || die "secrets file ${SECRETS_FILE} must be mode 0600, found ${mode}"
  line="$(awk -v k="${key}=" 'index($0, k) == 1 { print; exit }' "$SECRETS_FILE")"
  [[ -n "$line" ]] || die "secrets file has no ${key}"
  value="${line#"${key}="}"
  value="${value#\'}"
  value="${value%\'}"
  SECRET_VALUE="$value"
}

# Disable and mask ssh.service and ssh.socket unless allow_ssh=true; each unit is checked for existence before acting.
mask_ssh_units() {
  if [[ "${GOUGH_ALLOW_SSH:-false}" == "true" ]]; then
    log "allow_ssh=true; leaving ssh units unchanged"
    return 0
  fi
  local unit present
  for unit in ssh.service ssh.socket; do
    present="$(systemctl list-unit-files --no-legend "$unit" | awk -v u="$unit" '$1 == u { print $1 }')"
    if [[ -z "$present" ]]; then
      log "ssh unit ${unit} not present; nothing to mask"
      continue
    fi
    systemctl disable --now "$unit"
    systemctl mask "$unit"
    log "disabled and masked ${unit}"
  done
}

# Join the cluster using a config file so the bootstrap token never appears in process arguments.
worker_join() {
  if [[ -f /etc/kubernetes/kubelet.conf ]]; then
    systemctl is-active --quiet kubelet || die "node already joined but kubelet is not active; inspect kubelet logs (no re-join with a stale token)"
    log "node already joined and kubelet active; not re-joining"
    return 0
  fi
  read_secret GOUGH_JOIN_TOKEN
  local join_token="$SECRET_VALUE"
  [[ "$join_token" =~ ^[a-z0-9]{6}\.[a-z0-9]{16}$ ]] || die "join token has invalid format"
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
    token: "${join_token}"
    apiServerEndpoint: "${GOUGH_JOIN_ENDPOINT}"
    caCertHashes:
      - "${GOUGH_JOIN_CA_CERT_HASH}"
nodeRegistration:
  name: "$(hostname)"
  ignorePreflightErrors:
    - SystemVerification
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
  if ! kubeadm join --config "$JOIN_CONFIG"; then
    die "kubeadm join failed. If the join token expired (join_token_ttl), request a fresh join token from the hub and rerun"
  fi
  remove_file_securely "$JOIN_CONFIG"
  remove_file_securely "$SECRETS_FILE"
  log "join material removed from the instance"
}

# Confirm kubelet is running after join; node Ready is verified by the controller side.
worker_verify() {
  systemctl is-active --quiet kubelet || die "kubelet is not active after join"
  log "kubelet active; node registration complete (Ready is confirmed from the control plane)"
}

# Entry point: prepare the node, join the cluster, verify kubelet.
main() {
  require_params GOUGH_HOSTNAME GOUGH_APT_MIRROR GOUGH_K8S_APT_REPO GOUGH_K8S_APT_KEY_URL \
    GOUGH_K8S_APT_KEY_SHA256 GOUGH_K8S_APT_KEY_FINGERPRINT GOUGH_K8S_VERSION GOUGH_K8S_PACKAGE_VERSION \
    GOUGH_CONTAINERD_PACKAGE_VERSION GOUGH_REGISTRY_MIRROR GOUGH_MIRRORED_REGISTRIES \
    GOUGH_JOIN_ENDPOINT GOUGH_JOIN_CA_CERT_HASH
  mask_ssh_units
  run_step node-prep prepare_node
  run_step kubeadm-join worker_join
  run_step kubelet-verify worker_verify
  log "k8s-worker biome complete"
}

main "$@"

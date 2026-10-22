# Runbook: Kubernetes (kubeadm + Cilium) in LXD containers on a bridged LACP bond

Built interactively on 2026-10-21/22. Intended as input for the gough project, which will eventually automate these steps.

## 0. Status and trust levels

Read this first. Not everything below was verified end to end.

**Verified working (output seen):**
- Host `br0` over `bond0` (802.3ad) brought up after a reboot.
- LXD containers on `br0` with DHCP leases from the LAN.
- `kubeadm init` and `kubeadm join` succeeded; both nodes `Ready`.
- Cilium 1.20.1 agent, operator and envoy `OK`; CoreDNS `Running`.
- Download speed fixed after replacing a bad 10G DAC.

**Not verified / assumed (confirm before automating):**
- Gateway `192.168.8.1` and DNS `192.168.8.1` are **assumptions**. Replace with real values.
- Management port `192.168.4.31/24`: the `/24` is an assumption, and it was provided as YAML but never confirmed applied.
- The container static IPs came from the `k8s-controller` / `k8s-worker` LXD profiles. The nodes show `192.168.8.41` / `.51` in the cluster, so that worked, but profile attach and cloud-init rerun output was not pasted.
- The LXD privilege settings (section 3) were given as a loop. Confirm they are present with `lxc profile show`.
- `pkgs.k8s.io` v1.35 repo path was assumed from the version in use; it worked.
- `cilium connectivity test` was **not run**. Cross-node pod networking is untested beyond pods reaching `Running`.
- Behaviour across reboots (bpffs mount, `kmsg-fix.service`, kube-proxy ConfigMap) is expected but not tested.

## 1. Target topology

| Component | Value |
|---|---|
| Hypervisor | `dal2a-k8s1`, Ubuntu 26.04, kernel 7.0.0-38-generic |
| Host IP (on `br0`) | `192.168.8.31/24` |
| Management port | `enp88s0` = `192.168.4.31/24`, no default route, no DNS |
| Uplink | `bond0` = `enp3s0f0np0` + `enp3s0f1np1`, 802.3ad, 2x10G to Ubiquiti aggregation switch |
| Controller | LXD container `k8s-controller`, `192.168.8.41` |
| Worker | LXD container `k8s-worker`, `192.168.8.51` |
| Pod CIDR | `10.244.0.0/16` |
| Kubernetes | 1.35.9 (apt packages from `pkgs.k8s.io`) |
| CNI | Cilium 1.20.1, `ipam.mode=kubernetes` |
| LXD NAT bridge | `lxdbr0` `10.140.132.0/24` (unused by the cluster) |

Avoid `192.168.0.0/16` for pods (Calico default) because it overlaps the LAN.

## 2. Host netplan (bond + bridge + management port)

File: `/etc/netplan/<file>.yaml` (back it up first).

```yaml
network:
  bonds:
    bond0:
      dhcp4: false
      interfaces:
      - enp3s0f0np0
      - enp3s0f1np1
      parameters:
        lacp-rate: slow
        mode: 802.3ad
        transmit-hash-policy: layer2
  bridges:
    br0:
      interfaces:
      - bond0
      addresses:
      - 192.168.8.31/24
      routes:
      - to: default
        via: 192.168.8.1        # ASSUMED - confirm
      nameservers:
        addresses:
        - 192.168.8.1           # ASSUMED - confirm
      parameters:
        stp: false
        forward-delay: 0
  ethernets:
    enp1s0f0:
      match:
        macaddress: f4:e9:d4:9f:81:e0
      set-name: enp1s0f0
    enp1s0f1:
      accept-ra: true
    enp3s0f0np0: {}
    enp3s0f1np1: {}
    enp88s0:
      dhcp4: false
      dhcp6: false
      accept-ra: false
      addresses:
      - 192.168.4.31/24         # ASSUMED /24 - confirm
    enp89s0:
      accept-ra: true
  version: 2
  wifis: {}
```

Apply:

```bash
sudo cp /etc/netplan/<file>.yaml /etc/netplan/<file>.yaml.bak
sudo netplan generate
sudo netplan apply      # see lessons: netplan try refuses bond parameter changes
```

Verify:

```bash
bridge link             # bond0 ... master br0 ... forwarding
ip -br addr             # br0 UP with 192.168.8.31/24, bond0 has no IP
ip route                # one default via br0
cat /proc/net/bonding/bond0
```

Notes:
- `lacp-rate` is how often LACPDUs are sent: `slow` = 30s, `fast` = 1s. Mismatch with the switch is harmless. Left at `slow`. Changing bond parameters likely needs a reboot.
- `layer2` hash sends all traffic to one gateway MAC down one member. Fine for a 2.5G internet link.
- STP is off and forward-delay 0 since there is a single uplink.

## 3. LXD profiles

```bash
lxc profile create k8s-controller
lxc profile create k8s-worker
```

Network (bridged NIC on `br0` + static IP via cloud-init). Controller shown; the worker uses `192.168.8.51/24`.

```bash
lxc profile device add k8s-controller eth0 nic nictype=bridged parent=br0 name=eth0
lxc profile set k8s-controller cloud-init.network-config="$(cat <<'EOF'
version: 2
ethernets:
  eth0:
    dhcp4: false
    addresses:
    - 192.168.8.41/24
    routes:
    - to: default
      via: 192.168.8.1
    nameservers:
      addresses:
      - 192.168.8.1
EOF
)"
```

Privileges needed for Kubernetes inside LXC (apply to both profiles):

```bash
for p in k8s-controller k8s-worker; do
  lxc profile set $p security.nesting=true
  lxc profile set $p security.privileged=true
  lxc profile set $p linux.kernel_modules=ip_tables,ip6_tables,nf_nat,overlay,br_netfilter
  lxc profile set $p raw.lxc="lxc.apparmor.profile=unconfined
lxc.cap.drop=
lxc.cgroup.devices.allow=a
lxc.mount.auto=proc:rw sys:rw cgroup:rw"
done
```

`security.privileged` plus an unconfined AppArmor profile weakens container isolation. Acceptable for a lab, not for a shared or production host.

Launch/attach (role profile last so its `eth0` overrides `default`):

```bash
lxc launch ubuntu:24.04 k8s-controller -p default -p k8s-controller
lxc launch ubuntu:24.04 k8s-worker -p default -p k8s-worker
# existing containers:
lxc profile assign k8s-controller default,k8s-controller
lxc exec k8s-controller -- cloud-init clean --logs --configs network
lxc restart k8s-controller
```

cloud-init only applies network config on first boot, so rerun with `cloud-init clean` for containers that already booted. Check `/etc/netplan/` inside for a leftover DHCP file (`10-lxc.yaml`) that would add a second address. LXD's `ipv4.address` does not work on a bridged NIC attached to an unmanaged host bridge.

Exec into a container: `lxc exec <name> -- bash`.

## 4. Node bootstrap (run on both nodes)

Do not use the snap `kubeadm` / `kubelet` / `kubectl`. The snap `kubelet` provides no `kubelet.service` systemd unit, so `kubeadm init` fails.

```bash
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

# containerd with systemd cgroup driver
apt-get update
apt-get install -y containerd apt-transport-https ca-certificates curl gpg
mkdir -p /etc/containerd
containerd config default > /etc/containerd/config.toml
sed -i 's/SystemdCgroup = false/SystemdCgroup = true/' /etc/containerd/config.toml
systemctl enable --now containerd
systemctl restart containerd

# IP forwarding
sysctl -w net.ipv4.ip_forward=1
echo 'net.ipv4.ip_forward=1' > /etc/sysctl.d/k8s.conf

# /dev/kmsg workaround for kubelet in LXC
cat > /etc/systemd/system/kmsg-fix.service <<'EOF'
[Unit]
Description=Link /dev/kmsg to /dev/console for kubelet in LXC
Before=kubelet.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/bin/ln -sf /dev/console /dev/kmsg

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now kmsg-fix.service

# Kubernetes apt packages
mkdir -p /etc/apt/keyrings
curl -fsSL https://pkgs.k8s.io/core:/stable:/v1.35/deb/Release.key | gpg --dearmor --yes -o /etc/apt/keyrings/kubernetes-apt-keyring.gpg
echo 'deb [signed-by=/etc/apt/keyrings/kubernetes-apt-keyring.gpg] https://pkgs.k8s.io/core:/stable:/v1.35/deb/ /' > /etc/apt/sources.list.d/kubernetes.list
apt-get update
apt-get install -y kubelet kubeadm kubectl
apt-mark hold kubelet kubeadm kubectl
systemctl enable kubelet

ls -l /dev/kmsg /var/run/containerd/containerd.sock
which kubeadm
```

Pasting a `set -e` script into an interactive shell closes the shell on failure. Save it to a file and run `bash file` instead.

BPF filesystem (needed by Cilium). Check on both nodes, and persist it:

```bash
mount | grep bpf || mount bpffs /sys/fs/bpf -t bpf
grep -q '/sys/fs/bpf' /etc/fstab || echo 'bpffs /sys/fs/bpf bpf defaults 0 0' >> /etc/fstab
```

## 5. Cluster

Controller:

```bash
kubeadm init \
  --apiserver-advertise-address=192.168.8.41 \
  --pod-network-cidr=10.244.0.0/16 \
  --ignore-preflight-errors=SystemVerification

mkdir -p $HOME/.kube
cp /etc/kubernetes/admin.conf $HOME/.kube/config
```

`SystemVerification` is ignored because the kernel config module is not available in the container; the warning is harmless here.

Worker (get the command from the controller with `kubeadm token create --print-join-command`):

```bash
kubeadm join 192.168.8.41:6443 --token <TOKEN> \
  --discovery-token-ca-cert-hash sha256:<HASH> \
  --ignore-preflight-errors=SystemVerification
```

Nodes stay `NotReady` until a CNI is installed.

### kube-proxy fix for LXC (required)

`kube-proxy` crashes with `open /proc/sys/net/netfilter/nf_conntrack_max: permission denied`. Stop it managing conntrack:

```bash
kubectl -n kube-system edit configmap kube-proxy
```

In `config.conf`, set:

```yaml
    conntrack:
      maxPerCore: 0
      min: 0
      tcpEstablishedTimeout: 0s
      tcpCloseWaitTimeout: 0s
```

```bash
kubectl -n kube-system delete pod -l k8s-app=kube-proxy
```

Do this before or right after installing the CNI. Without it, the service IP `10.96.0.1` is unreachable and Cilium's init containers loop.

### Cilium

```bash
CILIUM_CLI_VERSION=$(curl -s https://raw.githubusercontent.com/cilium/cilium-cli/main/stable.txt)
curl -L --fail --remote-name-all https://github.com/cilium/cilium-cli/releases/download/${CILIUM_CLI_VERSION}/cilium-linux-amd64.tar.gz
tar xzvf cilium-linux-amd64.tar.gz -C /usr/local/bin
rm cilium-linux-amd64.tar.gz

cilium install --set ipam.mode=kubernetes
cilium status --wait
kubectl get nodes
```

`ipam.mode=kubernetes` makes Cilium use the per-node podCIDRs from kubeadm (`10.244.0.0/16`). The default `cluster-pool` would allocate from `10.0.0.0/8` instead. If agents sit in back-off after you fix a lower layer, delete the pods so they retry: `kubectl -n kube-system delete pod -l k8s-app=cilium`.

Hubble and ClusterMesh are disabled. Hubble (`cilium hubble enable --ui`) is optional observability; ClusterMesh is only for joining multiple clusters.

## 6. Verification checklist

| Layer | Command | Expect |
|---|---|---|
| Bond | `cat /proc/net/bonding/bond0` | both members up, 10000 Mbps, same Aggregator ID, no churn |
| Bridge | `bridge link` | `bond0 ... master br0 ... forwarding` |
| Host IP | `ip -br addr` | `br0` has `192.168.8.31/24` |
| Containers | `lxc list` | controller `.41`, worker `.51` |
| Runtime | `ls /var/run/containerd/containerd.sock` | exists |
| Kubelet | `systemctl status kubelet` | `active (running)` |
| kube-proxy | `kubectl -n kube-system get pods -l k8s-app=kube-proxy` | Running, 0 restarts |
| Cilium | `cilium status --wait` | Cilium, Operator, Envoy `OK` |
| Nodes | `kubectl get nodes` | both `Ready` |
| Pods | `kubectl get pods -A` | all `Running` |
| Network (TODO) | `cilium connectivity test` | not yet run |

## 7. Lessons learned

1. **`netplan try` refuses bond changes.** It reports `reverting custom parameters for bridges and bonds is not supported`. Use `netplan generate` then `netplan apply`, with a backup and console access.
2. **A running bond cannot be re-parented live.** After `netplan apply`, `br0` had the IP but `bond0` was not enslaved to it (`br0` `NO-CARRIER`). `ip link set bond0 master br0` returned `Device or resource busy`, even after taking the bond down. A reboot let networkd build bond and bridge in the right order.
3. **Do not install `bridge-utils`.** Netplan and networkd use the kernel bridge directly. Use `bridge link` and `ip -d link` from iproute2.
4. **Slow downloads were a bad 10G DAC.** Symptoms: 2-7 MB/s on a 2.5 Gbps line, switch port errors, while the host showed 0 interface errors. Replacing the cable made a 100 MB test near-instant. Diagnostic order: download from host and container, check DNS time, check bond status, test from another LAN device, check switch port counters.
5. **The test endpoint matters.** `speed.cloudflare.com/__down?bytes=500000000` returned 1 byte. Use a real file such as `http://cachefly.cachefly.net/100mb.test` and print `%{http_code} %{size_download}`.
6. **Snap `kubelet` has no systemd unit.** `kubeadm init` printed `unable to start the kubelet service: [exit status 5]` and timed out. Use the apt packages and `apt-mark hold` them.
7. **Bash caches command paths.** After removing the snap, `kubeadm` still resolved to `/snap/bin/kubeadm`. Run `hash -r`.
8. **`kubeadm init` needs a CRI first.** `[ERROR CRI]` means containerd is not installed or running.
9. **kubelet in LXC needs `/dev/kmsg`.** The journal showed `failed to create kubelet: open /dev/kmsg: no such file or directory`. The `kmsg-fix.service` symlink fixes it. Always run `kubeadm reset -f` before retrying a failed init.
10. **kube-proxy needs its conntrack config zeroed in LXC** (section 5). Without it, the Cilium agents sit at `Init:0/6`, the operator fails readiness, and CoreDNS stays `Pending`.
11. **`lxc profile set` cannot read stdin with `-`.** Use `key="$(cat <<'EOF' ... EOF)"`.
12. **Profiles must be created first.** `lxc profile device add` on a missing profile returns `Profile not found`.
13. **`ping` is not installed** on the minimal image. Use `curl`, `getent hosts`, and `apt-get install iputils-ping` if needed.
14. **A `Pending`/`Init` Cilium pod points to a lower layer.** Read `kubectl describe` Events, then check kube-proxy and kubelet before changing Cilium.

## 8. Open items for gough

- Replace the assumed gateway/DNS/management prefix with real values (inputs, not hardcoded).
- Verify the LXD profile settings and the cloud-init static IP flow on a fresh launch.
- Run `cilium connectivity test`, record which checks fail in LXC.
- Test a full host reboot: bond/bridge, `kmsg-fix.service`, bpffs mount, kube-proxy ConfigMap, Cilium recovery.
- Automate join-token handling and make the node bootstrap script idempotent.
- Decide whether to enable Hubble.
- Consider whether `security.privileged=true` is acceptable beyond the lab.

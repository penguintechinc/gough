# Biomes

A biome is a versioned directory that turns a fresh LXD instance into a workload. The gough hub caches pinned releases of this repo and renders a biome per instance into LXD cloud-init user-data plus a profile.

## Layout

```
biomes/
├── _lib/render.py                  # renderer: biome dir + params JSON -> user-data, network-config, profile.json
├── tests/                          # unittest: renderer + static script checks
├── k8s-controller/
│   ├── biome.yaml                  # manifest: lxd profile, parameter schema
│   ├── cloud-init/user-data.yaml.tmpl
│   ├── cloud-init/network-config.yaml.tmpl
│   └── scripts/bootstrap.sh        # runs once in the instance; idempotent
└── k8s-worker/                     # same shape
```

Run tests: `python3 -I -W error -m unittest discover -s biomes/tests -p 'test_*.py'` (needs PyYAML and shellcheck; a missing tool fails the test).

Render: `python3 -I biomes/_lib/render.py --biome biomes/k8s-worker --params params.json --out OUT`. Writes `user-data`, `network-config`, `profile.json` (mode 0600), or exits 1 with no output.

## Manifest (`biome.yaml`)

| Key | Meaning |
|---|---|
| `name`, `version`, `description` | Identity. Version is the biome release, not the Kubernetes version. |
| `lxd.image` | `{{ image_alias }}`, default `ubuntu/26.04`. |
| `lxd.profile_name` | Per-biome profile: `gough-k8s-controller` or `gough-k8s-worker`. |
| `lxd.config` | `security.nesting`, `security.privileged`, `linux.kernel_modules`, `raw.lxc` (runbook section 3). |
| `lxd.devices.eth0` | Bridged NIC on `{{ bridge }}`. |
| `parameters[]` | `name`, `type` (`string`, `bool`), `required`, `default`, `pattern` (full-match regex), `secret`, `description`. |

Placeholders are `{{ name }}`. Parameters are the only input. Values containing control characters, `"` or `\` are rejected, so templates can quote them safely.

## Co-location on one hypervisor

The bootstrap host runs `k8s-controller` and `k8s-worker` on the same hypervisor. The admin may remove the worker later.

- No fixed names: hostname, LXD instance name and static IP are required parameters. Templates contain no node names or addresses (a test enforces this).
- Per-biome LXD profiles: `gough-k8s-controller` and `gough-k8s-worker` are different profiles, so the two configs never collide. Each instance gets its profile at launch.
- No shared host paths: all writes happen inside the instance (`/etc`, `/var/lib/gough/biome`, `/opt/gough/biome`). Host-level effects are limited to what the profiles request (kernel module loads, which are idempotent) and the host bridge the NIC attaches to.
- Worker join material is a parameter set (`join_endpoint`, `join_token`, `join_ca_cert_hash`) supplied by the hub. Nothing is read from the controller's disk.
- Removing the worker: delete the worker instance and its profile. The controller's Cilium DaemonSet and the controller are unaffected.
- Not supported: two controllers on one host. The controller profile name is fixed per biome.

## Parameters: k8s-controller

| Parameter | Req | Default | Notes |
|---|---|---|---|
| `hostname`, `instance_name` | yes | - | Per instance; DNS label. |
| `static_ip` | yes | - | `a.b.c.d/nn`. |
| `gateway`, `dns_servers` | yes | - | No defaults. `dns_servers` is comma-separated. |
| `apiserver_advertise_address` | yes | - | This instance's IP. |
| `control_plane_endpoint` | yes | - | `host:port`, reachable from every node. Also Cilium `k8sServiceHost`. |
| `bridge` | no | `br0` | Host bridge. |
| `image_alias` | no | `ubuntu/26.04` | |
| `allow_privileged` | no | `false` | Must be `true`, or rendering fails (see Privilege). |
| `apt_mirror`, `k8s_apt_repo`, `k8s_apt_key_url`, `k8s_apt_key_sha256` | yes | - | Ubuntu archive and Kubernetes apt mirrors. |
| `k8s_version` | no | `1.35.9` | Bootstrap fails if `kubeadm version` differs. |
| `k8s_package_version` | no | `1.35.9-1.1` | Exact apt version. Confirm against the mirror before release. |
| `containerd_package_version` | yes | - | Exact Ubuntu archive version. No default: read it from the mirror. |
| `registry_mirror` | yes | - | Serves upstream paths (`registry.k8s.io/...`, `quay.io/cilium/...`). |
| `mirrored_registries` | no | `docker.io,quay.io,registry.k8s.io,ghcr.io` | Routed through the mirror by containerd `hosts.toml`. |
| `internal_ca_url`, `internal_ca_sha256` | no | empty | Private mirror CA. Both or neither. |
| `pod_cidr`, `service_cidr` | no | `10.244.0.0/16`, `10.96.0.0/12` | Avoid `192.168.0.0/16`. |
| `chart_mirror_url` | yes | - | Base URL; chart is `<base>/cilium-<cilium_version>.tgz`. |
| `cilium_version` | no | `1.20.1` | Verified in the runbook. Hub catalog still says 1.19.1; update it. |
| `cilium_chart_sha256`, `cilium_cli_url`, `cilium_cli_sha256` | yes | - | Pinned by checksum. |
| `join_token_ttl` | no | `2h` | TTL of the minted worker join token. |
| `report_url`, `report_bootstrap_token` | yes | - | Hub contract below. Token is secret. |

## Parameters: k8s-worker

| Parameter | Req | Default | Notes |
|---|---|---|---|
| `hostname`, `instance_name`, `static_ip`, `gateway`, `dns_servers` | yes | - | As controller. |
| `join_endpoint` | yes | - | `host:port` of the control plane. |
| `join_token` | yes | - | `[a-z0-9]{6}.[a-z0-9]{16}`, secret. |
| `join_ca_cert_hash` | yes | - | `sha256:<64 hex>`. |
| `bridge`, `image_alias`, `allow_privileged` | no | as controller | |
| `apt_mirror`, `k8s_apt_repo`, `k8s_apt_key_url`, `k8s_apt_key_sha256`, `k8s_version`, `k8s_package_version`, `containerd_package_version`, `registry_mirror`, `mirrored_registries`, `internal_ca_url`, `internal_ca_sha256` | as controller | | Must match the controller's versions. |

Parameters land in `/opt/gough/biome/params.env` (mode 0600) as `GOUGH_<NAME>=<shell-quoted>`. Secrets live there on the instance only, not in the profile.

## Airgap: artifacts and sources

Nothing is fetched from github.com, raw.githubusercontent.com, helm.cilium.io or pkgs.k8s.io unless that is the configured mirror.

| Artifact | Used by | Source (mirror) | Pin |
|---|---|---|---|
| LXD image `ubuntu/26.04` | launch | LXD image server or `lxc image import` | alias or fingerprint |
| Ubuntu archive packages (`ca-certificates`, `curl`, `gpg`, `conntrack`, `socat`, `ebtables`, `ethtool`, `iproute2`, `containerd`) | both | `apt_mirror` | containerd exact version |
| Ubuntu archive keyring | apt | image (`/usr/share/keyrings/ubuntu-archive-keyring.gpg`) | image |
| Kubernetes apt key | apt | `k8s_apt_key_url` | sha256 |
| Kubernetes packages `kubelet`, `kubeadm`, `kubectl` | both | `k8s_apt_repo` (minor line) | exact version |
| Kubernetes control-plane and pause images | controller (init), both (pause) | `registry_mirror`, path `registry.k8s.io/...` | Kubernetes version (kubeadm) |
| Cilium chart `cilium-<ver>.tgz` (includes CRDs) | controller | `chart_mirror_url` | sha256 |
| Cilium images (`quay.io/cilium/cilium`, `operator-generic`, `cilium-envoy`) | both nodes | `registry_mirror`, path `quay.io/cilium/...` | chart-pinned tags/digests |
| cilium-cli linux-amd64 tarball | controller | `cilium_cli_url` | sha256 |
| Optional internal CA PEM | both | `internal_ca_url` | sha256 |

The registry mirror must present upstream repository paths. A pull-through cache that keeps the same namespaces works. containerd `hosts.toml` redirects pulls, so chart values keep upstream image names.

## Hub contract (must be implemented by gough)

The controller's bootstrap POSTs once the control plane and Cilium are healthy.

```
POST {report_url}
Authorization: Bearer {report_bootstrap_token}   (sent via curl --config, not argv)
Content-Type: application/json

{"biome":"k8s-controller","hostname":"...","join_endpoint":"host:port",
 "join_token":"abcdef.0123456789abcdef","ca_cert_hash":"sha256:<64 hex>",
 "cilium_version":"1.20.1"}
```

Hub obligations:

- Authenticate the bearer token against the one-time token issued for this instance. Unknown or expired token: `401`.
- Store the join material against the controller instance. It is the only source for a worker's `join_*` parameters.
- Accept an identical repeat (the controller retries on network failure): `204`. A differing body for a consumed token: `409`.
- Consume the bootstrap token after the first accepted report. It is not reusable for other instances.
- Never log `join_token` or the bearer. Never return the join token to any caller except the installer that renders the worker biome.
- The bootstrap retries 5 times with 5 s delay, so `report_url` must be reachable from the controller.

## Runbook lessons mapped to bootstrap lines

| Runbook lesson (`k8s-lxd-cluster-runbook.md`) | Implementation |
|---|---|
| Use apt packages, not snap kubelet (sec. 4, lesson 6) | `prep_packages`, `apt-mark hold` (`bootstrap.sh` ~L80-90) |
| Snap `kubeadm` shadows apt (lesson 7) | Apt-only install; `kubeadm version` check (L89) |
| containerd SystemdCgroup=true (sec. 4) | `prep_containerd` L116-117 |
| ip_forward sysctl (sec. 4) | `prep_kernel_bits` L127-128 |
| `/dev/kmsg` shim for kubelet in LXC (lesson 9) | `kmsg-fix.service` L129-143 |
| bpffs mount and fstab (sec. 4) | `prep_kernel_bits` L144-145 |
| `--ignore-preflight-errors=SystemVerification` (sec. 5) | `controller_init` L203; worker join config |
| `kubeadm reset -f` before retry (lesson 9) | `kubeadm_reset_if_partial` L183; worker reset L190 |
| kube-proxy conntrack breakage (sec. 5, lesson 10) | Avoided: `--skip-phases=addon/kube-proxy` L202; Cilium `kubeProxyReplacement=true` |
| Cilium `ipam.mode=kubernetes` (sec. 5) | `cilium install --set ipam.mode=kubernetes` |
| Cilium CRDs before operator (plan_compiler lesson) | `kubectl apply --server-side` + `kubectl wait Established` L219-226 |
| `cilium status --wait` readiness with loud failure | L247 (`--wait-duration 15m`); `set -e` makes timeout fatal |
| Static IP from profile, not DHCP (sec. 3) | `network-config.yaml.tmpl` |
| Join token never in repo (task requirement) | Minted at runtime (L254), reported over HTTPS, stored 0600 |

Deliberately not reproduced: the runbook's `ubuntu:24.04` launch example (the host is 26.04); the Hubble enable step (disabled, `hubble.enabled=false`); the Gateway API pre-apply the hub catalog performs (`gatewayAPI.enabled=false`).

## Privilege

`security.privileged`, the unconfined AppArmor profile and `cgroup:rw` mounts are required for kubelet and Cilium in LXC. They weaken isolation. `allow_privileged` must be `true`, which is an explicit operator decision recorded in the render inputs. Acceptable for a lab, not for shared or production hosts.

## Open items

- Confirm the Cilium chart tgz contains `cilium/crds/*.yaml` (bootstrap fails loudly if not).
- Confirm `k8s_package_version` (`1.35.9-1.1`) and `containerd_package_version` against the actual mirror.
- Cilium 1.20.1 end-to-end is not verified in this repo; run `cilium connectivity test` on the live cluster.
- kubeProxyReplacement path is not yet verified in LXC. The runbook verified the kube-proxy path only.
- Worker Ready is confirmed by the controller side, not by the worker biome.
- The renderer needs PyYAML. `gough-install.py` is stdlib-only, so the installer needs a decision: vendor PyYAML or use a JSON manifest.

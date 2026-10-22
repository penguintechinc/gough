# Biomes

A biome is a versioned directory that turns a fresh LXD instance into a workload. The gough hub caches pinned releases of this repo and renders a biome per instance into LXD cloud-init user-data, network-config and an LXD profile.

## Layout

```
biomes/
├── _lib/render.py                  # stdlib-only renderer: biome dir + params JSON -> user-data, network-config, profile.json
├── tests/                          # unittest: renderer + static script checks
├── k8s-controller/
│   ├── biome.json                  # manifest: lxd profile, parameter schema
│   ├── cloud-init/user-data.yaml.tmpl
│   ├── cloud-init/network-config.yaml.tmpl
│   └── scripts/bootstrap.sh        # runs once in the instance; idempotent
└── k8s-worker/                     # same shape
```

- Stdlib only. The renderer runs on bare hosts (the installer imports it); no pip. A test fails if any module under `_lib` imports a non-stdlib package.
- Manifests are JSON (`biome.json`). Templates are plain text.
- Tests: `python3 -I -W error -m unittest discover -s biomes/tests -p 'test_*.py'`. Needs `shellcheck` on PATH; a missing tool fails the test.
- Render: `python3 -I biomes/_lib/render.py --biome biomes/k8s-worker --params params.json --out OUT` writes `user-data`, `network-config`, `profile.json` (mode 0600), or exits 1 with no output.

## Manifest (`biome.json`)

| Key | Meaning |
|---|---|
| `name`, `version`, `description` | Identity. Version is the biome release, not the Kubernetes version. |
| `lxd.image` | `{{ image_alias }}`, default `ubuntu/26.04`. |
| `lxd.profile_name` | Per-biome profile: `gough-k8s-controller` or `gough-k8s-worker`. |
| `lxd.config` | `security.nesting`, `security.privileged`, `linux.kernel_modules`, `raw.lxc` (runbook section 3). |
| `lxd.devices.eth0` | Bridged NIC on `{{ bridge }}`. |
| `parameters[]` | `name`, `type` (`string` or `bool`), `required`, `default`, `pattern` (full-match regex), `scheme` (`mirror`, see below), `secret`, `description`. |

Placeholders are `{{ name }}`. Parameters are the only input. Values containing control characters, `"` or `\` are rejected, so templates can quote them safely.

### Cleartext policy

- `report_url` must match `^https://\S+$`. It carries a bearer token. There is no override.
- Mirror URLs (`scheme: mirror`: apt, Kubernetes apt repo and key, registry, chart, Cilium CLI, internal CA, Gateway API CRDs) must be `https://`.
- `allow_insecure_mirrors: true` permits `http://` mirror URLs (common in airgapped labs). The bootstrap logs a warning. Artifacts are still sha256-verified; the report URL is never affected.

## Co-location on one hypervisor

The bootstrap host runs `k8s-controller` and `k8s-worker` on the same hypervisor. The admin may remove the worker later.

- No fixed names: hostname, LXD instance name and static IP are required parameters. Templates contain no node names or addresses (a test enforces this).
- Per-biome LXD profiles: `gough-k8s-controller` and `gough-k8s-worker` are different profiles, so the two configs never collide. Each instance gets its profile at launch.
- No shared host paths: all writes happen inside the instance (`/etc`, `/var/lib/gough/biome`, `/opt/gough/biome`). Host-level effects are limited to what the profiles request (kernel module loads, idempotent) and the host bridge the NIC attaches to.
- Worker join material is a parameter set (`join_endpoint`, `join_token`, `join_ca_cert_hash`) supplied by the hub. Nothing is read from the controller's disk.
- Removing the worker: delete the worker instance and its profile. The controller and Cilium are unaffected.
- Not supported: two controllers on one host. The controller profile name is fixed per biome.

## Orchestrator decisions

Set by the gough orchestrator for this release. Change them only through the coordinator.

| Decision | Setting | Why |
|---|---|---|
| Renderer is stdlib-only | Manifests are JSON; no PyYAML | The installer imports the renderer on bare hosts with no pip. |
| kube-proxy kept by default | `kube_proxy_replacement: false` | The runbook-verified path. Conntrack is zeroed through a `KubeProxyConfiguration` in the kubeadm config file, before the ConfigMap exists, rather than edited afterwards. |
| Cilium KPR opt-in | `kube_proxy_replacement: true` | Switches Cilium to `kubeProxyReplacement` and skips the `addon/kube-proxy` phase. Not verified in LXC. |
| Gateway API off | `cilium_gateway_api: false` | Needs its CRDs from the chart or file mirror (`gateway_api_crds_url`, `gateway_api_crds_sha256`). The bootstrap fails loudly if enabled without them. |
| Hubble off | `cilium_hubble: false` | Optional observability (runbook). |
| Cilium 1.20.1 | `cilium_version: 1.20.1` | Verified in the runbook. Gough follow-up: the hub catalog still pins 1.19.1 and must be updated to match. |

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
| `allow_privileged` | no | `false` | Must be `true`, or rendering fails. |
| `allow_insecure_mirrors` | no | `false` | Permits `http://` mirror URLs (warning logged). |
| `apt_mirror`, `k8s_apt_repo`, `k8s_apt_key_url`, `k8s_apt_key_sha256` | yes | - | Ubuntu archive and Kubernetes apt mirrors. |
| `k8s_version` | no | `1.35.9` | Bootstrap fails if `kubeadm version` differs. |
| `k8s_package_version` | no | `1.35.9-1.1` | Exact apt version. Confirm against the mirror before release. |
| `containerd_package_version` | yes | - | Exact Ubuntu archive version. No default: read it from the mirror. |
| `registry_mirror` | yes | - | Serves upstream paths (`registry.k8s.io/...`, `quay.io/cilium/...`). |
| `mirrored_registries` | no | `docker.io,quay.io,registry.k8s.io,ghcr.io` | Routed through the mirror by containerd `hosts.toml`. |
| `internal_ca_url`, `internal_ca_sha256` | no | empty | Private mirror CA. Installed only after sha256 verification. Both or neither. |
| `pod_cidr`, `service_cidr` | no | `10.244.0.0/16`, `10.96.0.0/12` | Avoid `192.168.0.0/16`. |
| `kube_proxy_replacement` | no | `false` | See Orchestrator decisions. |
| `chart_mirror_url` | yes | - | Base URL; chart is `<base>/cilium-<cilium_version>.tgz`. |
| `cilium_version` | no | `1.20.1` | |
| `cilium_chart_sha256`, `cilium_cli_url`, `cilium_cli_sha256` | yes | - | Pinned by checksum. |
| `cilium_crds_url`, `cilium_crds_sha256` | yes | - | Single CRD bundle on the mirror (https unless `allow_insecure_mirrors`). The chart tgz has no CRDs. Build it with the tool below. |
| `cilium_gateway_api` | no | `false` | Requires `gateway_api_crds_url` and `gateway_api_crds_sha256`. |
| `gateway_api_crds_url`, `gateway_api_crds_sha256` | no | empty | Standard-install Gateway API CRD manifest on the chart/file mirror. |
| `cilium_hubble` | no | `false` | Enables Hubble and Hubble relay. |
| `join_token_ttl` | no | `2h` | TTL of the report token minted for the worker join. |
| `report_url`, `report_bootstrap_token` | yes | - | `https` only. Hub contract below. Token is secret. |

## Parameters: k8s-worker

| Parameter | Req | Default | Notes |
|---|---|---|---|
| `hostname`, `instance_name`, `static_ip`, `gateway`, `dns_servers` | yes | - | As controller. |
| `join_endpoint` | yes | - | `host:port` of the control plane. |
| `join_token` | yes | - | `[a-z0-9]{6}.[a-z0-9]{16}`, secret. |
| `join_ca_cert_hash` | yes | - | `sha256:<64 hex>`. |
| `bridge`, `image_alias`, `allow_privileged`, `allow_insecure_mirrors` | no | as controller | |
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
| Cilium chart `cilium-<ver>.tgz` (no CRDs inside; verified: 0 of 166 entries) | controller | `chart_mirror_url` | sha256 |
| Cilium CRD bundle `cilium-crds-v<ver>.yaml` (22 CRDs from `pkg/k8s/apis/cilium.io/client/crds/{v2,v2alpha1}` at the tag) | controller | `cilium_crds_url`, built by `tools/build-cilium-crd-bundle.sh` on a connected machine | sha256 |
| Cilium images (`quay.io/cilium/cilium`, `operator-generic`, `cilium-envoy`) | both nodes | `registry_mirror`, path `quay.io/cilium/...` | chart-pinned tags/digests |
| cilium-cli linux-amd64 tarball | controller | `cilium_cli_url` | sha256 |
| Gateway API standard-install CRDs (only when `cilium_gateway_api`) | controller | `gateway_api_crds_url` (on the chart/file mirror) | sha256 |
| Optional internal CA PEM | both | `internal_ca_url` | sha256 |

The registry mirror must present upstream repository paths. A pull-through cache that keeps the same namespaces works. containerd `hosts.toml` redirects pulls, so chart values keep upstream image names.

## Building the Cilium CRD bundle (connected machine only)

```
tools/build-cilium-crd-bundle.sh 1.20.1 dist      # needs gh, authenticated
# -> dist/cilium-crds-v1.20.1.yaml (22 CRDs), prints sha256
```

Upload the file to the operator mirror and set `cilium_crds_url` and `cilium_crds_sha256` to the mirror URL and the printed hash. The airgapped host never runs this tool. Verified: v1.20.1 built with 22 files; sha256 `abb345439856d5aae0e5201e9fed3b72d7997095182941cb7e911a106e523d9d`.

## Hub contract (must be implemented by gough)

The controller's bootstrap POSTs once the control plane and Cilium are healthy. The body is passed to curl from a 0600 temp file, so it never appears in process arguments.

```
POST {report_url}                       (https only)
Authorization: Bearer {report_bootstrap_token}
Content-Type: application/json

{"biome":"k8s-controller","hostname":"...","join_endpoint":"host:port",
 "join_token":"abcdef.0123456789abcdef","ca_cert_hash":"sha256:<64 hex>",
 "cilium_version":"1.20.1"}
```

- The `join_token` in the report is a short-TTL token (`join_token_ttl`, default 2h) minted for this report. After a successful report, the controller deletes every other bootstrap token, including the default one `kubeadm init` created.
- Hub obligations:
  - Authenticate the bearer against the one-time token issued for this instance. Unknown or expired token: `401`.
  - Store the join material against the controller instance. It is the only source for a worker's `join_*` parameters.
  - Accept an identical repeat (the controller retries on network failure): `204`. A differing body for a consumed token: `409`.
  - Consume the bearer after the first accepted report. It is not reusable for other instances.
  - Never log `join_token` or the bearer. Return the join token only to the installer rendering the worker biome.
  - The controller retries 5 times with a 5 s delay, so `report_url` must be reachable from the controller over https.

## Security properties (both biomes)

| Concern | Control |
|---|---|
| Join token in the log | `kubeadm init` runs with `--skip-token-print`; its output goes to a 0600 file; failure tails are redacted. The biome log is created 0600. |
| Token or body in curl argv | Bearer and body are written to a 0600 temp file and passed via `--config` (`data-binary = "@file"`). Temp files are removed on return. A test asserts no curl line names a token. |
| Unverified CA trust anchor | `fetch_verified` downloads to a temp file, verifies sha256, and only then installs. On mismatch the temp file is removed and the script exits non-zero. Every `fetch_verified` use follows this. |
| Cleartext transport | `report_url` is https only. Mirror URLs are https unless `allow_insecure_mirrors` is set, which logs a warning. |
| Secrets in params.env | Stored 0600 root inside the instance only. |

## Runbook lessons mapped to bootstrap lines

| Runbook lesson (`k8s-lxd-cluster-runbook.md`) | Implementation |
|---|---|
| Use apt packages, not snap kubelet (sec. 4, lesson 6) | `prep_packages`, `apt-mark hold` (controller and worker `node-prep` block) |
| Snap `kubeadm` shadows apt (lesson 7) | Apt-only install; `kubeadm version` check in `prep_packages` |
| containerd SystemdCgroup=true (sec. 4) | `prep_containerd` |
| ip_forward sysctl (sec. 4) | `prep_kernel_bits` |
| `/dev/kmsg` shim for kubelet in LXC (lesson 9) | `kmsg-fix.service` in `prep_kernel_bits` |
| bpffs mount and fstab (sec. 4) | `prep_kernel_bits` |
| `--ignore-preflight-errors=SystemVerification` (sec. 5) | `InitConfiguration` in `write_kubeadm_config`; worker join config |
| `kubeadm reset -f` before retry (lesson 9) | `kubeadm_reset_if_partial`; worker reset in `worker_join` |
| kube-proxy conntrack breakage (sec. 5, lesson 10) | Default path: `KubeProxyConfiguration` with conntrack zeroed written into the kubeadm config before the ConfigMap exists (`write_kubeadm_config`). Opt-in KPR path skips kube-proxy instead. |
| Cilium `ipam.mode=kubernetes` (sec. 5) | `controller_cilium` `--set ipam.mode=kubernetes` |
| Cilium CRDs before operator (plan_compiler lesson) | `kubectl apply --server-side` + `kubectl wait Established` in `controller_cilium` |
| `cilium status --wait` readiness with loud failure | `cilium status --wait --wait-duration 15m`; `set -e` makes timeout fatal |
| Static IP from profile, not DHCP (sec. 3) | `network-config.yaml.tmpl` |
| Hubble and Gateway API (runbook: disabled) | Off by default; `cilium_hubble` and `cilium_gateway_api` opt-in |

## Privilege

`security.privileged`, the unconfined AppArmor profile and `cgroup:rw` mounts are required for kubelet and Cilium in LXC. They weaken isolation. `allow_privileged` must be `true`, an explicit operator decision recorded in the render inputs. Acceptable for a lab, not for shared or production hosts.

## Open items

- Confirm `k8s_package_version` (`1.35.9-1.1`) and `containerd_package_version` against the actual mirror.
- Cilium 1.20.1 end-to-end is not verified in this repo; run `cilium connectivity test` on the live cluster.
- kube_proxy_replacement=true is not verified in LXC; the default kube-proxy path is the runbook-verified one.
- Gough follow-up: the hub catalog pins Cilium 1.19.1 and must move to the biome's `cilium_version`.
- Worker Ready is confirmed by the controller side, not by the worker biome.

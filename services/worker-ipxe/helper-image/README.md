# Gough Phase 1 Helper Netboot Image

Ubuntu 24.04 LTS netboot image for Gough hardware discovery (Phase 1). Bundles hardware probing tools (smartmontools, lshw, SMART, lldpd, ipmitool) and a Go-based discovery-agent binary for inventory collection and communication with the Gough primary node.

**Output:** OCI image (`ghcr.io/penguintechinc/gough/helper-image:SHA`) containing kernel + initrd for PXE boot and full discovery environment.

**Build:**
```bash
make build              # Build amd64 image
make build-multiarch   # Build amd64 + arm64 (requires docker buildx)
make extract-kernel    # Extract vmlinuz + initrd for HTTP serving
```

## Architecture & Components

- **Base Image:** Ubuntu 24.04 LTS (`ubuntu:24.04@sha256:6042500...`)
- **Discovery Agent:** Go 1.24 static binary (<15 MB), injected via `/usr/local/bin/discovery-agent`
- **Hardware Tools:** lshw, smartmontools, nvme-cli, lldpd, ipmitool, ethtool
- **Boot System:** cloud-init orchestrates agent execution on PXE boot
- **Security:** Runs as non-root user `discovery` (UID 1000)
- **Network:** Dual-stack IPv4+IPv6 DHCP with SLAAC fallback

## Building the Helper Image

### Prerequisites

1. **Go Discovery-Agent Binary** — must be pre-built:
   ```bash
   cd ../../discovery-agent
   make build  # Produces dist/discovery-agent
   ```

2. **Docker** — with multi-arch support (optional):
   ```bash
   docker buildx create --use  # For multi-arch builds
   ```

### Local Build (amd64)

```bash
make build
# Output: localhost:32000/gough/helper-image:latest

# Verify
docker run --rm localhost:32000/gough/helper-image:latest \
  /usr/local/bin/discovery-agent --version
```

### Multi-Architecture Build (amd64 + arm64)

```bash
make build-multiarch
# Output: multi-arch manifest pushed to registry
```

### Extract Kernel + Initrd

For HTTP serving by worker-ipxe:

```bash
make extract-kernel
# Output:
#   ./dist/vmlinuz-helper
#   ./dist/initrd-helper
```

## Specification & Constraints

| Constraint | Value | Rationale |
|---|---|---|
| **Base Image** | Ubuntu 24.04 LTS | First-class LXD snap support; standard hardware drivers pre-included |
| **Max Size** | 220 MB (initrd) | PXE boot timeout constraints; fast network pull |
| **Discovery Agent** | < 15 MB static binary | No runtime dependencies in initrd; fast boot |
| **Security** | Non-root user (UID 1000) | Rootless per devops-containers.md; hardware probes run via sudo/systemd |
| **Network** | Dual-stack DHCP + SLAAC | IPv4 + IPv6 support; fallback for DHCP failures |
| **Cloud-init** | Ubuntu 24.04 minimal | Orchestrates discovery-agent execution; logs to /dev/ttyS0 + /var/log/gough/ |

### Rootless Container Note

The image runs as non-root user `discovery` (UID 1000) per security policy. Some hardware probes (IPMI, UEFI firmware read, SMART via restricted devices) may require elevated privileges at boot time. The cloud-init wrapper script (`gough-discovery-agent-wrapper.sh`) can be executed with `sudo` or via systemd-run if needed:

```systemd
[Service]
Type=oneshot
User=root
ExecStart=/usr/local/bin/gough-discovery-agent-wrapper.sh
```

The build enforces rootless; operator can override at runtime if hardware constraints require root probing.

## Discovery-Agent Integration

### Build Argument

The Dockerfile expects a pre-built discovery-agent binary via build arg:

```bash
docker build \
  --build-arg DISCOVERY_AGENT_BINARY=/path/to/discovery-agent \
  .
```

Default path: `../../discovery-agent/dist/discovery-agent`

### CI Integration

GitHub Actions workflow (`.github/workflows/helper-image-build.yml`):

1. **Triggers:** Push to main/release branches or PR changes to helper-image
2. **Builds:** Multi-arch (amd64 + arm64) via `docker buildx`
3. **Signs:** Keyless cosign signature (OIDC)
4. **Attaches:** CycloneDX SBOM as OCI attestation
5. **Tests:** Image build, size, rootless validation

### Updating the Image

To rebuild with a new discovery-agent binary:

```bash
# 1. Build discovery-agent
cd services/discovery-agent
make build  # Outputs dist/discovery-agent

# 2. Build helper image
cd ../worker-ipxe/helper-image
make build
# Or in CI: git push origin feature/update-agent
#          → CI rebuilds + signs + pushes to ghcr.io
```

## Testing

### Local Tests

```bash
cd services/worker-ipxe/helper-image

# Build validation
bash tests/test-image-builds.sh

# Rootless validation
bash tests/test-rootless.sh

# Multi-arch validation (requires docker buildx)
bash tests/test-multiarch.sh
```

### CI Tests

GitHub Actions runs tests on every push and PR:

```
push → docker buildx build → test-image-builds.sh → test-rootless.sh → Sign → Push to GHCR
```

## Health Check

Native binary health check (no curl/wget):

```dockerfile
HEALTHCHECK --interval=30s --timeout=3s --retries=3 \
  CMD /usr/local/bin/discovery-agent --version > /dev/null 2>&1 || exit 1
```

Run:
```bash
docker run --health-cmd="..." localhost:32000/gough/helper-image:latest
```

## Package Pinning

Every APT package is pinned to an exact version in `Dockerfile` for reproducible builds:

```dockerfile
RUN apt-get install -y --no-install-recommends \
    smartmontools=7.4-1ubuntu1 \
    lshw=B.02.20.2-3ubuntu1 \
    ...
```

**Refresh versions:**

```bash
# List latest version of a package
apt-cache madison smartmontools | head -1

# Update Dockerfile and apt-pins.list with exact versions
nano Dockerfile apt-pins.list

# Rebuild
make clean build
```

## Cloud-init Configuration

Two configuration files drive helper behavior:

1. **`cloud-init/user-data.yaml`** — Executes discovery-agent on boot; handles token injection and network setup
2. **`cloud-init/network-config.yaml`** — DHCP (IPv4) + DHCPv6/SLAAC (IPv6) network configuration

## OCI Labels & Metadata

Image includes standardized OCI labels:

```json
{
  "org.opencontainers.image.created": "2026-04-28T10:30:00Z",
  "org.opencontainers.image.revision": "a1b2c3d4e5f6...",
  "org.opencontainers.image.title": "Gough Phase 1 Helper Image",
  "org.opencontainers.image.description": "...",
  "org.opencontainers.image.vendor": "PenguinTech"
}
```

Inspect:
```bash
docker inspect ghcr.io/penguintechinc/gough/helper-image:SHA --format='{{json .Config.Labels}}' | jq .
```

## Known Limitations

- **Secure Boot (M1):** Disabled; Phase 2 permanent OS uses upstream signed kernel so Secure Boot works there.
- **IPv6-only networks:** Requires explicit `gough_prefer_ipv6=1` kernel parameter; default favors IPv4.
- **UEFI + PXE:** Requires Secure Boot disabled in M1; signed shim planned for M3.

## Debugging

### View cloud-init logs inside container

```bash
docker run --rm -it localhost:32000/gough/helper-image:latest \
  /bin/bash -c "cat /var/log/cloud-init-output.log"
```

### Check discovery-agent binary

```bash
docker run --rm localhost:32000/gough/helper-image:latest \
  file /usr/local/bin/discovery-agent
```

### Verify hardware tools

```bash
docker run --rm localhost:32000/gough/helper-image:latest \
  lshw -version
```

## References

- **Gough Spec:** [/let-s-create-a-spec-snoopy-nest.md](/../../.claude/plans/let-s-create-a-spec-snoopy-nest.md) — Phase 1 discovery details
- **Discovery Agent:** `services/discovery-agent/` — Go binary source
- **Worker iPXE:** `services/worker-ipxe/` — Boot script generation + HTTP serving
- **DevOps Standards:** `~/.claude/rules/devops-containers.md` — Image security & rootless policy

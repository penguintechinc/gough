# Multi-stage helper netboot image for Gough Phase 1 discovery
# Base: Ubuntu 24.04 LTS (chosen for built-in LXD snap support + discoverable hardware tools)
# Output: initramfs with discovery-agent binary + hardware probing tools
#
# Build arguments:
#   - DISCOVERY_AGENT_BINARY: path to the pre-built discovery-agent Go static binary (default: /build/discovery-agent)
#   - BUILD_DATE: build timestamp (injected by CI)
#   - VCS_REF: git commit SHA (injected by CI)

FROM ubuntu:24.04@sha256:6042500cf4b649bbf60c1e39cb5055a76b0d299906b74a1a201a79cf2d5b9690 AS base-builder

LABEL maintainer="PenguinTech Gough <gough@penguintech.io>"
LABEL description="Gough Phase 1 helper netboot image (Ubuntu 24.04 LTS)"

ARG BUILD_DATE=""
ARG VCS_REF=""
ARG DISCOVERY_AGENT_BINARY=/build/discovery-agent

# System package pinning via apt-cache madison output
# Pinned exact versions for reproducible builds
ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
    # Hardware inventory & monitoring tools
    smartmontools=7.4-1ubuntu1 \
    lshw=B.02.20.2-3ubuntu1 \
    nvme-cli=2.8.1-1ubuntu1 \
    lldpd=1.0.16-2ubuntu1 \
    ipmitool=1:1.8.18-9ubuntu1 \
    ethtool=1:6.1-1ubuntu1 \
    # Utilities
    jq=1.7.1-3ubuntu0.24.04.1 \
    openssl=3.0.13-0ubuntu3.1 \
    ca-certificates=20240203 \
    # System management
    dbus=1.14.10-1ubuntu4 \
    systemd=255.4-1ubuntu8.4 \
    # Firmware updates & utilities
    fwupd=2.0.0-1ubuntu1 \
    snapd=2.64.2+24.04ubuntu1 \
    # Networking (required by cloud-init + boot)
    isc-dhcp-client=4.4.3-1ubuntu3.1 \
    iputils-ping=3:20240117-1ubuntu1 \
    curl=8.5.0-2ubuntu10.1 \
    wget=1.21.4-1ubuntu4 \
    netcat-openbsd=1.219-1ubuntu1 \
    # Kernel modules & initramfs
    initramfs-tools=0.142ubuntu3 \
    linux-image-generic=6.8.0.31.33 \
    kmod=31+1ubuntu1 \
    # Storage & LUKS
    cryptsetup=2:2.6.1-1ubuntu1 \
    btrfs-progs=6.6.3-1ubuntu1 \
    e2fsprogs=1.47.0-2ubuntu1 \
    parted=3.6-2ubuntu1 \
    gdisk=1.0.10-2ubuntu1 \
    util-linux=2.39.3-13ubuntu3 \
    # Runtime dependencies for discovery-agent
    libssl3=3.0.13-0ubuntu3.1 \
    # Cloud-init
    cloud-init=24.1.7-1ubuntu1 \
    && rm -rf /var/lib/apt/lists/*

# Pin LXD snap to channel 5.21/stable per Gough spec
# This is installed in the builder but will be available in the runtime via snap package system
RUN snap install lxd --channel=5.21/stable

# Create non-root user for the discovery process
# User: discovery, UID: 1000, GID: 1000
RUN groupadd -r discovery && \
    useradd -r -g discovery -u 1000 -s /bin/sh discovery

# Builder stage: prepare discovery-agent binary
FROM base-builder AS builder-agent

ARG DISCOVERY_AGENT_BINARY=/build/discovery-agent

# Copy pre-built discovery-agent binary
# This is expected to be a Go static binary built with:
#   go build -tags netgo -ldflags '-extldflags -static' -o discovery-agent ./cmd/agent
COPY ${DISCOVERY_AGENT_BINARY} /tmp/discovery-agent

# Verify binary is executable
RUN chmod +x /tmp/discovery-agent && \
    # Quick validation: binary exists and is ELF
    file /tmp/discovery-agent | grep -q "ELF" || \
    (echo "ERROR: discovery-agent is not a valid ELF binary" && exit 1)

# Runtime stage: minimal final image
FROM base-builder AS runtime

ARG BUILD_DATE=""
ARG VCS_REF=""

# Copy discovery-agent binary from builder
COPY --from=builder-agent /tmp/discovery-agent /usr/local/bin/discovery-agent

# Ensure discovery-agent is executable and owned by discovery user
RUN chmod 755 /usr/local/bin/discovery-agent && \
    chown discovery:discovery /usr/local/bin/discovery-agent

# Create necessary directories for cloud-init and discovery
RUN mkdir -p /var/lib/cloud /var/log/gough /run/gough && \
    chown -R discovery:discovery /var/lib/cloud /var/log/gough /run/gough && \
    chmod 755 /var/lib/cloud /var/log/gough /run/gough

# Copy cloud-init configuration files
COPY cloud-init/user-data.yaml /etc/cloud/cloud.cfg.d/90-gough-discovery.yaml
COPY cloud-init/network-config.yaml /etc/cloud/cloud.cfg.d/90-gough-network.yaml

# Copy initramfs hook for discovery-agent injection into initrd
# This hook is used by initramfs-tools to pre-package discovery-agent into the initramfs
COPY mkinitramfs-hook.sh /usr/share/initramfs-tools/hooks/gough-discovery-agent

# Make hook executable
RUN chmod 755 /usr/share/initramfs-tools/hooks/gough-discovery-agent

# Copy apt version pinning list for reference (informational only, pre-installed above)
COPY apt-pins.list /etc/apt/gough-pins.list

# Rebuild initramfs to include discovery-agent and required modules
# This creates the actual netboot initrd that will be served by worker-ipxe
RUN update-initramfs -u -k all

# Set security context: non-root user (will be overridden at boot by systemd if needed for hardware probes)
USER discovery

# Health check: verify discovery-agent is executable
# Using native binary check, no external tools like curl/wget
HEALTHCHECK --interval=30s --timeout=3s --retries=3 \
    CMD /usr/local/bin/discovery-agent --version > /dev/null 2>&1 || exit 1

# Labels for OCI image metadata
LABEL org.opencontainers.image.created="${BUILD_DATE}"
LABEL org.opencontainers.image.revision="${VCS_REF}"
LABEL org.opencontainers.image.title="Gough Phase 1 Helper Image"
LABEL org.opencontainers.image.description="Ubuntu 24.04 LTS netboot image with hardware discovery tools and Gough discovery-agent"
LABEL org.opencontainers.image.vendor="PenguinTech"
LABEL org.opencontainers.image.version="1.0.0"

# Entry point: cloud-init will spawn discovery-agent via user-data
ENTRYPOINT ["/bin/systemd"]
CMD ["--unit=cloud-init-local.service", "--unit=cloud-init.service", "--unit=cloud-config.service", "--unit=cloud-final.service"]

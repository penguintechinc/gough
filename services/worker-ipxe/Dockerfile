# Stage 1: builder — compile and install dependencies
FROM python:3.13-slim-bookworm@sha256:01f42367a0a94ad4bc17111776fd66e3500c1d87c15bbd6055b7371d39c124fb AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    python3-dev \
    && rm -rf /var/lib/apt/lists/*

# Create and activate virtual environment for clean dependency isolation
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /build

# Copy and install Python dependencies
COPY requirements.txt .

# Copy penguin-libs packages from local source (until published to PyPI)
# Note: Requires penguin-libs stage or context available at build time
COPY --from=penguin-libs packages/python-libs /tmp/penguin-libs/python-libs
COPY --from=penguin-libs packages/python-licensing /tmp/penguin-libs/python-licensing

RUN pip install --no-cache-dir \
    /tmp/penguin-libs/python-libs \
    /tmp/penguin-libs/python-licensing && \
    pip install --no-cache-dir -r requirements.txt && \
    rm -rf /tmp/penguin-libs

# Stage 2: runtime — minimal image with installed packages only (no build tools)
FROM python:3.13-slim-bookworm@sha256:01f42367a0a94ad4bc17111776fd66e3500c1d87c15bbd6055b7371d39c124fb

# ROOT EXCEPTION (approved): requires CAP_NET_BIND_SERVICE for DHCP/TFTP low ports (67/udp, 69/udp)
# Process runs as non-root UID; kernel capability CAP_NET_BIND_SERVICE is granted via K8s securityContext.
RUN groupadd -r gough && useradd -r -g gough gough

# Install system dependencies (runtime only — no build-essential, python3-dev, curl, wget)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ipmitool \
    ipxe \
    dnsmasq \
    isc-dhcp-server \
    && rm -rf /var/lib/apt/lists/*

# Copy iPXE binaries to TFTP directory
RUN mkdir -p /var/lib/ipxe/tftp && \
    cp /usr/lib/ipxe/undionly.kpxe /var/lib/ipxe/tftp/ && \
    cp /usr/lib/ipxe/ipxe.efi /var/lib/ipxe/tftp/ && \
    chmod 644 /var/lib/ipxe/tftp/*

# Copy virtual environment from builder (contains all installed Python packages)
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /app

# Copy application code
COPY . .

# Set non-root user
USER gough

# Expose ports
# 69/udp - TFTP
# 4011/udp - ProxyDHCP
# 8080/tcp - HTTP boot server
# 67/udp - Full DHCP (when DHCP_MODE=full)
EXPOSE 69/udp 4011/udp 8080/tcp 67/udp

# Health check using native Python (socket connection to boot server)
HEALTHCHECK --interval=30s --timeout=3s --retries=3 \
    CMD python -c "import socket; s = socket.socket(); s.connect(('localhost', 8080)); s.close()" || exit 1

# Run worker
ENTRYPOINT ["python", "-m", "worker.main"]

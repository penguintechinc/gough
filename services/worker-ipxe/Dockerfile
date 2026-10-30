# Stage 1: builder — compile and install dependencies
FROM python:3.13-slim-bookworm@sha256:281d39ff02e3b389401586134ff9e6572954687822a58055ec07a60ded4e0b7f AS builder

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
FROM python:3.13-slim-bookworm@sha256:281d39ff02e3b389401586134ff9e6572954687822a58055ec07a60ded4e0b7f

LABEL org.opencontainers.image.source="https://github.com/penguintechinc/gough"

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

# Drop base-image packaging tooling the runtime never uses: setuptools/wheel/jaraco.context
# and pip itself (pip vendors its own urllib3/msgpack copies, which carry CVEs). The app
# runs from /opt/venv and never invokes pip. Absolute path: ENV PATH may point at the venv.
RUN /usr/local/bin/python -m pip uninstall -y setuptools wheel jaraco.context && \
    rm -rf /usr/local/lib/python3.13/site-packages/pip /usr/local/lib/python3.13/site-packages/pip-*.dist-info \
           /usr/local/bin/pip /usr/local/bin/pip3 /usr/local/bin/pip3.13 \
           /usr/local/lib/python3.13/ensurepip/_bundled

# Copy virtual environment from builder (contains all installed Python packages)
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
# Remove pip from the copied venv too (its vendored urllib3/msgpack carry CVEs; runtime never pip-installs).
RUN rm -rf /opt/venv/lib/python3.13/site-packages/pip /opt/venv/lib/python3.13/site-packages/pip-*.dist-info /opt/venv/bin/pip /opt/venv/bin/pip3 /opt/venv/bin/pip3.13


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

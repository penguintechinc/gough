FROM python:3.13-slim

# Install system dependencies for iPXE, TFTP, DHCP, IPMI, and build tools
RUN apt-get update && apt-get install -y --no-install-recommends \
    ipmitool \
    ipxe \
    dnsmasq \
    curl \
    isc-dhcp-server \
    build-essential \
    python3-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy iPXE binaries to TFTP directory
RUN mkdir -p /var/lib/ipxe/tftp && \
    cp /usr/lib/ipxe/undionly.kpxe /var/lib/ipxe/tftp/ && \
    cp /usr/lib/ipxe/ipxe.efi /var/lib/ipxe/tftp/ && \
    chmod 644 /var/lib/ipxe/tftp/*

# Create working directory
WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Expose ports
# 69/udp - TFTP
# 4011/udp - ProxyDHCP
# 8080/tcp - HTTP boot server
# 67/udp - Full DHCP (when DHCP_MODE=full)
EXPOSE 69/udp 4011/udp 8080/tcp 67/udp

# Run worker
CMD ["python", "-m", "worker.main"]

FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8
ENV LC_ALL=C.UTF-8

# Install system dependencies
RUN apt-get update && apt-get install -y \
    software-properties-common \
    curl \
    wget \
    gnupg \
    lsb-release \
    ca-certificates \
    apt-transport-https \
    systemd \
    systemd-sysv \
    init \
    && rm -rf /var/lib/apt/lists/*

# Add MaaS PPA and install MaaS
RUN add-apt-repository ppa:maas/3.4 && \
    apt-get update && \
    apt-get install -y \
    maas \
    maas-dhcp \
    maas-dns \
    maas-proxy \
    postgresql \
    postgresql-contrib \
    nginx \
    && rm -rf /var/lib/apt/lists/*

# Create maas user and directories
RUN useradd -m -s /bin/bash maas && \
    mkdir -p /var/lib/maas && \
    chown -R maas:maas /var/lib/maas

# Copy configuration files
COPY config/maas-config.yaml /etc/maas/
COPY scripts/init-maas.sh /usr/local/bin/
COPY scripts/setup-dhcp.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/init-maas.sh /usr/local/bin/setup-dhcp.sh

# Configure PostgreSQL for MaaS
RUN service postgresql start && \
    sudo -u postgres createdb -O postgres maasdb && \
    sudo -u postgres psql -c "CREATE USER maas WITH ENCRYPTED PASSWORD 'maaspass';" && \
    sudo -u postgres psql -c "GRANT ALL PRIVILEGES ON DATABASE maasdb TO maas;"

# Expose MaaS ports
EXPOSE 5240 5241 5242 5243 5247 5248 5249 53 67 68 69 80 443

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:5240/MAAS/ || exit 1

# Copy entrypoint script
COPY scripts/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
CMD ["maas-regiond"]
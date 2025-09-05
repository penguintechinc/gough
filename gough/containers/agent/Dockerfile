FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive
ENV AGENT_VERSION=1.0.0

# Install system dependencies
RUN apt-get update && apt-get install -y \
    curl \
    wget \
    python3 \
    python3-pip \
    python3-venv \
    systemd \
    systemd-sysv \
    cron \
    rsyslog \
    openssh-client \
    jq \
    htop \
    iotop \
    netstat-nat \
    tcpdump \
    dnsutils \
    iputils-ping \
    vim \
    git \
    && rm -rf /var/lib/apt/lists/*

# Create agent user and directories
RUN useradd -m -s /bin/bash agent && \
    mkdir -p /opt/maas-agent/{bin,config,logs,data} && \
    mkdir -p /etc/maas-agent && \
    chown -R agent:agent /opt/maas-agent

# Install Python dependencies
COPY requirements.txt /tmp/
RUN pip3 install --no-cache-dir -r /tmp/requirements.txt

# Copy agent scripts and configuration
COPY scripts/ /opt/maas-agent/bin/
COPY config/ /etc/maas-agent/
COPY systemd/ /etc/systemd/system/

# Make scripts executable
RUN chmod +x /opt/maas-agent/bin/*.py /opt/maas-agent/bin/*.sh

# Create systemd service
RUN systemctl enable maas-agent.service

# OSQuery installation
RUN curl -L https://pkg.osquery.io/deb/osquery_5.10.2-1.linux_amd64.deb -o /tmp/osquery.deb && \
    dpkg -i /tmp/osquery.deb && \
    rm /tmp/osquery.deb

# Configure OSQuery
COPY config/osquery.conf /etc/osquery/
COPY config/osquery.flags /etc/osquery/

# Health check script
COPY scripts/health-check.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/health-check.sh

# Expose agent API port
EXPOSE 9090

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD /usr/local/bin/health-check.sh

# Copy entrypoint
COPY scripts/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
CMD ["/opt/maas-agent/bin/agent.py"]
#!/bin/bash
set -e

echo "Starting MaaS Agent container..."

# Create necessary directories
mkdir -p /opt/maas-agent/logs
mkdir -p /opt/maas-agent/data

# Set permissions
chown -R agent:agent /opt/maas-agent

# Start systemd (if running in privileged mode)
if [ -d /run/systemd/system ]; then
    echo "Starting systemd services..."
    systemctl start rsyslog
    systemctl start cron
    
    # Start OSQuery if configured
    if [ "${ENABLE_OSQUERY:-true}" = "true" ]; then
        echo "Starting OSQuery service..."
        systemctl start osqueryd
    fi
fi

# Export environment variables for agent
export HOSTNAME=${HOSTNAME:-$(hostname)}
export SERVER_ID=${SERVER_ID:-""}
export AGENT_ID=${AGENT_ID:-""}
export MANAGEMENT_SERVER_URL=${MANAGEMENT_SERVER_URL:-"http://management-server:8000"}
export AGENT_API_KEY=${AGENT_API_KEY:-""}

echo "Starting MaaS Agent..."
echo "Agent ID: ${AGENT_ID}"
echo "Management Server: ${MANAGEMENT_SERVER_URL}"

# Switch to agent user and start the agent
exec su agent -c "cd /opt/maas-agent && python3 /opt/maas-agent/bin/agent.py"
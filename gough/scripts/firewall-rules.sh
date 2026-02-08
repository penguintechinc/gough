#!/bin/bash

# Gough Firewall Rules Setup Script
# Configures iptables rules for secure network segmentation
# Part of Phase 7.2 Network Configuration and Phase 8.3 Security Implementation

set -euo pipefail

# Network configuration
MGMT_SUBNET="172.20.0.0/16"
PXE_SUBNET="192.168.100.0/24"
MGMT_BRIDGE="br-mgmt"
PXE_BRIDGE="br-pxe"

# Service ports
MAAS_WEB_PORT="5240"
MAAS_API_PORT="5241"
MGMT_WEB_PORT="8000"
FLEET_WEB_PORT="8080"
FLEET_TLS_PORT="8443"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# Check if running as root
check_root() {
    if [[ $EUID -ne 0 ]]; then
        log_error "This script must be run as root"
        exit 1
    fi
}

# Flush existing rules
flush_rules() {
    log_info "Flushing existing iptables rules..."
    iptables -F
    iptables -X
    iptables -t nat -F
    iptables -t nat -X
    iptables -t mangle -F
    iptables -t mangle -X
}

# Set default policies
set_default_policies() {
    log_info "Setting default policies..."
    iptables -P INPUT DROP
    iptables -P FORWARD DROP
    iptables -P OUTPUT ACCEPT
}

# Allow loopback traffic
allow_loopback() {
    log_info "Allowing loopback traffic..."
    iptables -I INPUT 1 -i lo -j ACCEPT
    iptables -I OUTPUT 1 -o lo -j ACCEPT
}

# Allow established connections
allow_established() {
    log_info "Allowing established and related connections..."
    iptables -A INPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
    iptables -A FORWARD -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
}

# Management network rules
setup_management_rules() {
    log_info "Setting up management network rules..."
    
    # Allow all traffic within management subnet
    iptables -A INPUT -s $MGMT_SUBNET -d $MGMT_SUBNET -j ACCEPT
    iptables -A FORWARD -s $MGMT_SUBNET -d $MGMT_SUBNET -j ACCEPT
    
    # Allow management services from management network
    iptables -A INPUT -s $MGMT_SUBNET -p tcp --dport $MGMT_WEB_PORT -j ACCEPT
    iptables -A INPUT -s $MGMT_SUBNET -p tcp --dport $FLEET_WEB_PORT -j ACCEPT
    iptables -A INPUT -s $MGMT_SUBNET -p tcp --dport $FLEET_TLS_PORT -j ACCEPT
    
    # Allow database access from management network
    iptables -A INPUT -s $MGMT_SUBNET -p tcp --dport 5432 -j ACCEPT  # PostgreSQL
    iptables -A INPUT -s $MGMT_SUBNET -p tcp --dport 3306 -j ACCEPT  # MySQL
    iptables -A INPUT -s $MGMT_SUBNET -p tcp --dport 6379 -j ACCEPT  # Redis
    
    log_info "Management network rules configured"
}

# PXE network rules
setup_pxe_rules() {
    log_info "Setting up PXE network rules..."
    
    # Allow DHCP traffic
    iptables -A INPUT -i $PXE_BRIDGE -p udp --dport 67 -j ACCEPT
    iptables -A INPUT -i $PXE_BRIDGE -p udp --sport 68 -j ACCEPT
    
    # Allow TFTP traffic
    iptables -A INPUT -i $PXE_BRIDGE -p udp --dport 69 -j ACCEPT
    
    # Allow HTTP/HTTPS for image downloads
    iptables -A INPUT -i $PXE_BRIDGE -p tcp --dport 80 -j ACCEPT
    iptables -A INPUT -i $PXE_BRIDGE -p tcp --dport 443 -j ACCEPT
    
    # Allow DNS queries
    iptables -A INPUT -i $PXE_BRIDGE -p udp --dport 53 -j ACCEPT
    iptables -A INPUT -i $PXE_BRIDGE -p tcp --dport 53 -j ACCEPT
    
    # Allow MaaS API access from PXE network
    iptables -A INPUT -s $PXE_SUBNET -p tcp --dport $MAAS_WEB_PORT -j ACCEPT
    iptables -A INPUT -s $PXE_SUBNET -p tcp --dport $MAAS_API_PORT -j ACCEPT
    
    # Allow forwarding between PXE network and internet for package downloads
    iptables -A FORWARD -i $PXE_BRIDGE -o eth0 -j ACCEPT
    iptables -A FORWARD -i eth0 -o $PXE_BRIDGE -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
    
    log_info "PXE network rules configured"
}

# External access rules
setup_external_rules() {
    log_info "Setting up external access rules..."
    
    # Allow SSH access (configure carefully in production)
    iptables -A INPUT -p tcp --dport 22 -m conntrack --ctstate NEW -m recent --set
    iptables -A INPUT -p tcp --dport 22 -m conntrack --ctstate NEW -m recent --update --seconds 60 --hitcount 4 -j DROP
    iptables -A INPUT -p tcp --dport 22 -j ACCEPT
    
    # Allow web access to management interface (restrict in production)
    iptables -A INPUT -p tcp --dport 80 -j ACCEPT
    iptables -A INPUT -p tcp --dport 443 -j ACCEPT
    
    # Allow ping for connectivity testing
    iptables -A INPUT -p icmp --icmp-type echo-request -j ACCEPT
    
    log_info "External access rules configured"
}

# Docker integration rules
setup_docker_rules() {
    log_info "Setting up Docker integration rules..."
    
    # Allow Docker daemon communication
    iptables -A INPUT -i docker0 -j ACCEPT
    iptables -A FORWARD -i docker0 -j ACCEPT
    iptables -A FORWARD -o docker0 -j ACCEPT
    
    # NAT rules for Docker containers
    iptables -t nat -A POSTROUTING -s 172.17.0.0/16 ! -o docker0 -j MASQUERADE
    iptables -t nat -A POSTROUTING -s $MGMT_SUBNET ! -o $MGMT_BRIDGE -j MASQUERADE
    
    # Allow Docker networks to communicate
    iptables -A FORWARD -i br-+ -o br-+ -j ACCEPT
    
    log_info "Docker integration rules configured"
}

# Security hardening rules
setup_security_rules() {
    log_info "Setting up security hardening rules..."
    
    # Drop invalid packets
    iptables -A INPUT -m conntrack --ctstate INVALID -j DROP
    iptables -A FORWARD -m conntrack --ctstate INVALID -j DROP
    
    # Rate limit new connections
    iptables -A INPUT -p tcp -m conntrack --ctstate NEW -m limit --limit 60/s --limit-burst 20 -j ACCEPT
    iptables -A INPUT -p tcp -m conntrack --ctstate NEW -j DROP
    
    # Protect against port scanning
    iptables -N PORTSCAN
    iptables -A PORTSCAN -m limit --limit 1/s --limit-burst 1 -j RETURN
    iptables -A PORTSCAN -j DROP
    iptables -A INPUT -p tcp --tcp-flags SYN,ACK,FIN,RST RST -j PORTSCAN
    
    # Log dropped packets (optional, can be noisy)
    # iptables -A INPUT -m limit --limit 5/min -j LOG --log-prefix "iptables-dropped: " --log-level 4
    iptables -A INPUT -j DROP
    
    log_info "Security hardening rules configured"
}

# Save rules
save_rules() {
    log_info "Saving iptables rules..."
    if command -v iptables-save &> /dev/null; then
        iptables-save > /etc/iptables/rules.v4
    else
        log_warn "iptables-save not found, rules may not persist after reboot"
    fi
}

# Create systemd service for firewall persistence
create_firewall_service() {
    log_info "Creating systemd service for firewall persistence..."
    
    cat > /etc/systemd/system/gough-firewall.service << 'EOF'
[Unit]
Description=Gough Firewall Rules
After=network.target gough-bridges.service
Wants=network.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/opt/gough/scripts/firewall-rules.sh --load-only
ExecReload=/opt/gough/scripts/firewall-rules.sh --load-only
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

    systemctl daemon-reload
    systemctl enable gough-firewall.service
    log_info "Systemd service created and enabled"
}

# Display current rules
show_rules() {
    log_info "Current iptables rules:"
    echo
    echo "=== FILTER TABLE ==="
    iptables -L -n -v
    echo
    echo "=== NAT TABLE ==="
    iptables -t nat -L -n -v
}

# Main execution
main() {
    local load_only=false
    
    if [[ "${1:-}" == "--load-only" ]]; then
        load_only=true
    fi
    
    log_info "Starting Gough firewall setup..."
    
    check_root
    
    # Install dependencies if not in load-only mode
    if [[ "$load_only" == false ]]; then
        log_info "Installing iptables-persistent..."
        apt-get update -qq
        apt-get install -y iptables-persistent
    fi
    
    flush_rules
    set_default_policies
    allow_loopback
    allow_established
    setup_management_rules
    setup_pxe_rules
    setup_external_rules
    setup_docker_rules
    setup_security_rules
    
    if [[ "$load_only" == false ]]; then
        create_firewall_service
    fi
    
    save_rules
    show_rules
    
    log_info "Firewall setup completed successfully"
}

# Handle script termination
cleanup() {
    log_info "Firewall configuration script finished"
}

trap cleanup EXIT

# Run main function
main "$@"
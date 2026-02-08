#!/bin/bash

# Gough Network Bridge Setup Script
# Configures bridge interfaces for PXE boot and management networks
# Part of Phase 7.2 Network Configuration

set -euo pipefail

# Configuration variables
MGMT_BRIDGE="br-mgmt"
PXE_BRIDGE="br-pxe" 
MGMT_SUBNET="172.20.0.0/16"
PXE_SUBNET="192.168.100.0/24"
PXE_INTERFACE="${PXE_INTERFACE:-eth1}"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

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

# Install required packages
install_dependencies() {
    log_info "Installing bridge utilities..."
    apt-get update -qq
    apt-get install -y bridge-utils iptables-persistent
}

# Create management bridge
create_mgmt_bridge() {
    log_info "Creating management bridge: $MGMT_BRIDGE"
    
    if ! ip link show $MGMT_BRIDGE &>/dev/null; then
        brctl addbr $MGMT_BRIDGE
        ip link set $MGMT_BRIDGE up
        log_info "Management bridge $MGMT_BRIDGE created"
    else
        log_warn "Management bridge $MGMT_BRIDGE already exists"
    fi
}

# Create PXE boot bridge
create_pxe_bridge() {
    log_info "Creating PXE boot bridge: $PXE_BRIDGE"
    
    if ! ip link show $PXE_BRIDGE &>/dev/null; then
        brctl addbr $PXE_BRIDGE
        
        # Add physical interface if specified and exists
        if [[ -n "$PXE_INTERFACE" ]] && ip link show "$PXE_INTERFACE" &>/dev/null; then
            log_info "Adding $PXE_INTERFACE to $PXE_BRIDGE"
            brctl addif $PXE_BRIDGE $PXE_INTERFACE
            ip link set $PXE_INTERFACE up
        else
            log_warn "PXE interface $PXE_INTERFACE not found or not specified"
        fi
        
        ip link set $PXE_BRIDGE up
        log_info "PXE boot bridge $PXE_BRIDGE created"
    else
        log_warn "PXE boot bridge $PXE_BRIDGE already exists"
    fi
}

# Enable IP forwarding
enable_forwarding() {
    log_info "Enabling IP forwarding..."
    echo 'net.ipv4.ip_forward=1' > /etc/sysctl.d/99-gough-forwarding.conf
    echo 'net.ipv6.conf.all.forwarding=1' >> /etc/sysctl.d/99-gough-forwarding.conf
    sysctl -p /etc/sysctl.d/99-gough-forwarding.conf
}

# Configure bridge settings
configure_bridges() {
    log_info "Configuring bridge settings..."
    
    # Disable STP for better performance in containerized environment
    brctl stp $MGMT_BRIDGE off
    brctl stp $PXE_BRIDGE off
    
    # Set bridge aging time
    brctl setageing $MGMT_BRIDGE 300
    brctl setageing $PXE_BRIDGE 300
    
    log_info "Bridge configuration completed"
}

# Create systemd service for bridge persistence
create_bridge_service() {
    log_info "Creating systemd service for bridge persistence..."
    
    cat > /etc/systemd/system/gough-bridges.service << 'EOF'
[Unit]
Description=Gough Network Bridges
After=network.target
Wants=network.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/opt/gough/scripts/network-bridge-setup.sh --start-only
ExecStop=/opt/gough/scripts/network-bridge-teardown.sh
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

    systemctl daemon-reload
    systemctl enable gough-bridges.service
    log_info "Systemd service created and enabled"
}

# Verify bridge configuration
verify_bridges() {
    log_info "Verifying bridge configuration..."
    
    for bridge in $MGMT_BRIDGE $PXE_BRIDGE; do
        if ip link show $bridge &>/dev/null; then
            local state=$(ip link show $bridge | grep -o 'state [A-Z]*' | awk '{print $2}')
            log_info "Bridge $bridge: $state"
            brctl show $bridge
        else
            log_error "Bridge $bridge not found"
            return 1
        fi
    done
}

# Main execution
main() {
    log_info "Starting Gough network bridge setup..."
    
    check_root
    
    # Skip dependency installation if --start-only flag is used
    if [[ "${1:-}" != "--start-only" ]]; then
        install_dependencies
    fi
    
    create_mgmt_bridge
    create_pxe_bridge
    enable_forwarding
    configure_bridges
    
    if [[ "${1:-}" != "--start-only" ]]; then
        create_bridge_service
    fi
    
    verify_bridges
    
    log_info "Network bridge setup completed successfully"
}

# Handle script termination
cleanup() {
    log_info "Cleaning up on exit..."
}

trap cleanup EXIT

# Run main function
main "$@"
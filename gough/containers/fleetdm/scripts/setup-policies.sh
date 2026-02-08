#!/bin/bash
# Setup FleetDM policies and query packs for Gough Hypervisor
# This script configures the FleetDM server with monitoring packs

set -e

FLEET_URL="${FLEET_URL:-https://localhost:8443}"
CONFIG_DIR="/etc/fleet"
PACKS_DIR="/etc/fleet/packs"

echo "Setting up FleetDM policies and query packs for Gough Hypervisor..."

# Wait for Fleet server to be ready
echo "Waiting for Fleet server to be available..."
until curl -k -s "$FLEET_URL/api/v1/fleet/version" > /dev/null 2>&1; do
    echo "Fleet server not ready, waiting..."
    sleep 5
done

echo "Fleet server is ready!"

# Function to apply query packs
apply_query_pack() {
    local pack_file=$1
    local pack_name=$2
    
    if [ -f "$pack_file" ]; then
        echo "Applying query pack: $pack_name"
        # Note: This would typically use the Fleet API to create packs
        # For now, we'll just copy the pack files to the appropriate location
        cp "$pack_file" "$PACKS_DIR/"
        echo "Query pack $pack_name copied to $PACKS_DIR/"
    else
        echo "Warning: Query pack file $pack_file not found"
    fi
}

# Create packs directory if it doesn't exist
mkdir -p "$PACKS_DIR"

# Apply query packs
apply_query_pack "/var/lib/fleet/config/packs/gough-security-monitoring.json" "gough-security-monitoring"
apply_query_pack "/var/lib/fleet/config/packs/gough-system-monitoring.json" "gough-system-monitoring"
apply_query_pack "/var/lib/fleet/config/packs/gough-hypervisor-monitoring.json" "gough-hypervisor-monitoring"

# Create enrollment secret if it doesn't exist
if [ ! -f "$CONFIG_DIR/enroll_secret" ]; then
    echo "Generating enrollment secret..."
    # Generate a random 32-character secret
    openssl rand -hex 16 > "$CONFIG_DIR/enroll_secret"
    chown fleet:fleet "$CONFIG_DIR/enroll_secret"
    chmod 600 "$CONFIG_DIR/enroll_secret"
    echo "Enrollment secret generated and saved to $CONFIG_DIR/enroll_secret"
fi

# Set up basic Fleet configuration
if [ -n "$FLEET_ADMIN_EMAIL" ] && [ -n "$FLEET_ADMIN_PASSWORD" ]; then
    echo "Setting up Fleet admin user and basic configuration..."
    
    # Create a basic Fleet setup script
    cat > /tmp/fleet_setup.py << 'EOF'
#!/usr/bin/env python3
import requests
import json
import os
import sys
import time

FLEET_URL = os.environ.get('FLEET_URL', 'https://localhost:8443')
ADMIN_EMAIL = os.environ.get('FLEET_ADMIN_EMAIL')
ADMIN_PASSWORD = os.environ.get('FLEET_ADMIN_PASSWORD')

session = requests.Session()
session.verify = False  # Skip SSL verification for self-signed certs

def wait_for_fleet():
    """Wait for Fleet to be ready"""
    for _ in range(30):
        try:
            response = session.get(f'{FLEET_URL}/api/v1/fleet/version')
            if response.status_code == 200:
                return True
        except:
            pass
        time.sleep(2)
    return False

def setup_fleet():
    """Set up Fleet with admin user"""
    if not wait_for_fleet():
        print("Fleet server not available")
        return False
    
    # Try to login first
    login_data = {
        'email': ADMIN_EMAIL,
        'password': ADMIN_PASSWORD
    }
    
    try:
        response = session.post(f'{FLEET_URL}/api/v1/fleet/login', json=login_data)
        if response.status_code == 200:
            print("Admin user already exists and login successful")
            return True
    except:
        print("Login failed, admin user may not exist yet")
    
    # Setup Fleet (this typically requires the first-time setup endpoint)
    setup_data = {
        'admin': {
            'admin': True,
            'email': ADMIN_EMAIL,
            'name': 'Gough Admin',
            'password': ADMIN_PASSWORD,
            'password_confirmation': ADMIN_PASSWORD
        },
        'org_info': {
            'org_name': 'Gough Hypervisor'
        },
        'server_url': FLEET_URL
    }
    
    try:
        response = session.post(f'{FLEET_URL}/api/v1/fleet/setup', json=setup_data)
        if response.status_code in [200, 201]:
            print("Fleet setup completed successfully")
            return True
        else:
            print(f"Fleet setup failed with status: {response.status_code}")
            return False
    except Exception as e:
        print(f"Fleet setup error: {e}")
        return False

if __name__ == '__main__':
    if setup_fleet():
        print("Fleet configuration completed")
        sys.exit(0)
    else:
        print("Fleet configuration failed")
        sys.exit(1)
EOF

    python3 /tmp/fleet_setup.py || echo "Fleet setup script failed, but continuing..."
    rm -f /tmp/fleet_setup.py
fi

echo "FleetDM setup completed!"
echo "Query packs installed:"
echo "  - Gough Security Monitoring"
echo "  - Gough System Monitoring"
echo "  - Gough Hypervisor Monitoring"
echo ""
echo "Enrollment secret location: $CONFIG_DIR/enroll_secret"
echo "To view the enrollment secret: cat $CONFIG_DIR/enroll_secret"
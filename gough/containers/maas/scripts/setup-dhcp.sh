#!/bin/bash
set -e

echo "Setting up DHCP configuration..."

# Default network configuration
SUBNET=${DHCP_SUBNET:-"192.168.1.0/24"}
RANGE_START=${DHCP_RANGE_START:-"192.168.1.100"}
RANGE_END=${DHCP_RANGE_END:-"192.168.1.200"}
GATEWAY=${DHCP_GATEWAY:-"192.168.1.1"}
DNS_SERVERS=${DHCP_DNS_SERVERS:-"8.8.8.8,8.8.4.4"}

# Wait for MaaS to be ready
while ! curl -s http://localhost:5240/MAAS/api/2.0/ > /dev/null; do
    echo "Waiting for MaaS API to be ready..."
    sleep 5
done

# Get the API key
API_KEY=$(cat /var/lib/maas/admin-api-key)

# Login to MaaS
maas login admin http://localhost:5240/MAAS/api/2.0/ "$API_KEY"

# Create fabric and VLAN if they don't exist
FABRIC_ID=$(maas admin fabrics read | jq -r '.[0].id // empty')
if [ -z "$FABRIC_ID" ]; then
    echo "Creating fabric..."
    FABRIC_ID=$(maas admin fabrics create name=fabric-0 | jq -r '.id')
fi

VLAN_ID=$(maas admin vlans read "$FABRIC_ID" | jq -r '.[0].id // empty')
if [ -z "$VLAN_ID" ]; then
    echo "Creating VLAN..."
    VLAN_ID=$(maas admin vlans create "$FABRIC_ID" name=untagged vid=0 | jq -r '.id')
fi

# Create subnet if it doesn't exist
SUBNET_ID=$(maas admin subnets read | jq -r --arg subnet "$SUBNET" '.[] | select(.cidr == $subnet) | .id // empty')
if [ -z "$SUBNET_ID" ]; then
    echo "Creating subnet $SUBNET..."
    SUBNET_ID=$(maas admin subnets create cidr="$SUBNET" fabric="$FABRIC_ID" vlan="$VLAN_ID" gateway_ip="$GATEWAY" dns_servers="$DNS_SERVERS" | jq -r '.id')
fi

# Enable DHCP on the VLAN
echo "Enabling DHCP on VLAN..."
maas admin vlans update "$FABRIC_ID" "$VLAN_ID" dhcp_on=true primary_rack="$(hostname)"

# Create IP range for DHCP
echo "Creating DHCP IP range: $RANGE_START - $RANGE_END"
maas admin ipranges create type=dynamic start_ip="$RANGE_START" end_ip="$RANGE_END" subnet="$SUBNET_ID"

echo "DHCP setup completed successfully!"
echo "Subnet: $SUBNET"
echo "DHCP Range: $RANGE_START - $RANGE_END"
echo "Gateway: $GATEWAY"
echo "DNS Servers: $DNS_SERVERS"
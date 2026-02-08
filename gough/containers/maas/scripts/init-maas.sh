#!/bin/bash
set -e

echo "Initializing MaaS..."

# Wait for PostgreSQL to be ready
while ! pg_isready -h localhost -p 5432 -U postgres; do
    echo "Waiting for PostgreSQL to be ready..."
    sleep 2
done

# Initialize MaaS database
echo "Initializing MaaS database..."
maas-region dbupgrade

# Create initial admin user if it doesn't exist
if ! maas-region apikey --username=admin > /dev/null 2>&1; then
    echo "Creating admin user..."
    maas-region createadmin --username=admin --password=admin --email=admin@maas.local
fi

# Generate API key for admin user
API_KEY=$(maas-region apikey --username=admin)
echo "Admin API Key: $API_KEY"
echo "$API_KEY" > /var/lib/maas/admin-api-key

# Configure MaaS settings
echo "Configuring MaaS settings..."
maas login admin http://localhost:5240/MAAS/api/2.0/ "$API_KEY"

# Set up boot sources
maas admin boot-sources create url=http://images.maas.io/ephemeral-v3/daily/ keyring_filename=/usr/share/keyrings/ubuntu-cloudimage-keyring.gpg

# Import boot images
echo "Importing boot images (this may take a while)..."
maas admin boot-resources import

# Configure DHCP if enabled
if [ "${ENABLE_DHCP:-true}" = "true" ]; then
    echo "Configuring DHCP..."
    /usr/local/bin/setup-dhcp.sh
fi

echo "MaaS initialization completed successfully!"
echo "Web UI available at: http://localhost:5240/MAAS/"
echo "Admin username: admin"
echo "Admin password: admin"
echo "API Key saved to: /var/lib/maas/admin-api-key"
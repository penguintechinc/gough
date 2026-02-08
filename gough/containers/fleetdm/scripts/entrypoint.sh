#!/bin/bash
set -e

echo "Starting Fleet DM server..."

# Wait for MySQL to be ready
echo "Waiting for MySQL to be ready..."
while ! nc -z mysql 3306; do
  echo "Waiting for MySQL..."
  sleep 2
done

# Wait for Redis to be ready
echo "Waiting for Redis to be ready..."
while ! nc -z redis 6379; do
  echo "Waiting for Redis..."
  sleep 2
done

# Generate SSL certificates if they don't exist
if [ ! -f /etc/fleet/server.crt ] || [ ! -f /etc/fleet/server.key ]; then
    echo "Generating SSL certificates..."
    openssl req -x509 -newkey rsa:4096 -keyout /etc/fleet/server.key -out /etc/fleet/server.crt -days 365 -nodes \
        -subj "/C=US/ST=CA/L=San Francisco/O=Fleet/OU=Fleet/CN=fleet" \
        -addext "subjectAltName=DNS:fleet,DNS:fleetdm,DNS:localhost,IP:127.0.0.1"
    chown fleet:fleet /etc/fleet/server.crt /etc/fleet/server.key
    chmod 600 /etc/fleet/server.key
fi

# Initialize database if needed
echo "Preparing Fleet database..."
if ! /usr/bin/fleet db prepare --config /etc/fleet/fleet.yml; then
    echo "Database preparation failed, but continuing..."
fi

# Create admin user if it doesn't exist
if [ -n "$FLEET_ADMIN_EMAIL" ] && [ -n "$FLEET_ADMIN_PASSWORD" ]; then
    echo "Creating admin user..."
    /usr/bin/fleet user create \
        --email "$FLEET_ADMIN_EMAIL" \
        --password "$FLEET_ADMIN_PASSWORD" \
        --name "Fleet Admin" \
        --config /etc/fleet/fleet.yml || echo "Admin user may already exist"
fi

# Import any additional configuration
if [ -f /var/lib/fleet/scripts/setup-policies.sh ]; then
    echo "Running additional setup scripts..."
    /var/lib/fleet/scripts/setup-policies.sh || echo "Setup scripts failed, continuing..."
fi

echo "Starting Fleet server..."
exec /usr/bin/fleet serve --config /etc/fleet/fleet.yml
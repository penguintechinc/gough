#!/bin/bash
# Generate SSL certificates for FleetDM
# Gough Hypervisor Automation System

set -e

CERT_DIR="/etc/fleet"
SSL_DIR="/var/lib/fleet/ssl"
DOMAIN="${FLEET_TLS_HOSTNAME:-fleetdm}"

# Create directories
mkdir -p "$CERT_DIR" "$SSL_DIR"

# Generate CA private key
if [ ! -f "$SSL_DIR/ca-key.pem" ]; then
    echo "Generating CA private key..."
    openssl genrsa -out "$SSL_DIR/ca-key.pem" 4096
fi

# Generate CA certificate
if [ ! -f "$SSL_DIR/ca.pem" ]; then
    echo "Generating CA certificate..."
    openssl req -new -x509 -days 365 -key "$SSL_DIR/ca-key.pem" -out "$SSL_DIR/ca.pem" -subj "/C=US/ST=CA/L=San Francisco/O=Gough Hypervisor/OU=IT Department/CN=Gough CA"
fi

# Generate server private key
if [ ! -f "$SSL_DIR/server-key.pem" ]; then
    echo "Generating server private key..."
    openssl genrsa -out "$SSL_DIR/server-key.pem" 4096
fi

# Generate server certificate signing request
if [ ! -f "$SSL_DIR/server.csr" ]; then
    echo "Generating server certificate signing request..."
    openssl req -new -key "$SSL_DIR/server-key.pem" -out "$SSL_DIR/server.csr" -subj "/C=US/ST=CA/L=San Francisco/O=Gough Hypervisor/OU=IT Department/CN=$DOMAIN"
fi

# Generate server certificate
if [ ! -f "$SSL_DIR/server.pem" ]; then
    echo "Generating server certificate..."
    openssl x509 -req -days 365 -in "$SSL_DIR/server.csr" -CA "$SSL_DIR/ca.pem" -CAkey "$SSL_DIR/ca-key.pem" -out "$SSL_DIR/server.pem" -CAcreateserial
fi

# Copy certificates to Fleet directory
cp "$SSL_DIR/server.pem" "$CERT_DIR/server.crt"
cp "$SSL_DIR/server-key.pem" "$CERT_DIR/server.key"
cp "$SSL_DIR/ca.pem" "$CERT_DIR/ca.crt"

# Set permissions
chown -R fleet:fleet "$CERT_DIR" "$SSL_DIR"
chmod 600 "$CERT_DIR/server.key" "$SSL_DIR/server-key.pem" "$SSL_DIR/ca-key.pem"
chmod 644 "$CERT_DIR/server.crt" "$CERT_DIR/ca.crt" "$SSL_DIR/ca.pem" "$SSL_DIR/server.pem"

echo "SSL certificates generated successfully!"
echo "CA Certificate: $CERT_DIR/ca.crt"
echo "Server Certificate: $CERT_DIR/server.crt"
echo "Server Key: $CERT_DIR/server.key"
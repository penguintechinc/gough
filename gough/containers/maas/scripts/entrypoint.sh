#!/bin/bash
set -e

echo "Starting MaaS container..."

# Start PostgreSQL
service postgresql start

# Wait for PostgreSQL to be ready
while ! pg_isready -h localhost -p 5432 -U postgres; do
    echo "Waiting for PostgreSQL..."
    sleep 2
done

# Initialize MaaS if not already done
if [ ! -f /var/lib/maas/admin-api-key ]; then
    echo "First run - initializing MaaS..."
    /usr/local/bin/init-maas.sh
fi

# Start nginx
service nginx start

# Start MaaS region controller
echo "Starting MaaS region controller..."
exec maas-regiond
FROM fleetdm/fleet:v4.42.0

ENV FLEET_MYSQL_ADDRESS=mysql:3306
ENV FLEET_MYSQL_DATABASE=fleet
ENV FLEET_MYSQL_USERNAME=fleet
ENV FLEET_MYSQL_PASSWORD=fleetpass
ENV FLEET_REDIS_ADDRESS=redis:6379
ENV FLEET_SERVER_CERT=/etc/fleet/server.crt
ENV FLEET_SERVER_KEY=/etc/fleet/server.key
ENV FLEET_LOGGING_JSON=true

# Install additional utilities
USER root
RUN apt-get update && apt-get install -y \
    curl \
    wget \
    jq \
    openssl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Create fleet directories
RUN mkdir -p /etc/fleet /var/lib/fleet/scripts /var/log/fleet

# Copy configuration files
COPY config/fleet.yml /etc/fleet/
COPY config/osquery.flags /etc/fleet/
COPY scripts/ /var/lib/fleet/scripts/
COPY ssl/ /etc/fleet/

# Set permissions
RUN chown -R fleet:fleet /etc/fleet /var/lib/fleet /var/log/fleet
RUN chmod +x /var/lib/fleet/scripts/*.sh

# Switch back to fleet user
USER fleet

# Expose Fleet server port
EXPOSE 8080 8443

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD curl -f https://localhost:8443/api/v1/fleet/version -k || exit 1

# Copy custom entrypoint
COPY scripts/entrypoint.sh /entrypoint.sh
USER root
RUN chmod +x /entrypoint.sh
USER fleet

ENTRYPOINT ["/entrypoint.sh"]
CMD ["fleet", "serve"]
FROM python:3.12-slim

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1
ENV PY4WEB_APPS_FOLDER=/opt/py4web-apps

# Install system dependencies
RUN apt-get update && apt-get install -y \
    curl \
    wget \
    git \
    build-essential \
    libpq-dev \
    libffi-dev \
    libssl-dev \
    && rm -rf /var/lib/apt/lists/*

# Create application user
RUN useradd -m -s /bin/bash py4web && \
    mkdir -p /opt/py4web-apps && \
    chown -R py4web:py4web /opt/py4web-apps

# Install Python packages
COPY requirements.txt /tmp/
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# Copy application files
COPY py4web-app/ /opt/py4web-apps/maas_portal/
COPY config/ /opt/py4web-apps/maas_portal/config/
COPY scripts/ /usr/local/bin/
RUN chmod +x /usr/local/bin/*.sh

# Set ownership
RUN chown -R py4web:py4web /opt/py4web-apps

# Switch to application user
USER py4web
WORKDIR /opt/py4web-apps

# Expose port
EXPOSE 8000

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8000/ || exit 1

# Start py4web
CMD ["py4web", "run", "apps", "--host", "0.0.0.0", "--port", "8000", "--password_file", "password.txt"]
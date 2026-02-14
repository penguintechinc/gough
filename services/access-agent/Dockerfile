# Gough Access Agent Dockerfile
# Provides rssh server for remote shell access to managed resources

FROM python:3.13-slim-bookworm

# Labels
LABEL maintainer="Penguin Tech Inc <support@penguintech.io>"
LABEL description="Gough Access Agent - rssh server for remote shell access"
LABEL version="1.0.0"

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    openssh-client \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

# Create non-root user
RUN useradd -m -s /bin/bash -u 1000 gough-agent

# Create required directories
RUN mkdir -p /var/lib/gough-agent /var/log/gough-agent /app \
    && chown -R gough-agent:gough-agent /var/lib/gough-agent /var/log/gough-agent /app

# Set working directory
WORKDIR /app

# Copy requirements first for caching
COPY requirements.txt .

# Copy penguin-libs packages from local source (until published to PyPI)
# Build with: docker build --build-context penguin-libs=../../penguin-libs .
COPY --from=penguin-libs packages/python-libs /tmp/penguin-libs/python-libs
COPY --from=penguin-libs packages/python-licensing /tmp/penguin-libs/python-licensing

RUN pip install --no-cache-dir \
        /tmp/penguin-libs/python-libs \
        /tmp/penguin-libs/python-licensing && \
    pip install --no-cache-dir -r requirements.txt && \
    rm -rf /tmp/penguin-libs

# Copy application code
COPY agent/ ./agent/

# Switch to non-root user
USER gough-agent

# Environment variables
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV GOUGH_LOG_LEVEL=INFO

# Expose rssh port
EXPOSE 2222

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import socket; s=socket.socket(); s.connect(('localhost', 2222)); s.close()" || exit 1

# Run agent
CMD ["python", "-m", "agent.main"]

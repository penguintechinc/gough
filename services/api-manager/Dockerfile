# API Manager Dockerfile (Quart/ASGI)
# Multi-stage build for production

FROM python:3.13-slim-bookworm@sha256:281d39ff02e3b389401586134ff9e6572954687822a58055ec07a60ded4e0b7f AS builder

WORKDIR /app

# Install build dependencies including Rust for infisical-python
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install Rust (required for infisical-python compilation)
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y
ENV PATH="/root/.cargo/bin:$PATH"

# Copy requirements first for layer caching
COPY requirements.txt .

# Copy penguin-libs packages from local source (until published to PyPI)
COPY --from=penguin-libs packages/python-libs /tmp/penguin-libs/python-libs
COPY --from=penguin-libs packages/python-licensing /tmp/penguin-libs/python-licensing
COPY --from=penguin-libs packages/python-secrets /tmp/penguin-libs/python-secrets

# Create virtual environment and install dependencies
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir \
        /tmp/penguin-libs/python-libs \
        /tmp/penguin-libs/python-licensing \
        /tmp/penguin-libs/python-secrets && \
    pip install --no-cache-dir -r requirements.txt && \
    rm -rf /tmp/penguin-libs

# Production stage
FROM python:3.13-slim-bookworm@sha256:281d39ff02e3b389401586134ff9e6572954687822a58055ec07a60ded4e0b7f AS runtime

LABEL org.opencontainers.image.source="https://github.com/penguintechinc/gough"

WORKDIR /app

# Install runtime dependencies only
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    curl \
    openssh-client \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --shell /bin/bash appuser

# Drop base-image packaging tooling the runtime never uses: setuptools/wheel/jaraco.context
# and pip itself (pip vendors its own urllib3/msgpack copies, which carry CVEs). The app
# runs from /opt/venv and never invokes pip. Absolute path: ENV PATH may point at the venv.
RUN /usr/local/bin/python -m pip uninstall -y setuptools wheel jaraco.context && \
    rm -rf /usr/local/lib/python3.13/site-packages/pip /usr/local/lib/python3.13/site-packages/pip-*.dist-info \
           /usr/local/bin/pip /usr/local/bin/pip3 /usr/local/bin/pip3.13 \
           /usr/local/lib/python3.13/ensurepip/_bundled

# Copy virtual environment from builder
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
# Remove pip from the copied venv too (its vendored urllib3/msgpack carry CVEs; runtime never pip-installs).
RUN rm -rf /opt/venv/lib/python3.13/site-packages/pip /opt/venv/lib/python3.13/site-packages/pip-*.dist-info /opt/venv/bin/pip /opt/venv/bin/pip3 /opt/venv/bin/pip3.13


# Copy application code
COPY --chown=appuser:appuser . .

# Switch to non-root user
USER appuser

# Environment variables
ENV QUART_HOST=0.0.0.0
ENV QUART_PORT=5000
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# Expose port
EXPOSE 5000

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:5000/healthz')" || exit 1

# Run the application using the run.py entrypoint
CMD ["python", "run.py"]

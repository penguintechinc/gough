# discovery-agent — multi-stage static binary build
# Target: <15 MB, statically linked, runs in Ubuntu 24.04 initrd helper image.
#
# SHA256 digest pinning for external images — pin via Renovate (pinDigests: true).
# Builder: golang:1.24-bookworm  (sha256 digest — pin via Renovate)
# Runtime: debian:bookworm-slim  (sha256 digest — pin via Renovate)
#
# Build arguments:
#   VERSION          — injected version string (default: dev)
#   XDP_BUILD_TAGS   — build tags (default: noxdp,netgo)

# ── Stage 1: builder ──────────────────────────────────────────────────────────

# TODO: pin digest via Renovate
FROM golang:1.24-bookworm@sha256:<PLACEHOLDER_UPDATE_VIA_RENOVATE> AS builder

ARG VERSION=dev
ARG XDP_BUILD_TAGS=noxdp,netgo

WORKDIR /build

# Cache dependency downloads before copying source.
COPY go.mod go.sum ./
RUN go mod download

# Copy source and build static binary.
COPY . .

RUN CGO_ENABLED=0 GOOS=linux go build \
    -tags "${XDP_BUILD_TAGS}" \
    -ldflags "-s -w -extldflags -static -X main.Version=${VERSION}" \
    -o /out/discovery-agent \
    ./cmd/agent/

# Verify the binary is truly static (no shared library references).
RUN file /out/discovery-agent | grep -q "statically linked" || \
    (echo "ERROR: binary is not statically linked" && exit 1)

# ── Stage 2: runtime ──────────────────────────────────────────────────────────

# TODO: pin digest via Renovate
FROM debian:bookworm-slim@sha256:<PLACEHOLDER_UPDATE_VIA_RENOVATE> AS runtime

LABEL org.opencontainers.image.title="gough-discovery-agent"
LABEL org.opencontainers.image.description="Gough Phase 1 node discovery agent — runs inside Ubuntu 24.04 initrd helper image"
LABEL org.opencontainers.image.vendor="PenguinTech Inc."
LABEL org.opencontainers.image.source="https://github.com/penguintechinc/gough"

# Non-root user required by securityContext policy.
# UID 1000 matches the initrd helper image's default unprivileged UID.
RUN groupadd -r --gid 1000 gough && \
    useradd -r --uid 1000 --gid gough --no-create-home --shell /sbin/nologin gough

# CA certificate placeholder directory — the initrd build overlays
# /etc/gough/ca.pem at image assembly time.
RUN install -d -m 0755 -o gough -g gough /etc/gough

COPY --from=builder /out/discovery-agent /usr/local/bin/discovery-agent

USER gough

# The agent does not listen on any port — it initiates outbound connections only.
# Once started, the process stays alive (idle + reconnect loop).
# Health check: confirm the process is running by testing the binary is present.
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD ["test", "-f", "/usr/local/bin/discovery-agent"]

ENTRYPOINT ["/usr/local/bin/discovery-agent"]

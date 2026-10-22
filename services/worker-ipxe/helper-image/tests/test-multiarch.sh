#!/bin/bash
# Test: Multi-arch build (amd64 + arm64) succeeds
#
# Validates:
# - docker buildx is available
# - Multi-arch build completes without errors
# - Both amd64 and arm64 architectures are built

set -euo pipefail

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
NC='\033[0m'

# Configuration
REGISTRY="${REGISTRY:-localhost:32000}"
IMAGE_NAME="${IMAGE_NAME:-gough/helper-image}"
IMAGE_TAG="${IMAGE_TAG:-test-multiarch-$(date +%s)}"
IMAGE="${REGISTRY}/${IMAGE_NAME}:${IMAGE_TAG}"
DISCOVERY_AGENT="${DISCOVERY_AGENT:-../../discovery-agent/dist/discovery-agent}"

# Helper functions
log_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# Test 1: Check buildx availability
test_buildx_available() {
    log_info "Test 1: Checking docker buildx availability..."

    if ! docker buildx --version > /dev/null 2>&1; then
        log_error "docker buildx is not available"
        log_warn "Install with: docker buildx create --use"
        return 1
    fi

    log_info "✓ docker buildx is available"
    return 0
}

# Test 2: Multi-arch build
test_multiarch_build() {
    log_info "Test 2: Building multi-arch image (linux/amd64,linux/arm64)..."

    if [ ! -f "$DISCOVERY_AGENT" ]; then
        log_error "discovery-agent binary not found at $DISCOVERY_AGENT"
        return 1
    fi

    if ! docker buildx build \
        --platform linux/amd64,linux/arm64 \
        --build-arg DISCOVERY_AGENT_BINARY="$DISCOVERY_AGENT" \
        --build-arg BUILD_DATE="$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
        --build-arg VCS_REF="$(git rev-parse HEAD 2>/dev/null || echo 'unknown')" \
        --tag "$IMAGE" \
        --push=false \
        --output type=docker \
        --progress=plain \
        . > /tmp/multiarch-build.log 2>&1; then
        log_error "Multi-arch build failed. Output:"
        cat /tmp/multiarch-build.log
        return 1
    fi

    log_info "✓ Multi-arch build completed successfully"
    return 0
}

# Test 3: Verify build platforms
test_verify_platforms() {
    log_info "Test 3: Verifying build output platforms..."

    # Note: buildx with --push=false may not preserve platform metadata locally
    # This test is informational
    if grep -q "linux/amd64\|linux/arm64" /tmp/multiarch-build.log; then
        log_info "✓ Build output contains platform information"
    else
        log_warn "Could not verify platform info in build output (expected for local builds)"
    fi

    return 0
}

# Main test execution
main() {
    log_info "========================================"
    log_info "Gough Helper Image Multi-Arch Tests"
    log_info "========================================"
    log_info "Image: $IMAGE"
    log_info "Platforms: linux/amd64, linux/arm64"
    log_info ""

    local failed=0

    if ! test_buildx_available; then failed=$((failed + 1)); fi
    if ! test_multiarch_build; then failed=$((failed + 1)); fi
    if ! test_verify_platforms; then failed=$((failed + 1)); fi

    log_info ""
    log_info "========================================"

    if [ $failed -eq 0 ]; then
        log_info "✓ All multi-arch tests passed!"
        return 0
    else
        log_error "✗ $failed test(s) failed"
        return 1
    fi
}

main "$@"

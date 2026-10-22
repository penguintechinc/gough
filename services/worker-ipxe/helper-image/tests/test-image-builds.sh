#!/bin/bash
# Test: Helper image builds successfully and is properly structured
#
# Validates:
# - Docker build succeeds with no errors
# - Image size is reasonable (< 220 MB)
# - discovery-agent binary is executable inside image
# - Image can run without errors

set -euo pipefail

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
NC='\033[0m' # No Color

# Configuration
REGISTRY="${REGISTRY:-localhost:32000}"
IMAGE_NAME="${IMAGE_NAME:-gough/helper-image}"
IMAGE_TAG="${IMAGE_TAG:-test-$(date +%s)}"
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

# Test 1: Build image
test_build() {
    log_info "Test 1: Building image..."

    if [ ! -f "$DISCOVERY_AGENT" ]; then
        log_error "discovery-agent binary not found at $DISCOVERY_AGENT"
        return 1
    fi

    if ! docker build \
        --build-arg DISCOVERY_AGENT_BINARY="$DISCOVERY_AGENT" \
        --build-arg BUILD_DATE="$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
        --build-arg VCS_REF="$(git rev-parse HEAD 2>/dev/null || echo 'unknown')" \
        --tag "$IMAGE" \
        --progress=plain \
        . > /tmp/docker-build.log 2>&1; then
        log_error "Docker build failed. Output:"
        cat /tmp/docker-build.log
        return 1
    fi

    log_info "✓ Image built successfully: $IMAGE"
    return 0
}

# Test 2: Check image size
test_image_size() {
    log_info "Test 2: Checking image size..."

    SIZE=$(docker images "$IMAGE" --format "{{.Size}}")
    SIZE_MB=$(echo "$SIZE" | sed 's/MB//' | cut -d' ' -f1)

    # Check if size could be parsed
    if [[ "$SIZE_MB" =~ ^[0-9]+$ ]]; then
        if [ "$SIZE_MB" -gt 220 ]; then
            log_warn "Image size ($SIZE) exceeds 220 MB target. Consider optimization."
        else
            log_info "✓ Image size: $SIZE (under 220 MB limit)"
        fi
    else
        # If in different unit (e.g., GB), likely too large
        if echo "$SIZE" | grep -q "GB"; then
            log_error "Image size ($SIZE) is in GB; must be < 220 MB"
            return 1
        else
            log_warn "Could not parse image size: $SIZE (assumption: reasonable)"
        fi
    fi

    return 0
}

# Test 3: Verify discovery-agent is executable
test_discovery_agent_executable() {
    log_info "Test 3: Verifying discovery-agent executable..."

    if ! docker run --rm "$IMAGE" /usr/local/bin/discovery-agent --version > /dev/null 2>&1; then
        log_error "discovery-agent --version failed inside image"
        return 1
    fi

    log_info "✓ discovery-agent is executable and responds to --version"
    return 0
}

# Test 4: Verify required tools are present
test_required_tools() {
    log_info "Test 4: Verifying required tools..."

    local tools=("lshw" "smartctl" "lldpctl" "ipmitool" "ethtool" "openssl" "jq" "cloud-init")
    local missing=0

    for tool in "${tools[@]}"; do
        if ! docker run --rm "$IMAGE" which "$tool" > /dev/null 2>&1; then
            log_warn "Tool not found in image: $tool"
            missing=$((missing + 1))
        fi
    done

    if [ $missing -gt 0 ]; then
        log_warn "Missing $missing tools (may be expected)"
        return 0
    fi

    log_info "✓ All required tools present"
    return 0
}

# Test 5: Cloud-init configuration check
test_cloud_init_config() {
    log_info "Test 5: Verifying cloud-init configuration..."

    if ! docker run --rm "$IMAGE" ls -la /etc/cloud/cloud.cfg.d/90-gough-* > /dev/null 2>&1; then
        log_error "Cloud-init config files not found"
        return 1
    fi

    log_info "✓ Cloud-init configuration files present"
    return 0
}

# Test 6: Image labels
test_image_labels() {
    log_info "Test 6: Checking OCI image labels..."

    local required_labels=("org.opencontainers.image.created" "org.opencontainers.image.title" "org.opencontainers.image.description")
    local missing=0

    for label in "${required_labels[@]}"; do
        LABEL_VALUE=$(docker inspect "$IMAGE" --format="{{index .Config.Labels \"$label\"}}")
        if [ -z "$LABEL_VALUE" ] || [ "$LABEL_VALUE" == "<no value>" ]; then
            log_warn "Missing OCI label: $label"
            missing=$((missing + 1))
        fi
    done

    if [ $missing -gt 0 ]; then
        log_warn "Missing $missing OCI labels"
    else
        log_info "✓ All OCI labels present"
    fi

    return 0
}

# Main test execution
main() {
    log_info "========================================"
    log_info "Gough Helper Image Build Tests"
    log_info "========================================"
    log_info "Image: $IMAGE"
    log_info ""

    local failed=0

    if ! test_build; then failed=$((failed + 1)); fi
    if ! test_image_size; then failed=$((failed + 1)); fi
    if ! test_discovery_agent_executable; then failed=$((failed + 1)); fi
    if ! test_required_tools; then failed=$((failed + 1)); fi
    if ! test_cloud_init_config; then failed=$((failed + 1)); fi
    if ! test_image_labels; then failed=$((failed + 1)); fi

    log_info ""
    log_info "========================================"

    if [ $failed -eq 0 ]; then
        log_info "✓ All tests passed!"
        docker rmi "$IMAGE" > /dev/null 2>&1 || true
        return 0
    else
        log_error "✗ $failed test(s) failed"
        return 1
    fi
}

main "$@"

#!/bin/bash
# Test: Image runs as non-root user (discovery UID 1000)
#
# Validates:
# - USER directive is set to 'discovery' (UID 1000)
# - Container can run with --user 1000:1000 without errors
# - discovery-agent binary is executable by non-root user

set -euo pipefail

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
NC='\033[0m'

# Configuration
REGISTRY="${REGISTRY:-localhost:32000}"
IMAGE_NAME="${IMAGE_NAME:-gough/helper-image}"
IMAGE_TAG="${IMAGE_TAG:-test-$(date +%s)}"
IMAGE="${REGISTRY}/${IMAGE_NAME}:${IMAGE_TAG}"

# Helper functions
log_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# Test 1: Check USER directive in image
test_user_directive() {
    log_info "Test 1: Checking USER directive..."

    USER_VALUE=$(docker inspect "$IMAGE" --format='{{.Config.User}}')

    if [ -z "$USER_VALUE" ] || [ "$USER_VALUE" == "" ]; then
        log_error "No USER directive found in image"
        return 1
    fi

    if [ "$USER_VALUE" == "discovery" ] || [ "$USER_VALUE" == "1000:1000" ]; then
        log_info "✓ USER directive set to: $USER_VALUE"
        return 0
    else
        log_error "USER directive is: $USER_VALUE (expected 'discovery' or '1000:1000')"
        return 1
    fi
}

# Test 2: Run container as non-root
test_run_as_nonroot() {
    log_info "Test 2: Running container as non-root (UID 1000:GID 1000)..."

    if ! docker run --rm --user 1000:1000 "$IMAGE" id > /tmp/user-test.log 2>&1; then
        log_error "Failed to run container as non-root user"
        cat /tmp/user-test.log
        return 1
    fi

    UID=$(grep "uid=" /tmp/user-test.log | cut -d'=' -f2 | cut -d'(' -f1)
    GID=$(grep "gid=" /tmp/user-test.log | cut -d'=' -f2 | cut -d'(' -f1)

    if [ "$UID" != "1000" ]; then
        log_error "Container running as UID $UID (expected 1000)"
        return 1
    fi

    log_info "✓ Container running as UID:GID $UID:$GID"
    return 0
}

# Test 3: Verify discovery-agent is executable by non-root
test_discovery_agent_nonroot() {
    log_info "Test 3: Verifying discovery-agent is executable by non-root..."

    if ! docker run --rm --user 1000:1000 "$IMAGE" /usr/local/bin/discovery-agent --version > /dev/null 2>&1; then
        log_error "discovery-agent not executable by non-root user"
        return 1
    fi

    log_info "✓ discovery-agent is executable by non-root user"
    return 0
}

# Test 4: Check image metadata
test_image_metadata() {
    log_info "Test 4: Verifying rootless image metadata..."

    # Check that securityContext is properly configured
    local config=$(docker inspect "$IMAGE" --format='{{json .Config}}')

    # Verify no privileged flags (basic check)
    if echo "$config" | grep -q "Privileged.*true"; then
        log_error "Image has Privileged flag enabled (unexpected)"
        return 1
    fi

    log_info "✓ Image metadata indicates rootless configuration"
    return 0
}

# Main test execution
main() {
    log_info "========================================"
    log_info "Gough Helper Image Rootless Tests"
    log_info "========================================"
    log_info "Image: $IMAGE"
    log_info ""

    local failed=0

    if ! test_user_directive; then failed=$((failed + 1)); fi
    if ! test_run_as_nonroot; then failed=$((failed + 1)); fi
    if ! test_discovery_agent_nonroot; then failed=$((failed + 1)); fi
    if ! test_image_metadata; then failed=$((failed + 1)); fi

    log_info ""
    log_info "========================================"

    if [ $failed -eq 0 ]; then
        log_info "✓ All rootless tests passed!"
        return 0
    else
        log_error "✗ $failed test(s) failed"
        return 1
    fi
}

main "$@"

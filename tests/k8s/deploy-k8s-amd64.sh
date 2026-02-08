#!/bin/bash
# Gough Kubernetes Deployment Script (AMD64 only)
#
# Usage:
#   ./deploy-k8s-amd64.sh                                  # Deploy to default registry
#   ./deploy-k8s-amd64.sh registry.penguintech.io staging # Deploy to staging namespace
#
# This version builds AMD64 only to avoid arm64 registry DNS issues

set -e

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

# Configuration
REGISTRY="${1:-registry.penguintech.io}"
NAMESPACE="${2:-gough}"
VERSION="${VERSION:-$(cat .version 2>/dev/null || echo 'development')}"
TIMESTAMP=$(date +%s)
LOG_DIR="/tmp/gough-k8s-deploy-${TIMESTAMP}"

mkdir -p "$LOG_DIR"

# Logging
log_info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

log_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

log_warning() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

# Check prerequisites
check_prerequisites() {
    log_info "Checking prerequisites..."

    if ! command -v docker &> /dev/null; then
        log_error "Docker is not installed"
        exit 1
    fi

    if ! command -v kubectl &> /dev/null; then
        log_error "kubectl is not installed"
        exit 1
    fi

    if ! kubectl cluster-info &> /dev/null; then
        log_error "kubectl cannot connect to cluster"
        exit 1
    fi

    log_success "Prerequisites check passed"
}

# Build amd64-only images
build_images() {
    log_info "Building amd64 images..."

    # Build and push api-manager
    log_info "Building api-manager image..."
    docker build \
        --tag "${REGISTRY}/gough/api-manager:${VERSION}" \
        --tag "${REGISTRY}/gough/api-manager:latest" \
        -f services/api-manager/Dockerfile \
        services/api-manager/ \
        2>&1 | tee "${LOG_DIR}/build-api-manager.log"
    docker push "${REGISTRY}/gough/api-manager:${VERSION}" 2>&1 | tee -a "${LOG_DIR}/build-api-manager.log"
    docker push "${REGISTRY}/gough/api-manager:latest" 2>&1 | tee -a "${LOG_DIR}/build-api-manager.log"

    # Build and push webui
    log_info "Building webui image..."
    docker build \
        --tag "${REGISTRY}/gough/webui:${VERSION}" \
        --tag "${REGISTRY}/gough/webui:latest" \
        -f services/webui/Dockerfile \
        services/webui/ \
        2>&1 | tee "${LOG_DIR}/build-webui.log"
    docker push "${REGISTRY}/gough/webui:${VERSION}" 2>&1 | tee -a "${LOG_DIR}/build-webui.log"
    docker push "${REGISTRY}/gough/webui:latest" 2>&1 | tee -a "${LOG_DIR}/build-webui.log"

    # Build and push worker-ipxe
    log_info "Building worker-ipxe image..."
    docker build \
        --tag "${REGISTRY}/gough/worker-ipxe:${VERSION}" \
        --tag "${REGISTRY}/gough/worker-ipxe:latest" \
        -f services/worker-ipxe/Dockerfile \
        services/worker-ipxe/ \
        2>&1 | tee "${LOG_DIR}/build-worker-ipxe.log"
    docker push "${REGISTRY}/gough/worker-ipxe:${VERSION}" 2>&1 | tee -a "${LOG_DIR}/build-worker-ipxe.log"
    docker push "${REGISTRY}/gough/worker-ipxe:latest" 2>&1 | tee -a "${LOG_DIR}/build-worker-ipxe.log"

    log_success "Images built and pushed to ${REGISTRY}"
}

# Create namespace if it doesn't exist
create_namespace() {
    log_info "Ensuring namespace ${NAMESPACE} exists..."

    if ! kubectl get namespace "$NAMESPACE" &> /dev/null; then
        kubectl create namespace "$NAMESPACE"
        log_success "Created namespace ${NAMESPACE}"
    else
        log_info "Namespace ${NAMESPACE} already exists"
    fi
}

# Apply Kubernetes manifests
apply_manifests() {
    log_info "Applying Kubernetes manifests..."

    # Update image references in manifests (with defaults)
    export REGISTRY_URL="${REGISTRY:-registry.penguintech.io}"
    export IMAGE_VERSION="${VERSION:-latest}"

    # Apply manifests in order
    for manifest in infrastructure/k8s/*.yaml; do
        if [ -f "$manifest" ]; then
            log_info "Applying $(basename $manifest)..."
            envsubst < "$manifest" | kubectl apply -n "$NAMESPACE" -f - \
                2>&1 | tee -a "${LOG_DIR}/apply-manifests.log"
        fi
    done

    log_success "Manifests applied"
}

# Wait for deployments to be ready
wait_for_deployments() {
    log_info "Waiting for deployments to be ready..."

    local deployments=("api-manager" "webui")

    for deployment in "${deployments[@]}"; do
        log_info "Waiting for $deployment..."
        if kubectl wait --for=condition=available \
            --timeout=300s \
            -n "$NAMESPACE" \
            deployment/"$deployment" 2>&1 | tee -a "${LOG_DIR}/wait-deployments.log"; then
            log_success "$deployment is ready"
        else
            log_error "$deployment failed to become ready"
            kubectl logs -n "$NAMESPACE" -l app="$deployment" --tail=50
            return 1
        fi
    done

    log_success "All deployments are ready"
}

# Validate deployment
validate_deployment() {
    log_info "Validating deployment..."

    # Check pod status
    log_info "Pod status:"
    kubectl get pods -n "$NAMESPACE" -o wide

    # Check services
    log_info "Services:"
    kubectl get services -n "$NAMESPACE"

    log_success "Deployment validation complete"
}

# Rollback on failure
rollback() {
    log_error "Deployment failed, rolling back..."

    kubectl rollout undo deployment/api-manager -n "$NAMESPACE" || true
    kubectl rollout undo deployment/webui -n "$NAMESPACE" || true

    log_info "Rollback complete"
}

# Main execution
main() {
    log_info "=== Gough Kubernetes Deployment (AMD64) ==="
    log_info "Registry: $REGISTRY"
    log_info "Namespace: $NAMESPACE"
    log_info "Version: $VERSION"
    echo ""

    check_prerequisites
    build_images
    create_namespace
    apply_manifests

    if wait_for_deployments && validate_deployment; then
        log_success "Deployment completed successfully!"
        log_info "Logs saved to: $LOG_DIR"
        exit 0
    else
        rollback
        log_error "Deployment failed. Check logs in $LOG_DIR"
        exit 1
    fi
}

# Trap errors and rollback
trap rollback ERR

main

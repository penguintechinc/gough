#!/bin/bash
# Gough Kubernetes Deployment Script
#
# Usage:
#   ./deploy-k8s.sh                                    # Deploy to default registry
#   ./deploy-k8s.sh registry.example.com              # Deploy to custom registry
#   ./deploy-k8s.sh registry.penguintech.io staging   # Deploy to staging namespace
#
# This script:
# 1. Builds multi-arch Docker images
# 2. Pushes to specified registry
# 3. Applies Kubernetes manifests
# 4. Validates deployment health

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

# Build multi-arch images
build_images() {
    log_info "Building multi-arch images..."

    # Create buildx builder if it doesn't exist
    if ! docker buildx inspect gough-builder &> /dev/null; then
        log_info "Creating buildx builder..."
        docker buildx create --name gough-builder --use
    else
        docker buildx use gough-builder
    fi

    # Build api-manager
    log_info "Building api-manager image..."
    docker buildx build \
        --platform linux/amd64,linux/arm64 \
        --tag "${REGISTRY}/gough/api-manager:${VERSION}" \
        --tag "${REGISTRY}/gough/api-manager:latest" \
        --push \
        -f services/api-manager/Dockerfile \
        services/api-manager/ \
        2>&1 | tee "${LOG_DIR}/build-api-manager.log"

    # Build webui
    log_info "Building webui image..."
    docker buildx build \
        --platform linux/amd64,linux/arm64 \
        --tag "${REGISTRY}/gough/webui:${VERSION}" \
        --tag "${REGISTRY}/gough/webui:latest" \
        --push \
        -f services/webui/Dockerfile \
        services/webui/ \
        2>&1 | tee "${LOG_DIR}/build-webui.log"

    # Build worker-ipxe
    log_info "Building worker-ipxe image..."
    docker buildx build \
        --platform linux/amd64,linux/arm64 \
        --tag "${REGISTRY}/gough/worker-ipxe:${VERSION}" \
        --tag "${REGISTRY}/gough/worker-ipxe:latest" \
        --push \
        -f services/worker-ipxe/Dockerfile \
        services/worker-ipxe/ \
        2>&1 | tee "${LOG_DIR}/build-worker-ipxe.log"

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

    # Update image references in manifests
    export REGISTRY_URL="$REGISTRY"
    export IMAGE_VERSION="$VERSION"

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

    local deployments=("api-manager" "webui" "worker-ipxe")

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

    # Get API endpoint
    API_ENDPOINT=$(kubectl get service api-manager -n "$NAMESPACE" -o jsonpath='{.status.loadBalancer.ingress[0].ip}' 2>/dev/null || echo "pending")

    if [ "$API_ENDPOINT" != "pending" ]; then
        log_info "Testing API endpoint at http://${API_ENDPOINT}..."
        if curl -f -s "http://${API_ENDPOINT}/api/v1/status" > /dev/null; then
            log_success "API is responding"
        else
            log_warning "API not responding yet"
        fi
    else
        log_warning "LoadBalancer IP pending"
    fi

    log_success "Deployment validation complete"
}

# Rollback on failure
rollback() {
    log_error "Deployment failed, rolling back..."

    kubectl rollout undo deployment/api-manager -n "$NAMESPACE" || true
    kubectl rollout undo deployment/webui -n "$NAMESPACE" || true
    kubectl rollout undo deployment/worker-ipxe -n "$NAMESPACE" || true

    log_info "Rollback complete"
}

# Main execution
main() {
    log_info "=== Gough Kubernetes Deployment ==="
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

#!/bin/bash

################################################################################
# Deploy Script for Gough - Beta Environment (dal2 cluster)
#
# This script handles building, pushing, and deploying Gough services to the
# beta environment on the dal2 Kubernetes cluster.
#
# Usage: ./deploy-beta.sh [OPTIONS]
# Options:
#   --tag <tag>              Build tag (default: latest)
#   --service <service>      Deploy specific service (api-manager, webui, worker-ipxe)
#   --skip-build             Skip building and pushing images
#   --dry-run                Show what would be deployed without applying changes
#   --rollback               Rollback to previous deployment
#   --help                   Show this help message
################################################################################

set -euo pipefail

################################################################################
# Configuration
################################################################################

RELEASE_NAME="gough"
NAMESPACE="gough-beta"
CHART_PATH="k8s/helm"
IMAGE_REGISTRY="registry-dal2.penguintech.io"
KUBE_CONTEXT="dal2-beta"
APP_HOST="gough.penguintech.cloud"

# Services to deploy
SERVICES=("api-manager" "webui" "worker-ipxe")
TARGET_SERVICE=""

# Build options
BUILD_TAG="latest"
SKIP_BUILD=false
DRY_RUN=false
ROLLBACK=false

################################################################################
# Color helpers
################################################################################

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

log_info() {
    echo -e "${BLUE}[INFO]${NC} $*"
}

log_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $*"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $*"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $*"
}

################################################################################
# Help function
################################################################################

show_help() {
    cat << 'EOF'
Usage: ./deploy-beta.sh [OPTIONS]

Deploy Gough services to the beta environment (dal2 cluster).

Options:
  --tag <tag>              Build tag (default: latest)
  --service <service>      Deploy specific service (api-manager, webui, worker-ipxe)
                           If not specified, all services are deployed
  --skip-build             Skip building and pushing Docker images
  --dry-run                Show deployment manifest without applying
  --rollback               Rollback to previous deployment
  --help                   Show this help message

Examples:
  # Deploy all services with latest tag
  ./deploy-beta.sh

  # Deploy specific service with custom tag
  ./deploy-beta.sh --service api-manager --tag v1.2.3

  # Skip build and deploy only (images must already exist)
  ./deploy-beta.sh --skip-build

  # Dry run to preview deployment
  ./deploy-beta.sh --dry-run

  # Rollback to previous version
  ./deploy-beta.sh --rollback

EOF
}

################################################################################
# Argument parsing
################################################################################

while [[ $# -gt 0 ]]; do
    case $1 in
        --tag)
            BUILD_TAG="$2"
            shift 2
            ;;
        --service)
            TARGET_SERVICE="$2"
            shift 2
            ;;
        --skip-build)
            SKIP_BUILD=true
            shift
            ;;
        --dry-run)
            DRY_RUN=true
            shift
            ;;
        --rollback)
            ROLLBACK=true
            shift
            ;;
        --help)
            show_help
            exit 0
            ;;
        *)
            log_error "Unknown option: $1"
            show_help
            exit 1
            ;;
    esac
done

################################################################################
# Prerequisite checks
################################################################################

check_prerequisites() {
    log_info "Checking prerequisites..."

    # Check kubectl
    if ! command -v kubectl &> /dev/null; then
        log_error "kubectl not found. Please install kubectl."
        exit 1
    fi

    # Check helm
    if ! command -v helm &> /dev/null; then
        log_error "helm not found. Please install helm."
        exit 1
    fi

    # Check docker (if not skipping build)
    if [[ $SKIP_BUILD == false ]]; then
        if ! command -v docker &> /dev/null; then
            log_error "docker not found. Please install docker."
            exit 1
        fi
    fi

    # Check kubernetes context
    log_info "Checking Kubernetes context..."
    current_context=$(kubectl config current-context)
    if [[ "$current_context" != "$KUBE_CONTEXT" ]]; then
        log_warn "Current context is '$current_context', but target is '$KUBE_CONTEXT'"
        log_info "Switching context to $KUBE_CONTEXT..."
        kubectl config use-context "$KUBE_CONTEXT"
    fi

    # Check namespace exists
    if ! kubectl get namespace "$NAMESPACE" &> /dev/null; then
        log_warn "Namespace $NAMESPACE does not exist. Creating..."
        kubectl create namespace "$NAMESPACE"
    fi

    log_success "Prerequisites check passed"
}

################################################################################
# Build and push images
################################################################################

build_and_push_images() {
    if [[ $SKIP_BUILD == true ]]; then
        log_info "Skipping image build and push"
        return
    fi

    log_info "Building and pushing Docker images..."

    local services_to_build=("${SERVICES[@]}")
    if [[ -n "$TARGET_SERVICE" ]]; then
        services_to_build=("$TARGET_SERVICE")
    fi

    for service in "${services_to_build[@]}"; do
        local dockerfile_path="services/$service/Dockerfile"
        local image_name="$IMAGE_REGISTRY/gough/$service:$BUILD_TAG"

        if [[ ! -f "$dockerfile_path" ]]; then
            log_error "Dockerfile not found for service: $service at $dockerfile_path"
            exit 1
        fi

        log_info "Building image: $image_name"
        docker build \
            --tag "$image_name" \
            --file "$dockerfile_path" \
            --build-arg BUILD_DATE="$(date -u +'%Y-%m-%dT%H:%M:%SZ')" \
            "services/$service"

        log_info "Pushing image: $image_name"
        docker push "$image_name"

        log_success "Image built and pushed: $image_name"
    done
}

################################################################################
# Deployment
################################################################################

do_deploy() {
    log_info "Deploying to $NAMESPACE namespace..."

    local services_to_deploy=("${SERVICES[@]}")
    if [[ -n "$TARGET_SERVICE" ]]; then
        services_to_deploy=("$TARGET_SERVICE")
    fi

    local helm_args=(
        "upgrade"
        "--install"
        "--namespace" "$NAMESPACE"
        "--create-namespace"
        "--timeout" "5m"
    )

    if [[ $DRY_RUN == true ]]; then
        helm_args+=("--dry-run" "--debug")
    fi

    for service in "${services_to_deploy[@]}"; do
        local chart_dir="$CHART_PATH/$service"
        local values_base="$chart_dir/values.yaml"
        local values_beta="$chart_dir/values-beta.yaml"

        if [[ ! -f "$values_base" ]]; then
            log_error "Base values file not found: $values_base"
            exit 1
        fi

        if [[ ! -f "$values_beta" ]]; then
            log_error "Beta values file not found: $values_beta"
            exit 1
        fi

        log_info "Deploying $service..."

        helm "${helm_args[@]}" \
            "$RELEASE_NAME-$service" \
            "$chart_dir" \
            --values "$values_base" \
            --values "$values_beta" \
            --set-string "image.tag=$BUILD_TAG" \
            --set-string "image.registry=$IMAGE_REGISTRY"

        if [[ $DRY_RUN == false ]]; then
            log_success "Deployed $service successfully"
        else
            log_info "Dry run for $service completed"
        fi
    done

    if [[ $DRY_RUN == false ]]; then
        log_success "All services deployed successfully"
    fi
}

################################################################################
# Rollback
################################################################################

do_rollback() {
    log_info "Rolling back deployment..."

    local services_to_rollback=("${SERVICES[@]}")
    if [[ -n "$TARGET_SERVICE" ]]; then
        services_to_rollback=("$TARGET_SERVICE")
    fi

    for service in "${services_to_rollback[@]}"; do
        log_info "Rolling back $service..."

        helm rollback "$RELEASE_NAME-$service" \
            --namespace "$NAMESPACE"

        log_success "Rolled back $service"
    done

    log_success "Rollback completed successfully"
}

################################################################################
# Verification
################################################################################

verify_deployment() {
    log_info "Verifying deployment..."

    local services_to_verify=("${SERVICES[@]}")
    if [[ -n "$TARGET_SERVICE" ]]; then
        services_to_verify=("$TARGET_SERVICE")
    fi

    local max_retries=30
    local retry_count=0

    for service in "${services_to_verify[@]}"; do
        log_info "Waiting for $service to be ready..."

        retry_count=0
        while [[ $retry_count -lt $max_retries ]]; do
            local ready_replicas=$(kubectl get deployment \
                -n "$NAMESPACE" \
                -l "app.kubernetes.io/name=$service" \
                -o jsonpath='{.items[0].status.readyReplicas}' 2>/dev/null || echo "0")

            local desired_replicas=$(kubectl get deployment \
                -n "$NAMESPACE" \
                -l "app.kubernetes.io/name=$service" \
                -o jsonpath='{.items[0].spec.replicas}' 2>/dev/null || echo "0")

            if [[ "$ready_replicas" == "$desired_replicas" && "$desired_replicas" != "0" ]]; then
                log_success "$service is ready (replicas: $ready_replicas/$desired_replicas)"
                break
            fi

            retry_count=$((retry_count + 1))
            if [[ $retry_count -eq $max_retries ]]; then
                log_error "$service failed to become ready after $((max_retries * 10)) seconds"
                return 1
            fi

            sleep 10
        done
    done

    log_success "All services verified successfully"

    # Show service endpoints
    log_info "Service endpoints:"
    kubectl get ingress -n "$NAMESPACE" -o wide 2>/dev/null || true
}

################################################################################
# Main
################################################################################

main() {
    log_info "=========================================="
    log_info "Gough Deployment - Beta Environment"
    log_info "=========================================="
    log_info "Release: $RELEASE_NAME"
    log_info "Namespace: $NAMESPACE"
    log_info "Context: $KUBE_CONTEXT"
    log_info "Registry: $IMAGE_REGISTRY"
    log_info "Build Tag: $BUILD_TAG"
    if [[ -n "$TARGET_SERVICE" ]]; then
        log_info "Target Service: $TARGET_SERVICE"
    fi
    log_info ""

    check_prerequisites

    if [[ $ROLLBACK == true ]]; then
        do_rollback
    else
        build_and_push_images
        do_deploy
        if [[ $DRY_RUN == false ]]; then
            verify_deployment
        fi
    fi

    log_success "=========================================="
    log_success "Deployment completed successfully"
    log_success "=========================================="
}

main "$@"

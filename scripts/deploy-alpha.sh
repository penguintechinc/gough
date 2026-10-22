#!/usr/bin/env bash
# Deploy the gough hub to the local-alpha context via Helm only (no Kustomize).
# Build is NOT done here: images must already exist locally as
# ghcr.io/penguintechinc/gough/<svc>:alpha-local (see the docker-build make targets).
#
# Usage:
#   scripts/deploy-alpha.sh               # import images, helm upgrade --install
#   scripts/deploy-alpha.sh --clean       # delete namespace gough first, then deploy
#   scripts/deploy-alpha.sh --skip-import # images already in the cluster runtime
set -euo pipefail

readonly KUBE_CONTEXT="local-alpha"          # never dal2alpha or any other context
readonly NAMESPACE="gough"
readonly RELEASE="gough"
readonly CHART_DIR="k8s/helm/gough"
readonly VALUES_FILE="k8s/helm/gough/alpha.yml"
readonly IMAGE_REGISTRY="ghcr.io/penguintechinc/gough"
readonly IMAGE_TAG="alpha-local"
readonly SERVICES=(api-manager webui worker-ipxe)
readonly KEYSTORE_SECRET="api-manager-oidc-keystore"

CLEAN=false
SKIP_IMPORT=false

log()  { printf '%s\n' "$*"; }
die()  { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

usage() {
    sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'
}

parse_args() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --clean)       CLEAN=true ;;
            --skip-import) SKIP_IMPORT=true ;;
            -h|--help)     usage; exit 0 ;;
            *)             die "unknown argument: $1" ;;
        esac
        shift
    done
}

require_tool() {
    command -v "$1" >/dev/null 2>&1 || die "$1 not found in PATH"
}

preflight() {
    require_tool helm
    require_tool kubectl
    require_tool docker
    local contexts
    contexts="$(kubectl config get-contexts -o name)"
    grep -qx "$KUBE_CONTEXT" <<<"$contexts" \
        || die "kube context '$KUBE_CONTEXT' does not exist"
    for svc in "${SERVICES[@]}"; do
        docker image inspect "$IMAGE_REGISTRY/$svc:$IMAGE_TAG" >/dev/null \
            || die "image missing: $IMAGE_REGISTRY/$svc:$IMAGE_TAG (build it first; this script does not build)"
    done
    log "preflight: helm, kubectl, docker present; context '$KUBE_CONTEXT' exists; ${#SERVICES[@]} images found"
}

import_images() {
    if [[ $SKIP_IMPORT == true ]]; then
        log "import: skipped (--skip-import)"
        return
    fi
    require_tool microk8s
    for svc in "${SERVICES[@]}"; do
        log "import: $IMAGE_REGISTRY/$svc:$IMAGE_TAG"
        docker save "$IMAGE_REGISTRY/$svc:$IMAGE_TAG" | microk8s ctr image import -
    done
}

ensure_namespace() {
    if [[ $CLEAN == true ]]; then
        log "clean: deleting namespace $NAMESPACE on $KUBE_CONTEXT"
        kubectl --context "$KUBE_CONTEXT" delete namespace "$NAMESPACE" --ignore-not-found=true --wait=true
    fi
    kubectl --context "$KUBE_CONTEXT" get namespace "$NAMESPACE" >/dev/null 2>&1 \
        || kubectl --context "$KUBE_CONTEXT" create namespace "$NAMESPACE"
    kubectl --context "$KUBE_CONTEXT" label namespace "$NAMESPACE" \
        pod-security.kubernetes.io/enforce=restricted --overwrite >/dev/null
}

check_keystore() {
    # api-manager mounts a pre-provisioned ES256 keystore; the pod cannot start without it.
    kubectl --context "$KUBE_CONTEXT" -n "$NAMESPACE" get secret "$KEYSTORE_SECRET" >/dev/null 2>&1 \
        || die "secret $KEYSTORE_SECRET missing in $NAMESPACE - create the one-time ES256 keystore first (k8s/helm/api-manager/README.md)"
    log "keystore: $KEYSTORE_SECRET present"
}

helm_deploy() {
    helm dependency update "$CHART_DIR"
    helm upgrade --install "$RELEASE" "$CHART_DIR" \
        -f "$VALUES_FILE" \
        -n "$NAMESPACE" \
        --create-namespace \
        --kube-context "$KUBE_CONTEXT" \
        --wait --timeout 10m
    log "helm: release $RELEASE deployed to $KUBE_CONTEXT/$NAMESPACE"
}

main() {
    parse_args "$@"
    cd "$(dirname "$0")/.."
    preflight
    import_images
    ensure_namespace
    check_keystore
    helm_deploy
}

main "$@"

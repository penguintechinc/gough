#!/usr/bin/env bash
# Build the Cilium CRD bundle for one release tag from the upstream source tree.
# Run on a CONNECTED machine to stock the operator mirror; never run on the airgapped host.
# Usage: tools/build-cilium-crd-bundle.sh <version> [out_dir]   e.g. 1.20.1
# Output: <out_dir>/cilium-crds-v<version>.yaml (multi-document) and its sha256 on stdout.
set -euo pipefail

REPO="cilium/cilium"
CRD_DIRS=(
  "pkg/k8s/apis/cilium.io/client/crds/v2"
  "pkg/k8s/apis/cilium.io/client/crds/v2alpha1"
)

# Print usage and exit non-zero.
usage() {
  echo "usage: $0 <version> [out_dir]" >&2
  exit 2
}

# Download every *.yaml CRD in one upstream directory at the given tag, appending to the bundle.
append_dir() {
  local dir="$1" tag="$2" bundle="$3" name count=0
  local -a names
  mapfile -t names < <(gh api "repos/${REPO}/contents/${dir}?ref=${tag}" --jq '.[] | select(.type == "file") | .name' | grep -E '\.yaml$' | sort)
  (( ${#names[@]} > 0 )) || { echo "no CRD yaml files under ${dir} at ${tag}" >&2; exit 1; }
  for name in "${names[@]}"; do
    {
      printf -- '---\n# source: %s/%s @ %s\n' "$dir" "$name" "$tag"
      gh api -H "Accept: application/vnd.github.raw" "repos/${REPO}/contents/${dir}/${name}?ref=${tag}"
      printf '\n'
    } >> "$bundle"
    count=$((count + 1))
  done
  echo "$count"
}

main() {
  (( $# >= 1 && $# <= 2 )) || usage
  local version="$1" out_dir="${2:-dist}"
  [[ "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "version must be X.Y.Z, got: $version" >&2; exit 2; }
  local tag="v${version}"
  local bundle="${out_dir}/cilium-crds-${tag}.yaml"
  mkdir -p "$out_dir"
  : > "$bundle"
  local total=0 dir n
  for dir in "${CRD_DIRS[@]}"; do
    n="$(append_dir "$dir" "$tag" "$bundle")"
    total=$((total + n))
  done
  [[ -s "$bundle" ]] || { echo "bundle is empty: $bundle" >&2; exit 1; }
  local sha
  sha="$(sha256sum "$bundle" | awk '{print $1}')"
  echo "tag=${tag} files=${total} bundle=${bundle}"
  echo "sha256=${sha}"
}

main "$@"

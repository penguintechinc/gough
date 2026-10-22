# Gough Helper Image Build Targets
#
# Targets:
#   build              - Build amd64 image locally
#   build-multiarch    - Build amd64 + arm64 images (requires buildx)
#   extract-kernel     - Extract kernel + initrd for HTTP serving
#   sbom              - Generate CycloneDX SBOM via syft
#   sign              - Sign image with cosign (keyless OIDC)
#   clean             - Remove build artifacts
#
# Environment:
#   REGISTRY          - Docker registry (default: localhost:32000 for alpha, ghcr.io for CI)
#   IMAGE_TAG         - Image tag (default: latest, overridden by CI with git-sha)
#   DISCOVERY_AGENT   - Path to discovery-agent binary (default: ../../discovery-agent/dist/discovery-agent)
#   BUILD_DATE        - Build timestamp (auto-detected from git)
#   VCS_REF           - Git commit SHA (auto-detected)

SHELL := /bin/bash
.PHONY: build build-multiarch extract-kernel sbom sign clean

# Build configuration
REGISTRY ?= localhost:32000
IMAGE_NAME ?= gough/helper-image
IMAGE_TAG ?= latest
IMAGE ?= $(REGISTRY)/$(IMAGE_NAME):$(IMAGE_TAG)

DISCOVERY_AGENT ?= ../../discovery-agent/dist/discovery-agent
BUILD_DATE ?= $(shell git log -1 --format=%ai 2>/dev/null || date -u +%Y-%m-%dT%H:%M:%SZ)
VCS_REF ?= $(shell git rev-parse HEAD 2>/dev/null || echo "unknown")

# Extract paths
EXTRACT_DIR ?= ./dist
KERNEL_FILE ?= $(EXTRACT_DIR)/vmlinuz-helper
INITRD_FILE ?= $(EXTRACT_DIR)/initrd-helper

# Validation targets
.PHONY: validate
validate:
	@echo "Validating build configuration..."
	@if [ ! -f "$(DISCOVERY_AGENT)" ]; then \
		echo "ERROR: discovery-agent binary not found at $(DISCOVERY_AGENT)"; \
		echo "Please build discovery-agent first: cd ../../discovery-agent && make build"; \
		exit 1; \
	fi
	@file $(DISCOVERY_AGENT) | grep -q "ELF" || (echo "ERROR: $(DISCOVERY_AGENT) is not a valid ELF binary"; exit 1)
	@echo "✓ discovery-agent binary validated"
	@echo "✓ Build date: $(BUILD_DATE)"
	@echo "✓ VCS ref: $(VCS_REF)"

# Build target: amd64 only (local development)
build: validate
	@echo "Building helper image (amd64): $(IMAGE)"
	@docker build \
		--build-arg DISCOVERY_AGENT_BINARY=$(DISCOVERY_AGENT) \
		--build-arg BUILD_DATE="$(BUILD_DATE)" \
		--build-arg VCS_REF="$(VCS_REF)" \
		--label org.opencontainers.image.created="$(BUILD_DATE)" \
		--label org.opencontainers.image.revision="$(VCS_REF)" \
		--tag $(IMAGE) \
		--progress=plain \
		.
	@echo "✓ Image built: $(IMAGE)"
	@docker images $(IMAGE) --no-trunc
	@echo "Run 'make extract-kernel' to extract vmlinuz + initrd for HTTP serving"

# Build target: multi-arch (amd64 + arm64) - requires buildx
build-multiarch: validate
	@echo "Building multi-arch image (linux/amd64,linux/arm64): $(IMAGE)"
	@which docker-buildx > /dev/null || (echo "ERROR: docker buildx not available"; exit 1)
	@docker buildx build \
		--platform linux/amd64,linux/arm64 \
		--build-arg DISCOVERY_AGENT_BINARY=$(DISCOVERY_AGENT) \
		--build-arg BUILD_DATE="$(BUILD_DATE)" \
		--build-arg VCS_REF="$(VCS_REF)" \
		--tag $(IMAGE) \
		--push=false \
		--progress=plain \
		.
	@echo "✓ Multi-arch image built: $(IMAGE)"
	@echo "Note: Images not pushed to registry (remove --push=false to upload)"

# Extract kernel + initrd from image for HTTP serving
# These files are served by worker-ipxe during Phase 1 netboot
extract-kernel: build
	@echo "Extracting kernel + initrd from built image..."
	@mkdir -p $(EXTRACT_DIR)
	@CONTAINER_ID=$$(docker create $(IMAGE)); \
	docker cp $$CONTAINER_ID:/boot/vmlinuz-* $(KERNEL_FILE) 2>/dev/null || \
		docker cp $$CONTAINER_ID:/vmlinuz $(KERNEL_FILE) || \
		(echo "ERROR: Could not extract kernel"; exit 1); \
	docker cp $$CONTAINER_ID:/boot/initrd.img-* $(INITRD_FILE) 2>/dev/null || \
		docker cp $$CONTAINER_ID:/initrd.img $(INITRD_FILE) || \
		(echo "ERROR: Could not extract initrd"; exit 1); \
	docker rm $$CONTAINER_ID
	@ls -lh $(KERNEL_FILE) $(INITRD_FILE)
	@echo "✓ Kernel: $(KERNEL_FILE)"
	@echo "✓ Initrd: $(INITRD_FILE)"
	@echo "Add these paths to worker-ipxe boot configuration"

# Generate SBOM (Software Bill of Materials) using syft
sbom: build
	@echo "Generating SBOM for image: $(IMAGE)"
	@which syft > /dev/null || (echo "ERROR: syft not installed"; exit 1)
	@syft $(IMAGE) --output cyclonedx-json > $(EXTRACT_DIR)/sbom.cyclonedx.json
	@echo "✓ SBOM generated: $(EXTRACT_DIR)/sbom.cyclonedx.json"

# Sign image with cosign (keyless OIDC)
# Requires: COSIGN_EXPERIMENTAL=1 environment variable
sign: build
	@echo "Signing image: $(IMAGE)"
	@which cosign > /dev/null || (echo "ERROR: cosign not installed"; exit 1)
	@COSIGN_EXPERIMENTAL=1 cosign sign $(IMAGE)
	@echo "✓ Image signed with keyless OIDC"
	@echo "Verify with: COSIGN_EXPERIMENTAL=1 cosign verify $(IMAGE)"

# Clean build artifacts
clean:
	@echo "Cleaning build artifacts..."
	@rm -rf $(EXTRACT_DIR)
	@docker rmi $(IMAGE) 2>/dev/null || true
	@echo "✓ Cleaned"

# Info target: print build configuration
.PHONY: info
info:
	@echo "Gough Helper Image Build Configuration"
	@echo "========================================"
	@echo "Registry:             $(REGISTRY)"
	@echo "Image Name:           $(IMAGE_NAME)"
	@echo "Image Tag:            $(IMAGE_TAG)"
	@echo "Full Image:           $(IMAGE)"
	@echo "Discovery Agent:      $(DISCOVERY_AGENT)"
	@echo "Build Date:           $(BUILD_DATE)"
	@echo "VCS Ref (git SHA):    $(VCS_REF)"
	@echo "Extract Directory:    $(EXTRACT_DIR)"
	@echo ""
	@echo "Targets:"
	@echo "  make build          - Build amd64 image locally"
	@echo "  make build-multiarch- Build amd64 + arm64 (requires buildx)"
	@echo "  make extract-kernel - Extract kernel + initrd for HTTP serving"
	@echo "  make sbom          - Generate CycloneDX SBOM"
	@echo "  make sign          - Sign with cosign (keyless OIDC)"
	@echo "  make clean         - Remove build artifacts"
	@echo "  make info          - Print this help"

# Help target
.PHONY: help
help: info

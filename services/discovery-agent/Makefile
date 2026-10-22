# discovery-agent Makefile
# Static binary for Gough Phase 1 node discovery (initrd helper image)

.PHONY: help build test lint gosec clean docker-build proto

# ── Variables ──────────────────────────────────────────────────────────────────

SERVICE        := discovery-agent
MODULE         := github.com/penguintechinc/gough/services/discovery-agent
VERSION        := $(shell cat ../../.version 2>/dev/null || echo "dev")
BUILD_EPOCH    := $(shell date +%s)
LDFLAGS        := -s -w -X main.Version=$(VERSION)
BUILD_TAGS     := noxdp,netgo
BINARY         := $(SERVICE)
GOFLAGS        := CGO_ENABLED=0 GOOS=linux
DOCKER_IMAGE   := ghcr.io/penguintechinc/gough/$(SERVICE)
DOCKER_TAG     ?= alpha-$(BUILD_EPOCH)

# ── Build ──────────────────────────────────────────────────────────────────────

build: ## Build the static binary for linux/amd64
	$(GOFLAGS) go build \
		-tags "$(BUILD_TAGS)" \
		-ldflags "$(LDFLAGS) -extldflags -static" \
		-o $(BINARY) \
		./cmd/agent/

build-arm64: ## Build the static binary for linux/arm64
	$(GOFLAGS) GOARCH=arm64 go build \
		-tags "$(BUILD_TAGS)" \
		-ldflags "$(LDFLAGS) -extldflags -static" \
		-o $(BINARY)-arm64 \
		./cmd/agent/

# ── Test ───────────────────────────────────────────────────────────────────────

test: ## Run all unit tests with coverage
	go test -tags noxdp -v -race -count=1 -coverprofile=coverage.out ./...
	go tool cover -func=coverage.out | grep total

test-coverage: test ## Open HTML coverage report
	go tool cover -html=coverage.out -o coverage.html
	@echo "Coverage report: coverage.html"

test-unit: ## Run pure-logic unit tests (cmdline, tags) — no system deps
	go test -tags noxdp -v -race -count=1 \
		./internal/cmdline/... \
		./internal/tags/... \
		./internal/probe/...

test-integration: ## Run integration tests (requires lshw, lsblk, smartctl on PATH)
	go test -tags "noxdp,integration" -v -count=1 ./...

bench: ## Run benchmark tests
	go test -tags noxdp -bench=. -benchmem -run='^$$' ./...

# ── Lint / Security ────────────────────────────────────────────────────────────

lint: ## Run golangci-lint
	golangci-lint run --build-tags noxdp ./...

gosec: ## Run gosec security scanner
	gosec -tags noxdp ./...

vet: ## Run go vet
	go vet -tags noxdp ./...

# ── Proto ──────────────────────────────────────────────────────────────────────

proto: ## Regenerate gRPC code from .proto files via buf
	buf generate

# ── Docker ────────────────────────────────────────────────────────────────────

docker-build: ## Build multi-arch Docker image (amd64 + arm64)
	docker buildx build \
		--platform linux/amd64,linux/arm64 \
		--build-arg VERSION=$(VERSION) \
		--tag $(DOCKER_IMAGE):$(DOCKER_TAG) \
		.

docker-build-local: ## Build single-arch Docker image for local push (localhost:32000)
	docker build \
		--platform linux/amd64 \
		--build-arg VERSION=$(VERSION) \
		--tag localhost:32000/$(SERVICE):latest \
		.
	docker push localhost:32000/$(SERVICE):latest

# ── Clean ─────────────────────────────────────────────────────────────────────

clean: ## Remove build artifacts
	rm -f $(BINARY) $(BINARY)-arm64 coverage.out coverage.html

# ── Help ──────────────────────────────────────────────────────────────────────

help: ## Show this help
	@awk 'BEGIN {FS = ":.*?## "} /^[a-zA-Z_-]+:.*?## / {printf "  \033[33m%-20s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

.DEFAULT_GOAL := help

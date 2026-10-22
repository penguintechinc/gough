# gough CLI Makefile
# Version injection via ldflags; multi-arch via GOARCH/GOOS

.PHONY: help build build-all test test-unit lint clean install

# --------------------------------------------------------------------------- #
# Variables
# --------------------------------------------------------------------------- #
BINARY      := gough
MODULE      := github.com/penguintechinc/gough/cli/gough
VERSION     := $(shell cat ../../.version 2>/dev/null || echo "dev")
COMMIT      := $(shell git rev-parse --short HEAD 2>/dev/null || echo "unknown")
BUILD_DATE  := $(shell date -u +%Y-%m-%dT%H:%M:%SZ)
GO_VERSION  := $(shell go version | awk '{print $$3}')

LDFLAGS := -X $(MODULE)/internal/version.Version=$(VERSION) \
           -X $(MODULE)/internal/version.Commit=$(COMMIT) \
           -X $(MODULE)/internal/version.BuildDate=$(BUILD_DATE) \
           -X $(MODULE)/internal/version.GoVersion=$(GO_VERSION)

BUILD_TAGS := noxdp

DIST_DIR    := dist

# --------------------------------------------------------------------------- #
# Default
# --------------------------------------------------------------------------- #
.DEFAULT_GOAL := help

help: ## Show this help
	@awk 'BEGIN {FS = ":.*?## "} /^[a-zA-Z_-]+:.*?## / {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

# --------------------------------------------------------------------------- #
# Build
# --------------------------------------------------------------------------- #
build: ## Build for the current platform
	go build -tags $(BUILD_TAGS) -ldflags "$(LDFLAGS)" -o $(BINARY) .

build-all: ## Build for all target platforms (linux/amd64, linux/arm64, darwin/amd64, darwin/arm64, windows/amd64)
	@mkdir -p $(DIST_DIR)
	GOOS=linux  GOARCH=amd64 go build -tags $(BUILD_TAGS) -ldflags "$(LDFLAGS)" -o $(DIST_DIR)/$(BINARY)-linux-amd64   .
	GOOS=linux  GOARCH=arm64 go build -tags $(BUILD_TAGS) -ldflags "$(LDFLAGS)" -o $(DIST_DIR)/$(BINARY)-linux-arm64   .
	GOOS=darwin GOARCH=amd64 go build -tags $(BUILD_TAGS) -ldflags "$(LDFLAGS)" -o $(DIST_DIR)/$(BINARY)-darwin-amd64  .
	GOOS=darwin GOARCH=arm64 go build -tags $(BUILD_TAGS) -ldflags "$(LDFLAGS)" -o $(DIST_DIR)/$(BINARY)-darwin-arm64  .
	GOOS=windows GOARCH=amd64 go build -tags $(BUILD_TAGS) -ldflags "$(LDFLAGS)" -o $(DIST_DIR)/$(BINARY)-windows-amd64.exe .
	@echo "Built all targets in $(DIST_DIR)/"

install: build ## Install gough to GOBIN
	cp $(BINARY) $(shell go env GOBIN 2>/dev/null || echo $(HOME)/go/bin)/$(BINARY)
	@echo "Installed $(BINARY) to $(shell go env GOBIN 2>/dev/null || echo $(HOME)/go/bin)/$(BINARY)"

# --------------------------------------------------------------------------- #
# Test
# --------------------------------------------------------------------------- #
test: test-unit ## Run all tests

test-unit: ## Run unit tests
	go test -tags $(BUILD_TAGS) -v -count=1 ./...

test-race: ## Run tests with race detector
	go test -tags $(BUILD_TAGS) -v -race -count=1 ./...

# --------------------------------------------------------------------------- #
# Lint
# --------------------------------------------------------------------------- #
lint: ## Run golangci-lint
	golangci-lint run --build-tags $(BUILD_TAGS) ./...

# --------------------------------------------------------------------------- #
# Clean
# --------------------------------------------------------------------------- #
clean: ## Remove build artifacts
	rm -f $(BINARY)
	rm -rf $(DIST_DIR)

# --------------------------------------------------------------------------- #
# Smoke test
# --------------------------------------------------------------------------- #
smoke-test: build ## Run smoke tests (build + version + help)
	./$(BINARY) version
	./$(BINARY) --help
	./$(BINARY) node --help
	./$(BINARY) egg --help
	./$(BINARY) cluster --help
	@echo "Smoke tests passed"

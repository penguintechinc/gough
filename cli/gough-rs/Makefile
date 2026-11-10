# Local gates for the Rust gough CLI; the root Makefile will delegate here.
# Every target can fail: no `|| true`, and pipelines run with pipefail.
SHELL := /bin/bash
.SHELLFLAGS := -euo pipefail -c

export CARGO_TARGET_DIR ?= $(HOME)/.cache/cargo-target/gough
COVERAGE_MIN_LINES ?= 90

.PHONY: lint test coverage build audit deny fmt

fmt:
	cargo fmt --all

lint:
	cargo fmt --all --check
	cargo clippy --workspace --all-targets -- -D warnings

test:
	cargo test --workspace

# Fails below 90% line coverage; the denominator is printed in the summary table.
coverage:
	cargo llvm-cov --workspace --fail-under-lines $(COVERAGE_MIN_LINES) --summary-only

build:
	cargo build --workspace --release --locked

audit:
	cargo audit --deny warnings

deny:
	cargo deny check

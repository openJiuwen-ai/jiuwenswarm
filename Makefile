# Makefile for JiuwenSwarm
# Convenience targets wrapping uv / pytest / lint / build workflows.
# Run `make help` to list available targets.

# Default shell: fail fast on the first error, propagate exit codes.
SHELL := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c

# Run all Python invocations through `uv run` so the project venv is used.
PY := uv run python
UV := uv

# Color output when stdout is a tty.
ifneq (, $(shell test -t 1 && echo 1))
	C_RESET := \033[0m
	C_INFO  := \033[1;34m
	C_OK    := \033[1;32m
	C_WARN  := \033[1;33m
else
	C_RESET :=
	C_INFO  :=
	C_OK    :=
	C_WARN  :=
endif

# Target that is not a file -> always considered out of date.
.PHONY: help install sync lock update-deps update-openjiuwen test test-unit test-integration \
		test-cov lint lint-fix format typecheck clean build \
		init start-debug start-debug-rebuild

# ----------------------------------------------------------------------
# Setup
# ----------------------------------------------------------------------

help: ## Show this help
	@printf "$(C_INFO)JiuwenSwarm Makefile targets:$(C_RESET)\n\n"
	@awk 'BEGIN {FS = ":.*?## "} /^[a-zA-Z_-]+:.*?## / { printf "  $(C_OK)%-20s$(C_RESET) %s\n", $$1, $$2 }' $(MAKEFILE_LIST)
	@printf "\n"

install: sync ## Install the project (editable) into the uv venv
	$(UV) pip install -e .

sync: ## Sync the environment from uv.lock (no upgrades)
	$(UV) sync

lock: ## Re-resolve uv.lock from pyproject.toml (all packages)
	$(UV) lock

update-deps: ## Upgrade all locked dependencies to their latest allowed version
	$(UV) lock --upgrade
	$(UV) sync

# ----------------------------------------------------------------------
# openjiuwen (agent-core) — pinned to the `develop` branch in pyproject.toml
# ----------------------------------------------------------------------

update-openjiuwen: ## Pin openjiuwen to the latest commit on the develop branch
	@printf "$(C_INFO)Updating openjiuwen to latest on the develop branch...$(C_RESET)\n"
	$(UV) lock --upgrade-package openjiuwen
	$(UV) sync
	@printf "$(C_OK)openjiuwen now pinned to:$(C_RESET)\n"
	@awk '/^name = "openjiuwen"/{f=1} f && /^source = /{sub(/.*#/,""); sub(/".*/,""); print "  commit " $$0; f=0}' uv.lock

# ----------------------------------------------------------------------
# Run — workspace init & launching services
# ----------------------------------------------------------------------

init: ## Initialize the jiuwenswarm workspace (~/.jiuwenswarm)
	@printf "$(C_INFO)Initializing jiuwenswarm workspace...$(C_RESET)\n"
	$(UV) run jiuwenswarm-init

start-debug: ## Start services in debug mode, reusing the existing frontend build
	@printf "$(C_INFO)Starting services in debug mode (skip build)...$(C_RESET)\n"
	$(UV) run jiuwenswarm-start debug --skip-build

start-debug-rebuild: ## Start services in debug mode, rebuilding the frontend first
	@printf "$(C_INFO)Starting services in debug mode (full build)...$(C_RESET)\n"
	$(UV) run jiuwenswarm-start debug

# ----------------------------------------------------------------------
# Testing — wraps the run_tests.sh helper for richer flags
# ----------------------------------------------------------------------

test: ## Run the full test suite
	@bash run_tests.sh

test-unit: ## Run only unit tests
	@bash run_tests.sh -u

test-integration: ## Run only integration tests
	@bash run_tests.sh -i

test-cov: ## Run tests and emit an HTML coverage report
	@bash run_tests.sh -c

# ----------------------------------------------------------------------
# Lint & format
# ----------------------------------------------------------------------

lint: ## Run ruff + pylint + mypy + codespell (read-only)
	$(PY) -m ruff check .
	$(PY) -m pylint jiuwenswarm || true
	$(PY) -m mypy jiuwenswarm || true
	$(PY) -m codespell || true

lint-fix: ## Apply ruff's auto-fixes
	$(PY) -m ruff check --fix .

format: ## Format code with ruff
	$(PY) -m ruff format .

typecheck: ## Run mypy only
	$(PY) -m mypy jiuwenswarm

# ----------------------------------------------------------------------
# Build & clean
# ----------------------------------------------------------------------

build: ## Build the wheel/sdist via the project build script
	@bash scripts/build.sh

clean: ## Remove build artifacts, caches, and coverage output
	rm -rf build/ dist/ .eggs/ *.egg-info htmlcov/ .pytest_cache/ .mypy_cache/ .ruff_cache/
	find . -type d -name '__pycache__' -prune -exec rm -rf {} +

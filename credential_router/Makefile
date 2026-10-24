# Makefile for credential_router
# Run `make help` for a list of targets.

ROOT := $(shell pwd)

.PHONY: help build test test-race test-e2e fuzz-quick fuzz-full fuzz-quick-pkg fuzz-full-pkg fmt-check vet clean

help:
	@echo "credential_router — Make targets"
	@echo
	@echo "  build             build the credential-router binary into ./dist/bin/"
	@echo "  test              run unit tests (CGO_ENABLED=1, no -race)"
	@echo "  test-race         run unit tests with -race"
	@echo "  test-e2e          run e2e tests via scripts/run-tests.sh (subprocess cleanup)"
	@echo "  fuzz-quick        smoke fuzz (10s per package, ~5 min total)"
	@echo "  fuzz-full         thorough fuzz (60s per package, ~30 min total)"
	@echo "  fuzz-quick-pkg PKG=internal/crypto"
	@echo "                    single-package quick fuzz"
	@echo "  fuzz-full-pkg PKG=internal/keystore"
	@echo "                    single-package full fuzz"
	@echo "  fmt-check         verify all .go files are gofmt-clean (CI gate)"
	@echo "  vet               run go vet ./... (CI gate)"
	@echo "  clean             remove dist/ and Go build cache"

build:
	CGO_ENABLED=1 go build -trimpath -ldflags "-s -w" -o dist/bin/credential-router ./cmd/credential-router

test:
	CGO_ENABLED=1 go test -tags test -count=1 ./...

test-race:
	CGO_ENABLED=1 go test -tags test -race -count=1 \
		$$(go list ./... | grep -v '/tpcc' | grep -v '/tests$$')

test-e2e:
	@./scripts/run-tests.sh

fuzz-quick:
	@./scripts/fuzz.sh quick

fuzz-full:
	@./scripts/fuzz.sh full

fuzz-quick-pkg:
	@./scripts/fuzz.sh quick $(PKG)

fuzz-full-pkg:
	@./scripts/fuzz.sh full $(PKG)

fmt-check:
	@files=$$(gofmt -l .); \
	if [ -n "$$files" ]; then \
		echo "go fmt issues found:"; \
		echo "$$files"; \
		exit 1; \
	fi
	@echo "gofmt clean"

vet:
	@go vet -tags test ./...

clean:
	rm -rf dist
	go clean -cache

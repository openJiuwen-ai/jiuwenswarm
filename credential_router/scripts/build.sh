#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

APP="credential-router"
GOOS="${GOOS:-linux}"
DIST="${DIST:-dist}"
VERSION="${VERSION:-$(git describe --tags --always --dirty 2>/dev/null || echo dev)}"
LDFLAGS="-s -w -X main.version=${VERSION}"

# arch_label: normalize user-facing arch name (x86_64|amd64|aarch64|arm64) to
# distribution label (x86_64|arm64). Matches agent-os build_credential_router
# convention so binary lookups in module.sh::_cr_arch_label work the same.
arch_label() {
  case "$1" in
    x86_64|amd64)   echo x86_64 ;;
    aarch64|arm64)  echo arm64  ;;
    *)
      echo "error: unsupported arch '$1' (use x86_64 or arm64)" >&2
      exit 1
      ;;
  esac
}

# Map distribution label to Go's internal GOARCH.
goarch_for() {
  case "$1" in
    x86_64) echo amd64 ;;
    arm64)  echo arm64  ;;
    *) echo "error: unsupported label '$1'" >&2; exit 1 ;;
  esac
}

require_go() {
  if ! command -v go >/dev/null 2>&1; then
    echo "error: go is not installed" >&2
    exit 1
  fi
  local ver
  ver="$(go env GOVERSION | sed 's/^go//')"
  if ! printf '%s\n%s\n' "1.23.3" "$ver" | sort -C -V; then
    echo "error: go >= 1.23.3 required, found go${ver}" >&2
    exit 1
  fi
}

build_one() {
  local goos="$1"
  local goarch="$2"
  local label
  label="$(arch_label "$goarch")"
  local out="${DIST}/bin/${APP}_${goos}_${label}"
  mkdir -p "${DIST}/bin"
  echo "building ${goos}/${label} (GOARCH=${goarch}) -> ${out}"
  # CGO_ENABLED=1 is required: internal/credmgr and its store subpackage
  # are gated behind //go:build cgo because they depend on
  # github.com/mattn/go-sqlite3. Cross-compiling to Windows requires
  # setting CC (e.g. CC=x86_64-w64-mingw32-gcc).
  GOOS="$goos" GOARCH="$goarch" CGO_ENABLED=1 \
    go build -trimpath -ldflags "$LDFLAGS" \
    -o "$out" ./cmd/credential-router
}

package_one() {
  local goos="$1"
  local goarch="$2"
  local label
  label="$(arch_label "$goarch")"
  local bin="${DIST}/bin/${APP}_${goos}_${label}"
  local pkg_dir="${DIST}/${APP}_${VERSION}_${goos}_${label}"
  local archive="${DIST}/${APP}_${VERSION}_${goos}_${label}.tar.gz"

  rm -rf "$pkg_dir"
  mkdir -p "$pkg_dir"
  cp "$bin" "$pkg_dir/${APP}"
  cp config.example.yaml "$pkg_dir/config.example.yaml"
  cp README.md "$pkg_dir/README.md"

  tar -czf "$archive" -C "$DIST" "$(basename "$pkg_dir")"
  rm -rf "$pkg_dir"
  echo "packaged ${archive}"
}

build_and_package() {
  local goarch="$1"
  build_one "$GOOS" "$goarch"
  package_one "$GOOS" "$goarch"
}

usage() {
  cat <<EOF
Usage: $(basename "$0") [TARGET]

  x86_64   linux/amd64 (default on x86_64/amd64 host)
  amd64    alias for x86_64
  arm64    linux/arm64 (default on aarch64/arm64 host)
  aarch64  alias for arm64
  all      build both x86_64 + arm64 (requires cross-toolchain)

If TARGET is omitted, defaults to the current host arch via uname -m.
Pass 'all' explicitly to build both — not the default, because one host
typically can't cross-compile both arches without extra toolchain.

Environment:
  VERSION  Release version tag (default: git describe or "dev")
  DIST     Output directory (default: dist)
  GOOS     Target OS (default: linux)
  CC       C compiler for cross-compile (e.g. x86_64-w64-mingw32-gcc for Windows)
EOF
}

main() {
  local mode="${1:-$(uname -m)}"
  require_go
  rm -rf "$DIST"
  mkdir -p "$DIST"

  case "$mode" in
    all)
      build_and_package amd64
      build_and_package arm64
      cp config.example.yaml "${DIST}/config.example.yaml"
      echo
      echo "build artifacts in ${DIST}/"
      ls -1 "${DIST}"/*.tar.gz
      ;;
    x86_64|amd64)
      build_and_package "$(goarch_for x86_64)"
      ;;
    arm64|aarch64)
      build_and_package "$(goarch_for arm64)"
      ;;
    -h|--help|help)
      usage
      ;;
    *)
      echo "error: unknown mode '$mode'" >&2
      usage >&2
      exit 1
      ;;
  esac
}

main "$@"

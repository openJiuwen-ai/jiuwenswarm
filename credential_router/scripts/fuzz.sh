#!/usr/bin/env bash
# Fuzz runner with two presets:
#   quick — 10s per package (CI smoke, ~5 min total)
#   full  — 60s per package (nightly / pre-release, ~30 min total)
#
# Per-target fuzztime is per-package (Go runs every Fuzz* target in the
# package for that many seconds, sequentially).
#
# Usage:
#   scripts/fuzz.sh                          # default preset (30s)
#   scripts/fuzz.sh quick                    # recommended CI preset
#   scripts/fuzz.sh full                     # thorough preset
#   scripts/fuzz.sh quick tests/unit/credmgr/keystore  # single package, quick preset
#   scripts/fuzz.sh full tests/unit/credmgr/crypto     # single package, full preset
#   FUZZTIME=5s scripts/fuzz.sh quick        # override per-package time
#
# Exits 0 on clean pass; non-zero mirrors the first failing `go test` exit.

set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# Discover packages containing fuzz_test.go at runtime so the list
# never goes stale when packages are added or renamed.
mapfile -t PACKAGES < <(find tests/unit -name fuzz_test.go -exec dirname {} \; | sort -u)

PRESET_QUICK=10s
PRESET_FULL=60s
PRESET_DEFAULT=30s

usage() {
    cat <<EOF
Usage: $(basename "$0") <preset> [package...]

Presets:
  quick    ${PRESET_QUICK} per package (recommended for CI)
  full     ${PRESET_FULL} per package (nightly / pre-release)
  default  ${PRESET_DEFAULT} per package

Packages (default = all):
  $(printf '  %s\n' "${PACKAGES[@]}")

Environment:
  FUZZTIME  Override the per-package fuzztime (e.g. 5s, 2m)
EOF
}

resolve_time() {
    case "$1" in
        quick)   echo "$PRESET_QUICK" ;;
        full)    echo "$PRESET_FULL" ;;
        default) echo "$PRESET_DEFAULT" ;;
        *)       echo "error: unknown preset '$1'" >&2; usage >&2; exit 2 ;;
    esac
}

main() {
    if [[ $# -lt 1 ]]; then
        usage; exit 2
    fi
    local preset="$1"; shift

    local time="${FUZZTIME:-}"
    if [[ -z "$time" ]]; then
        time="$(resolve_time "$preset")"
    fi

    local pkgs=("$@")
    if [[ ${#pkgs[@]} -eq 0 ]]; then
        pkgs=("${PACKAGES[@]}")
    fi

    local total=${#pkgs[@]}
    echo "==> fuzz preset='${preset}' time='${time}' packages=${total}"
    echo

    local rc=0
    local i=0
    for pkg in "${pkgs[@]}"; do
        i=$((i + 1))
        # go test -fuzz refuses multi-target patterns, so iterate per target via the
        # -list manifest (filter to Fuzz* since -list also surfaces Test/Benchmark).
        local pkg_log="${TMPDIR:-/tmp}/fuzz.$(echo "$pkg" | tr '/' '_').log"
        local targets
        targets="$(CGO_ENABLED=1 go test -tags test -list '^Fuzz' "./$pkg/..." 2>/dev/null | grep '^Fuzz' || true)"
        if [[ -z "$targets" ]]; then
            printf '[%d/%d] %s ... no Fuzz* targets found, skipping\n' "$i" "$total" "$pkg"
            echo
            continue
        fi
        local pkg_rc=0
        local pkg_total
        pkg_total=$(printf '%s\n' "$targets" | wc -l | tr -d ' ')
        local pkg_i=0
        {
            echo "=== package $pkg (${pkg_total} target(s), time=${time} each) ==="
            while IFS= read -r t; do
                [[ -z "$t" ]] && continue
                pkg_i=$((pkg_i + 1))
                printf '[%s %d/%d] %s/%s ... ' "$pkg" "$pkg_i" "$pkg_total" "$pkg" "$t"
                if CGO_ENABLED=1 go test -tags test -run='^$' -fuzz="^${t}$" -fuzztime="$time" "./$pkg/..." > "$pkg_log" 2>&1; then
                    echo "OK"
                else
                    echo "FAIL"
                    pkg_rc=1
                    grep -E "(FAIL|panic:|fatal error:|found new failing)" "$pkg_log" | head -3 | sed 's/^/    /'
                fi
            done <<<"$targets"
        } | tee -a "$pkg_log"
        if [[ $pkg_rc -ne 0 ]]; then
            rc=1
            echo "    full log: $pkg_log"
        fi
        echo
    done

    if [[ $rc -eq 0 ]]; then
        echo "==> all ${total} package(s) passed"
    else
        echo "==> at least one package failed (rc=$rc)"
    fi
    exit "$rc"
}

main "$@"

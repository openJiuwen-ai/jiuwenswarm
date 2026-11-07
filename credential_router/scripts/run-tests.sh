#!/usr/bin/env bash
# Wrapper around `go test ./tests/...` that guarantees no leftover
# credential-router-test subprocesses after the test exits for the
# common termination cases: Ctrl-C, panic, normal completion, test
# failure, hangup.
#
# Why this exists: Go's exec.Cmd with Pdeathsig does NOT fire when the
# child is reparented (which `go test` / `bash` do on parent death).
# This wrapper installs an EXIT trap that runs cleanup before the
# wrapper itself exits, so SIGINT/SIGTERM/SIGQUIT/SIGHUP/normal-exit all
# trigger cleanup. SIGKILL of the wrapper itself is still a known
# hole (operator must `pkill -9 -f credential-router-test`).
#
# Usage:
#   scripts/run-tests.sh                       # default: all tests
#   scripts/run-tests.sh -run TestE2E          # filtered
#   scripts/run-tests.sh -run TestTPCC         # TPCC only
#   TPCC_DURATION=60s scripts/run-tests.sh     # TPCC 60s
#
# Exits 0 on clean test pass; non-zero mirrors `go test` exit.

set -u

cleanup() {
    local pid
    pkill -9 -f "credential-router-test" 2>/dev/null || true
    pkill -9 -f "dist/bin/credential-router_" 2>/dev/null || true
    while IFS= read -r pid; do
        [ -n "$pid" ] && kill -- "-$pid" 2>/dev/null || true
    done < <(pgrep -f "credential-router-test" 2>/dev/null)
    while IFS= read -r pid; do
        [ -n "$pid" ] && kill -- "-$pid" 2>/dev/null || true
    done < <(pgrep -f "dist/bin/credential-router_" 2>/dev/null)
}

trap cleanup EXIT INT TERM QUIT HUP

cd "$(dirname "$0")/.." || exit 1

go test -tags test -count=1 -timeout=180s "$@" ./tests/...
status=$?

exit "$status"
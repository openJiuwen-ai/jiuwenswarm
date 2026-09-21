#!/bin/sh
# Start JiuwenSwarm AgentServer with the complete HNP runtime environment.

set -u

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
REPO_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
OHOS_ENV_SCRIPTS_DIR="$REPO_ROOT/scripts/ohos"
OHOS_REPO_ROOT="$REPO_ROOT"
OFFICE_CLAW=${OFFICE_CLAW:-$(CDPATH= cd -- "$REPO_ROOT/.." && pwd)}
export OHOS_ENV_SCRIPTS_DIR
export OHOS_REPO_ROOT
export OFFICE_CLAW

# shellcheck disable=SC1091
. "$OHOS_ENV_SCRIPTS_DIR/ohos-env.sh"

export JIUWENSWARM_RUNTIME_PLATFORM=ohos
# Legacy name kept for on-device launchers / docs that still reference it.
export JIUWENCLAW_RUNTIME_PLATFORM=ohos
TMPDIR=${TMPDIR:-${OHOS_STORAGE_ROOT:-/storage/Users/currentUser}/tmp}
export TMPDIR
if ! mkdir -p "$TMPDIR"; then
  printf '[ohos-agentserver] ERROR: cannot create TMPDIR: %s\n' "$TMPDIR" >&2
  exit 1
fi
VENV_DIR=${VENV_DIR:-$REPO_ROOT/.venv}
PYTHON=${PYTHON:-$VENV_DIR/bin/python}
PORT=${1:-${OHOS_AGENTSERVER_PORT:-18092}}

if [ ! -x "$PYTHON" ]; then
  printf '[ohos-agentserver] ERROR: Python not found: %s\n' "$PYTHON" >&2
  exit 1
fi

_verify_ld=$(ohos_native_ld_library_path 2>/dev/null || true)
if [ -n "$_verify_ld" ]; then
  export LD_LIBRARY_PATH="$_verify_ld"
fi

# TLS trust anchors (2026-09 device finding): some OHOS images ship a system CA
# bundle at /etc/ssl/certs/cacert.pem while python's default verify path points
# to the non-existent /etc/ssl/cert.pem — outbound HTTPS (model API gateways)
# then fails with SSLCertVerificationError. Export the existing bundle when the
# variables are not already set; guarded so non-OHOS-ish layouts are untouched.
if [ -f /etc/ssl/certs/cacert.pem ]; then
  : "${SSL_CERT_FILE:=/etc/ssl/certs/cacert.pem}"
  : "${REQUESTS_CA_BUNDLE:=/etc/ssl/certs/cacert.pem}"
  export SSL_CERT_FILE REQUESTS_CA_BUNDLE
fi

if ! "$PYTHON" -c 'import openjiuwen, jiuwenswarm' >/dev/null 2>&1; then
  printf '[ohos-agentserver] ERROR: core Python imports failed\n' >&2
  exit 1
fi

printf '[ohos-agentserver] python=%s port=%s\n' "$PYTHON" "$PORT"
exec "$PYTHON" -m jiuwenswarm.server.app_agentserver --port "$PORT"

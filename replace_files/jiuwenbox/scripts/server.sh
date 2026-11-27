#!/usr/bin/env bash
set -euo pipefail

CALLER_CWD="$(pwd)"
PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"

usage() {
  cat <<'EOF'
Usage: scripts/server.sh [policy-config.yaml] [uvicorn args...]
       scripts/server.sh [policy-config.yaml] [port] [uvicorn args...]

Examples:
  scripts/server.sh configs/jiuwenclaw-policy.yaml
  scripts/server.sh configs/default-policy.yaml 9000 --reload
EOF
}

POLICY_CONFIG="${JIUWENBOX_DEFAULT_POLICY_PATH:-}"
PORT="${JIUWENBOX_PORT:-8321}"

if [[ $# -gt 0 ]]; then
  case "$1" in
    -h|--help)
      usage
      exit 0
      ;;
    -*)
      ;;
    *)
      POLICY_CONFIG="$1"
      shift
      ;;
  esac
fi

if [[ $# -gt 0 && "$1" =~ ^[0-9]+$ ]]; then
  PORT="$1"
  shift
fi

POLICY_CONFIG="${POLICY_CONFIG:-configs/default-policy.yaml}"
POLICY_CONFIG_ABS=""

if [[ "$POLICY_CONFIG" = /* && -f "$POLICY_CONFIG" ]]; then
  POLICY_CONFIG_ABS="$(realpath "$POLICY_CONFIG")"
elif [[ -f "$CALLER_CWD/$POLICY_CONFIG" ]]; then
  POLICY_CONFIG_ABS="$(realpath "$CALLER_CWD/$POLICY_CONFIG")"
else
  echo "error: policy config not found: $POLICY_CONFIG" >&2
  exit 1
fi

export JIUWENBOX_DEFAULT_POLICY_PATH="$POLICY_CONFIG_ABS"

echo "Starting jiuwenbox with policy config: $JIUWENBOX_DEFAULT_POLICY_PATH"
echo "Listening on port: $PORT"
python3 -m uvicorn jiuwenbox.server.app:app --app-dir src --host 0.0.0.0 --port "$PORT" --log-level debug "$@"

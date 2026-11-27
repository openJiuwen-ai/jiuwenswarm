#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_DIR"

if [ -d ".venv" ]; then
    source .venv/bin/activate
fi

TEST_TARGET="tests/integration/"

if [[ $# -gt 0 ]]; then
    case "$1" in
        default)
            TEST_TARGET="tests/integration/test_server_api_default.py"
            shift
            ;;
        jiuwenclaw-tool)
            TEST_TARGET="tests/integration/test_server_api_jiuwenclaw_tool.py"
            shift
            ;;
    esac
fi

python3 -m pytest "$TEST_TARGET" -v --tb=short "$@"

#!/usr/bin/env bash
# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
set -euo pipefail

TEST_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$TEST_DIR/.." && pwd)"
cd "$PROJECT_DIR"

JIUWENBOX_DIR="$(realpath "$PROJECT_DIR/src")"

# 默认 test 目标 & 类别; 各子命令可以覆盖。
TEST_TARGETS=("tests/integration/")
TEST_KIND="integration"
PERF_SANDBOX_COUNT="${JIUWENBOX_PERF_SANDBOX_COUNT:-1}"
PERF_CONCURRENCY="${JIUWENBOX_PERF_CONCURRENCY:-4}"
PERF_LOOP="${JIUWENBOX_PERF_LOOP:-8}"
PERF_EXEC_TIMEOUT_SECONDS="${JIUWENBOX_PERF_EXEC_TIMEOUT_SECONDS:-180}"

usage() {
    cat <<'EOF'
Usage: tests/test.sh [target] [--server-endpoint=URI] [pytest args...]

Targets:
  default                 Run test_server_api_default.py + test_cli_default.py
  inference-privacy-proxy Run test_inference_privacy_proxy.py
  performance             Run performance suite (accepts --sandbox-count /
                          --concurrency / --loop)
  (omitted)               Run the whole tests/integration/ directory

Server endpoint:
  --server-endpoint=URI   Pick the listener to test against. Transport is
                          inferred from the URI shape:

                            http(s)://host:port    TCP listener
                            host:port              TCP listener (http:// added)
                            unix:///abs/path       Unix Domain Socket file

                          Defaults to the pytest fixture default
                          (http://127.0.0.1:8321) when omitted.

Examples:
  tests/test.sh default
  tests/test.sh default --server-endpoint=http://127.0.0.1:18321
  tests/test.sh default --server-endpoint=unix:///tmp/jiuwenbox.sock
EOF
}

require_value() {
    local option="$1"
    if [[ $# -lt 2 || "$2" == --* ]]; then
        echo "Missing value for ${option}" >&2
        exit 2
    fi
}

# 子命令选择 (default / performance / etc).
if [[ $# -gt 0 ]]; then
    case "$1" in
        -h|--help)
            usage
            exit 0
            ;;
        default)
            TEST_TARGETS=(
                "tests/integration/test_server_api_default.py"
                "tests/integration/test_cli_default.py"
            )
            TEST_KIND="integration"
            shift
            ;;
        inference-privacy-proxy)
            TEST_TARGETS=("tests/integration/test_inference_privacy_proxy.py")
            TEST_KIND="integration"
            shift
            ;;
        performance)
            TEST_TARGETS=("tests/performance/")
            TEST_KIND="performance"
            shift
            ;;
    esac
fi

PYTEST_ARGS=()

# performance target 还需要识别 --sandbox-count / --concurrency / --loop;
# 其余一律透传给 pytest, 包括 --server-endpoint (conftest.py 自己解析).
if [[ "$TEST_KIND" == "performance" ]]; then
    PYTEST_ARGS+=(
        "-s"
        "--log-cli-level=INFO"
        "--log-cli-format=%(message)s"
        "--log-disable=httpx"
        "--log-disable=httpcore"
    )
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --sandbox-count)
                require_value "$1" "${2:-}"
                PERF_SANDBOX_COUNT="$2"
                shift 2
                ;;
            --concurrency)
                require_value "$1" "${2:-}"
                PERF_CONCURRENCY="$2"
                shift 2
                ;;
            --loop)
                require_value "$1" "${2:-}"
                PERF_LOOP="$2"
                shift 2
                ;;
            *)
                PYTEST_ARGS+=("$1")
                shift
                ;;
        esac
    done
else
    while [[ $# -gt 0 ]]; do
        PYTEST_ARGS+=("$1")
        shift
    done
fi

# 友好提示: 在用户传了 --server-endpoint 时回显, 没传时回显 pytest fixture
# 默认值, 让 CI 日志里一眼看清打的是哪个 listener (TCP or UDS).
user_endpoint=""
if (( ${#PYTEST_ARGS[@]} > 0 )); then
    for ((i = 0; i < ${#PYTEST_ARGS[@]}; i++)); do
        arg="${PYTEST_ARGS[$i]}"
        case "$arg" in
            --server-endpoint=*)
                user_endpoint="${arg#--server-endpoint=}"
                break
                ;;
            --server-endpoint)
                next_idx=$((i + 1))
                if (( next_idx < ${#PYTEST_ARGS[@]} )); then
                    user_endpoint="${PYTEST_ARGS[$next_idx]}"
                fi
                break
                ;;
        esac
    done
fi

if [[ -n "$user_endpoint" ]]; then
    case "$user_endpoint" in
        unix://*)
            echo "[test.sh] endpoint=${user_endpoint} (uds)" >&2
            ;;
        http://*|https://*)
            echo "[test.sh] endpoint=${user_endpoint} (tcp)" >&2
            ;;
        *)
            # ``host:port`` 形式: conftest.py 会自动补 http:// 前缀。
            echo "[test.sh] endpoint=${user_endpoint} (tcp, http:// inferred)" >&2
            ;;
    esac
else
    echo "[test.sh] endpoint=(pytest default http://127.0.0.1:8321)" >&2
fi

if [[ "$TEST_KIND" == "performance" ]]; then
    JIUWENBOX_PERF_SANDBOX_COUNT=${PERF_SANDBOX_COUNT} \
        JIUWENBOX_PERF_CONCURRENCY=${PERF_CONCURRENCY} \
        JIUWENBOX_PERF_LOOP=${PERF_LOOP} \
        JIUWENBOX_PERF_EXEC_TIMEOUT_SECONDS=${PERF_EXEC_TIMEOUT_SECONDS} \
        PYTHONPATH=${JIUWENBOX_DIR} \
        python3 -m pytest "${TEST_TARGETS[@]}" -v --tb=short "${PYTEST_ARGS[@]}"
else
    PYTHONPATH=${JIUWENBOX_DIR} \
        python3 -m pytest "${TEST_TARGETS[@]}" -v --tb=short "${PYTEST_ARGS[@]}"
fi

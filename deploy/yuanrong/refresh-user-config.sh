#!/usr/bin/env bash
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# ============================================================
# refresh-user-config.sh — 薄入口
#
# 实际实现见同目录 refresh_user_config.py。保留 .sh 入口是为了
# 与 deploy/jiuwenswarm/ 下其它脚本的调用习惯一致。
#
# 用法：./refresh-user-config.sh <username> [options]
# ============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python${YR_PYTHON_VERSION:-3.11}}"

if ! command -v "${PYTHON_BIN}" >/dev/null 2>&1; then
    echo "❌ 找不到 Python 解释器: ${PYTHON_BIN}（可用 PYTHON_BIN 或 YR_PYTHON_VERSION 覆盖）" >&2
    exit 1
fi

exec "${PYTHON_BIN}" "${SCRIPT_DIR}/refresh_user_config.py" "$@"

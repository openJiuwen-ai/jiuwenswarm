#!/usr/bin/env bash
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
set -eu
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUSINESS_PYTHON="${SWARM_PYTHON:-python3.11}"
export SWARM_PYTHON="${BUSINESS_PYTHON}"
SERVICE_SETTINGS="${SCRIPT_DIR}/service.py"
SWARM_SETTINGS="${SWARM_SETTINGS:-${SCRIPT_DIR}/settings.yaml}"
SWARM_BINDINGS="${SWARM_BINDINGS:-}"
SWARM_SERVICE_PREFLIGHT="${SWARM_SERVICE_PREFLIGHT:-}"
CMD=""
declare -a MODULES=()
declare -A DEPLOY_VARS=(
    [JIUWENSWARM_INSTANCE_NAME]=""
    [GATEWAY_HOST]="0.0.0.0" [GATEWAY_PORT]="19001"
    [WEB_HOST]="127.0.0.1" [WEB_PORT]="19000"
    [WEB_STATIC_HOST]="0.0.0.0" [WEB_STATIC_PORT]="5173"
)

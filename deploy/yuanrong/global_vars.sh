#!/usr/bin/env bash
set -euo >/dev/null 2>&1

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

CUSTOM_ENV_FILE="${SCRIPT_DIR}/.env.custom"
ENV_FILE="${SCRIPT_DIR}/.env"

CLAW_META_PROCESS_TEMPLATE_FILE="${SCRIPT_DIR}/conf/claw_meta_process.template.json"
CLAW_META_PROCESS_FILE="${SCRIPT_DIR}/conf/claw_meta_process.json"

GATEWAY_CONFIG_TEMPLATE_FILE="${SCRIPT_DIR}/conf/gateway-config-yuanrong.template.yaml"
GATEWAY_CONFIG_FILE="${SCRIPT_DIR}/conf/gateway-config.yaml"
GATEWAY_ENV_FILE="${SCRIPT_DIR}/conf/gateway.env"

REG_FUNC_FILE="${SCRIPT_DIR}/../../jiuwenswarm/extensions/clawee.py"

META_PORT=""
CMD=""

declare -ga MODULES=()

declare -A DEPLOY_VARS=(
    ["FUNC_SVC_NAME"]="0@jiuwen@swarm"
    ["SANDBOX_TYPE"]=""
    ["MGR_CPU"]="300"
    ["MGR_MEMORY"]="600"
    ["MGR_MIN_INSTANCE"]="1"
    ["MGR_MAX_INSTANCE"]="10"
    ["MGR_CONCURRENT_NUM"]="10"
    ["CLUSTER_HOSTS"]=""
    ["YR_PYTHON_VERSION"]="3.11"
    ["YR_FUNC_CODE_DIR"]=""
    ["YR_SESSION_DIR"]=""
    ["JIUWENSWARM_PACKAGE_URL"]=""
    ["JIUWENSWARM_INSTANCE_NAME"]=""
    ["GATEWAY_CONCURRENCY"]="1"
    ["GATEWAY_INVOKE_TIMEOUT"]="60"
    ["GATEWAY_SESSION_MAP_SCOPE"]="per_chat_bot_user"
    ["MODEL_PROVIDER"]=""
    ["MODEL_NAME"]=""
    ["API_BASE"]=""
    ["API_KEY"]=""
    ["EMBED_API_KEY"]=""
    ["EMBED_API_BASE"]=""
    ["EMBED_MODEL"]=""
    ["FRONTEND_PORT"]=""
    ["FUNCTION_ID"]=""
    ["MASTER_NODE_IP"]=""
    ["REGISTRY_PORT"]=""
    ["SSH_PORT"]=""
    # TUI GatewayServer bind host; empty → default to MASTER_NODE_IP at deploy check time
    ["GATEWAY_HOST"]=""
    ["GATEWAY_PORT"]=""
    # WebChannel bind host; empty → default to MASTER_NODE_IP at deploy check time
    ["WEB_HOST"]=""
    ["WEB_PORT"]=""
    ["SANDBOX_IDLE_TIMEOUT_SECONDS"]=""
    ["OS_TYPE"]=""
    ["EXTENSION_DIRS"]=""
    ["AGENTOS_AUTH_SERVICE_URL"]=""
    ["AGENTOS_AUTH_TIMEOUT"]=""
)

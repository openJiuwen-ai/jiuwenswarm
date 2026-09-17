#!/usr/bin/env bash
set -euo >/dev/null 2>&1

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

CUSTOM_ENV_FILE="${SCRIPT_DIR}/.env.custom"
ENV_FILE="${SCRIPT_DIR}/.env"

GATEWAY_CONFIG_TEMPLATE_FILE="${SCRIPT_DIR}/conf/gateway-config-yuanrong.template.yaml"
GATEWAY_CONFIG_FILE="${SCRIPT_DIR}/conf/gateway-config.yaml"
GATEWAY_ENV_FILE="${SCRIPT_DIR}/conf/gateway.env"

REG_FUNC_FILE="${SCRIPT_DIR}/../../jiuwenswarm/extensions/clawee.py"

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
    ["INGRESS_VIP"]=""
    ["REGISTRY_PORT"]=""
    ["SSH_PORT"]=""
    # TUI GatewayServer bind host; empty → default to 0.0.0.0 at deploy check time
    ["GATEWAY_HOST"]=""
    ["GATEWAY_PORT"]=""
    # WebChannel bind host; empty → default to 0.0.0.0 at deploy check time
    ["WEB_HOST"]=""
    ["WEB_PORT"]=""
    # jiuwenswarm web 静态服务器 (jiuwenswarm-web, serve frontend/dist)
    # /ws 代理到 gateway 的 WEB_PORT; 独立变量, 不复用 FRONTEND_PORT(后者指 yuanrong frontend 8888)
    ["WEB_STATIC_HOST"]=""
    ["WEB_STATIC_PORT"]=""
    ["SANDBOX_IDLE_TIMEOUT_SECONDS"]=""
    # Channel timeout cleanup; empty -> default to gateway.agentos.disconnect_cleanup_timeout_seconds
    ["DISCONNECT_CLEANUP_TIMEOUT_SECONDS"]=""
    ["OS_TYPE"]=""
    ["EXTENSION_DIRS"]=""
    # AgentOS IAM; empty URL → http://MASTER_NODE_IP:8090 at deploy check time
    ["AGENTOS_AUTH_SERVICE_URL"]=""
    ["AGENTOS_AUTH_TIMEOUT"]=""
)

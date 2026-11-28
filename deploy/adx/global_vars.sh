#!/usr/bin/env bash
set -euo >/dev/null 2>&1

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

CUSTOM_ENV_FILE="${SCRIPT_DIR}/.env.custom"
ENV_FILE="${SCRIPT_DIR}/.env"

# 默认使用随部署脚本提供的 ADX 模板，支持指定其他模板路径。
GATEWAY_CONFIG_TEMPLATE_FILE="${GATEWAY_CONFIG_TEMPLATE_FILE:-${SCRIPT_DIR}/conf/gateway-config.template.yaml}"
GATEWAY_CONFIG_FILE="${SCRIPT_DIR}/conf/gateway-config.yaml"
# Gateway/AgentServer 共用一份 env，最终落到 <workspace>/config/.env。
# 运行期配置面板只写这一份，避免 Gateway/AS 各自持有副本后模型凭据漂移。
SHARED_ENV_FILE="${SCRIPT_DIR}/conf/jiuwenswarm.env"


CMD=""

declare -ga MODULES=()

declare -A DEPLOY_VARS=(
    ["CLUSTER_HOSTS"]=""
    ["PYTHON_VERSION"]="3.11"
    ["ADX_INGRESS_URL"]=""
    ["ADX_TENANT"]="agentos"
    ["ADX_TOKEN"]=""
    ["ADX_AGENTSERVER_IMAGE"]="agentos-supervisor:v1"
    ["ADX_AGENTSERVER_RUNTIME"]="supervisor"
    ["JIUWENSWARM_PACKAGE_URL"]=""
    ["JIUWENSWARM_INSTANCE_NAME"]=""
    ["GATEWAY_SESSION_MAP_SCOPE"]="per_chat_bot_user"
    # Cron 作业持久化：file（单节点，默认）| etcd（一体机主备）；AgentOS 一体机模板默认 etcd
    ["CRON_STORE_BACKEND"]="etcd"
    ["ETCD_ENDPOINTS"]=""
    ["AGENTOS_CODE_GRAPH_PROFILE"]="off"
    ["TOOL_SANDBOX_TYPE"]="jiuwenbox"
    ["TOOL_SANDBOX_ENABLE"]="true"
    ["TOOL_SANDBOX_EXECUTOR"]="bubblewrap"
    ["TOOL_SANDBOX_IMAGE"]=""
    ["TOOL_SANDBOX_CPU"]=""
    ["TOOL_SANDBOX_MEMORY"]=""
    ["TOOL_SANDBOX_USER"]=""
    ["TOOL_SANDBOX_GROUP"]=""
    ["CONCH_TEMPLATE_NAME"]=""
    ["CONCH_SDK_CONFIG"]=""
    ["MODEL_PROVIDER"]=""
    ["MODEL_NAME"]=""
    ["API_BASE"]=""
    ["API_KEY"]=""
    ["EMBED_API_KEY"]=""
    ["EMBED_API_BASE"]=""
    ["EMBED_MODEL"]=""
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
    # /ws 代理到 gateway 的 WEB_PORT; 独立变量, 与 Gateway 的 WebChannel 端口分开配置
    ["WEB_STATIC_HOST"]=""
    ["WEB_STATIC_PORT"]=""
    ["SANDBOX_IDLE_TIMEOUT_SECONDS"]=""
    # Channel timeout cleanup; empty -> default to gateway.agentos.disconnect_cleanup_timeout_seconds
    ["DISCONNECT_CLEANUP_TIMEOUT_SECONDS"]=""
    ["OS_TYPE"]=""
    ["EXTENSION_DIRS"]=""
    # Gateway config directory override. Defaults to <workspace>/infra/config.
    ["JIUWENSWARM_GATEWAY_CONFIG_DIR"]=""
    # Optional explicit Gateway template path; empty -> use the bundled ADX template.
    ["GATEWAY_CONFIG_TEMPLATE_FILE"]=""
    # AgentOS IAM; empty URL → http://MASTER_NODE_IP:8090 at deploy check time
    ["AGENTOS_AUTH_SERVICE_URL"]=""
    ["AGENTOS_AUTH_TIMEOUT"]=""
)

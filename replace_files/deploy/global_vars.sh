#!/usr/bin/env bash
set -euo >/dev/null 2>&1

# =============================================================================
# CORE DATA STRUCTURE
# =============================================================================
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

OYL_REPO_NAME="charts_dev"
OYL_REPO_URL="http://openyuanrong.obs.cn-southwest-2.myhuaweicloud.com/charts_dev"
OYL_CHART_NAME="openyuanrong"
OYL_CHART_VERSION="1.0.202603301703"
OYL_REPO_DIR="${SCRIPT_DIR}/${OYL_CHART_NAME}"

CUSTOM_ENV_FILE="${SCRIPT_DIR}/.env.custom"
ENV_FILE="${SCRIPT_DIR}/.env"

POOL_TEMPLATE_FILE="${SCRIPT_DIR}/conf/pool.template.json"
POOL_FILE="${SCRIPT_DIR}/conf/pool.json"

CLAW_META_TEMPLATE_FILE="${SCRIPT_DIR}/conf/claw_meta.template.json"
CLAW_META_FILE="${SCRIPT_DIR}/conf/claw_meta.json"

GATEWAY_CONFIG_TEMPLATE_FILE=""
GATEWAY_CONFIG_FILE="${SCRIPT_DIR}/conf/config.yaml"

GATEWAY_ENV_TEMPLATE_FILE="${SCRIPT_DIR}/conf/gateway.template.env"
GATEWAY_ENV_FILE="${SCRIPT_DIR}/conf/gateway.env"

GATEWAY_RBAC_TEMPLATE_FILE="${SCRIPT_DIR}/conf/gateway-rbac.template.yaml"
GATEWAY_RBAC_FILE="${SCRIPT_DIR}/conf/gateway-rbac.yaml"

GATEWAY_DEPLOYMENT_FILE="${SCRIPT_DIR}/conf/deployment.yaml"

NFS_SERVER_TEMPLATE_FILE="${SCRIPT_DIR}/conf/nfs-server.template.yaml"
NFS_SERVER_FILE="${SCRIPT_DIR}/conf/nfs-server.yaml"

PV_TEMPLATE_FILE="${SCRIPT_DIR}/conf/pv-nfs.template.yaml"
PV_FILE="${SCRIPT_DIR}/conf/pv-nfs.yaml"

PVC_TEMPLATE_FILE="${SCRIPT_DIR}/conf/pvc-nfs.template.yaml"
PVC_FILE="${SCRIPT_DIR}/conf/pvc-nfs.yaml"

REG_FUNC_FILE="${SCRIPT_DIR}/func/clawee.py"

META_PORT=""
CMD=""
MASTER_NODE_NAME=""


# ==== All available modules ====
declare -ga ALL_MODULES=("NFS" "YR_CLAW" "GATEWAY")

declare -ga MODULES=()

declare -ga WORKER_NODE_IPS=()

declare -A DEPLOY_VARS=(
    ["NAMESPACE"]="default"
    ["POOL_ID"]="claw"
    ["GATEWAY_DEPLOYMENT_NAME"]="jiuwenclaw-gateway"
    ["FUNC_SVC_NAME"]="0@jiuwen@clawtest"
    ["GATEWAY_CONFIG_MAP_NAME"]="jiuwenclaw-gateway-config"
    ["GATEWAY_ENV_FILE_NAME"]="jiuwenclaw-gateway-env"
    ["NFS_NAME"]="nfs-server"
    ["NFS_HOST_PATH"]="/data/nfs"
    ["PVC_NAME"]="pvc-nfs-shared"
    ["PV_NAME"]="pv-nfs-shared"
    ["NFS_SHARE_PATH"]="/"
    ["GATEWAY_SERVICE_ACCOUNT"]="jiuwenclaw-gateway-sa"
    ["AGENT_SERVER_POD_NAME"]="jiuwenclaw-agentserver"
)

declare -A OYR_COMPONENTS=(
        ["frontend"]="deployment"
        ["function-scheduler"]="deployment"
        ["function-manager"]="deployment"
        ["function-master"]="deployment"
        ["meta-service"]="deployment"
        ["iam-adaptor"]="deployment"
        ["function-proxy"]="daemonset"
        ["ds-worker"]="daemonset"
        ["etcd"]="statefulset"
        ["minio"]="statefulset"
    )
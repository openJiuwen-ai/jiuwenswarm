#!/usr/bin/env bash
# 生成部署工具配置文件 .env.custom
# 用法: ./gen_env_custom.sh <ARCH> <VERSION> <TYPE>
#   ARCH:    arm64 | amd64
#   VERSION: 镜像版本号（如 0.0.12）
#   TYPE:    product — 客户交付版，不含 JINA / Proxy / Manager / Identity 相关变量
#            dev     — 内部使用版，包含全部变量
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${SCRIPT_DIR}/.env.custom"

usage() {
    echo "Usage: $0 <ARCH> <VERSION> <TYPE>"
    echo "  ARCH:    arm64 | amd64"
    echo "  VERSION: 镜像版本号，例如 0.0.12"
    echo "  TYPE:    product（客户交付，不含 JINA/Proxy/Manager/Identity）"
    echo "           dev（内部使用，包含全部变量）"
    exit 1
}

[[ $# -ne 3 ]] && usage

ARCH="$1"
VERSION="$2"
TYPE="$3"

[[ "${ARCH}" == "arm64" || "${ARCH}" == "amd64" ]] || { echo "ERROR: ARCH 必须为 arm64 或 amd64"; exit 1; }
[[ -n "${VERSION}" ]] || { echo "ERROR: VERSION 不能为空"; exit 1; }
[[ "${TYPE}" == "product" || "${TYPE}" == "dev" ]] || { echo "ERROR: TYPE 必须为 product 或 dev"; exit 1; }

NFS_SC_IMAGE="swr.cn-north-4.myhuaweicloud.com/ddn-k8s/registry.k8s.io/sig-storage/nfs-subdir-external-provisioner:v4.0.2"
if [[ "${ARCH}" == "arm64" ]]; then
  NFS_SC_IMAGE="swr.cn-north-4.myhuaweicloud.com/ddn-k8s/registry.k8s.io/sig-storage/nfs-subdir-external-provisioner:v4.0.2-linuxarm64"
fi

{
cat <<EOF
NFS_IMAGE="swr.cn-north-4.myhuaweicloud.com/openjiuwen/ghcr.io/obeone/nfs-server-${ARCH}:2.2.2"
NFS_SC_IMAGE="${NFS_SC_IMAGE}"
MYSQL_IMAGE="swr.cn-north-4.myhuaweicloud.com/openjiuwen/claw-mysql-${ARCH}:8.0"
POSTGRESQL_IMAGE="swr.cn-north-4.myhuaweicloud.com/openjiuwen/postgres-${ARCH}:16"
RABBITMQ_IMAGE="swr.cn-north-4.myhuaweicloud.com/openjiuwen/rabbitmq-${ARCH}:3.9.22-management"
REDIS_IMAGE="swr.cn-north-4.myhuaweicloud.com/openjiuwen/redis-${ARCH}:7-alpine"
MINIO_IMAGE="swr.cn-north-4.myhuaweicloud.com/openjiuwen/minio/minio-${ARCH}:RELEASE.2024-12-18T13-15-44Z"
FLUENT_BIT_IMAGE="swr.cn-north-4.myhuaweicloud.com/openjiuwen/fluent/fluent-bit-${ARCH}:3.0.0"
VECTOR_IMAGE="swr.cn-north-4.myhuaweicloud.com/openjiuwen/timberio/vector-${ARCH}:0.40.0-alpine-vim"
EOF

if [[ "${TYPE}" == "dev" ]]; then
cat <<EOF
JINA_READER_IMAGE="swr.cn-north-4.myhuaweicloud.com/openjiuwen/ghcr.1ms.run/jina-ai/reader-${ARCH}:latest"
JINA_CACHE_IMAGE="swr.cn-north-4.myhuaweicloud.com/openjiuwen/nginx-${ARCH}:alpine"
PROXY_IMAGE="swr.cn-north-4.myhuaweicloud.com/openjiuwen/nginx-${ARCH}:alpine"
EOF
fi

cat <<EOF

GATEWAY_IMAGE="swr.cn-north-4.myhuaweicloud.com/openjiuwen/jiuwenclaw-core-${ARCH}:${VERSION}"
AGENT_SERVER_IMAGE="swr.cn-north-4.myhuaweicloud.com/openjiuwen/jiuwenclaw-core-${ARCH}:${VERSION}"
WEB_IMAGE="swr.cn-north-4.myhuaweicloud.com/openjiuwen/jiuwenclaw-web-${ARCH}:${VERSION}"
EOF

if [[ "${TYPE}" == "dev" ]]; then
cat <<EOF
MANAGER_SERVER_IMAGE="swr.cn-north-4.myhuaweicloud.com/openjiuwen/jiuwenclaw-manager-server-${ARCH}:${VERSION}"
MANAGER_WEB_IMAGE="swr.cn-north-4.myhuaweicloud.com/openjiuwen/jiuwenclaw-manager-web-${ARCH}:${VERSION}"
EOF
fi

cat <<EOF
JIUWENBOX_IMAGE="swr.cn-north-4.myhuaweicloud.com/openjiuwen/jiuwenclaw-sandbox-${ARCH}:${VERSION}"
EOF

if [[ "${TYPE}" == "dev" ]]; then
cat <<EOF
IDENTITY_IMAGE="swr.cn-north-4.myhuaweicloud.com/openjiuwen/jiuwenclaw-identity-${ARCH}:${VERSION}"
EOF
fi

cat <<EOF
AGENT_RUNTIME_IMAGE="swr.cn-north-4.myhuaweicloud.com/openjiuwen/jiuwenclaw-agent-runtime-${ARCH}:${VERSION}"

MODE=product
#CLAW_CODE_PATH=""
#RUNTIME_CODE_PATH=""
#CORE_CODE_PATH=""
#IS_MOUNT_WEB_CODE="true"
EOF

if [[ "${TYPE}" == "dev" ]]; then
cat <<'EOF'
#IS_MOUNT_MANAGER_WEB_CODE="true"
EOF
fi

cat <<EOF

DB_TYPE="mysql"
GATEWAY_SESSION_MAP_SCOPE="per_chat_bot_user"

# 请求级扩展字段白名单，逗号分隔；空值表示关闭。
# HTTP 从同名请求头读取，WebSocket 从握手 query 读取。
JIUWENSWARM_REQUEST_EXT_FORWARD_HEADERS=""

# 不设置就能自动寻找空闲端口，如需要设置，请确保该端口的唯一性（范围: 30000-32767）
# MYSQL_NODE_PORT=30036
# POSTGRESQL_NODE_PORT=30032
# RABBITMQ_AMQ_NODE_PORT=30073
# RABBITMQ_MGR_NODE_PORT=30072
# REDIS_NODE_PORT=30079
# GATEWAY_NODE_PORT=30088
EOF

if [[ "${TYPE}" == "dev" ]]; then
cat <<EOF
# MANAGER_SERVER_NODE_PORT=30086
# MANAGER_WEB_NODE_PORT=30273
EOF
fi

cat <<EOF
# WEB_NODE_PORT=30080

# 设置副本数
EOF

if [[ "${TYPE}" == "dev" ]]; then
cat <<EOF
# MANAGER_SERVER_REPLICAS=
EOF
fi

cat <<EOF
# GATEWAY_REPLICAS=
# AGENT_RUNTIME_REPLICAS=

MODEL_PROVIDER=""
MODEL_NAME=""
API_BASE=""
API_KEY=""

#LOG_MASK_ENABLED=false
APPLY_PATCH=true
EOF
} > "${ENV_FILE}"

echo "Generated: ${ENV_FILE} (ARCH=${ARCH}, VERSION=${VERSION}, TYPE=${TYPE})"

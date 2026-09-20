#!/usr/bin/env bash
# 容器启动入口（仅由 gateway / agentserver 容器调用）：
#   校验 ROLE → 生成企业版配置 → 初始化 workspace → 启动对应服务
#   ROLE=gateway     -> jiuwenswarm-gateway
#   ROLE=agentserver -> jiuwenswarm-agentserver
# JIUWENSWARM_CONFIG_DIR 等环境变量由 k8s envFrom 在进程启动前注入，
# 到本脚本执行时已就绪。
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
echo "[start.sh] ROLE='${ROLE}', JIUWENSWARM_EDITION='${JIUWENSWARM_EDITION}'"

# 前置校验：ROLE 不合法直接报错退出（不生成配置、不初始化）
if [ "${ROLE}" != "gateway" ] && [ "${ROLE}" != "agentserver" ]; then
    echo "[start.sh] ERROR: ROLE must be 'gateway' or 'agentserver', got '${ROLE}'" >&2
    exit 1
fi

# 初始化 workspace（先建目录/稀疏配置；企业配置由下一步 update_conf.py 覆盖）
echo "[start.sh] initializing workspace"
jiuwenswarm-init

# 生成企业版配置（app 身份执行 → 覆盖上一步稀疏版；失败即退出，不 startup）
echo "[start.sh] generating config via update_conf.py"
echo "[start.sh] JIUWENSWARM_CONFIG_DIR='${JIUWENSWARM_CONFIG_DIR}'"
if ! python3 "${SCRIPT_DIR}/update_conf.py"; then
    echo "[start.sh] update_conf.py FAILED" >&2
    exit 1
fi

echo "[start.sh] starting jiuwenswarm-${ROLE}"
exec "jiuwenswarm-${ROLE}"

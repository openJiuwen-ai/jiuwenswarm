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


# 生成企业版配置（app 身份执行 → 覆盖上一步稀疏版；失败即退出，不 startup）
echo "[start.sh] generating config via update_conf.py"
echo "[start.sh] JIUWENSWARM_CONFIG_DIR='${JIUWENSWARM_CONFIG_DIR}'"
# 用绝对路径调用：UBI 镜像的 BASH_ENV 会激活 /opt/app-root venv，
# 裸 python3 会命中 venv（无业务依赖）；/usr/local/bin/python3 恒为系统 3.11
if ! /usr/local/bin/python3 "${SCRIPT_DIR}/update_conf.py"; then
    echo "[start.sh] update_conf.py FAILED" >&2
    exit 1
fi

# AGENT_RUNTIME_ENABLED=false（不部署 agent-runtime）时，关断 gateway 的 runtime
# 路由扩展
if [ "${ROLE}" = "gateway" ] && [ "${AGENT_RUNTIME_ENABLED}" == "false" ]; then
    ext_dir="${EXTENSION_DIRS:-packages/jiuwenclaw-ee/gateway/extensions}"
    if [ -z "${EXTENSION_DIRS:-}" ]; then
        echo "[start.sh] WARNING: EXTENSION_DIRS not set, fallback to '${ext_dir}'" >&2
    fi
    file="${ext_dir}/runtime_management_extension/extension.yaml"
    if [ -f "${file}" ]; then
        yq -i '.enabled = false' "${file}"
        echo "[start.sh] AGENT_RUNTIME_ENABLED=false → runtime extension disabled: ${file}"
    else
        echo "[start.sh] WARNING: runtime extension file not found, skip disabling: ${file}" >&2
    fi
fi

echo "[start.sh] starting jiuwenswarm-${ROLE}"
exec "jiuwenswarm-${ROLE}"

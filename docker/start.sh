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

# AGENT_RUNTIME_DEPLOY_MODE=off（不部署 agent-runtime）时，关断 gateway 的 runtime
# 路由扩展；routing/full 保持扩展加载（配合 GATEWAY_AGENT_CLIENT_MODE=runtime 生效）。
# EXTENSION_DIRS 为分号分隔的多目录（如 dir1;dir2）；内置扩展目录 
# /app/jiuwenswarm/jiuwenswarm/extensions 无条件追加（+= 语义，无论 EXTENSION_DIRS 
# 是否设置/是否已含该目录）
if [ "${ROLE}" = "gateway" ] && [ "${AGENT_RUNTIME_DEPLOY_MODE}" == "off" ]; then
    ext_dirs=()
    if [ -n "${EXTENSION_DIRS:-}" ]; then
        IFS=';' read -r -a ext_dirs <<< "${EXTENSION_DIRS}"
    fi
    builtin_dir="/app/jiuwenswarm/jiuwenswarm/extensions"
    dir_in_list=0
    for d in "${ext_dirs[@]}"; do
        [ "${d%/}" = "${builtin_dir}" ] && dir_in_list=1 && break
    done
    [ "${dir_in_list}" -eq 0 ] && ext_dirs+=("${builtin_dir}")
    disabled_any=0
    for ext_dir in "${ext_dirs[@]}"; do
        [ -z "${ext_dir}" ] && continue
        file="${ext_dir%/}/runtime_management_extension/extension.yaml"
        if [ -f "${file}" ]; then
            yq -i '.enabled = false' "${file}"
            echo "[start.sh] AGENT_RUNTIME_DEPLOY_MODE=off → runtime extension disabled: ${file}"
            disabled_any=1
        fi
    done
    if [ "${disabled_any}" -eq 0 ]; then
        echo "[start.sh] WARNING: runtime extension file not found in any extension dir ($(IFS=';'; echo "${ext_dirs[*]}")), skip disabling" >&2
    fi
fi

echo "[start.sh] starting jiuwenswarm-${ROLE}"
exec "jiuwenswarm-${ROLE}"

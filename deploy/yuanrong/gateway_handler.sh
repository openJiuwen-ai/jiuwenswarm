#!/usr/bin/env bash
set -eu

# gateway 就绪检查最大重试次数（服务健康检查与部署前端口检查共用）
GATEWAY_MAX_RETRY=15

gateway_get_config_dir() {
    local instance_name="${DEPLOY_VARS["JIUWENSWARM_INSTANCE_NAME"]}"
    if [ -n "${instance_name}" ]; then
        echo "${DEPLOY_VARS["HOME"]:-/root}/.jiuwenswarm/config"
    else
        echo "${DEPLOY_VARS["HOME"]:-/root}/.jiuwenswarm/config"
    fi
}

gateway_load_config() {
    local bindings
    bindings=$(swarm_service variables)
    eval "${bindings}"
}

# ===== systemd 支持 =====
# 检测目标主机上 systemd 是否可用
gateway_has_systemd() {
    local host="$1"
    exec_on_host "${host}" "command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]" 2>/dev/null
}

# 服务名（多实例时带实例后缀，避免单元冲突）
gateway_service_name() {
    local instance_name="${DEPLOY_VARS["JIUWENSWARM_INSTANCE_NAME"]:-}"
    if [ -n "${instance_name}" ]; then
        echo "jiuwenswarm-gateway-${instance_name}"
    else
        echo "jiuwenswarm-gateway"
    fi
}

# systemd 模式启动：生成 unit + drop-in（覆盖 User + 环境变量），daemon-reload，restart，健康检查
gateway_start_systemd() {
    local master_host="$1"
    local force_root_home="${2:-0}"
    local instance_name="${DEPLOY_VARS["JIUWENSWARM_INSTANCE_NAME"]:-}"
    local gw_host="${DEPLOY_VARS["GATEWAY_HOST"]:-0.0.0.0}"
    local gw_port="${DEPLOY_VARS["GATEWAY_PORT"]:-19001}"
    local web_port="${DEPLOY_VARS["WEB_PORT"]:-19000}"

    local svc_name
    svc_name=$(gateway_service_name)
    local unit_file="/etc/systemd/system/${svc_name}.service"
    local dropin_dir="/etc/systemd/system/${svc_name}.service.d"
    local dropin_file="${dropin_dir}/env.conf"

    # gateway 日志独立目录（gateway.log 及轮转归档）；源码默认与其它日志同目录，
    # 由部署脚本显式指定 /var/log/agentos（Linux），可通过 .env.custom 覆盖。
    local gateway_log_dir="${DEPLOY_VARS["AGENTOS_GATEWAY_LOG_DIR"]:-/var/log/agentos}"

    # 解析 jiuwenswarm-gateway 和 python bin/lib 目录的绝对路径（远程主机上）
    local gw_bin py_bindir py_libdir
    gw_bin=$(exec_on_host "${master_host}" "command -v jiuwenswarm-gateway" 2>/dev/null | tr -d '\r') || true
    [ -n "${gw_bin}" ] || error "jiuwenswarm-gateway not found on ${master_host}; run 'install' first"
    py_bindir=$(exec_on_host "${master_host}" "dirname \$(command -v ${BUSINESS_PYTHON}) 2>/dev/null" | tr -d '\r') || py_bindir="/usr/local/bin"
    py_libdir="${py_bindir}/lib"

    # 获取目标主机现有的 PATH / LD_LIBRARY_PATH（安装前可能已被设置，需追加保留）
    local remote_path remote_ld_lib
    remote_path=$(exec_on_host "${master_host}" "echo \$PATH" 2>/dev/null | tr -d '\r') || remote_path=""
    remote_ld_lib=$(exec_on_host "${master_host}" "echo \${LD_LIBRARY_PATH:-}" 2>/dev/null | tr -d '\r') || remote_ld_lib=""

    local check_script="${SWARM_SERVICE_PREFLIGHT}"
    if [ -n "${check_script}" ]; then
        [ -x "${check_script}" ] || error "Service preflight is not executable: ${check_script}"
    fi

    # 生成 unit 文件内容
    local exec_start_pre=""
    if [ -n "${check_script}" ]; then
        # 注意这里是换行
        exec_start_pre="ExecStartPre=${check_script}
"
    fi
    local unit_content
    unit_content="[Unit]
Description=Jiuwenswarm Gateway
After=network.target
StartLimitIntervalSec=60
StartLimitBurst=5

[Service]
${exec_start_pre}ExecStart=${gw_bin} --require-adx-router --dotenv \"${DEPLOY_VARS["APP_ENV_FILE"]}\"
Environment=HOME=${DEPLOY_VARS["HOME"]:-/root}
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target"

    # 生成 drop-in 内容（覆盖 User + 注入环境变量）
    local dropin_content
    dropin_content="[Service]
User=
Environment=PATH=${py_bindir}:${remote_path}
Environment=LD_LIBRARY_PATH=${py_libdir}:${remote_ld_lib}
Environment=GATEWAY_HOST=${gw_host}
Environment=GATEWAY_PORT=${gw_port}
Environment=WEB_HOST=${DEPLOY_VARS["WEB_HOST"]:-127.0.0.1}
Environment=WEB_PORT=${web_port}
Environment=AGENTOS_GATEWAY_LOG_DIR=${gateway_log_dir}"
    dropin_content="${dropin_content}
Environment=JIUWENSWARM_RUNTIME_PROFILE=${DEPLOY_VARS["RUNTIME_PROFILE"]:-standalone}"
    if [ "${force_root_home}" = "1" ]; then
        dropin_content="${dropin_content}
Environment=JIUWENSWARM_HOME=${DEPLOY_VARS["HOME"]:-/root}"
    fi
    if [ -n "${instance_name}" ]; then
        dropin_content="${dropin_content}
Environment=JIUWENSWARM_DATA_DIR=${DEPLOY_VARS["HOME"]:-/root}/.jiuwenswarm"
    fi

    # 确保 gateway 日志目录存在且运行用户可写（服务以 root 运行，通常天然可写）
    exec_on_host "${master_host}" "mkdir -p '${gateway_log_dir}'" || true

    # 写本地临时文件后 copy_to_host 到目标主机（与 config 文件下发方式一致）
    info "Creating systemd unit ${svc_name} on ${master_host}..."
    local tmp_unit="/tmp/${svc_name}.service.$$"
    printf '%s\n' "${unit_content}" > "${tmp_unit}"
    copy_to_host "${master_host}" "${tmp_unit}" "${unit_file}"
    rm -f "${tmp_unit}"

    local tmp_dropin="/tmp/${svc_name}-env.conf.$$"
    printf '%s\n' "${dropin_content}" > "${tmp_dropin}"
    exec_on_host "${master_host}" "mkdir -p '${dropin_dir}'"
    copy_to_host "${master_host}" "${tmp_dropin}" "${dropin_file}"
    rm -f "${tmp_dropin}"

    exec_on_host "${master_host}" "systemctl daemon-reload"
    exec_on_host "${master_host}" "systemctl enable ${svc_name}" 2>/dev/null || true
    exec_on_host "${master_host}" "systemctl restart ${svc_name}" || error "Failed to start ${svc_name} on ${master_host}"

    # 健康检查：systemd active 且 GATEWAY_PORT/WEB_PORT 端口均处于 LISTEN，
    # 避免服务"刚开始即崩溃"时 is-active 短暂返回 active 而误报成功。
    local retry=0
    while [ ${retry} -lt ${GATEWAY_MAX_RETRY} ]; do
        sleep 2
        if exec_on_host "${master_host}" "systemctl is-active --quiet ${svc_name}" 2>/dev/null \
            && port_is_listening "${master_host}" "${gw_port}" \
            && port_is_listening "${master_host}" "${web_port}"; then
            success "Gateway service is running on ${master_host} (systemd: ${svc_name}) (ports ${gw_port}/${web_port} listening)"
            return 0
        fi
        retry=$((retry + 1))
        info "Waiting for gateway to start... (${retry}/${GATEWAY_MAX_RETRY})"
    done

    # 服务起不来：分别探测 systemd 状态与端口监听，明确给出是 gateway 未启动、还是启动了但端口未监听。
    # 直接取 systemctl is-active 的原始输出（active/inactive/failed/activating/auto-restart/deactivating 等），
    # 避免仅用 --quiet 判断导致正在重试启动(activating/auto-restart)被误报为 inactive。
    local gw_state
    gw_state=$(exec_on_host "${master_host}" "systemctl is-active ${svc_name} 2>/dev/null" | tr -d '\r')
    [ -n "${gw_state}" ] || gw_state="unknown"

    local gw_listen="no" web_listen="no"
    port_is_listening "${master_host}" "${gw_port}" && gw_listen="yes"
    port_is_listening "${master_host}" "${web_port}" && web_listen="yes"
    info "jiuwenswarm-gateway on ${master_host}: systemd=${gw_state}; port ${gw_port}(GATEWAY_PORT)=${gw_listen}, port ${web_port}(WEB_PORT)=${web_listen}"

    warning "netstat -ltn on ${master_host} (ports ${gw_port}/${web_port}):"
    exec_on_host "${master_host}" "netstat -ltn 2>/dev/null | grep -E ':(${gw_port}|${web_port})\\b' || true"
    warning "systemctl status ${svc_name} on ${master_host}:"
    exec_on_host "${master_host}" "systemctl status --no-pager -l ${svc_name} 2>/dev/null || true"
    error "jiuwenswarm-gateway did NOT start on ${master_host} (systemd=${gw_state}, ports ${gw_port}=${gw_listen}/${web_port}=${web_listen}). Check: journalctl -u ${svc_name} -n 50"
}

# nohup 模式启动（systemd 不可用时回退）
gateway_start_nohup() {
    local master_host="$1"
    local home_prefix="${2:-}"
    local instance_name="${DEPLOY_VARS["JIUWENSWARM_INSTANCE_NAME"]:-}"
    local gw_port="${DEPLOY_VARS["GATEWAY_PORT"]:-19001}"
    local web_port="${DEPLOY_VARS["WEB_PORT"]:-19000}"

    # gateway 日志独立目录（与 systemd 模式一致），通过环境变量传给进程
    local gateway_log_dir="${DEPLOY_VARS["AGENTOS_GATEWAY_LOG_DIR"]:-/var/log/agentos}"
    exec_on_host "${master_host}" "mkdir -p '${gateway_log_dir}'" || true
    local log_dir_prefix="JIUWENSWARM_RUNTIME_PROFILE=${DEPLOY_VARS["RUNTIME_PROFILE"]:-standalone} AGENTOS_GATEWAY_LOG_DIR=${gateway_log_dir} GATEWAY_HOST=${DEPLOY_VARS["GATEWAY_HOST"]:-0.0.0.0} GATEWAY_PORT=${gw_port} WEB_HOST=${DEPLOY_VARS["WEB_HOST"]:-127.0.0.1} WEB_PORT=${web_port} "

    local start_cmd="${home_prefix}${log_dir_prefix}nohup jiuwenswarm-gateway --require-adx-router --dotenv \"${DEPLOY_VARS["APP_ENV_FILE"]}\" </dev/null > /tmp/jiuwenswarm-gateway.log 2>&1 &"
    if [ -n "${instance_name}" ]; then
        start_cmd="${home_prefix}JIUWENSWARM_DATA_DIR=${DEPLOY_VARS["HOME"]:-/root}/.jiuwenswarm ${log_dir_prefix}nohup jiuwenswarm-gateway --require-adx-router --dotenv \"${DEPLOY_VARS["APP_ENV_FILE"]}\" </dev/null > /tmp/jiuwenswarm-gateway.log 2>&1 &"
    fi

    info "Starting jiuwenswarm-gateway on ${master_host} (nohup)..."
    exec_on_host "${master_host}" "bash -c '${start_cmd}'"

    local retry=0
    while [ ${retry} -lt ${GATEWAY_MAX_RETRY} ]; do
        sleep 2
        if exec_on_host "${master_host}" "pgrep -f '[j]iuwenswarm-gateway' >/dev/null 2>&1" \
            && port_is_listening "${master_host}" "${gw_port}" \
            && port_is_listening "${master_host}" "${web_port}"; then
            success "Gateway process is running on ${master_host} (ports ${gw_port}/${web_port} listening)"
            return 0
        fi
        retry=$((retry + 1))
        info "Waiting for gateway to start... (${retry}/${GATEWAY_MAX_RETRY})"
    done

    # 失败诊断：区分是进程没起来、还是起来了但端口未监听（对标 systemd 分支的 gw_state/监听探测）。
    local gw_proc="no" gw_listen="no" web_listen="no"
    exec_on_host "${master_host}" "pgrep -f '[j]iuwenswarm-gateway' >/dev/null 2>&1" && gw_proc="yes"
    port_is_listening "${master_host}" "${gw_port}" && gw_listen="yes"
    port_is_listening "${master_host}" "${web_port}" && web_listen="yes"
    info "jiuwenswarm-gateway on ${master_host}: proc(alive)=${gw_proc}; port ${gw_port}(GATEWAY_PORT)=${gw_listen}, port ${web_port}(WEB_PORT)=${web_listen}"

    warning "netstat -ltn on ${master_host} (ports ${gw_port}/${web_port}):"
    exec_on_host "${master_host}" "netstat -ltn 2>/dev/null | grep -E ':(${gw_port}|${web_port})\\b' || true"
    error "Gateway process failed to start on ${master_host} (proc=${gw_proc}, ports ${gw_port}=${gw_listen}/${web_port}=${web_listen}). Check: /tmp/jiuwenswarm-gateway.log"
}

# 本机是否属于 config.yaml 的 cluster.master_nodes；返回 0 表示属于。
# 部署层据此安装 gateway（含 ExecStartPre VIP 拦截），启动时再判定是否持有 VIP。
# 无 master_nodes 配置视为单机模式：本机即 master 节点（向后兼容）。
gateway_deploy_process() {
    local master_host
    master_host=$(get_local_ip)   # 本机即 ingress master，gateway 部署在本机
    local instance_name="${DEPLOY_VARS["JIUWENSWARM_INSTANCE_NAME"]}"

    # 幂等保护：服务已运行时跳过整个部署（与 agent-registry 一致），
    # 避免重复 up 触发 jiuwenswarm-init 重建工作区 + systemctl restart 重启运行中进程、
    # 以及 nohup 模式重复拉起；配置更新需先 down 再 up。
    local svc_name
    svc_name=$(gateway_service_name)
    if gateway_has_systemd "${master_host}"; then
        if exec_on_host "${master_host}" "systemctl is-active --quiet ${svc_name}" 2>/dev/null; then
            warning "${svc_name} already running; run 'down' first to redeploy"
            return 0
        fi
    fi
    if ! gateway_has_systemd "${master_host}" && exec_on_host "${master_host}" "pgrep -f '[j]iuwenswarm-gateway' >/dev/null 2>&1"; then
        warning "jiuwenswarm-gateway process already alive (unit not active: deactivating/legacy nohup); run 'down' first to redeploy"
        return 0
    fi

    info "Deploying gateway on ${master_host}..."

    local home_prefix="JIUWENSWARM_HOME=${DEPLOY_VARS["HOME"]:-/root} "
    local force_root_home=1

    # 启动 gateway：优先 systemd，不可用时回退 nohup
    if gateway_has_systemd "${master_host}"; then
        gateway_start_systemd "${master_host}" "${force_root_home}"
    else
        warning "systemd not available on ${master_host}, falling back to nohup mode"
        gateway_start_nohup "${master_host}" "${home_prefix}"
    fi
}

# systemd 模式停止，不禁用开机自启动
gateway_stop_systemd() {
    local master_host="$1"
    local svc_name
    svc_name=$(gateway_service_name)
    info "Stopping gateway service on ${master_host}..."
    local loaded
    loaded=$(exec_on_host "${master_host}" "systemctl show --property=LoadState --value ${svc_name}")
    [ "${loaded}" != "not-found" ] || return 0
    exec_on_host "${master_host}" "systemctl stop ${svc_name}"
    success "Gateway service stopped on ${master_host}"
}

# nohup 模式停止
gateway_stop_nohup() {
    local master_host="$1"
    local instance_name="${DEPLOY_VARS["JIUWENSWARM_INSTANCE_NAME"]:-}"
    if [ -n "${instance_name}" ]; then
        exec_on_host "${master_host}" "pkill -f 'JIUWENSWARM_DATA_DIR=${DEPLOY_VARS["HOME"]:-/root}/.jiuwenswarm.*[j]iuwenswarm-gateway' || true"
    else
        exec_on_host "${master_host}" "pkill -f '[j]iuwenswarm-gateway' || true"
    fi
    success "Gateway stopped on ${master_host}"
}

gateway_undeploy_process() {
    local master_host
    master_host=$(get_local_ip)
    info "Stopping gateway on ${master_host}..."

    if gateway_has_systemd "${master_host}"; then
        gateway_stop_systemd "${master_host}"
    else
        gateway_stop_nohup "${master_host}"
    fi

    success "Gateway stopped on ${master_host}"
}

deploy_gateway() {
    gateway_deploy_process
}

uninstall_gateway() {
    gateway_undeploy_process
}

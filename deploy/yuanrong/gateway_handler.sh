#!/usr/bin/env bash
set -euo >/dev/null 2>&1

gateway_get_config_dir() {
    local instance_name="${DEPLOY_VARS["JIUWENSWARM_INSTANCE_NAME"]}"
    if [ -n "${instance_name}" ]; then
        echo "/root/.jiuwenswarm-instances/${instance_name}/config"
    else
        echo "/root/.jiuwenswarm/config"
    fi
}

# yuanrong 部署固定使用 /root；若目标机 JIUWENSWARM_HOME（或 $HOME）不是 /root 则提示，
# 调用方应强制以 JIUWENSWARM_HOME=/root 覆盖拉起。
gateway_check_jiuwenswarm_home() {
    local host="$1"
    local effective_home
    # 脚本顶层是 set -euo（-o 无参数被静默忽略），无 pipefail；管道退出码由 tr 决定。
    # 必须在 $() 子 shell 内读 PIPESTATUS[0] 再 exit，才能把 exec_on_host 失败传出。
    # 父 shell 在 $(cmd1|cmd2) 之后的 PIPESTATUS[0] 只是子 shell 整体状态，拿不到 cmd1。
    effective_home=$(
        exec_on_host "${host}" 'printf %s "${JIUWENSWARM_HOME:-$HOME}"' | tr -d '\r'
        exit "${PIPESTATUS[0]}"
    ) || {
        warning "Failed to read JIUWENSWARM_HOME from ${host}"
        return 1
    }
    if [ -z "${effective_home}" ]; then
        effective_home="/root"
    fi
    # 去掉末尾 /
    effective_home="${effective_home%/}"

    if [ "${effective_home}" != "/root" ]; then
        warning "JIUWENSWARM_HOME on ${host} is '${effective_home}', expected '/root'. Forcing overwrite under /root."
        return 1
    fi
    return 0
}

gateway_compute_extension_dirs() {
    if [ -n "${DEPLOY_VARS["EXTENSION_DIRS"]:-}" ]; then
        info "EXTENSION_DIRS already set: ${DEPLOY_VARS["EXTENSION_DIRS"]}"
        return 0
    fi

    local master_host
    master_host=$(get_local_ip)   # gateway 在本机（ingress master）运行，EXTENSION_DIRS 取本机 jiuwenswarm 安装位置
    local python_version="${DEPLOY_VARS["YR_PYTHON_VERSION"]}"

    local jiuwenswarm_location
    jiuwenswarm_location=$(exec_on_host "${master_host}" "python${python_version} -m pip show jiuwenswarm 2>/dev/null | grep -i '^Location:' | awk '{print \$2}'" | tr -d '\r') || true

    if [ -n "${jiuwenswarm_location}" ]; then
        DEPLOY_VARS["EXTENSION_DIRS"]="${jiuwenswarm_location}/jiuwenswarm/extensions"
        info "EXTENSION_DIRS inferred from jiuwenswarm install on ${master_host}: ${DEPLOY_VARS["EXTENSION_DIRS"]}"
    else
        warning "Could not infer EXTENSION_DIRS: jiuwenswarm not found on ${master_host}. You may set EXTENSION_DIRS in .env.custom manually."
    fi
}

gateway_gen_config() {
    gateway_compute_extension_dirs

    info "Generating gateway config.yaml from template..."
    render_config_template "${GATEWAY_CONFIG_TEMPLATE_FILE}" "${GATEWAY_CONFIG_FILE}" "DEPLOY_VARS"

    info "Generating gateway .env from DEPLOY_VARS..."
    write_env_to_file "${GATEWAY_ENV_FILE}" "DEPLOY_VARS"

    local config_dir
    config_dir=$(gateway_get_config_dir)
    info "Gateway config will be deployed to: ${config_dir}/"
    success "Gateway config files generated"
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
    local python_version="${DEPLOY_VARS["YR_PYTHON_VERSION"]}"

    local svc_name
    svc_name=$(gateway_service_name)
    local unit_file="/etc/systemd/system/${svc_name}.service"
    local dropin_dir="/etc/systemd/system/${svc_name}.service.d"
    local dropin_file="${dropin_dir}/env.conf"

    # 解析 jiuwenswarm-gateway 和 python bin/lib 目录的绝对路径（远程主机上）
    local gw_bin py_bindir py_libdir
    gw_bin=$(exec_on_host "${master_host}" "command -v jiuwenswarm-gateway" 2>/dev/null | tr -d '\r') || true
    [ -n "${gw_bin}" ] || error "jiuwenswarm-gateway not found on ${master_host}; run 'install' first"
    py_bindir=$(exec_on_host "${master_host}" "dirname \$(command -v python${python_version}) 2>/dev/null" | tr -d '\r') || py_bindir="/usr/local/bin"
    py_libdir="${py_bindir}/lib"

    # 获取目标主机现有的 PATH / LD_LIBRARY_PATH（安装前可能已被设置，需追加保留）
    local remote_path remote_ld_lib
    remote_path=$(exec_on_host "${master_host}" "echo \$PATH" 2>/dev/null | tr -d '\r') || remote_path=""
    remote_ld_lib=$(exec_on_host "${master_host}" "echo \${LD_LIBRARY_PATH:-}" 2>/dev/null | tr -d '\r') || remote_ld_lib=""

    # 确保 ingress master 检查脚本在目标主机上存在
    # 优先检查远程主机是否已有（可能由 agent-gateway install 已复制）；
    # 不存在则从本地 ${SCRIPT_DIR}/../check-ingress-master.sh 复制到远程主机，
    # 使 gateway 部署不依赖注册中心先安装的顺序。
    local check_script="/usr/local/bin/agentos-check-ingress-master"
    if ! exec_on_host "${master_host}" "test -f '${check_script}'" 2>/dev/null; then
        local check_src="${SCRIPT_DIR}/../check-ingress-master.sh"
        if [ -f "${check_src}" ]; then
            info "Copying check-ingress-master to ${master_host}:${check_script}..."
            copy_to_host "${master_host}" "${check_src}" "${check_script}"
            exec_on_host "${master_host}" "chmod +x '${check_script}'"
            success "Installed ingress master check script on ${master_host}: ${check_script}"
        else
            warning "check-ingress-master.sh not found at ${check_src}, ExecStartPre will be skipped"
            check_script=""
        fi
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
${exec_start_pre}ExecStart=${gw_bin}
Environment=HOME=/root
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
Environment=WEB_PORT=${web_port}"
    if [ "${force_root_home}" = "1" ]; then
        dropin_content="${dropin_content}
Environment=JIUWENSWARM_HOME=/root"
    fi
    if [ -n "${instance_name}" ]; then
        dropin_content="${dropin_content}
Environment=JIUWENSWARM_DATA_DIR=/root/.jiuwenswarm-instances/${instance_name}"
    fi

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

    # 健康检查
    local retry=0
    local max_retry=10
    while [ ${retry} -lt ${max_retry} ]; do
        sleep 2
        if exec_on_host "${master_host}" "systemctl is-active --quiet ${svc_name}" 2>/dev/null; then
            success "Gateway service is running on ${master_host} (systemd: ${svc_name})"
            return 0
        fi
        retry=$((retry + 1))
        info "Waiting for gateway to start... (${retry}/${max_retry})"
    done

    error "Gateway service failed to start on ${master_host}, check: journalctl -u ${svc_name}"
}

# nohup 模式启动（systemd 不可用时回退）
gateway_start_nohup() {
    local master_host="$1"
    local home_prefix="${2:-}"
    local instance_name="${DEPLOY_VARS["JIUWENSWARM_INSTANCE_NAME"]:-}"

    local start_cmd="${home_prefix}nohup jiuwenswarm-gateway </dev/null > /tmp/jiuwenswarm-gateway.log 2>&1 &"
    if [ -n "${instance_name}" ]; then
        start_cmd="${home_prefix}JIUWENSWARM_DATA_DIR=/root/.jiuwenswarm-instances/${instance_name} nohup jiuwenswarm-gateway </dev/null > /tmp/jiuwenswarm-gateway.log 2>&1 &"
    fi

    info "Starting jiuwenswarm-gateway on ${master_host} (nohup)..."
    exec_on_host "${master_host}" "bash -c '${start_cmd}'"

    local retry=0
    local max_retry=10
    while [ ${retry} -lt ${max_retry} ]; do
        sleep 2
        if exec_on_host "${master_host}" "pgrep -f '[j]iuwenswarm-gateway' >/dev/null 2>&1"; then
            success "Gateway process is running on ${master_host}"
            return 0
        fi
        retry=$((retry + 1))
        info "Waiting for gateway to start... (${retry}/${max_retry})"
    done

    error "Gateway process failed to start on ${master_host}, check /tmp/jiuwenswarm-gateway.log"
}

gateway_deploy_process() {
    # gateway 跟随 ingress_virtual_ip：仅在本机持有 VIP 时部署/启动 gateway，否则跳过。
    # 不依赖 master_nodes / CLUSTER_HOSTS[0]（可能有多个 master_nodes，且其 IP 与 VIP 不同）。
    if ! /usr/local/bin/agentos-check-ingress-master >/dev/null 2>&1; then
        warning "Local host does not hold ingress vip, skip gateway deploy process."
        return 0
    fi

    local master_host
    master_host=$(get_local_ip)   # 本机即 ingress master，gateway 部署在本机
    local instance_name="${DEPLOY_VARS["JIUWENSWARM_INSTANCE_NAME"]}"

    info "Deploying gateway on ${master_host}..."

    gateway_gen_config

    local config_dir
    config_dir=$(gateway_get_config_dir)

    local home_prefix=""
    local force_root_home=0
    if ! gateway_check_jiuwenswarm_home "${master_host}"; then
        home_prefix="JIUWENSWARM_HOME=/root "
        force_root_home=1
    fi

    local init_cmd="${home_prefix}jiuwenswarm-init -f </dev/null"
    if [ -n "${instance_name}" ]; then
        init_cmd="${home_prefix}JIUWENSWARM_DATA_DIR=/root/.jiuwenswarm-instances/${instance_name} jiuwenswarm-init -f </dev/null"
    fi

    info "Running jiuwenswarm-init on ${master_host}..."
    if exec_on_host "${master_host}" "${init_cmd}"; then
        success "jiuwenswarm-init completed on ${master_host}"
    else
        error "Failed to run jiuwenswarm-init on ${master_host}"
    fi

    info "Copying gateway config.yaml and .env to ${master_host}:${config_dir}/..."
    exec_on_host "${master_host}" "mkdir -p ${config_dir}"
    copy_to_host "${master_host}" "${GATEWAY_CONFIG_FILE}" "${config_dir}/config.yaml"
    copy_to_host "${master_host}" "${GATEWAY_ENV_FILE}" "${config_dir}/.env"

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
    exec_on_host "${master_host}" "systemctl stop ${svc_name} 2>/dev/null || true"
    success "Gateway service stopped on ${master_host}"
}

# nohup 模式停止
gateway_stop_nohup() {
    local master_host="$1"
    local instance_name="${DEPLOY_VARS["JIUWENSWARM_INSTANCE_NAME"]:-}"
    if [ -n "${instance_name}" ]; then
        exec_on_host "${master_host}" "pkill -f 'JIUWENSWARM_DATA_DIR=/root/.jiuwenswarm-instances/${instance_name}.*[j]iuwenswarm-gateway' || true"
    else
        exec_on_host "${master_host}" "pkill -f '[j]iuwenswarm-gateway' || true"
    fi
    success "Gateway stopped on ${master_host}"
}

gateway_undeploy_process() {
    # 与 up 对称：gateway 跟随 ingress_virtual_ip，仅在本机持有 VIP 时停止本机 gateway。
    if ! /usr/local/bin/agentos-check-ingress-master >/dev/null 2>&1; then
        warning "Local host does not hold ingress vip, skip stopping jiuwenswarm-gateway."
        return 0
    fi

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

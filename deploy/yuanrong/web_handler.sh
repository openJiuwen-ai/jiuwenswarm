#!/usr/bin/env bash
set -euo >/dev/null 2>&1

# ============================================================
# 模块: web (jiuwenswarm 前端静态服务器)
# 入口: jiuwenswarm-web (随 jiuwenswarm whl 安装,console script)
# 作用: serve jiuwenswarm/channels/web/frontend/dist 静态文件,
#       /ws 代理到 gateway 的 WebChannel (WEB_PORT, 默认 19000),
#       /api 代理到后端。
# 钩子函数与 gateway_handler.sh 对齐:
#   deploy_web / uninstall_web (systemd 优先, nohup 回退)
#
# 命名说明:
#   app_web.py 内部读 FRONTEND_HOST/FRONTEND_PORT 作为 --host/--port 默认值,
#   但 deploy 仓的 FRONTEND_PORT 已被 yuanrong frontend (8888) 占用。
#   故此处用独立变量 WEB_STATIC_HOST / WEB_STATIC_PORT, 并通过 --host/--port
#   显式传给 jiuwenswarm-web, 不依赖 app_web.py 的默认值读取, 避免撞车。
# ============================================================

# 解析 web server 所在主机(与 gateway 同,均部署在 master 节点)
web_resolve_host() {
    local master_host="${DEPLOY_VARS["MASTER_NODE_IP"]:-}"
    if [ -z "${master_host}" ]; then
        if [ -n "${DEPLOY_VARS["CLUSTER_HOSTS"]:-}" ]; then
            IFS=',' read -ra _web_host_list <<< "${DEPLOY_VARS["CLUSTER_HOSTS"]}"
            master_host="${_web_host_list[0]}"
        else
            master_host=$(get_local_ip)
            info "MASTER_NODE_IP not set, defaulting to local: ${master_host}" >&2
        fi
    fi
    echo "${master_host}"
}

# 多实例时服务名带实例后缀(与 gateway 一致,避免 systemd unit 冲突)
web_service_name() {
    local instance_name="${DEPLOY_VARS["JIUWENSWARM_INSTANCE_NAME"]:-}"
    if [ -n "${instance_name}" ]; then
        echo "jiuwenswarm-web-${instance_name}"
    else
        echo "jiuwenswarm-web"
    fi
}

# 检测目标主机上 systemd 是否可用(与 gateway 同)
web_has_systemd() {
    local host="$1"
    exec_on_host "${host}" "command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]" 2>/dev/null
}

# systemd 模式启动:生成 unit + drop-in(注入 PATH/LD_LIBRARY_PATH),daemon-reload,restart,健康检查
web_start_systemd() {
    local master_host="$1"
    local web_host="${DEPLOY_VARS["WEB_STATIC_HOST"]:-0.0.0.0}"
    local web_port="${DEPLOY_VARS["WEB_STATIC_PORT"]:-5173}"
    local web_port_target="${DEPLOY_VARS["WEB_PORT"]:-19000}"
    local python_version="${DEPLOY_VARS["YR_PYTHON_VERSION"]}"
    local instance_name="${DEPLOY_VARS["JIUWENSWARM_INSTANCE_NAME"]:-}"

    local svc_name
    svc_name=$(web_service_name)
    local unit_file="/etc/systemd/system/${svc_name}.service"
    local dropin_dir="/etc/systemd/system/${svc_name}.service.d"
    local dropin_file="${dropin_dir}/env.conf"

    # 解析 jiuwenswarm-web 和 python bin/lib 绝对路径(远程主机上)
    local web_bin py_bindir py_libdir
    web_bin=$(exec_on_host "${master_host}" "command -v jiuwenswarm-web" 2>/dev/null | tr -d '\r') || true
    [ -n "${web_bin}" ] || error "jiuwenswarm-web not found on ${master_host}; run 'install' first (installs jiuwenswarm whl)"
    py_bindir=$(exec_on_host "${master_host}" "dirname \$(command -v python${python_version}) 2>/dev/null" | tr -d '\r') || py_bindir="/usr/local/bin"
    py_libdir="${py_bindir}/lib"

    # 保留目标主机现有 PATH / LD_LIBRARY_PATH
    local remote_path remote_ld_lib
    remote_path=$(exec_on_host "${master_host}" "echo \$PATH" 2>/dev/null | tr -d '\r') || remote_path=""
    remote_ld_lib=$(exec_on_host "${master_host}" "echo \${LD_LIBRARY_PATH:-}" 2>/dev/null | tr -d '\r') || remote_ld_lib=""

    # /ws 代理目标:web server 本机内的 gateway WebChannel
    local proxy_target="http://127.0.0.1:${web_port_target}"

    # unit 文件:ExecStart 显式传 --host/--port/--proxy-target, 不依赖 app_web.py 的 FRONTEND_HOST/PORT 默认
    local unit_content
    unit_content="[Unit]
Description=JiuwenSwarm Web Static Server
After=network.target jiuwenswarm-gateway.service
StartLimitIntervalSec=60
StartLimitBurst=5

[Service]
ExecStart=${web_bin} --host ${web_host} --port ${web_port} --proxy-target ${proxy_target}
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target"

    # drop-in: 注入 PATH/LD_LIBRARY_PATH + 多实例 data dir
    local dropin_content
    dropin_content="[Service]
User=
Environment=PATH=${py_bindir}:${remote_path}
Environment=LD_LIBRARY_PATH=${py_libdir}:${remote_ld_lib}"
    if [ -n "${instance_name}" ]; then
        dropin_content="${dropin_content}
Environment=JIUWENSWARM_DATA_DIR=/root/.jiuwenswarm-instances/${instance_name}"
    fi

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

    # 健康检查:systemctl is-active
    local retry=0
    local max_retry=10
    while [ ${retry} -lt ${max_retry} ]; do
        sleep 2
        if exec_on_host "${master_host}" "systemctl is-active --quiet ${svc_name}" 2>/dev/null; then
            success "Web server service is running on ${master_host} (systemd: ${svc_name}) -> http://${web_host}:${web_port}"
            return 0
        fi
        retry=$((retry + 1))
        info "Waiting for web server to start... (${retry}/${max_retry})"
    done

    error "Web server failed to start on ${master_host}, check: journalctl -u ${svc_name}"
}

# nohup 模式启动(systemd 不可用时回退)
web_start_nohup() {
    local master_host="$1"
    local home_prefix="${2:-}"
    local web_host="${DEPLOY_VARS["WEB_STATIC_HOST"]:-0.0.0.0}"
    local web_port="${DEPLOY_VARS["WEB_STATIC_PORT"]:-5173}"
    local web_port_target="${DEPLOY_VARS["WEB_PORT"]:-19000}"
    local instance_name="${DEPLOY_VARS["JIUWENSWARM_INSTANCE_NAME"]:-}"

    local web_bin
    web_bin=$(exec_on_host "${master_host}" "command -v jiuwenswarm-web" 2>/dev/null | tr -d '\r') || true
    [ -n "${web_bin}" ] || error "jiuwenswarm-web not found on ${master_host}; run 'install' first (installs jiuwenswarm whl)"

    local proxy_target="http://127.0.0.1:${web_port_target}"
    local instance_env=""
    local pidfile="/tmp/jiuwenswarm-web.pid"
    if [ -n "${instance_name}" ]; then
        instance_env="JIUWENSWARM_DATA_DIR=/root/.jiuwenswarm-instances/${instance_name} "
        pidfile="/tmp/jiuwenswarm-web-${instance_name}.pid"
    fi

    # 显式传 --host/--port/--proxy-target, 避开 app_web.py 的 FRONTEND_HOST/PORT 默认值。
    # JIUWENSWARM_DATA_DIR 等环境变量前缀只进子进程环境、不会出现在 /proc/PID/cmdline,
    # pkill -f 匹配不到, 故启动时把 PID 写入 pidfile, 停止时按 PID 精确结束。
    local start_cmd="${home_prefix}${instance_env}nohup ${web_bin} --host ${web_host} --port ${web_port} --proxy-target ${proxy_target} </dev/null > /tmp/jiuwenswarm-web.log 2>&1 & echo \$! > ${pidfile}"

    info "Starting jiuwenswarm-web on ${master_host} (nohup) -> http://${web_host}:${web_port} (proxy /ws -> ${proxy_target})..."
    exec_on_host "${master_host}" "bash -c '${start_cmd}'"

    local retry=0
    local max_retry=10
    while [ ${retry} -lt ${max_retry} ]; do
        sleep 2
        if exec_on_host "${master_host}" "pgrep -f '[j]iuwenswarm-web' >/dev/null 2>&1"; then
            success "Web process is running on ${master_host} -> http://${web_host}:${web_port}"
            return 0
        fi
        retry=$((retry + 1))
        info "Waiting for web server to start... (${retry}/${max_retry})"
    done

    error "Web process failed to start on ${master_host}, check /tmp/jiuwenswarm-web.log"
}

web_deploy_process() {
    local master_host
    master_host=$(web_resolve_host)
    local instance_name="${DEPLOY_VARS["JIUWENSWARM_INSTANCE_NAME"]}"

    info "Deploying web server on ${master_host}..."

    # web server 无配置文件需渲染(jiuwenswarm whl 自带 frontend/dist), 直接启动。
    # 强制 JIUWENSWARM_HOME=/root 与 gateway 一致(确保多实例 data dir 解析正确)
    local home_prefix=""
    if ! gateway_check_jiuwenswarm_home "${master_host}"; then
        home_prefix="JIUWENSWARM_HOME=/root "
    fi

    if web_has_systemd "${master_host}"; then
        web_start_systemd "${master_host}"
    else
        warning "systemd not available on ${master_host}, falling back to nohup mode"
        web_start_nohup "${master_host}" "${home_prefix}"
    fi
}

# systemd 模式停止,不禁用开机自启(与 gateway 一致)
web_stop_systemd() {
    local master_host="$1"
    local svc_name
    svc_name=$(web_service_name)
    info "Stopping web server service on ${master_host}..."
    exec_on_host "${master_host}" "systemctl stop ${svc_name} 2>/dev/null || true"
    success "Web server service stopped on ${master_host}"
}

# nohup 模式停止
web_stop_nohup() {
    local master_host="$1"
    local instance_name="${DEPLOY_VARS["JIUWENSWARM_INSTANCE_NAME"]:-}"
    local pidfile="/tmp/jiuwenswarm-web.pid"
    local fallback="pkill -f '[j]iuwenswarm-web'"
    if [ -n "${instance_name}" ]; then
        local web_port="${DEPLOY_VARS["WEB_STATIC_PORT"]:-5173}"
        pidfile="/tmp/jiuwenswarm-web-${instance_name}.pid"
        # JIUWENSWARM_DATA_DIR=... 只进环境不进 cmdline, 无法用 pkill -f 精确匹配。
        # 优先按启动时写入的 pidfile 停止; 无 pidfile(遗留进程)时按命令行可见的 --port 匹配。
        fallback="pkill -f 'jiuwenswarm-web.*--port ${web_port}'"
    fi
    # 检测停止结果: 回退 pkill 未匹配到进程(pkill 返回非零)时报错, 而非静默成功。
    # pidfile 分支中 kill 已死进程由 || true 吞掉, 进程本来就不存在, 视为停止成功。
    if ! exec_on_host "${master_host}" "if [ -f '${pidfile}' ]; then kill \$(cat '${pidfile}') 2>/dev/null || true; rm -f '${pidfile}'; else ${fallback}; fi"; then
        error "No running web process found on ${master_host}; nothing was stopped"
    fi
    success "Web server stopped on ${master_host}"
}

web_undeploy_process() {
    local master_host
    master_host=$(web_resolve_host)
    info "Stopping web server on ${master_host}..."

    if web_has_systemd "${master_host}"; then
        web_stop_systemd "${master_host}"
    else
        web_stop_nohup "${master_host}"
    fi

    success "Web server stopped on ${master_host}"
}

deploy_web() {
    web_deploy_process
}

uninstall_web() {
    web_undeploy_process
}

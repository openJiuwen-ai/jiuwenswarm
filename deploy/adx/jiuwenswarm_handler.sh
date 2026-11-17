#!/usr/bin/env bash
set -euo >/dev/null 2>&1

SSH_OPTS="-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=10"

is_local_host() {
    local host="$1"
    if [ "${host}" = "127.0.0.1" ] || [ "${host}" = "localhost" ]; then
        return 0
    fi
    local local_ips
    local_ips=$(hostname -I 2>/dev/null || echo "")
    for ip in ${local_ips}; do
        if [ "${host}" = "${ip}" ]; then
            return 0
        fi
    done
    return 1
}

exec_on_host() {
    local host="$1"
    shift
    if is_local_host "${host}"; then
        bash -c "$*"
    else
        ssh ${SSH_OPTS} root@${host} "$*"
    fi
}

copy_to_host() {
    local host="$1"
    local src="$2"
    local dst="$3"
    if is_local_host "${host}"; then
        local src_real
        src_real=$(realpath "${src}" 2>/dev/null || echo "${src}")
        local dst_real
        if [[ "${dst}" == */ ]]; then
            dst_real=$(realpath "${dst}" 2>/dev/null || echo "${dst}")
            dst_real="${dst_real}/$(basename "${src}")"
        else
            dst_real=$(realpath "${dst}" 2>/dev/null || echo "${dst}")
        fi
        if [ "${src_real}" = "${dst_real}" ]; then
            return 0
        fi
        cp -r "${src}" "${dst}"
    else
        scp ${SSH_OPTS} -r "${src}" "root@${host}:${dst}"
    fi
}

# ===== 目标主机端口监听检查（gateway / web 等平级组件共用） =====
# 判断指定 TCP 端口是否处于 LISTEN。用于服务健康检查，
# 防止仅看 systemctl is-active 会在"服务刚 active 即崩溃"时误报成功的缺陷。
port_is_listening() {
    local host="$1"
    local port="$2"
    exec_on_host "${host}" "if command -v ss >/dev/null 2>&1; then ss -ltn 2>/dev/null | grep -qE ':${port}[[:space:]]'; else timeout 3 bash -c 'exec 3<>/dev/tcp/${host}/${port}' 2>/dev/null; fi" 2>/dev/null
}

# 发行包名：0.2.8 起为 workswarm，旧包/日构建为 jiuwenswarm。
# 输出目标机上已安装的发行包名；未安装时输出空串（返回 0，避免调用方在 set -e 下中断）。
jiuwenswarm_dist_name() {
    local host="$1"
    local python_version="${DEPLOY_VARS["PYTHON_VERSION"]}"
    local name=""
    if exec_on_host "${host}" "python${python_version} -m pip show workswarm >/dev/null 2>&1"; then
        name="workswarm"
    elif exec_on_host "${host}" "python${python_version} -m pip show jiuwenswarm >/dev/null 2>&1"; then
        name="jiuwenswarm"
    fi
    printf '%s' "${name}"
}

jiuwenswarm_check_ssh() {
    local host="$1"
    if is_local_host "${host}"; then
        return 0
    fi
    if ssh ${SSH_OPTS} root@${host} "echo ok" >/dev/null 2>&1; then
        return 0
    else
        return 1
    fi
}

jiuwenswarm_install() {
    local host="$1"
    local python_version="${DEPLOY_VARS["PYTHON_VERSION"]}"

    info "Checking jiuwenswarm on ${host}..."
    local dist_name check_result=""
    dist_name=$(jiuwenswarm_dist_name "${host}")
    if [ -n "${dist_name}" ]; then
        check_result=$(exec_on_host "${host}" "python${python_version} -m pip show ${dist_name} 2>/dev/null | grep -i '^Version:' | awk '{print \$2}'" | tr -d '\r') || true
    fi

    if [ -n "${check_result}" ]; then
        success "jiuwenswarm already installed on ${host}: ${check_result} (${dist_name})"
        return 0
    fi

    local package_url="${DEPLOY_VARS["JIUWENSWARM_PACKAGE_URL"]}"
    if [ -z "${package_url}" ]; then
        error "jiuwenswarm is not installed on ${host} and JIUWENSWARM_PACKAGE_URL is not set. Please set JIUWENSWARM_PACKAGE_URL in .env.custom to provide the install package URL."
    fi

    info "Installing jiuwenswarm from ${package_url} on ${host}..."
    if exec_on_host "${host}" "python${python_version} -m pip install ${package_url} --quiet"; then
        success "jiuwenswarm installed on ${host}"
    else
        error "Failed to install jiuwenswarm on ${host}"
    fi
}



deploy_jiuwenswarm() {
    local hosts_str="${DEPLOY_VARS["CLUSTER_HOSTS"]}"
    local master_host

    IFS=',' read -ra JIUWENSWARM_HOST_LIST <<< "${hosts_str}"
    master_host="${JIUWENSWARM_HOST_LIST[0]}"

    info "Deploying jiuwenswarm"
    info "Master host: ${master_host}"
    info "Total hosts: ${#JIUWENSWARM_HOST_LIST[@]}"
    info "ADX must already be deployed on all hosts"

    info "Checking connectivity to all hosts..."
    for host in "${JIUWENSWARM_HOST_LIST[@]}"; do
        if is_local_host "${host}"; then
            success "${host} is local host, skip SSH check"
        elif jiuwenswarm_check_ssh "${host}"; then
            success "SSH to ${host} OK"
        else
            error "SSH to ${host} failed! Please configure SSH key authentication first."
        fi
    done

    for host in "${JIUWENSWARM_HOST_LIST[@]}"; do
        jiuwenswarm_install "${host}"
    done

    success "jiuwenswarm deployment completed!"
    echo ""
    echo "=========================================="
    success "Deployment Summary"
    echo "=========================================="
    echo "  Master: ${master_host}"
    echo ""
    echo "  Next step: deploy gateway"
    echo "    ./$(basename "$0") up gateway --hosts ${hosts_str}"
    echo "=========================================="
}

uninstall_jiuwenswarm() {
    local hosts_str="${DEPLOY_VARS["CLUSTER_HOSTS"]:-}"

    if [ -z "${hosts_str}" ]; then
        hosts_str=$(get_local_ip)
        DEPLOY_VARS["CLUSTER_HOSTS"]="${hosts_str}"
        warning "CLUSTER_HOSTS not set, using local IP: ${hosts_str}"
    fi

    IFS=',' read -ra JIUWENSWARM_HOST_LIST <<< "${hosts_str}"
    local master_host="${JIUWENSWARM_HOST_LIST[0]}"

    # down stops services; package removal belongs to AgentOS uninstall.
    # pip 包卸载由 agentos uninstall 流程（module.sh 的 jiuwenswarm_uninstall 钩子）负责。
    echo ""
    echo "=========================================="
    success "jiuwenswarm down completed!"
    echo "=========================================="
    echo "  Master: ${master_host}"
    echo "=========================================="
}

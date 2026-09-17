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
    local python_version="${DEPLOY_VARS["YR_PYTHON_VERSION"]}"

    info "Checking jiuwenswarm on ${host}..."
    local check_result
    check_result=$(exec_on_host "${host}" "python${python_version} -m pip show jiuwenswarm 2>/dev/null | grep -i '^Version:' | awk '{print \$2}'" | tr -d '\r') || true

    if [ -n "${check_result}" ]; then
        success "jiuwenswarm already installed on ${host}: ${check_result}"
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

jiuwenswarm_infer_func_code_dir() {
    local host="$1"
    local python_version="${DEPLOY_VARS["YR_PYTHON_VERSION"]}"

    local jiuwenswarm_location
    jiuwenswarm_location=$(exec_on_host "${host}" "python${python_version} -m pip show jiuwenswarm 2>/dev/null | grep -i '^Location:' | awk '{print \$2}'" | tr -d '\r') || true

    if [ -z "${jiuwenswarm_location}" ]; then
        error "Failed to infer YR_FUNC_CODE_DIR: jiuwenswarm not found on ${host}. Please set YR_FUNC_CODE_DIR in .env.custom."
    fi

    local inferred_dir="${jiuwenswarm_location}/jiuwenswarm/extensions"

    if [ -n "${DEPLOY_VARS["YR_FUNC_CODE_DIR"]:-}" ]; then
        if [ "${DEPLOY_VARS["YR_FUNC_CODE_DIR"]}" != "${inferred_dir}" ]; then
            warning "YR_FUNC_CODE_DIR on ${host} (${inferred_dir}) differs from configured value (${DEPLOY_VARS["YR_FUNC_CODE_DIR"]})"
        else
            success "YR_FUNC_CODE_DIR verified on ${host}: ${inferred_dir}"
        fi
    else
        DEPLOY_VARS["YR_FUNC_CODE_DIR"]="${inferred_dir}"
        info "YR_FUNC_CODE_DIR inferred from jiuwenswarm install on ${host}: ${DEPLOY_VARS["YR_FUNC_CODE_DIR"]}"
    fi
}

jiuwenswarm_ensure_func_code() {
    local host="$1"
    local func_dir="${DEPLOY_VARS["YR_FUNC_CODE_DIR"]}"
    local func_file="${func_dir}/clawee.py"

    info "Checking function code on ${host}:${func_dir}..."

    if exec_on_host "${host}" "test -f '${func_file}'" 2>/dev/null; then
        success "Function code already exists on ${host}: ${func_file}, skip sync"
        return 0
    fi

    info "Function code missing on ${host}, syncing from local..."
    exec_on_host "${host}" "mkdir -p ${func_dir}"
    if copy_to_host "${host}" "${REG_FUNC_FILE}" "${func_dir}/"; then
        success "Function code synced to ${host}"
    else
        error "Failed to sync function code to ${host}"
    fi
}

deploy_jiuwenswarm() {
    local hosts_str="${DEPLOY_VARS["CLUSTER_HOSTS"]}"
    local master_host

    IFS=',' read -ra JIUWENSWARM_HOST_LIST <<< "${hosts_str}"
    master_host="${JIUWENSWARM_HOST_LIST[0]}"

    info "Deploying jiuwenswarm"
    info "Master host (yr master): ${master_host}"
    info "Total hosts: ${#JIUWENSWARM_HOST_LIST[@]}"
    info "Assuming yuanrong is already deployed on all hosts"

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
        jiuwenswarm_infer_func_code_dir "${host}"
        jiuwenswarm_ensure_func_code "${host}"
    done

    success "jiuwenswarm deployment completed!"
    echo ""
    echo "=========================================="
    success "Deployment Summary"
    echo "=========================================="
    echo "  YR Master: ${master_host}"
    echo "  Func Code Dir: ${DEPLOY_VARS["YR_FUNC_CODE_DIR"]}"
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

    # 注意: down 仅停止服务，不注销 function（注册已不再需要）。
    # pip 包卸载由 agentos uninstall 流程（module.sh 的 jiuwenswarm_uninstall 钩子）负责。
    echo ""
    echo "=========================================="
    success "jiuwenswarm down completed!"
    echo "=========================================="
    echo "  YR Master: ${master_host}"
    echo "=========================================="
}

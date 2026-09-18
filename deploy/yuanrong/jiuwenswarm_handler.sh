#!/usr/bin/env bash
set -euo >/dev/null 2>&1

# _get_local_ips / get_local_ip 已在 check_handler.sh 中统一定义（先 source），
# 此处不再重复，避免静默覆盖导致的认知陷阱。

# 所有操作均在本机执行，host 参数保留仅为兼容旧调用签名，实际不使用
exec_on_host() {
    local host="$1"
    shift
    bash -c "$*"
}

# 所有操作均在本机执行，host 参数保留仅为兼容旧调用签名，实际不使用
copy_to_host() {
    local host="$1"
    local src="$2"
    local dst="$3"
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
}

# ===== 目标主机端口监听检查（gateway / web 等平级组件共用） =====
# 判断指定 TCP 端口是否处于 LISTEN。用于服务健康检查，
# 防止仅看 systemctl is-active 会在"服务刚 active 即崩溃"时误报成功的缺陷。
port_is_listening() {
    local host="$1"
    local port="$2"
    exec_on_host "${host}" "if command -v ss >/dev/null 2>&1; then ss -ltn 2>/dev/null | grep -qE ':${port}[[:space:]]'; else timeout 3 bash -c 'exec 3<>/dev/tcp/${host}/${port}' 2>/dev/null; fi" 2>/dev/null
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
    local hosts_str="${BIND_IP:-$(_yr_consistent_local_ip)}"
    local master_host="${hosts_str}"

    info "Deploying jiuwenswarm"
    info "Master host (yr master): ${master_host}"
    info "Assuming yuanrong is already deployed on this host"

    jiuwenswarm_install "${master_host}"
    jiuwenswarm_infer_func_code_dir "${master_host}"
    jiuwenswarm_ensure_func_code "${master_host}"

    success "jiuwenswarm deployment completed!"
    echo ""
    echo "=========================================="
    success "Deployment Summary"
    echo "=========================================="
    echo "  YR Master: ${master_host}"
    echo "  Func Code Dir: ${DEPLOY_VARS["YR_FUNC_CODE_DIR"]}"
    echo ""
    echo "  Next step: deploy gateway"
    echo "    ./$(basename "$0") up gateway --ip ${hosts_str}"
    echo "=========================================="
}

uninstall_jiuwenswarm() {
    local hosts_str="${BIND_IP:-$(_yr_consistent_local_ip)}"
    local master_host="${hosts_str}"

    # 注意: down 仅停止服务，不注销 function（注册已不再需要）。
    # pip 包卸载由 agentos uninstall 流程（module.sh 的 jiuwenswarm_uninstall 钩子）负责。
    echo ""
    echo "=========================================="
    success "jiuwenswarm down completed!"
    echo "=========================================="
    echo "  YR Master: ${master_host}"
    echo "=========================================="
}

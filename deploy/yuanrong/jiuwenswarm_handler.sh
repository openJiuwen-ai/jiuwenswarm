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

jiuwenswarm_detect_session_dir() {
    local master_host="$1"

    DEPLOY_VARS["YR_SESSION_DIR"]=$(exec_on_host "${master_host}" "readlink /tmp/yr_sessions/latest 2>/dev/null || echo ''" | tr -d '\r')

    if [ -z "${DEPLOY_VARS["YR_SESSION_DIR"]}" ]; then
        error "Could not detect yuanrong session dir on ${master_host}. Please ensure yuanrong is deployed and started (run yuanrong_deploy.sh up first)."
    fi

    info "Detected yuanrong session dir on ${master_host}: ${DEPLOY_VARS["YR_SESSION_DIR"]}"
}

jiuwenswarm_get_meta_port() {
    local master_host="$1"
    local session_dir="${DEPLOY_VARS["YR_SESSION_DIR"]}"

    if [ -n "${session_dir}" ]; then
        META_PORT=$(exec_on_host "${master_host}" "cat ${session_dir}/metaservice_config.json 2>/dev/null" | jq -r '.server.port // empty' 2>/dev/null | tr -d '\r')
    fi

    if [ -z "${META_PORT}" ]; then
        META_PORT="31182"
        warning "Could not detect meta_service port, using default ${META_PORT}"
    fi

    info "META_PORT: ${META_PORT}"
}

jiuwenswarm_get_frontend_port() {
    local master_host="$1"
    local session_dir="${DEPLOY_VARS["YR_SESSION_DIR"]}"

    if [ -n "${session_dir}" ]; then
        DEPLOY_VARS["FRONTEND_PORT"]=$(exec_on_host "${master_host}" "cat ${session_dir}/frontend_init_args.json 2>/dev/null" | jq -r '.http.serverListenPort // empty' 2>/dev/null | tr -d '\r')
    fi

    if [ -z "${DEPLOY_VARS["FRONTEND_PORT"]:-}" ]; then
        DEPLOY_VARS["FRONTEND_PORT"]="8888"
        warning "Could not detect frontend port, using default ${DEPLOY_VARS["FRONTEND_PORT"]}"
    fi

    info "FRONTEND_PORT: ${DEPLOY_VARS["FRONTEND_PORT"]}"
}

jiuwenswarm_register_func() {
    local master_host="$1"

    jiuwenswarm_get_meta_port "${master_host}"
    jiuwenswarm_get_frontend_port "${master_host}"

    DEPLOY_VARS["MASTER_NODE_IP"]="${master_host}"

    info "Registering jiuwenswarm function on process-mode openyuanrong..."
    render_config_template "${CLAW_META_PROCESS_TEMPLATE_FILE}" "${CLAW_META_PROCESS_FILE}" "DEPLOY_VARS"

    info "curl -X POST -H \"Content-Type: application/json\" -H \"x-storage-type: local\" http://${master_host}:${META_PORT}/serverless/v1/functions -d @${CLAW_META_PROCESS_FILE}"
    local res
    res=$(curl -s -X POST -H "Content-Type: application/json" -H "x-storage-type: local" "http://${master_host}:${META_PORT}/serverless/v1/functions" -d @"${CLAW_META_PROCESS_FILE}")
    info "Function registration result: ${res}"

    local code
    code=$(echo "${res}" | jq -r '.code' 2>/dev/null || echo "-1")
    if [[ "${code}" != "0" ]]; then
        error "Failed to register serverless function"
    fi

    DEPLOY_VARS["FUNCTION_ID"]=$(echo "${res}" | jq -r '.function.id')
    success "Serverless function registered successfully! function_id: ${DEPLOY_VARS["FUNCTION_ID"]}"
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

    info "Registering function on yr master node (${master_host})..."
    jiuwenswarm_detect_session_dir "${master_host}"
    jiuwenswarm_register_func "${master_host}"

    success "jiuwenswarm deployment completed!"
    echo ""
    echo "=========================================="
    success "Deployment Summary"
    echo "=========================================="
    echo "  YR Master: ${master_host}"
    echo "  Function ID: ${DEPLOY_VARS["FUNCTION_ID"]}"
    echo "  Meta Port: ${META_PORT}"
    echo "  Frontend Port: ${DEPLOY_VARS["FRONTEND_PORT"]}"
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

    local func_svc_name="${DEPLOY_VARS["FUNC_SVC_NAME"]}"
    if [ -z "${func_svc_name}" ]; then
        error "FUNC_SVC_NAME is not set. Please set it in .env.custom."
    fi

    info "Unregistering jiuwenswarm function on yr master (${master_host})..."

    # 复用 up 流程的检测逻辑，获取 session dir 和 meta port
    jiuwenswarm_detect_session_dir "${master_host}"
    jiuwenswarm_get_meta_port "${master_host}"

    local delete_url="http://${master_host}:${META_PORT}/serverless/v1/functions/${func_svc_name}"
    info "curl -H \"Content-type: application/json\" -X DELETE ${delete_url}"

    local res
    res=$(curl -s -H "Content-type: application/json" -X DELETE "${delete_url}" || true)
    info "Function unregistration result: ${res}"

    # DELETE 成功返回 {}（无 code 字段）或 code=0；其他视为失败
    local code
    code=$(echo "${res}" | jq -r '.code // "none"' 2>/dev/null || echo "error")
    if [[ "${code}" == "0" || "${code}" == "none" ]]; then
        success "Serverless function unregistered successfully: ${func_svc_name}"
    else
        warning "Failed to unregister serverless function (may not exist): ${res}"
    fi

    echo ""
    echo "=========================================="
    success "jiuwenswarm uninstall completed!"
    echo "=========================================="
    echo "  YR Master: ${master_host}"
    echo "  Function: ${func_svc_name}"
    echo "  Meta Port: ${META_PORT}"
    echo "=========================================="
}

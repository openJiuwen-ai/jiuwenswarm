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

gateway_resolve_host() {
    local master_host="${DEPLOY_VARS["MASTER_NODE_IP"]:-}"
    if [ -z "${master_host}" ]; then
        if [ -n "${DEPLOY_VARS["CLUSTER_HOSTS"]:-}" ]; then
            IFS=',' read -ra _gw_host_list <<< "${DEPLOY_VARS["CLUSTER_HOSTS"]}"
            master_host="${_gw_host_list[0]}"
        else
            master_host=$(get_local_ip)
            info "MASTER_NODE_IP not set, defaulting to local: ${master_host}" >&2
        fi
    fi
    echo "${master_host}"
}

gateway_compute_extension_dirs() {
    if [ -n "${DEPLOY_VARS["EXTENSION_DIRS"]:-}" ]; then
        info "EXTENSION_DIRS already set: ${DEPLOY_VARS["EXTENSION_DIRS"]}"
        return 0
    fi

    local master_host
    master_host=$(gateway_resolve_host)
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

gateway_deploy_process() {
    local master_host
    master_host=$(gateway_resolve_host)
    local instance_name="${DEPLOY_VARS["JIUWENSWARM_INSTANCE_NAME"]}"

    info "Deploying gateway in process mode on ${master_host}..."

    gateway_gen_config

    local config_dir
    config_dir=$(gateway_get_config_dir)

    local home_prefix=""
    if ! gateway_check_jiuwenswarm_home "${master_host}"; then
        home_prefix="JIUWENSWARM_HOME=/root "
    fi

    local init_cmd="${home_prefix}jiuwenswarm-init -f </dev/null"
    local start_cmd="${home_prefix}nohup jiuwenswarm-gateway </dev/null > /tmp/jiuwenswarm-gateway.log 2>&1 &"

    if [ -n "${instance_name}" ]; then
        init_cmd="${home_prefix}JIUWENSWARM_DATA_DIR=/root/.jiuwenswarm-instances/${instance_name} jiuwenswarm-init -f </dev/null"
        start_cmd="${home_prefix}JIUWENSWARM_DATA_DIR=/root/.jiuwenswarm-instances/${instance_name} nohup jiuwenswarm-gateway </dev/null > /tmp/jiuwenswarm-gateway.log 2>&1 &"
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

    info "Starting jiuwenswarm-gateway on ${master_host}..."
    exec_on_host "${master_host}" "bash -c '${start_cmd}'"

    local retry=0
    local max_retry=10
    while [ ${retry} -lt ${max_retry} ]; do
        sleep 2
        if exec_on_host "${master_host}" "pgrep -f 'jiuwenswarm-gateway' >/dev/null 2>&1"; then
            success "Gateway process is running on ${master_host}"
            return 0
        fi
        retry=$((retry + 1))
        info "Waiting for gateway to start... (${retry}/${max_retry})"
    done

    error "Gateway process failed to start on ${master_host}, check /tmp/jiuwenswarm-gateway.log"
}

gateway_undeploy_process() {
    local master_host
    master_host=$(gateway_resolve_host)
    local instance_name="${DEPLOY_VARS["JIUWENSWARM_INSTANCE_NAME"]}"

    info "Stopping gateway on ${master_host}..."

    if [ -n "${instance_name}" ]; then
        exec_on_host "${master_host}" "pkill -f 'JIUWENSWARM_DATA_DIR=/root/.jiuwenswarm-instances/${instance_name}.*[j]iuwenswarm-gateway' || true"
    else
        exec_on_host "${master_host}" "pkill -f '[j]iuwenswarm-gateway' || true"
    fi

    success "Gateway stopped on ${master_host}"
}

deploy_gateway() {
    gateway_deploy_process
}

uninstall_gateway() {
    gateway_undeploy_process
}

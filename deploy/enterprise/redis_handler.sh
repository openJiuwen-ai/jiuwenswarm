#!/usr/bin/env bash
set -euo >/dev/null 2>&1

render_redis_files() {
    local template_file="${CONFIG["REDIS_TEMPLATE_FILE"]}"
    local file="${CONFIG["REDIS_FILE"]}"

    ensure_available_port "REDIS_NODE_PORT"
    render_config_template "${template_file}" "${file}" "DEPLOY_VARS"
}

deploy_redis() {
    local namespace="${DEPLOY_VARS["NAMESPACE"]}"
    local redis_name="${DEPLOY_VARS["REDIS_NAME"]}"
    local file="${CONFIG["REDIS_FILE"]}"

    exec_cmd kubectl apply -f "${file}"
    wait_k8s_resource_ready "deployment" "${redis_name}" "${namespace}"
}

uninstall_redis() {
    local namespace="${DEPLOY_VARS["NAMESPACE"]}"
    local redis_name="${DEPLOY_VARS["REDIS_NAME"]}"
    local file="${CONFIG["REDIS_FILE"]}"

    exec_cmd kubectl delete -f "${file}" --ignore-not-found=true
    wait_pod_terminated "${redis_name}" "${namespace}"
}


# gateway / runtime 两个模块共用同一份内置 Redis，不能贸然关停。
# 只有两个模块 都关停之后，才能关停
ensure_redis_down() {
    local namespace="${DEPLOY_VARS["NAMESPACE"]}"
    local name="${DEPLOY_VARS["REDIS_NAME"]}"

    # 外挂 Redis 由用户自行管理，本工具不负责卸载
    if [ "${DEPLOY_VARS["ENABLE_EXTERNAL_REDIS"]}" == "true" ]; then
        info "External Redis in use, skip shutting down built-in Redis."
        return
    fi

    # 当前命名空间内已无内置 Redis Deployment，说明已卸载或从未部署
    if ! check_k8s_resource_exists "deployment" "${name}" "${namespace}"; then
        info "Built-in Redis '${name}' not found in namespace '${namespace}', nothing to do."
        return
    fi

    for dname in ${DEPLOY_VARS["GATEWAY_NAME"]} ${DEPLOY_VARS["AGENT_RUNTIME_NAME"]}
    do
        if check_k8s_resource_exists "deployment" "${dname}" "${namespace}"; then
            info " ${dname} still running in namespace '${namespace}', keep Redis alive."
            return
        fi
    done
    uninstall_redis
    success "Built-in Redis '${name}' has been shut down."
}

#!/usr/bin/env bash
set -euo >/dev/null 2>&1


render_secret_configmap() {
    local namespace="${DEPLOY_VARS["NAMESPACE"]}"
    local name="${DEPLOY_VARS["SECRET_CM_NAME"]}"
    if [[ "${DEPLOY_VARS["RENDER_ONLY"]}" == "false" ]] && check_k8s_resource_exists "secret" "${name}" "${namespace}"; then
        warning "Secret ${namespace}/${name} exists, skip rendering."
        return
    fi

    local template_file="${CONFIG["SECRET_CM_TEMPLATE_FILE"]}"
    local file="${CONFIG["SECRET_CM_FILE"]}"
    local secret_keys=(
        "GATEWAY_DB_PASSWORD"
        "MANAGER_DB_PASSWORD"
        "IDENTITY_DB_PASSWORD"
        "WEB_DB_PASSWORD"
        "RUNTIME_DB_PASSWORD"
        "REDIS_PASSWORD"
        "OBS_SECRET_KEY"
    )
    for key in "${secret_keys[@]}"; do
        # 空值直接跳过，无需编码
        if [ -z "${DEPLOY_VARS[$key]:-}" ]; then
            continue
        fi

        local ekey="${key}_ENCODED"
        DEPLOY_VARS["$ekey"]=$(echo -n "${DEPLOY_VARS[$key]}" | base64 -w 0)
    done
    render_config_template "${template_file}" "${file}" "DEPLOY_VARS"
}

ensure_secret_configmap() {
    local namespace="${DEPLOY_VARS["NAMESPACE"]}"
    local name="${DEPLOY_VARS["SECRET_CM_NAME"]}"
    local file="${CONFIG["SECRET_CM_FILE"]}"

    if check_k8s_resource_exists "secret" "${name}" "${namespace}"; then
        warning "Secret ${namespace}/${name} exists, skip creating."
        return
    fi

    exec_cmd kubectl apply -f ${file}
}

uninstall_secret_configmap() {
    local namespace="${DEPLOY_VARS["NAMESPACE"]}"
    local name="${DEPLOY_VARS["SECRET_CM_NAME"]}"
    local component_names=(
        "${DEPLOY_VARS["GATEWAY_NAME"]}"
        "${DEPLOY_VARS["MANAGER_SERVER_NAME"]}"
        "${DEPLOY_VARS["WEB_NAME"]}"
        "${DEPLOY_VARS["AGENT_RUNTIME_NAME"]}"
    )
    local file="${CONFIG["SECRET_CM_FILE"]}"

    # Gateway、Web、Manager、AgentRuntime这些组件都依赖于本资源，检查它们是否存在，若存在不能删除本资源
    for cname in "${component_names[@]}"; do
        if check_k8s_resource_exists "deployment" "${cname}" "${namespace}"; then
            warning "Deployment ${namespace}/${cname} exists, skip deleting ${namespace}/${name}."
            return
        fi
    done

    exec_cmd kubectl delete -f ${file} --ignore-not-found=true
}
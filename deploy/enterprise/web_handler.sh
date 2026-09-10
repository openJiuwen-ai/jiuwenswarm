#!/usr/bin/env bash
set -euo >/dev/null 2>&1

gen_web_file() {
    local template_file="${CONFIG["WEB_TEMPLATE_FILE"]}"
    local file="${CONFIG["WEB_FILE"]}"

    render_config_template "${template_file}" "${file}" "DEPLOY_VARS"

    if [ "${DEPLOY_VARS["DB_TYPE"]}" == "postgresql" ]; then
        yq eval '
        select(.kind == "Deployment").spec.template.spec.containers[0].env += [
            {
                "name": "WEB_PG_SCHEMA",
                "value": "'"${DEPLOY_VARS["WEB_PG_SCHEMA"]}"'"
            }
        ]' -i "${file}"
    fi

    if [[ "${DEPLOY_VARS["APPLY_PATCH"]}" == "false" ]]; then
        yq eval '
        select(.kind == "Deployment").spec.template.spec.containers[0].env += [
            {
                "name": "USER_WEB_IDP_TARGET",
                "value": "'"${DEPLOY_VARS["USER_WEB_IDP_TARGET"]}"'"
            },
            {
                "name": "USER_WEB_MANAGER_TARGET",
                "value": "'"${DEPLOY_VARS["USER_WEB_MANAGER_TARGET"]}"'"
            }
        ]' -i "${file}"
    fi

    add_resource_if_set "WEB" "${file}"

    # yq 追加资源配置时可能重复 env；Deployment strategic merge patch 不接受重复键，
    # 这里按名称去重，保留最后一次生成的值。
    yq eval 'select(.kind == "Deployment").spec.template.spec.containers[0].env |= unique_by(.name)' -i "${file}"

    enable_dev_mode_if_needed ${file} web
}

render_web_files() {
    render_secret_configmap
    ensure_available_port "WEB_NODE_PORT"
    gen_web_file
}

deploy_web() {
    local namespace="${DEPLOY_VARS["NAMESPACE"]}"
    local name="${DEPLOY_VARS["WEB_NAME"]}"
    local file="${CONFIG["WEB_FILE"]}"

    ensure_secret_configmap
    exec_cmd kubectl apply -f ${file}
    wait_k8s_resource_ready "deployment" "${name}" "${namespace}"
    success "WEB_NODE_PORT: ${DEPLOY_VARS["WEB_NODE_PORT"]}"
}

uninstall_web() {
    local namespace="${DEPLOY_VARS["NAMESPACE"]}"
    local name="${DEPLOY_VARS["WEB_NAME"]}"
    local file="${CONFIG["WEB_FILE"]}"

    exec_cmd kubectl delete -f ${file} --ignore-not-found=true
    delete_k8s_resource "service" "${name}-nodeport" "${namespace}"
    wait_pod_terminated "${name}" "${namespace}"
    uninstall_secret_configmap
}

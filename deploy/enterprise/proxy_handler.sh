#!/usr/bin/env bash
set -euo >/dev/null 2>&1

render_proxy_files() {
    local template_file="${CONFIG["PROXY_TEMPLATE_FILE"]}"
    local file="${CONFIG["PROXY_FILE"]}"

    render_config_template "${template_file}" "${file}" "DEPLOY_VARS"
}

deploy_proxy() {
    local name="${DEPLOY_VARS["PROXY_NAME"]}"
    local file="${CONFIG["PROXY_FILE"]}"
    local ip="${DEPLOY_VARS["CURRENT_NODE_IP"]}"
    local port="${DEPLOY_VARS["PROXY_PORT"]}"
    local upstream="${DEPLOY_VARS["PROXY_UPSTREAM"]}"

    exec_cmd kubectl apply -f ${file}
    wait_k8s_resource_ready "deployment" "${name}"
    success "PROXY ready: http://${ip}:${port} -> ${upstream}"
    
}

uninstall_proxy() {
    local name="${DEPLOY_VARS["PROXY_NAME"]}"
    local file="${CONFIG["PROXY_FILE"]}"

    exec_cmd kubectl delete -f ${file} --ignore-not-found=true
    wait_pod_terminated "${name}"
}

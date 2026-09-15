#!/usr/bin/env bash
set -euo >/dev/null 2>&1

gen_manager_server_file() {
    local template_file="${CONFIG["MANAGER_SERVER_TEMPLATE_FILE"]}"
    local file="${CONFIG["MANAGER_SERVER_FILE"]}"

    render_config_template "${template_file}" "${file}" "DEPLOY_VARS"
    enable_dev_mode_if_needed ${file} manager-server

    if [ "${DEPLOY_VARS["DB_TYPE"]}" == "postgresql" ]; then
        yq eval '
        select(.kind == "Deployment").spec.template.spec.containers[0].env += [
            {
                "name": "MANAGER_PG_SCHEMA",
                "value": "'"${DEPLOY_VARS["MANAGER_PG_SCHEMA"]}"'"
            }
        ]' -i "${file}"
    fi

    add_resource_if_set "MANAGER_SERVER" "${file}"
}

gen_identity_file() {
    local template_file="${CONFIG["IDENTITY_TEMPLATE_FILE"]}"
    local file="${CONFIG["IDENTITY_FILE"]}"

    render_config_template "${template_file}" "${file}" "DEPLOY_VARS"
    enable_dev_mode_if_needed ${file} identity

    # yq quotes arbitrary password characters correctly in the generated YAML.
    IDENTITY_ADMIN_PASSWORD="${DEPLOY_VARS[IDENTITY_ADMIN_PASSWORD]}" \
    IDENTITY_USER1_PASSWORD="${DEPLOY_VARS[IDENTITY_USER1_PASSWORD]}" \
        yq eval '(. | select(.kind == "Deployment") | .spec.template.spec.containers[0].env) += [
            {"name": "IDENTITY_ADMIN_PASSWORD", "value": strenv(IDENTITY_ADMIN_PASSWORD)},
            {"name": "IDENTITY_USER1_PASSWORD", "value": strenv(IDENTITY_USER1_PASSWORD)}
        ]' -i "${file}"

    if [ "${DEPLOY_VARS["DB_TYPE"]}" == "postgresql" ]; then
        yq eval '
        select(.kind == "Deployment").spec.template.spec.containers[0].env += [
            {
                "name": "IDENTITY_PG_SCHEMA",
                "value": "'"${DEPLOY_VARS["IDENTITY_PG_SCHEMA"]}"'"
            }
        ]' -i "${file}"
    fi

    add_resource_if_set "IDENTITY" "${file}"
}

# MANAGER_WEB_RESOLVER=auto → 解析为具体 DNS。
# 优先级：kube-dns/coredns ClusterIP → 本机私网 nameserver → kube-dns 服务名兜底。
resolve_manager_web_resolver() {
    local current="${DEPLOY_VARS["MANAGER_WEB_RESOLVER"]:-auto}"
    [[ "${current}" == "auto" || -z "${current}" ]] || return 0

    local dns_ip=""
    if command -v kubectl >/dev/null 2>&1; then
        dns_ip=$(kubectl get svc -n kube-system -l k8s-app=kube-dns \
            -o jsonpath='{.items[0].spec.clusterIP}' 2>/dev/null || true)
        if [[ -z "${dns_ip}" ]]; then
            dns_ip=$(kubectl get svc -n kube-system kube-dns \
                -o jsonpath='{.spec.clusterIP}' 2>/dev/null || true)
        fi
        if [[ -z "${dns_ip}" ]]; then
            dns_ip=$(kubectl get svc -n kube-system coredns \
                -o jsonpath='{.spec.clusterIP}' 2>/dev/null || true)
        fi
    fi

    # 仅接受 RFC1918，避免 --render-only 在笔记本上把公网 DNS 写进集群 nginx
    if [[ -z "${dns_ip}" && -r /etc/resolv.conf ]]; then
        local ns
        ns=$(awk '/^nameserver[[:space:]]+/ { print $2; exit }' /etc/resolv.conf 2>/dev/null || true)
        if [[ "${ns}" =~ ^10\.([0-9]{1,3}\.){2}[0-9]{1,3}$ ]] \
            || [[ "${ns}" =~ ^192\.168\.([0-9]{1,3}\.)[0-9]{1,3}$ ]] \
            || [[ "${ns}" =~ ^172\.(1[6-9]|2[0-9]|3[0-1])\.([0-9]{1,3}\.)[0-9]{1,3}$ ]]; then
            dns_ip="${ns}"
        fi
    fi

    if [[ -n "${dns_ip}" ]]; then
        DEPLOY_VARS["MANAGER_WEB_RESOLVER"]="${dns_ip}"
        info "MANAGER_WEB_RESOLVER=auto → ${dns_ip}"
    else
        DEPLOY_VARS["MANAGER_WEB_RESOLVER"]="kube-dns.kube-system.svc.cluster.local"
        warning "MANAGER_WEB_RESOLVER=auto 未能解析到 DNS IP，回退为 kube-dns.kube-system.svc.cluster.local"
    fi
}

render_manager_files() {
    render_secret_configmap
    ensure_available_port "MANAGER_SERVER_NODE_PORT" "MANAGER_WEB_NODE_PORT"
    gen_manager_server_file
    if [[ "${DEPLOY_VARS[JIUWENSWARM_LINK_MTLS_MODE]:-off}" != off ]]; then
        link_mtls_render manager "${CONFIG[MANAGER_SERVER_FILE]}"
    fi
    gen_identity_file

    local manager_web_template_file="${CONFIG["MANAGER_WEB_TEMPLATE_FILE"]}"
    local manager_web_file="${CONFIG["MANAGER_WEB_FILE"]}"

    resolve_manager_web_resolver
    render_config_template "${manager_web_template_file}" "${manager_web_file}" "DEPLOY_VARS"
    enable_dev_mode_if_needed "${manager_web_file}" manager-web
    add_resource_if_set "MANAGER_WEB" "${manager_web_file}"
}

deploy_manager() {
    local namespace="${DEPLOY_VARS["NAMESPACE"]}"

    ensure_secret_configmap

    # manager-server
    local manager_server_name="${DEPLOY_VARS["MANAGER_SERVER_NAME"]}"
    local manager_server_file="${CONFIG["MANAGER_SERVER_FILE"]}"
    exec_cmd kubectl apply -f ${manager_server_file}
    wait_k8s_resource_ready "deployment" "${manager_server_name}" "${namespace}"
    success "MANAGER_SERVER_NODE_PORT: ${DEPLOY_VARS["MANAGER_SERVER_NODE_PORT"]}"

    # identity
    local identity_name="${DEPLOY_VARS["IDENTITY_NAME"]}"
    local identity_file="${CONFIG["IDENTITY_FILE"]}"
    exec_cmd kubectl apply -f ${identity_file}
    wait_k8s_resource_ready "deployment" "${identity_name}" "${namespace}"

    # manager-web
    local manager_web_name="${DEPLOY_VARS["MANAGER_WEB_NAME"]}"
    local manager_web_file="${CONFIG["MANAGER_WEB_FILE"]}"

    exec_cmd kubectl apply -f ${manager_web_file}
    wait_k8s_resource_ready "deployment" "${manager_web_name}" "${namespace}"
    success "MANAGER_WEB_NODE_PORT: ${DEPLOY_VARS["MANAGER_WEB_NODE_PORT"]}"
}

uninstall_manager() {
    local namespace="${DEPLOY_VARS["NAMESPACE"]}"

    # 反序：manager-web → identity → manager-server
    local manager_web_name="${DEPLOY_VARS["MANAGER_WEB_NAME"]}"
    local manager_web_file="${CONFIG["MANAGER_WEB_FILE"]}"
    exec_cmd kubectl delete -f ${manager_web_file} --ignore-not-found=true
    wait_pod_terminated "${manager_web_name}" "${namespace}"

    local identity_name="${DEPLOY_VARS["IDENTITY_NAME"]}"
    local identity_file="${CONFIG["IDENTITY_FILE"]}"
    exec_cmd kubectl delete -f ${identity_file} --ignore-not-found=true
    wait_pod_terminated "${identity_name}" "${namespace}"

    local manager_server_name="${DEPLOY_VARS["MANAGER_SERVER_NAME"]}"
    local manager_server_file="${CONFIG["MANAGER_SERVER_FILE"]}"
    exec_cmd kubectl delete -f ${manager_server_file} --ignore-not-found=true
    wait_pod_terminated "${manager_server_name}" "${namespace}"

    uninstall_secret_configmap
}

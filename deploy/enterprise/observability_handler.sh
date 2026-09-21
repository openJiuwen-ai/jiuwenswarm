#!/usr/bin/env bash
set -euo >/dev/null 2>&1

render_monitor_files() {
    if [ "${DEPLOY_VARS["OTEL_ENABLED"]}" == "false" ]; then
        return
    fi

    ensure_available_port "OBSERVABILITY_NODE_PORT"

    render_config_template "${CONFIG["OTEL_TEMPLATE_FILE"]}" "${CONFIG["OTEL_FILE"]}" "DEPLOY_VARS"
    render_config_template "${CONFIG["LOKI_TEMPLATE_FILE"]}" "${CONFIG["LOKI_FILE"]}" "DEPLOY_VARS"
    render_config_template "${CONFIG["OBSERVABILITY_TEMPLATE_FILE"]}" "${CONFIG["OBSERVABILITY_FILE"]}" "DEPLOY_VARS"
    enable_dev_mode_if_needed "${CONFIG["OBSERVABILITY_FILE"]}" observability
}

deploy_monitor() {
    if [ "${DEPLOY_VARS["OTEL_ENABLED"]}" == "false" ]; then
        return
    fi

    local namespace="${DEPLOY_VARS["NAMESPACE"]}"

    exec_cmd kubectl apply -f ${CONFIG["LOKI_FILE"]}
    wait_k8s_resource_ready "statefulset" "${DEPLOY_VARS["LOKI_NAME"]}" "${namespace}"

    exec_cmd kubectl apply -f ${CONFIG["OTEL_FILE"]}
    wait_k8s_resource_ready "daemonset" "${DEPLOY_VARS["OTEL_NAME"]}" "${namespace}"

    exec_cmd kubectl apply -f ${CONFIG["OBSERVABILITY_FILE"]}
    wait_k8s_resource_ready "deployment" "${DEPLOY_VARS["OBSERVABILITY_NAME"]}" "${namespace}"

    success "OBSERVABILITY_NODE_PORT: ${DEPLOY_VARS["OBSERVABILITY_NODE_PORT"]}"
}

uninstall_monitor() {
    if [ "${DEPLOY_VARS["OTEL_ENABLED"]}" == "false" ]; then
        return
    fi

    local namespace="${DEPLOY_VARS["NAMESPACE"]}"

    exec_cmd kubectl delete -f ${CONFIG["OBSERVABILITY_FILE"]} --ignore-not-found=true
    wait_pod_terminated "${DEPLOY_VARS["OBSERVABILITY_NAME"]}" "${namespace}"

    exec_cmd kubectl delete -f ${CONFIG["OTEL_FILE"]} --ignore-not-found=true
    wait_pod_terminated "${DEPLOY_VARS["OTEL_NAME"]}" "${namespace}"

    exec_cmd kubectl delete -f ${CONFIG["LOKI_FILE"]} --ignore-not-found=true
    wait_pod_terminated "${DEPLOY_VARS["LOKI_NAME"]}" "${namespace}"
}

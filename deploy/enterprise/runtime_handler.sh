#!/usr/bin/env bash
set -euo >/dev/null 2>&1

render_agentserver_env_configmap() {
    local namespace="${DEPLOY_VARS["NAMESPACE"]}"
    local env_template="${CONFIG["AS_ENV_TEMPLATE_FILE"]}"
    local env_file="${CONFIG["AS_ENV_FILE"]}"
    local envfile_name="${DEPLOY_VARS["AGENT_SERVER_ENV_CM_NAME"]}"
    local yaml_file="${CONFIG["AS_ENV_YAML_FILE"]}"

    render_config_template "${env_template}" "${env_file}" "DEPLOY_VARS"

    # 移除所有注释行、过滤空值行 KEY=、按变量名排序
    # 注意：不能 sort > 同一个文件，shell 会在管道启动前就截断输出文件，
    # 导致左侧 grep 读到空。先写临时文件再 mv 覆盖。
    grep -v '^[[:space:]]*#' "${env_file}" \
        | grep '=' \
        | awk -F'=' '$2 != ""' \
        | sort > "${env_file}.tmp" && mv -f "${env_file}.tmp" "${env_file}"

    kubectl create configmap -n "${namespace}" "${envfile_name}" \
        --from-env-file="${env_file}" \
        --dry-run=client -o yaml \
        | yq eval 'del(.metadata.creationTimestamp)' > "${yaml_file}"
    success "AgentServer env ConfigMap rendered: ${yaml_file}"
}

create_agentserver_env_configmap() {
    local yaml_file="${CONFIG["AS_ENV_YAML_FILE"]}"
    exec_cmd kubectl apply -f "${yaml_file}"
}

delete_agentserver_env_configmap() {
    local yaml_file="${CONFIG["AS_ENV_YAML_FILE"]}"
    exec_cmd kubectl delete -f "${yaml_file}" --ignore-not-found=true
}

gen_runtime_file() {
    local template_file="${CONFIG["RUNTIME_TEMPLATE_FILE"]}"
    local file="${CONFIG["RUNTIME_FILE"]}"

    local redis_mode="${DEPLOY_VARS["REDIS_MODE"]}"
    local redis_host="${DEPLOY_VARS["REDIS_HOST"]}"
    local redis_port="${DEPLOY_VARS["REDIS_PORT"]}"
    local redis_db="${DEPLOY_VARS["AGENT_RUNTIME_REDIS_DB"]}"

    if [[ "${redis_mode}" == "cluster" ]]; then
        DEPLOY_VARS["OPENJIUWEN_SERVICE_REDIS_URL"]="redis+cluster://${redis_host}:${redis_port}"
    else
        DEPLOY_VARS["OPENJIUWEN_SERVICE_REDIS_URL"]="redis://${redis_host}:${redis_port}/${redis_db}"
    fi

    render_config_template "${template_file}" "${file}" "DEPLOY_VARS"
    enable_dev_mode_if_needed ${file} runtime
    if [ "${DEPLOY_VARS["DB_TYPE"]}" == "postgresql" ]; then
        yq eval '
        select(.kind == "Deployment").spec.template.spec.containers[0].env += [
            {
                "name": "OPENJIUWEN_SERVICE_PG_SCHEMA",
                "value": "'"${DEPLOY_VARS["RUNTIME_PG_SCHEMA"]}"'"
            }
        ]' -i "${file}"
    fi

    add_resource_if_set "RUNTIME" "${file}"
    if [[ "${DEPLOY_VARS["APPLY_PATCH"]}" != "true" ]]; then
        yq eval-all -i 'select(.kind != "Service" or .spec.type != "NodePort")' "${file}"
    fi
}

render_runtime_files() {
    local pvc_template_file="${CONFIG["CLAW_PVC_TEMPLATE_FILE"]}"
    local pvc_file="${CONFIG["CLAW_PVC_FILE"]}"
    local mount_type="${DEPLOY_VARS["CLAW_MOUNT_TYPE"]}"
    local is_external_pvc="${DEPLOY_VARS["ENABLE_EXTERNAL_PVC"]}"

    render_secret_configmap
    ensure_available_port "AGENT_RUNTIME_NODE_PORT"
    gen_runtime_file
    render_patch_file
    render_agentserver_env_configmap

    # agentserver 由 runtime 动态创建并挂载内置 PVC，PVC 渲染归属 runtime
    if [[ "${mount_type}" == "pvc" && "${is_external_pvc}" == "false" ]]; then
        render_config_template "${pvc_template_file}" "${pvc_file}" "DEPLOY_VARS"
    fi
}

deploy_runtime() {
    local namespace="${DEPLOY_VARS["NAMESPACE"]}"
    local name="${DEPLOY_VARS["AGENT_RUNTIME_NAME"]}"
    local file="${CONFIG["RUNTIME_FILE"]}"
    local pvc_file="${CONFIG["CLAW_PVC_FILE"]}"
    local mount_type="${DEPLOY_VARS["CLAW_MOUNT_TYPE"]}"
    local is_external_pvc="${DEPLOY_VARS["ENABLE_EXTERNAL_PVC"]}"

    ensure_secret_configmap
    # PVC 先于 runtime 部署：后续 config_sync 创建的 agentserver pod 会挂载它
    if [[ "${mount_type}" == "pvc" && "${is_external_pvc}" == "false" && -f "${pvc_file}" ]]; then
        exec_cmd kubectl apply -f "${pvc_file}"
    fi
    exec_cmd kubectl apply -f ${file}
    wait_k8s_resource_ready "deployment" "${name}" "${namespace}"
    create_agentserver_env_configmap
    install_patch
}

uninstall_runtime() {
    local namespace="${DEPLOY_VARS["NAMESPACE"]}"
    local name="${DEPLOY_VARS["AGENT_RUNTIME_NAME"]}"
    local file="${CONFIG["RUNTIME_FILE"]}"
    local pvc_file="${CONFIG["CLAW_PVC_FILE"]}"
    local mount_type="${DEPLOY_VARS["CLAW_MOUNT_TYPE"]}"

    # 先删 Deployment（保持 ServiceAccount 不删），等 Pod 优雅退出
    # runtime.yaml 含 ServiceAccount / Role / Deployment 等同文件资源。
    # 不可 kubectl delete -f 一次性删：SA 被删后 Pod 仍在 Terminating 窗口内，
    # in-cluster token 立即失效 → runtime shutdown 删 AgentServer Pod 会 401。
    info "Deleting Runtime Deployment first (keep ServiceAccount for graceful shutdown)"
    exec_cmd kubectl delete deployment "${name}" -n "${namespace}" --ignore-not-found=true
    wait_pod_terminated "${name}" "${namespace}"

    # 兜底清理 runtime 动态创建的 agentserver pod（按 label）
    local orphan_pods
    orphan_pods=$(kubectl get pods -n "${namespace}" -l jiuwenclaw-component=agentserver -o name 2>/dev/null || true)
    if [ -n "${orphan_pods}" ]; then
        info "Cleaning up orphan agentserver pods created by runtime (label: jiuwenclaw-component=agentserver)"
        exec_cmd kubectl delete pod -n "${namespace}" -l jiuwenclaw-component=agentserver --ignore-not-found=true
    else
        info "No orphan agentserver pods to clean up."
    fi

    info "Deleting remaining Runtime resources (ServiceAccount, Role, Service, ...)"
    exec_cmd kubectl delete -f "${file}" --ignore-not-found=true
    uninstall_secret_configmap
    ensure_redis_down
    delete_agentserver_env_configmap

    # 仅在内置 PVC 时删（外部 PVC 不动）。必须在孤儿 agentserver pod 清理之后执行：
    # PVC 带 pvc-protection finalizer，agentserver pod 释放挂载前删除此 PVC 
    # 会一直阻塞
    if [[ "${mount_type}" == "pvc" && -z "${DEPLOY_VARS["CLAW_PVC"]:-}" && -f "${pvc_file}" ]]; then
        exec_cmd kubectl delete -f "${pvc_file}" --ignore-not-found=true
    fi
}

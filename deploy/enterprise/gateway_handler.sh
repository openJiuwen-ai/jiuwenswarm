#!/usr/bin/env bash
set -euo >/dev/null 2>&1

gen_gateway_env_file() {
    local namespace="${DEPLOY_VARS["NAMESPACE"]}"
    local env_template_file="${CONFIG["GATEWAY_ENV_TEMPLATE_FILE"]}"
    local envfile_name="${DEPLOY_VARS["GATEWAY_ENV_FILE_CM_NAME"]}"
    local env_file="${CONFIG["GATEWAY_ENV_FILE"]}"
    local yaml_file="${CONFIG["GATEWAY_ENV_YAML_FILE"]}"

    render_config_template "${env_template_file}" "${env_file}" "DEPLOY_VARS"

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
}

gen_gateway_config_file() {
    local field_name="feishu"
    local template_file="${CONFIG["GATEWAY_CONFIG_TEMPLATE_FILE"]}"
    local file="${CONFIG["GATEWAY_CONFIG_FILE"]}"
    local yaml_file="${CONFIG["GATEWAY_CONFIG_YAML_FILE"]}"
    local namespace="${DEPLOY_VARS["NAMESPACE"]}"
    local conf_name="${DEPLOY_VARS["GATEWAY_CONFIG_MAP_NAME"]}"

    info "GATEWAY_CONFIG_TEMPLATE_FILE: ${template_file}"
    render_config_template "${template_file}" "${file}" "DEPLOY_VARS"

    # Clear configuration
    yq eval ".channels.${field_name} = {}" -i "${file}"

    # 飞书机器人配置（可选）：未配置 FEISHU_BOTS 时跳过，channels.feishu 保持为空
    local feishu_bots="${DEPLOY_VARS["FEISHU_BOTS"]:-}"
    if [ -n "${feishu_bots}" ]; then
        echo "${feishu_bots}" | while read -r line; do
            # Skip empty lines
            [ -z "${line}" ] && continue

            # Split by colon
            IFS=':' read -r bot_name app_id app_secret <<< "${line}"

            # info "Adding bot: ${bot_name}"
            yq eval ".channels.${field_name}.${bot_name}.app_id = \"${app_id}\"" -i "${file}"
            yq eval ".channels.${field_name}.${bot_name}.app_secret = \"${app_secret}\"" -i "${file}"
            yq eval ".channels.${field_name}.${bot_name}.encrypt_key = \"\"" -i "${file}"
            yq eval ".channels.${field_name}.${bot_name}.verification_token = \"\"" -i "${file}"
            yq eval ".channels.${field_name}.${bot_name}.allow_from = []" -i "${file}"
            yq eval ".channels.${field_name}.${bot_name}.enable_streaming = true" -i "${file}"
            yq eval ".channels.${field_name}.${bot_name}.chat_id = \"\"" -i "${file}"
            yq eval ".channels.${field_name}.${bot_name}.enabled = true" -i "${file}"
        done
    fi

    success "Gateway config rendered: ${file}; ConfigMap yaml: ${yaml_file}"
    kubectl create configmap -n "${namespace}" "${conf_name}" \
        --from-file=config.yaml="${file}" \
        --dry-run=client -o yaml \
        | yq eval 'del(.metadata.creationTimestamp)' > "${yaml_file}"
}

gen_gateway_file() {
    local mode="${DEPLOY_VARS["MODE"]}"
    local template_file="${CONFIG["GATEWAY_TEMPLATE_FILE"]}"
    local file="${CONFIG["GATEWAY_FILE"]}"
    local enable_gw_lable="${DEPLOY_VARS["GATEWAY_SCHED_LABEL_ENABLED"]}"

    render_config_template "${template_file}" "${file}" "DEPLOY_VARS"
    enable_dev_mode_if_needed "${file}" gateway

    # No need to install packages
    if [[ "${mode}" == "dev" && -n "${DEPLOY_VARS["CLAW_CODE_PATH"]:-}" ]]; then
        local claw_code="${DEPLOY_VARS["CLAW_CODE_PATH"]}"
        yq eval '.dependencies = {}' -i "${claw_code}/packages/jiuwenclaw-ee/gateway/extensions/runtime_management_extension/extension.yaml"
        yq eval '.dependencies = {}' -i "${claw_code}/packages/jiuwenclaw-ee/gateway/extensions/manager_config_receiver/extension.yaml"
    fi

    add_resource_if_set "GATEWAY" "${file}"

    if [[ "${mode}" != "dev" && "${enable_gw_lable}" == "true" ]]; then
        # Automatically create nodeSelector and set gateway=enable
        yq eval 'select(.kind == "Deployment").spec.template.spec.nodeSelector |= {"gateway": "enable"}' -i "${file}"
    fi

    if [[ "${DEPLOY_VARS["APPLY_PATCH"]}" != "true" ]]; then
        yq eval-all -i 'select(.kind != "Service" or .spec.type != "NodePort")' "${file}"
    fi

    success "Gateway file generation completed: ${file}"
}

render_gateway_files() {
    local mode="${DEPLOY_VARS["MODE"]}"

    if [ "${mode}" == "dev" ]; then
        DEPLOY_VARS["CLAW_HOME"]="/root"
    else
        DEPLOY_VARS["CLAW_HOME"]="/home/app"
    fi

    render_secret_configmap
    gen_gateway_env_file
    gen_gateway_config_file

    ensure_available_port "GATEWAY_CONFIG_HTTP_NODE_PORT"
    gen_gateway_file
}

deploy_gateway() {
    local namespace="${DEPLOY_VARS["NAMESPACE"]}"
    local env_yaml_file="${CONFIG["GATEWAY_ENV_YAML_FILE"]}"
    local conf_yaml_file="${CONFIG["GATEWAY_CONFIG_YAML_FILE"]}"
    local name="${DEPLOY_VARS["GATEWAY_NAME"]}"
    local gateway_file="${CONFIG["GATEWAY_FILE"]}"

    ensure_secret_configmap
    # 使用 apply 保证重复部署幂等：ConfigMap 已存在时更新内容，不因 create 冲突失败。
    exec_cmd kubectl apply -f "${env_yaml_file}"
    exec_cmd kubectl apply -f "${conf_yaml_file}"

    exec_cmd kubectl apply -f "${gateway_file}"
    wait_k8s_resource_ready "deployment" "${name}" "${namespace}"
}

uninstall_gateway() {
    local gateway_file="${CONFIG["GATEWAY_FILE"]}"
    local env_yaml_file="${CONFIG["GATEWAY_ENV_YAML_FILE"]}"
    local conf_yaml_file="${CONFIG["GATEWAY_CONFIG_YAML_FILE"]}"

    exec_cmd kubectl delete -f "${gateway_file}" --ignore-not-found=true
    exec_cmd kubectl delete -f "${env_yaml_file}" --ignore-not-found=true
    exec_cmd kubectl delete -f "${conf_yaml_file}" --ignore-not-found=true

    uninstall_secret_configmap
    ensure_redis_down
}

#!/usr/bin/env bash
set -euo >/dev/null 2>&1


gen_gateway_config_file() {
    local client_type="${DEPLOY_VARS["AGENT_RUNTIME"]}"

    info "AGENT_RUNTIME: ${client_type}"
    if [ "${client_type}" == "yuanrong" ]; then
        collect_oyr_info
        GATEWAY_CONFIG_TEMPLATE_FILE="${SCRIPT_DIR}/conf/config-yr.template.yaml"
    elif [ "${client_type}" == "jiuwen" ]; then
        GATEWAY_CONFIG_TEMPLATE_FILE="${SCRIPT_DIR}/conf/config-rt.template.yaml"
    else
        error "Unsupported AGENT_RUNTIME: ${client_type}"
    fi
    info "GATEWAY_CONFIG_TEMPLATE_FILE: ${GATEWAY_CONFIG_TEMPLATE_FILE}"

    render_config_template ${GATEWAY_CONFIG_TEMPLATE_FILE} ${GATEWAY_CONFIG_FILE} "DEPLOY_VARS"

    # Clear configuration
    yq eval '.channels.feishu_enterprise = {}' -i "${GATEWAY_CONFIG_FILE}"

    echo "${DEPLOY_VARS["FEISHU_BOTS"]}" | while read -r line; do
        # Skip empty lines
        [ -z "${line}" ] && continue

        # Split by colon
        IFS=':' read -r bot_name app_id app_secret <<< "${line}"

        # info "Adding bot: ${bot_name}"
        yq eval ".channels.feishu_enterprise.${bot_name}.app_id = \"${app_id}\"" -i "${GATEWAY_CONFIG_FILE}"
        yq eval ".channels.feishu_enterprise.${bot_name}.app_secret = \"${app_secret}\"" -i "${GATEWAY_CONFIG_FILE}"
        yq eval ".channels.feishu_enterprise.${bot_name}.encrypt_key = \"\"" -i "${GATEWAY_CONFIG_FILE}"
        yq eval ".channels.feishu_enterprise.${bot_name}.verification_token = \"\"" -i "${GATEWAY_CONFIG_FILE}"
        yq eval ".channels.feishu_enterprise.${bot_name}.allow_from = []" -i "${GATEWAY_CONFIG_FILE}"
        yq eval ".channels.feishu_enterprise.${bot_name}.enable_streaming = true" -i "${GATEWAY_CONFIG_FILE}"
        yq eval ".channels.feishu_enterprise.${bot_name}.chat_id = \"\"" -i "${GATEWAY_CONFIG_FILE}"
        yq eval ".channels.feishu_enterprise.${bot_name}.enabled = true" -i "${GATEWAY_CONFIG_FILE}"
    done

    success "ConfigMap file generation completed: ${GATEWAY_CONFIG_FILE}"
}

gen_gateway_deploy_file() {
    local client_type="${DEPLOY_VARS["AGENT_RUNTIME"]}"
    local mode="${DEPLOY_VARS["MODE"]}"
    local deploy_template_file="${SCRIPT_DIR}/conf/deployment-${mode}.template.yaml"

    if [ "${client_type}" == "jiuwen" ]; then
        # Create and apply Gateway RBAC resources for pod creation permissions
        render_config_template "${GATEWAY_RBAC_TEMPLATE_FILE}" "${GATEWAY_RBAC_FILE}" "DEPLOY_VARS"
        exec_cmd kubectl apply -f ${GATEWAY_RBAC_FILE}
        deploy_template_file="${SCRIPT_DIR}/conf/deployment-dev.template.yaml"
    fi

    # Render deployment manifest template with global variable substitution
    render_config_template ${deploy_template_file} ${GATEWAY_DEPLOYMENT_FILE} "DEPLOY_VARS"

    if [ "${client_type}" == "jiuwen" ]; then
        # Bind dedicated ServiceAccount to grant pod creation privileges
        yq eval ".spec.template.spec.serviceAccountName = \"${DEPLOY_VARS["GATEWAY_SERVICE_ACCOUNT"]}\"" -i "${GATEWAY_DEPLOYMENT_FILE}"

        # Override container runtime image with dedicated release image
        yq eval ".spec.template.spec.containers[0].image = \"${DEPLOY_VARS["CLAW_GATEWAY_RT_IMAGE"]}\"" -i "${GATEWAY_DEPLOYMENT_FILE}"

         # Inject environment variables via ConfigMap binding
        yq eval "
            .spec.template.spec.containers[0].envFrom += [{
                \"configMapRef\": {
                    \"name\": \"${DEPLOY_VARS["GATEWAY_ENV_FILE_NAME"]}\"
                }
            }]
        " -i "${GATEWAY_DEPLOYMENT_FILE}"

        if  [ "${mode}" == "product" ]; then
             # Remove local host code mounting for production hardening
            yq eval 'del(.spec.template.spec.containers[].volumeMounts[] | select(.name == "host-code"))' -i "${GATEWAY_DEPLOYMENT_FILE}"

            yq eval 'del(.spec.template.spec.volumes[] | select(.name == "host-code"))' -i "${GATEWAY_DEPLOYMENT_FILE}"
        fi
    fi
    success "Gateway deployment file generation completed: ${GATEWAY_DEPLOYMENT_FILE}"
}


deploy_gateway() {
    local file_name=$(basename "${GATEWAY_CONFIG_FILE}")
    local client_type="${DEPLOY_VARS["AGENT_RUNTIME"]}"

    # create configMap from config.yaml
    gen_gateway_config_file
    info "Executing: kubectl create configmap ${DEPLOY_VARS["GATEWAY_CONFIG_MAP_NAME"]} --from-file=${file_name}=${GATEWAY_CONFIG_FILE} --dry-run=client -o yaml | kubectl apply -f -"
    kubectl create configmap ${DEPLOY_VARS["GATEWAY_CONFIG_MAP_NAME"]} --from-file=${file_name}=${GATEWAY_CONFIG_FILE} --dry-run=client -o yaml | kubectl apply -f -

    # create configMap from gateway.env
    if [ "${client_type}" == "jiuwen" ]; then
        render_config_template "${GATEWAY_ENV_TEMPLATE_FILE}" "${GATEWAY_ENV_FILE}" "DEPLOY_VARS"
        exec_cmd kubectl create configmap ${DEPLOY_VARS["GATEWAY_ENV_FILE_NAME"]} --from-env-file=${GATEWAY_ENV_FILE}
    fi

    # start gateway
    gen_gateway_deploy_file
    exec_cmd kubectl apply -f ${GATEWAY_DEPLOYMENT_FILE}
    wait_k8s_resource_ready "deployment" "${DEPLOY_VARS["GATEWAY_DEPLOYMENT_NAME"]}"
}


uninstall_gateway() {
    # Clean up standalone gateway deployment if it exists
    delete_k8s_resource "deployment" "${DEPLOY_VARS["GATEWAY_DEPLOYMENT_NAME"]}"
    wait_pod_terminated "${DEPLOY_VARS["GATEWAY_DEPLOYMENT_NAME"]}"

    delete_k8s_resource "configmap" "${DEPLOY_VARS["GATEWAY_CONFIG_MAP_NAME"]}"

    if [ "${DEPLOY_VARS["AGENT_RUNTIME"]}" == "jiuwen" ]; then
        exec_cmd kubectl delete -f ${GATEWAY_RBAC_FILE} false
        exec_cmd kubectl delete configmap ${DEPLOY_VARS["GATEWAY_ENV_FILE_NAME"]} false
        # delete_k8s_pods "jiuwenclaw"
    fi
}

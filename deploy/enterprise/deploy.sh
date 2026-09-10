#!/usr/bin/env bash
set -euo >/dev/null 2>&1

source "common.sh"
source "global_vars.sh"
source "args_handler.sh"
source "check_handler.sh"
source "cmd_handler.sh"
source "envfile_handler.sh"
source "template_handler.sh"
source "k8s_handler.sh"
source "ports_handler.sh"
source "nfs_handler.sh"
source "mysql_handler.sh"
source "postgresql_handler.sh"
source "minio_handler.sh"
source "rabbitmq_handler.sh"
source "redis_handler.sh"
source "log_handler.sh"
source "jina_handler.sh"
source "proxy_handler.sh"
source "configmap_secret_handler.sh"
source "gateway_handler.sh"
source "manager_handler.sh"
source "web_handler.sh"
source "runtime_handler.sh"
source "patch_handler.sh"

process_up() {
    # MODULES是ALL_MODULES的子集，启动顺序正着来
    local sorted_modules=()
    for m in "${ALL_MODULES[@]}"; do
        if [[ " ${MODULES[@]} " =~ " ${m} " ]]; then
            sorted_modules+=("$m")
        fi
    done
    info "sorted_modules=${sorted_modules[@]}"

    if [ "${DEPLOY_VARS["RENDER_ONLY"]}" != "true" ]; then
        local namespace="${DEPLOY_VARS["NAMESPACE"]}"
        if [ ${namespace} != "default" ]; then
            create_k8s_resource "ns" ${namespace}
        fi
        collect_k8s_cluster_info
    fi
    
    exec_cmd mkdir -p ${CONFIG_DIR}

    for module in "${sorted_modules[@]}"; do
        local lmodule=${module,,}
        local fname=${lmodule//-/_}

        if [ "${DEPLOY_VARS["RENDER_ONLY"]}" == "true" ]; then
            check_${fname}_up_dependency
            render_${fname}_files
        else
            check_${fname}_up_dependency
            render_${fname}_files
            deploy_${fname}
        fi
    done
}


process_down() {
    if [ "${DEPLOY_VARS["RENDER_ONLY"]}" == "true" ]; then
        return
    fi

    local namespace="${DEPLOY_VARS["NAMESPACE"]}"

    # MODULES是ALL_MODULES的子集，卸载顺序倒着来
    local reversed_modules=()
    for ((i=${#ALL_MODULES[@]}-1; i>=0; i--)); do
        m="${ALL_MODULES[$i]}"
        if [[ " ${MODULES[@]} " =~ " ${m} " ]]; then
            reversed_modules+=("$m")
        fi
    done
    info "reversed_modules=${reversed_modules[@]}"

    for module in "${reversed_modules[@]}"; do
        local lmodule=${module,,}
        local fname=${lmodule//-/_}
        uninstall_${fname}
    done
}

process_restart() {
    process_down
    process_up
}

# ==================== Main function ====================
main() {
    read_env_from_file "${CUSTOM_ENV_FILE}" "DEPLOY_VARS"
    parse_args "$@"
    detect_os
    check_dependency
    process_${CMD}
}


# Execute main function
main "$@"

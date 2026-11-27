#!/usr/bin/env bash
set -euo >/dev/null 2>&1

source "global_vars.sh"
source "common.sh"
source "cmd_handler.sh"
source "args_handler.sh"
source "check_handler.sh"
source "envfile_handler.sh"
source "k8s_handler.sh"
source "template_handler.sh"
source "storage_handler.sh"
source "oyr_handler.sh"
source "gateway_handler.sh"

process_up() {
    # MODULES是ALL_MODULES的子集，启动顺序正着来
    local sorted_modules=()
    for m in "${ALL_MODULES[@]}"; do
        if [[ " ${MODULES[@]} " =~ " ${m} " ]]; then
            sorted_modules+=("$m")
        fi
    done
    info "sorted_modules=${sorted_modules[@]}"
    collect_k8s_cluster_info

    for module in "${sorted_modules[@]}"; do
        case "${module}" in
            NFS)
                check_nfs_up_dependency      
                deploy_nfs
                ;;
            YR_CLAW)
                check_yr_claw_up_dependency
                deploy_oyr
                ;;
            GATEWAY)
                check_gateway_up_dependency
                deploy_gateway
                ;;
        esac
    done
}

process_down() {
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
        case "${module}" in
            NFS)
                uninstall_nfs
                ;;
            YR_CLAW)
                uninstall_oyr
                ;;
            GATEWAY)
                uninstall_gateway
                ;;
        esac
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
    check_dependency
    process_${CMD}
}


# Execute main function
main "$@"

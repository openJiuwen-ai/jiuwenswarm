#!/usr/bin/env bash
set -euo >/dev/null 2>&1

source "global_vars.sh"
source "common.sh"
source "cmd_handler.sh"
source "args_handler.sh"
source "check_handler.sh"
source "envfile_handler.sh"
source "template_handler.sh"
source "jiuwenswarm_handler.sh"
source "gateway_handler.sh"
source "web_handler.sh"

process_up() {
    for module in "${MODULES[@]}"; do
        case "${module}" in
            jiuwenswarm)
                check_jiuwenswarm_up_dependency
                deploy_jiuwenswarm
                ;;
            gateway)
                check_gateway_up_dependency
                deploy_gateway
                ;;
            web)
                check_web_up_dependency
                deploy_web
                ;;
        esac
    done
}

process_down() {
    local reversed_modules=()
    for ((i=${#MODULES[@]}-1; i>=0; i--)); do
        reversed_modules+=("${MODULES[$i]}")
    done

    for module in "${reversed_modules[@]}"; do
        case "${module}" in
            jiuwenswarm)
                uninstall_jiuwenswarm
                ;;
            gateway)
                uninstall_gateway
                ;;
            web)
                uninstall_web
                ;;
        esac
    done
}

process_restart() {
    process_down
    process_up
}

main() {
    read_env_from_file "${CUSTOM_ENV_FILE}" "DEPLOY_VARS"
    # Preserve the interpreter selected by an existing installer configuration.
    if [ -n "${DEPLOY_VARS[YR_PYTHON_VERSION]:-}" ] \
        && ! grep -qE '^PYTHON_VERSION=' "${CUSTOM_ENV_FILE}"; then
        DEPLOY_VARS[PYTHON_VERSION]="${DEPLOY_VARS[YR_PYTHON_VERSION]}"
    fi
    parse_args "$@"
    check_dependency
    process_${CMD}
}

if [ -n "${SWARM_SETTINGS:-}" ]; then
    source "${SCRIPT_DIR}/agentos_handler.sh"
fi

main "$@"

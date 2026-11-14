#!/usr/bin/env bash
set -eu

cd "$(dirname "${BASH_SOURCE[0]}")"
source "global_vars.sh"
source "common.sh"
source "args_handler.sh"
source "check_handler.sh"
source "jiuwenswarm_handler.sh"
source "gateway_handler.sh"
source "web_handler.sh"

swarm_service() {
    local -a args=(--settings "${SWARM_SETTINGS}")
    [ -z "${SWARM_BINDINGS}" ] || args+=(--bindings "${SWARM_BINDINGS}")
    "${BUSINESS_PYTHON}" "${SERVICE_SETTINGS}" "$@" "${args[@]}"
}

process_up() {
    gateway_load_config
    for module in "${MODULES[@]}"; do
        case "${module}" in
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
    local identity
    identity=$(swarm_service identity)
    eval "${identity}"
    local reversed_modules=()
    for ((i=${#MODULES[@]}-1; i>=0; i--)); do
        reversed_modules+=("${MODULES[$i]}")
    done

    for module in "${reversed_modules[@]}"; do
        case "${module}" in
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
    parse_args "$@"
    check_dependency
    process_${CMD}
}

main "$@"

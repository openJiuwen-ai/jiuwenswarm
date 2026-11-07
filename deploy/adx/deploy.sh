#!/usr/bin/env bash
set -eu

cd "$(dirname "${BASH_SOURCE[0]}")"
source "global_vars.sh"
source "common.sh"
source "args_handler.sh"
source "check_handler.sh"
source "host_handler.sh"
source "gateway_handler.sh"
source "web_handler.sh"
source "service_handler.sh"

swarm_config() {
    local -a args=(--settings "${SWARM_SETTINGS}")
    [ -z "${SWARM_BINDINGS}" ] || args+=(--bindings "${SWARM_BINDINGS}")
    "${BUSINESS_PYTHON}" "${INTEGRATION}" "$@" "${args[@]}"
}

process_configure() { gateway_gen_config; }
process_preflight() { swarm_config preflight; }

process_install() {
    local -a core swarm
    local package="${SWARM_PACKAGE_DIR:?Set SWARM_PACKAGE_DIR to the offline wheel directory}"
    shopt -s nullglob
    core=("${package}"/openjiuwen-*.whl)
    swarm=("${package}"/jiuwenswarm-*-py3-none-any.whl "${package}"/workswarm-*-py3-none-any.whl)
    shopt -u nullglob
    [ "${#core[@]}" -eq 1 ] || error "release must contain exactly one matching Core wheel"
    [ "${#swarm[@]}" -eq 1 ] || error "release must contain exactly one Swarm wheel"
    (umask 022; "${BUSINESS_PYTHON}" -m pip install --no-index --no-deps --force-reinstall "${core[0]}" "${swarm[0]}") || return 1
    "${BUSINESS_PYTHON}" "${INTEGRATION}" components
}

process_up() {
    gateway_gen_config
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
    identity=$(swarm_config identity)
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

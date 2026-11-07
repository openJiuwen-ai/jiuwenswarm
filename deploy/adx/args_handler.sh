#!/usr/bin/env bash
set -eu

parse_args() {
    local i=0
    local args=("$@")

    while [ $i -lt ${#args[@]} ]; do
        case "${args[$i]}" in
            install|configure|preflight|up|down|restart|status|uninstall)
                CMD="${args[$i]}"
                i=$((i+1))
                ;;
            gateway|web)
                MODULES+=("${args[$i]}")
                i=$((i+1))
                ;;
            -h|--help)
                print_help
                ;;
            *)
                error "Invalid Args: ${args[$i]}"
                ;;
        esac
    done

    if [ -z "${CMD:-}" ]; then
        error "Command not specified! Use 'up' or 'down'"
        exit 1
    fi

    if [ ${#MODULES[@]} -eq 0 ]; then
        MODULES=("gateway" "web")
    fi

    info "Executing command: $*"
    info "CMD=${CMD}"
    info "MODULES=${MODULES[@]}"
}

print_help() {
    cat << EOF
Usage: ./deploy.sh install|configure|preflight|up|down|restart|status|uninstall [gateway|web]
Deploy Gateway/Web on this node using SWARM_SETTINGS.
Prepare ADX and install the application dependencies before up. An active service is preserved;
use down/up or restart to apply configuration changes.
EOF
    exit 0
}

#!/usr/bin/env bash
set -euo >/dev/null 2>&1

# ========== Parses command-line arguments ========== 
parse_args() {
    local i=0
    local args=("$@")

    while [ $i -lt ${#args[@]} ]; do
        case "${args[$i]}" in
            up|down|restart)
                CMD="${args[$i]}"
                i=$((i+1))
                ;;
            nfs|yr_claw|gateway)
                MODULES+=("${args[$i]^^}")
                i=$((i+1))
                ;;
            all)
                process_modules
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

    # Verify that the command must exist
    if [ -z "${CMD:-}" ]; then
        error "Command not specified! Use 'up' or 'down'"
        exit 1
    fi

    # If no modules are specified, deploy all by default
    if [ ${#MODULES[@]} -eq 0 ]; then
        process_modules
    fi

    info "Executing command: $*"
    info "CMD=${CMD}"
    info "MODULES=${MODULES[@]}"
}

process_modules() {
    if [ -n "${DEPLOY_VARS["NFS_SERVER_ADDR"]:-}" ]; then
        info "Use external NFS server"
        MODULES=("GATEWAY")
    else
        MODULES=("NFS" "GATEWAY")
    fi

    if [ "${DEPLOY_VARS["AGENT_RUNTIME"]}" == "yuanrong" ]; then
        MODULES+=("YR_CLAW")
    fi
}

# Print help info and exit
print_help() {
    cat << EOF
Usage: ./$(basename "$0") [COMMAND] [MODULES...]

Commands (required):
  up        Start and deploy the specified modules
  down      Stop and shutdown the specified modules

Modules (optional, default: all):
  yr_claw   Deploy/Shutdown OpenYuanRong CLAW module
  nfs       Deploy/Shutdown NFS module
  gateway   Deploy/Shutdown Gateway module
  all       Deploy/Shutdown all modules (default if no module is specified)

Options:
  -h, --help    Show this help message and exit

Examples:
  ./$(basename "$0") up                # Start all modules (default)
  ./$(basename "$0") up yr_claw nfs    # Start only CLAW and NFS modules
  ./$(basename "$0") down all          # Shutdown all modules
  ./$(basename "$0") down gateway      # Shutdown only Gateway module
EOF
    exit 0
}
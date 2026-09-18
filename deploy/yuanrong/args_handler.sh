#!/usr/bin/env bash
set -euo >/dev/null 2>&1

parse_args() {
    local i=0
    local args=("$@")

    while [ $i -lt ${#args[@]} ]; do
        case "${args[$i]}" in
            up|down|restart)
                CMD="${args[$i]}"
                i=$((i+1))
                ;;
            jiuwenswarm|gateway|web)
                MODULES+=("${args[$i]}")
                i=$((i+1))
                ;;
            --ip)
                BIND_IP="${args[$((i+1))]}"
                export BIND_IP
                i=$((i+2))
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
        MODULES=("jiuwenswarm" "gateway" "web")
    fi

    info "Executing command: $*"
    info "CMD=${CMD}"
    info "MODULES=${MODULES[@]}"
    info "BIND_IP=${BIND_IP:-auto}"
}

print_help() {
    cat << EOF
Usage: ./$(basename "$0") [COMMAND] [MODULES...] [OPTIONS]

Commands (Required):
  up        Deploy and start specified modules
  down      Stop and uninstall specified modules
  restart   Restart specified modules

Modules (Optional, default: jiuwenswarm gateway web):
  jiuwenswarm    Install jiuwenswarm on local host + register function on yr master
  gateway        jiuwenswarm gateway service (process-mode)
  web            jiuwenswarm web static server (serves frontend dist on WEB_STATIC_PORT, /ws proxies to gateway WEB_PORT)

Options:
  --ip IP            Specify the local machine IP (required for multi-NIC environments).
                     Overrides auto-detection. All modules use this IP.
  -h, --help         Display this help message and exit

Prerequisites:
  yuanrong must be already deployed on this host (use yuanrong_deploy.sh up --ip ...)

Examples:
  ./$(basename "$0") up --ip 192.168.1.1                          # Deploy all (jiuwenswarm + gateway + web)
  ./$(basename "$0") up jiuwenswarm --ip 192.168.1.1              # Deploy jiuwenswarm only
  ./$(basename "$0") up gateway --ip 192.168.1.1                  # Deploy gateway only
  ./$(basename "$0") up web --ip 192.168.1.1                      # Deploy web server only
  ./$(basename "$0") up                                           # Deploy all on local machine (auto-detect IP)
  ./$(basename "$0") down --ip 192.168.1.1                        # Stop all
  ./$(basename "$0") down gateway --ip 192.168.1.1                # Stop gateway only
  ./$(basename "$0") down web --ip 192.168.1.1                    # Stop web server only
  ./$(basename "$0") restart --ip 192.168.1.1                     # Restart all
EOF
    exit 0
}

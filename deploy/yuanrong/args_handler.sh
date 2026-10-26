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
            jiuwenswarm|gateway)
                MODULES+=("${args[$i]}")
                i=$((i+1))
                ;;
            --hosts)
                DEPLOY_VARS["CLUSTER_HOSTS"]="${args[$((i+1))]}"
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
        MODULES=("jiuwenswarm" "gateway")
    fi

    info "Executing command: $*"
    info "CMD=${CMD}"
    info "MODULES=${MODULES[@]}"
    info "CLUSTER_HOSTS=${DEPLOY_VARS["CLUSTER_HOSTS"]}"
}

print_help() {
    cat << EOF
Usage: ./$(basename "$0") [COMMAND] [MODULES...] [OPTIONS]

Commands (Required):
  up        Deploy and start specified modules
  down      Stop and uninstall specified modules
  restart   Restart specified modules

Modules (Optional, default: jiuwenswarm gateway):
  jiuwenswarm    Install jiuwenswarm on all hosts + register function on yr master
  gateway        jiuwenswarm gateway service (process-mode)

Options:
  --hosts HOSTS      Comma-separated IP list for cluster hosts (default: local machine IP)
                     Single machine: --hosts 192.168.1.1
                     Multi-machine:  --hosts 192.168.1.1,192.168.1.2,192.168.1.3
                     First IP is yr master node, others are agent nodes
                     If not specified, defaults to local machine IP
                     Can also be set in .env.custom via CLUSTER_HOSTS
  -h, --help         Display this help message and exit

Prerequisites:
  yuanrong must be already deployed on all hosts (use yuanrong_deploy.sh up --hosts ...)

Examples:
  ./$(basename "$0") up --hosts 192.168.1.1                          # Deploy all (jiuwenswarm + gateway)
  ./$(basename "$0") up jiuwenswarm --hosts 192.168.1.1              # Deploy jiuwenswarm only
  ./$(basename "$0") up gateway --hosts 192.168.1.1                  # Deploy gateway only
  ./$(basename "$0") up                                              # Deploy all on local machine
  ./$(basename "$0") down --hosts 192.168.1.1                        # Stop all
  ./$(basename "$0") down gateway --hosts 192.168.1.1                # Stop gateway only
  ./$(basename "$0") restart --hosts 192.168.1.1                     # Restart all
EOF
    exit 0
}

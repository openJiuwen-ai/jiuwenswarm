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
            nfs|nfs-sc|rabbitmq|mysql|postgresql|minio|log|jina|proxy|gateway|web|manager|runtime)
                MODULES+=("${args[$i]^^}")
                i=$((i+1))
                ;;
            -n)
                DEPLOY_VARS["NAMESPACE"]="${args[$((i+1))]}"
                i=$((i+2))
                ;;
            --render-only)
                DEPLOY_VARS["RENDER_ONLY"]="true"
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
        error "Command not specified! Use 'up', 'down' or 'restart'"
        exit 1
    fi

    # If no modules are specified, deploy all by default
    if [ ${#MODULES[@]} -eq 0 ]; then
        process_modules
    fi

    info "Executing command: $*"
    info "CMD=${CMD}"
    info "MODULES=${MODULES[@]}"
    info "NAMESPACE=${DEPLOY_VARS["NAMESPACE"]}"
}

process_modules() {
    MODULES=("GATEWAY" "WEB" "RUNTIME")
}

# Print help info and exit
print_help() {
    cat << EOF
Usage: ./$(basename "$0") [COMMAND] [MODULES...] [OPTIONS]

Commands (Required):
  up        Deploy and start specified modules
  down      Stop and uninstall specified modules
  restart   Restart specified modules

Modules (Optional):
  nfs       NFS service module (deploys to default namespace, ignores -n parameter)
  rabbitmq  RabbitMQ module (deploys to default namespace, ignores -n parameter)
  mysql     MySQL module (deploys to default namespace, ignores -n parameter)
  minio     Minio module (deploys to default namespace, ignores -n parameter)
  log       Log module (deploys to default namespace, ignores -n parameter)
  jina      Jina module (deploys to default namespace, ignores -n parameter)
  proxy     Proxy module (deploys to default namespace, ignores -n parameter)
  gateway   Gateway service module
  web       Web frontend module
  manager   CLAW Manager module

Options:
  -n NAMESPACE              Specify Kubernetes namespace (defaults to default if unspecified)
  --render-only             Only render and output YAML manifests to conf directory
  -h, --help                Display this help message and exit

Examples:
  ./$(basename "$0") up                                # Deploy default modules in default namespace
  ./$(basename "$0") up nfs                            # Deploy NFS (always uses default namespace, ignores -n parameter)
  ./$(basename "$0") down                              # Uninstall default modules in default namespace
  ./$(basename "$0") restart                           # Restart default modules in default namespace
  ./$(basename "$0") restart gateway                   # Restart gateway module in default namespace
EOF
    exit 0
}

#!/usr/bin/env bash
set -euo >/dev/null 2>&1

check_cmd() {
    if command -v "$1" >/dev/null 2>&1; then
        success "$1 is OK."
    else
        error "$1 is not installed. Please install it first."
    fi
}

detect_os() {
    if [ "$(uname -s)" != "Linux" ]; then
        error "Unsupported OS: $(uname -s)"
    fi
    DEPLOY_VARS["OS_TYPE"]="linux"
}

check_if_root() {
    if [[ ${EUID} -ne 0 ]]; then
        error "This script must be run as root (sudo)."
    fi
}

get_local_ip() {
    local local_ips
    local_ips=$(hostname -I 2>/dev/null || echo "")
    for ip in ${local_ips}; do
        if [ "${ip}" != "127.0.0.1" ] && [ "${ip}" != "localhost" ]; then
            echo "${ip}"
            return 0
        fi
    done
    echo "127.0.0.1"
}

check_cmds() {
    for cmd in python3 jq; do
        check_cmd ${cmd}
    done

    local hosts_str="${DEPLOY_VARS["CLUSTER_HOSTS"]:-}"
    local need_ssh=false
    if [ -n "${hosts_str}" ]; then
        IFS=',' read -ra _host_list <<< "${hosts_str}"
        for h in "${_host_list[@]}"; do
            if [ "${h}" != "127.0.0.1" ] && [ "${h}" != "localhost" ]; then
                local local_ips
                local_ips=$(hostname -I 2>/dev/null || echo "")
                local is_local=false
                for ip in ${local_ips}; do
                    if [ "${h}" = "${ip}" ]; then
                        is_local=true
                        break
                    fi
                done
                if [ "${is_local}" = "false" ]; then
                    need_ssh=true
                    break
                fi
            fi
        done
    fi
    if [ "${need_ssh}" = "true" ]; then
        check_cmd ssh
    fi
}

check_jiuwenswarm_up_dependency() {
    local hosts_str="${DEPLOY_VARS["CLUSTER_HOSTS"]}"

    if [ -z "${hosts_str}" ]; then
        hosts_str=$(get_local_ip)
        DEPLOY_VARS["CLUSTER_HOSTS"]="${hosts_str}"
        warning "CLUSTER_HOSTS not set, using local IP: ${hosts_str}"
    fi

    IFS=',' read -ra HOST_LIST <<< "${hosts_str}"
    if [ ${#HOST_LIST[@]} -eq 0 ]; then
        error "CLUSTER_HOSTS is empty. Please specify at least one host IP"
    fi

    info "CLUSTER_HOSTS validated: ${hosts_str} (${#HOST_LIST[@]} host(s))"
    info "Note: yuanrong is assumed to be already deployed on all hosts"
}

check_gateway_up_dependency() {
    if [ -z "${DEPLOY_VARS["MASTER_NODE_IP"]:-}" ]; then
        if [ -n "${DEPLOY_VARS["CLUSTER_HOSTS"]:-}" ]; then
            IFS=',' read -ra _gw_host_list <<< "${DEPLOY_VARS["CLUSTER_HOSTS"]}"
            DEPLOY_VARS["MASTER_NODE_IP"]="${_gw_host_list[0]}"
            info "MASTER_NODE_IP inferred from CLUSTER_HOSTS: ${DEPLOY_VARS["MASTER_NODE_IP"]}"
        else
            local local_ip
            local_ip=$(get_local_ip)
            DEPLOY_VARS["MASTER_NODE_IP"]="${local_ip}"
            info "MASTER_NODE_IP not set, defaulting to local: ${local_ip}"
        fi
    fi

    if [ -z "${DEPLOY_VARS["FRONTEND_PORT"]:-}" ]; then
        DEPLOY_VARS["FRONTEND_PORT"]="8888"
        warning "FRONTEND_PORT not set, using default: 8888"
    fi

    if [ -z "${DEPLOY_VARS["REGISTRY_PORT"]:-}" ]; then
        DEPLOY_VARS["REGISTRY_PORT"]="4003"
        warning "REGISTRY_PORT not set, using default: 4003"
    fi

    if [ -z "${DEPLOY_VARS["SSH_PORT"]:-}" ]; then
        DEPLOY_VARS["SSH_PORT"]="2223"
        warning "SSH_PORT not set, using default: 2223"
    fi

    # TUI (/tui on GatewayServer) bind host defaults to master node IP.
    if [ -z "${DEPLOY_VARS["GATEWAY_HOST"]:-}" ]; then
        DEPLOY_VARS["GATEWAY_HOST"]="${DEPLOY_VARS["MASTER_NODE_IP"]}"
        info "GATEWAY_HOST not set, using MASTER_NODE_IP: ${DEPLOY_VARS["GATEWAY_HOST"]}"
    fi

    if [ -z "${DEPLOY_VARS["GATEWAY_PORT"]:-}" ]; then
        DEPLOY_VARS["GATEWAY_PORT"]="19001"
        warning "GATEWAY_PORT not set, using default: 19001"
    fi

    # WebChannel (/ws) bind host defaults to master node IP.
    if [ -z "${DEPLOY_VARS["WEB_HOST"]:-}" ]; then
        DEPLOY_VARS["WEB_HOST"]="${DEPLOY_VARS["MASTER_NODE_IP"]}"
        info "WEB_HOST not set, using MASTER_NODE_IP: ${DEPLOY_VARS["WEB_HOST"]}"
    fi

    if [ -z "${DEPLOY_VARS["WEB_PORT"]:-}" ]; then
        DEPLOY_VARS["WEB_PORT"]="19000"
        warning "WEB_PORT not set, using default: 19000"
    fi

    if [ -z "${DEPLOY_VARS["FUNCTION_ID"]:-}" ]; then
        error "FUNCTION_ID is not set. Please deploy jiuwenswarm first or set FUNCTION_ID in .env.custom."
    fi
}

check_dependency() {
    detect_os
    check_cmds
    check_if_root
}

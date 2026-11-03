#!/usr/bin/env bash
set -eu

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
    if [ -n "${BIND_IP:-}" ]; then echo "${BIND_IP}"; return; fi
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

check_gateway_up_dependency() {
    check_cmd "${BUSINESS_PYTHON}"
}

check_web_up_dependency() {
    check_gateway_up_dependency
}

check_dependency() {
    detect_os
    check_if_root
    check_cmd "${BUSINESS_PYTHON}"
}

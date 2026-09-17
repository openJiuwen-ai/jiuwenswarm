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

# 读取 config.yaml 的 ingress_virtual_ip（VIP）。gateway 监听端口统一绑定 VIP，
# 使各服务对外可通过统一入口访问；无 VIP 配置时输出空串。
_ingress_vip() {
    local config_file="${HOME:-/root}/.agentos/deploy/config.yaml"
    [ -f "${config_file}" ] || return 1
    local py="python${DEPLOY_VARS["YR_PYTHON_VERSION"]:-3.11}" vip=""
    if command -v "${py}" >/dev/null 2>&1 && "${py}" -c 'import yaml' >/dev/null 2>&1; then
        vip=$("${py}" -c '
import sys, yaml
try:
    with open(sys.argv[1]) as f:
        cfg = yaml.safe_load(f)
    print((cfg or {}).get("cluster", {}).get("ingress_virtual_ip", "") or "", end="")
except Exception:
    print("", end="")
' "${config_file}" 2>/dev/null)
    fi
    if [ -n "${vip}" ] && echo "${vip}" | grep -qE '^([0-9]{1,3}\.){3}[0-9]{1,3}$'; then
        echo "${vip}"
    fi
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

    # INGRESS_VIP 用于 registry endpoint / ssh listen_host 等（见 gateway-config 模板）。
    # 缺失时回退 MASTER_NODE_IP，避免渲染出 http://:4003 / 空 listen_host 等非法配置，
    # 与下方 GATEWAY_HOST / WEB_HOST 的回退逻辑保持一致。
    local ingress_vip
    ingress_vip=$(_ingress_vip || true)
    if [ -z "${ingress_vip}" ]; then
        ingress_vip="${DEPLOY_VARS["MASTER_NODE_IP"]}"
        info "INGRESS_VIP not set, using MASTER_NODE_IP: ${ingress_vip}"
    fi
    DEPLOY_VARS["INGRESS_VIP"]="${ingress_vip}"
    if [ -z "${DEPLOY_VARS["GATEWAY_HOST"]:-}" ]; then
        if [ -n "${ingress_vip}" ]; then
            DEPLOY_VARS["GATEWAY_HOST"]="${ingress_vip}"
            info "GATEWAY_HOST defaulted to ingress_virtual_ip: ${ingress_vip}"
        else
            DEPLOY_VARS["GATEWAY_HOST"]="${DEPLOY_VARS["MASTER_NODE_IP"]}"
            info "GATEWAY_HOST not set, using MASTER_NODE_IP: ${DEPLOY_VARS["GATEWAY_HOST"]}"
        fi
    fi

    if [ -z "${DEPLOY_VARS["GATEWAY_PORT"]:-}" ]; then
        DEPLOY_VARS["GATEWAY_PORT"]="19001"
        warning "GATEWAY_PORT not set, using default: 19001"
    fi

    # WebChannel (/ws) bind host defaults to ingress VIP (fallback MASTER_NODE_IP).
    if [ -z "${DEPLOY_VARS["WEB_HOST"]:-}" ]; then
        if [ -n "${ingress_vip}" ]; then
            DEPLOY_VARS["WEB_HOST"]="${ingress_vip}"
            info "WEB_HOST defaulted to ingress_virtual_ip: ${ingress_vip}"
        else
            DEPLOY_VARS["WEB_HOST"]="${DEPLOY_VARS["MASTER_NODE_IP"]}"
            info "WEB_HOST not set, using MASTER_NODE_IP: ${DEPLOY_VARS["WEB_HOST"]}"
        fi
    fi

    if [ -z "${DEPLOY_VARS["WEB_PORT"]:-}" ]; then
        DEPLOY_VARS["WEB_PORT"]="19000"
        warning "WEB_PORT not set, using default: 19000"
    fi

    # AgentOS IAM：空则默认 http://MASTER_NODE_IP:8090（与 registry/frontend 同 host 约定）。
    # 外置 / K8s Service 等场景请在 .env.custom 写完整 URL 覆盖。
    if [ -z "${DEPLOY_VARS["AGENTOS_AUTH_SERVICE_URL"]:-}" ]; then
        DEPLOY_VARS["AGENTOS_AUTH_SERVICE_URL"]="http://${DEPLOY_VARS["MASTER_NODE_IP"]}:8090"
        info "AGENTOS_AUTH_SERVICE_URL not set, using MASTER_NODE_IP: ${DEPLOY_VARS["AGENTOS_AUTH_SERVICE_URL"]}"
    fi

    if [ -z "${DEPLOY_VARS["AGENTOS_AUTH_TIMEOUT"]:-}" ]; then
        DEPLOY_VARS["AGENTOS_AUTH_TIMEOUT"]="10"
        warning "AGENTOS_AUTH_TIMEOUT not set, using default: 10"
    fi

    if [ -z "${DEPLOY_VARS["FUNCTION_ID"]:-}" ]; then
        error "FUNCTION_ID is not set. Please deploy jiuwenswarm first or set FUNCTION_ID in .env.custom."
    fi
}

# web server 依赖 gateway 已起(WEB_PORT 的 /ws 是 web 的代理目标),
# 故复用 check_gateway_up_dependency 完成 MASTER_NODE_IP / WEB_PORT 等公共变量初始化,
# 再补 web 专有变量 WEB_STATIC_HOST / WEB_STATIC_PORT。
check_web_up_dependency() {
    check_gateway_up_dependency

    # web 静态服务器监听地址,默认 0.0.0.0(对外可访问)。
    if [ -z "${DEPLOY_VARS["WEB_STATIC_HOST"]:-}" ]; then
        DEPLOY_VARS["WEB_STATIC_HOST"]="0.0.0.0"
        info "WEB_STATIC_HOST not set, using default: 0.0.0.0"
    fi

    # web 静态服务器监听端口,默认 5173(app_web.py 的 FRONTEND_PORT 默认值)。
    # 注意:此处用独立变量 WEB_STATIC_PORT, 不复用 FRONTEND_PORT(后者已指 yuanrong frontend 8888)。
    if [ -z "${DEPLOY_VARS["WEB_STATIC_PORT"]:-}" ]; then
        DEPLOY_VARS["WEB_STATIC_PORT"]="5173"
        warning "WEB_STATIC_PORT not set, using default: 5173"
    fi
}

check_dependency() {
    detect_os
    check_cmds
    check_if_root
}

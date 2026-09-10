#!/usr/bin/env bash
set -euo >/dev/null 2>&1

# ======== Check if a single port is occupied ===============
is_port_occupied() {
    local port="$1"
    local port_occupied=0
    local os_type=${DEPLOY_VARS["OS_TYPE"]}

    case "${os_type}" in
        macos)
            # macOS: use lsof which is more reliable
            if lsof -Pi :$port -sTCP:LISTEN -t >/dev/null 2>&1; then
                port_occupied=1
            fi
            ;;
        linux)
            netstat_output=$(netstat -tuln 2>&1)
            if echo "${netstat_output}" | grep -q ":$port"; then
                port_occupied=1
            fi
            ;;
        windows)
            # Windows Git Bash/Cygwin: Match LISTENING state in netstat -an output
            if netstat -an | grep -qiE ":$port[^0-9].*LISTENING.*" 2>/dev/null; then
                port_occupied=1
            fi
            ;;
    esac

    # 集群环境补充校验（专门适配K3s 纯 iptables 转发，不会在用户态进程监听任何 NodePort 端口）
    # 校验1：所有命名空间Service是否绑定该nodePort
    if [ "$port_occupied" -eq 0 ]; then
        if kubectl get svc --all-namespaces -o json 2>/dev/null | jq -r '.items[] | .spec.ports[].nodePort' | grep -q "^${port}$"; then
             port_occupied=1
        fi
    fi

    # 校验2：兜底iptables检查
    # 极端情况处理：遇到那种强制删 Service、删的时候集群节点失联、etcd 事务中断，apiserver 标记端口释放，但 k3s/kube-proxy 没来得及清理 iptables；
    if [ "$port_occupied" -eq 0 ]; then
        if iptables -t nat -L KUBE-NODEPORTS -n 2>/dev/null | grep -q ":${port} "; then
            port_occupied=1
        fi
    fi

    # Return result: 0 = occupied, 1 = available
    if [ "$port_occupied" -eq 1 ]; then
        return 0
    else
        return 1
    fi
}

# =========== Allocate multiple available ports at once ==============
# Usage: ensure_available_port "PORT_NAME_1" ["PORT_NAME_2" ...]
# Function:
#   1. If port is already configured in DEPLOY_VARS, check if it's available
#   2. If no port configured, auto-allocate from START_PORT ~ END_PORT
ensure_available_port() {
    if [ "${DEPLOY_VARS["NO_CHECK_PORTS"]}" == "true" ]; then
        for port_name in "$@"; do
            if [ -z "${DEPLOY_VARS["${port_name}"]:-}" ]; then
                error "Please define ${port_name} in .env.custom"
            fi
        done
        return
    fi

    local start_port=${CONFIG["START_PORT"]}
    local end_port=${CONFIG["END_PORT"]}

    # Iterate over all passed port name arguments
    for port_name in "$@"; do
        # If port is already set in config, validate it
        if [ -n "${DEPLOY_VARS["${port_name}"]:-}" ]; then
            local port=${DEPLOY_VARS["${port_name}"]}
            if is_port_occupied "${port}"; then
                error "[${port_name}] Port ${port} is occupied, please choose another one."
            fi
            info "Using pre-configured port ${port} for ${port_name}"
            continue
        fi

        # Auto allocate available port from range
        for port in $(seq "$start_port" "$end_port"); do
            if ! is_port_occupied "$port"; then
                DEPLOY_VARS["${port_name}"]="$port"
                # Move start port forward to avoid reusing the same port
                start_port=$((port + 1))
                break
            fi
        done
    done
}

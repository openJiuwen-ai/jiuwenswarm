#!/usr/bin/env bash
set -eu

SSH_OPTS="-o BatchMode=yes -o StrictHostKeyChecking=yes -o ConnectTimeout=10"

is_local_host() {
    local host="$1"
    if [ "${host}" = "127.0.0.1" ] || [ "${host}" = "localhost" ]; then
        return 0
    fi
    local local_ips
    local_ips=$(hostname -I 2>/dev/null || echo "")
    for ip in ${local_ips}; do
        if [ "${host}" = "${ip}" ]; then
            return 0
        fi
    done
    return 1
}

exec_on_host() {
    local host="$1"
    shift
    if is_local_host "${host}"; then
        bash -c "$*"
    else
        ssh ${SSH_OPTS} root@${host} "$*"
    fi
}

copy_to_host() {
    local host="$1"
    local src="$2"
    local dst="$3"
    if is_local_host "${host}"; then
        local src_real
        src_real=$(realpath "${src}" 2>/dev/null || echo "${src}")
        local dst_real
        if [[ "${dst}" == */ ]]; then
            dst_real=$(realpath "${dst}" 2>/dev/null || echo "${dst}")
            dst_real="${dst_real}/$(basename "${src}")"
        else
            dst_real=$(realpath "${dst}" 2>/dev/null || echo "${dst}")
        fi
        if [ "${src_real}" = "${dst_real}" ]; then
            return 0
        fi
        cp -r "${src}" "${dst}"
    else
        scp ${SSH_OPTS} -r "${src}" "root@${host}:${dst}"
    fi
}

# ===== 目标主机端口监听检查（gateway / web 等平级组件共用） =====
# 判断指定 TCP 端口是否处于 LISTEN。用于服务健康检查，
# 防止仅看 systemctl is-active 会在"服务刚 active 即崩溃"时误报成功的缺陷。
port_is_listening() {
    local host="$1"
    local port="$2"
    exec_on_host "${host}" "if command -v ss >/dev/null 2>&1; then ss -ltn 2>/dev/null | grep -qE ':${port}[[:space:]]'; else timeout 3 bash -c 'exec 3<>/dev/tcp/${host}/${port}' 2>/dev/null; fi" 2>/dev/null
}

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
}

# 停用并禁用 jiuwenswarm-gateway 和 jiuwenswarm-web 的 systemd 服务（本机），清理 unit 文件与 drop-in
# uninstall 是本机操作（与 install 对称），故仅处理本机 systemd 服务；
swarm_disable_systemd() {
    command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ] || return 0
    local unit_file found=0
    # 枚举 gateway 和 web 两类 unit
    for unit_file in "/etc/systemd/system/$(gateway_service_name).service" "/etc/systemd/system/$(web_service_name).service"; do
        [ -f "${unit_file}" ] || continue
        found=1
        local svc_name
        svc_name=$(basename "${unit_file}" .service)
        info "Disabling systemd service ${svc_name}..."
        systemctl disable --now "${svc_name}" 2>/dev/null || true
        systemctl reset-failed "${svc_name}" 2>/dev/null || true
        rm -rf "${unit_file}" "/etc/systemd/system/${svc_name}.service.d"
    done

    systemctl daemon-reload 2>/dev/null || true
    if [ "${found}" -eq 1 ]; then
        success "jiuwenswarm systemd services disabled and cleaned up"
    else
        info "No jiuwenswarm systemd unit found, skipping"
    fi
}

process_uninstall() {
    local identity
    identity=$(swarm_service identity)
    eval "${identity}"
    local local_host
    local_host="${BIND_IP:-$(hostname -I 2>/dev/null | awk '{print $1}')}"
    [ -z "${local_host}" ] && local_host="127.0.0.1"

    # 先停用 systemd 服务，再卸载 pip 包，避免卸载后残留 unit 文件
    swarm_disable_systemd
    rm -f -- "${DEPLOY_VARS["HOME"]}/.jiuwenswarm/config/adx-service.env"

    if "${BUSINESS_PYTHON}" -m pip uninstall -y jiuwenswarm workswarm openjiuwen; then
        success "jiuwenswarm and openjiuwen uninstalled on ${local_host}"
    else
        warning "jiuwenswarm/openjiuwen not installed or failed to uninstall on ${local_host}"
    fi
}

# 只读探测本机 jiuwenswarm 运行状态
# 检测 jiuwenswarm-gateway 和 jiuwenswarm-web 两个服务
# 输出格式: jiuwenswarm|<service>|<state>|<detail>|<version>
process_status() {
    local identity
    identity=$(swarm_service identity)
    eval "${identity}"
    local rc=0

    # 探测 jiuwenswarm 版本号：importlib.metadata 比 pip show 快约 30 倍
    local jwsw_ver="-"
    local pkg_ver
    pkg_ver=$("${BUSINESS_PYTHON}" -c "from importlib.metadata import distributions; print(next((d.version for d in distributions() if d.metadata['Name'] in ('workswarm', 'jiuwenswarm')), '-'))" 2>/dev/null || true)
    [ -n "${pkg_ver}" ] && jwsw_ver="${pkg_ver}"

    # systemd 模式：枚举 gateway 和 web 两类 unit
    if command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]; then
        local unit_file found=0
        for unit_file in "/etc/systemd/system/$(gateway_service_name).service" "/etc/systemd/system/$(web_service_name).service"; do
            [ -f "${unit_file}" ] || continue
            found=1
            local svc_name
            svc_name=$(basename "${unit_file}" .service)

            local state detail
            if systemctl is-active --quiet "${svc_name}" 2>/dev/null; then
                state="running"
                detail="active"
            elif systemctl is-failed --quiet "${svc_name}" 2>/dev/null; then
                state="failed"
                detail="failed"
                rc=1
            else
                state="stopped"
                detail="inactive"
            fi
            echo "jiuwenswarm|${svc_name}|${state}|${detail}|${jwsw_ver}"
        done
        if [ "${found}" -eq 1 ]; then
            return ${rc}
        fi
        # 无 unit 文件，落入进程模式
    fi

    # 进程模式：pgrep 检测 jiuwenswarm-gateway 和 jiuwenswarm-web
    _jwsw_pids_of() {
        local pat="$1"
        if command -v pgrep >/dev/null 2>&1; then
            pgrep -f "${pat}" 2>/dev/null || true
        else
            ps -ef 2>/dev/null | grep "${pat}" | grep -v grep | awk '{print $2}' || true
        fi
    }

    local gw_pids web_pids gw_alive=0 web_alive=0
    gw_pids=$(_jwsw_pids_of 'jiuwenswarm-gateway')
    [ -n "${gw_pids}" ] && gw_alive=1
    web_pids=$(_jwsw_pids_of 'jiuwenswarm-web')
    [ -n "${web_pids}" ] && web_alive=1

    # gateway 状态
    if [ "${gw_alive}" -eq 1 ]; then
        echo "jiuwenswarm|jiuwenswarm-gateway|running|active|${jwsw_ver}"
    else
        echo "jiuwenswarm|jiuwenswarm-gateway|stopped|no process|${jwsw_ver}"
    fi

    # web 状态
    if [ "${web_alive}" -eq 1 ]; then
        echo "jiuwenswarm|jiuwenswarm-web|running|active|${jwsw_ver}"
    else
        echo "jiuwenswarm|jiuwenswarm-web|stopped|no process|${jwsw_ver}"
    fi

    return ${rc}
}

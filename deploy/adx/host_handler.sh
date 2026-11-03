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

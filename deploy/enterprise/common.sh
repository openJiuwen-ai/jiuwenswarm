#!/usr/bin/env bash
set -euo >/dev/null 2>&1

# ==================== Log functions ====================
info() { echo -e "\033[36m=== $@ ===\033[0m"; }
success() { echo -e "\033[32m✅ $@\033[0m"; }
warning() { echo -e "\033[33m⚠️  $@\033[0m"; }
error() { echo -e "\033[31m❌ $@\033[0m"; exit 1; }


# Print all key-value pairs of bash array
print_array() {
    local array_name="$1"
    local -n arr_ref="$1"
    
    echo -e "\033[33m$ ${array_name}\033[0m"
    
    if [[ ! "$(declare -p ${array_name})" =~ "declare -a" && ! "$(declare -p ${array_name})" =~ "declare -A" ]]; then
        echo -e "\033[31m[ERROR] ${array_name} is not a bash array variable!\033[0m"
        return 1
    fi

    for key in "${!arr_ref[@]}"; do
        echo -e "\033[36m  ├─ ${array_name}[${key}] = ${arr_ref[${key}]}\033[0m"
    done
    
    echo -e "\033[33m  └─ Total elements count: ${#arr_ref[@]}\033[0m\n"
}



urlencode() {
    local string="${1}"
    local strlen=${#string}
    local encoded=""
    local pos c o

    for (( pos=0 ; pos<strlen ; pos++ )); do
        c=${string:$pos:1}
        case "$c" in
            [-_.~a-zA-Z0-9] ) o="${c}" ;;
            * ) printf -v o '%%%02x' "'$c'"
        esac
        encoded+="${o}"
    done
    echo "${encoded}"
}

# 生成 uuid4 格式实例 ID（小写，带连字符）
gen_uuid4() {
    if command -v uuidgen >/dev/null 2>&1; then
        uuidgen | tr '[:upper:]' '[:lower:]' | tr -d '\n'
        return
    fi
    if command -v python3 >/dev/null 2>&1; then
        python3 -c 'import uuid; print(uuid.uuid4(), end="")'
        return
    fi
    error "Cannot generate uuid4: uuidgen or python3 is required"
}

#   dev     -> root（hostPath 挂载源码）          HOME=/root
#   product -> app（镜像内 USER app，uid 1000）   HOME=/home/app
# CLAW_HOME： HOME目录
# CLAW_FS_GROUP：（PVC/NFS）要靠 kubelet 按属组 授权
# CLAW_USER: 运行时用户
# CLAW_GROUP： 运行时组
set_user_context() {
    if [ "${DEPLOY_VARS["MODE"]}" == "dev" ]; then
        DEPLOY_VARS["CLAW_HOME"]="/root"
        DEPLOY_VARS["CLAW_FS_GROUP"]="0"
        DEPLOY_VARS["CLAW_USER"]="0"
        DEPLOY_VARS["CLAW_GROUP"]="0"
    else
        DEPLOY_VARS["CLAW_HOME"]="/home/app"
        DEPLOY_VARS["CLAW_FS_GROUP"]="1000"
        DEPLOY_VARS["CLAW_USER"]="1000"
        DEPLOY_VARS["CLAW_GROUP"]="1000"
    fi
}

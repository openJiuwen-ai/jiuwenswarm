#!/usr/bin/env bash
set -euo >/dev/null 2>&1

check_cmd() {
    if command -v "$1" >/dev/null 2>&1; then
        success "$1 is OK."
    else
        error "$1 is not installed. Please install it first."
    fi
}

check_yq() {
    local YQ_VERSION=$(yq --version 2>&1)

    check_cmd "yq"
    if echo "$YQ_VERSION" | grep -q "mikefarah" && echo "$YQ_VERSION" | grep -qE "version v4\.|version v[5-9]\."; then
        success "yq is OK: $YQ_VERSION"
    else
        error "The detected yq is not mikefarah/yq v4+. Current version info: $YQ_VERSION"
    fi
}

check_cmds() {
    for cmd in jq mount.nfs base64
    do
        check_cmd ${cmd}
    done

    check_yq

    local os_type=${DEPLOY_VARS["OS_TYPE"]}
    if [ "${os_type}" == "macos" ]; then
        for cmd in jot lsof
        do
            check_cmd ${cmd}
        done
    fi
}

detect_os() {
    if [ "$(uname -s)" != "Linux" ]; then
        error "Unsupported OS: ${os_type}"
    fi
    DEPLOY_VARS["OS_TYPE"]="linux"
}

check_if_root() {
    if [[ ${EUID} -ne 0 ]]; then
        error "This script must be run as root (sudo)."
    fi
}

# ======== Check if the cluster has at least 2 nodes ======== 
check_cluster_has_enough_nodes() {
    if [ "${CMD}" == "down"  ]; then
        return
    fi
    info "===== Checking cluster node count ====="

    # Get ready node count (only Ready nodes)
    local node_count=$(kubectl get nodes --no-headers | grep -w "Ready" | wc -l)

    # Check if node count >= 2
    if [[ ${node_count} -lt 2 ]]; then
        error "Cluster only has ${node_count} Ready node(s), at least 2 required!"
    fi

    success "Cluster has ${node_count} Ready nodes, check passed!"
}

check_dependency(){
    if [ "${DEPLOY_VARS["RENDER_ONLY"]}" == "true" ]; then
        return
    fi

    check_cmds
    check_if_root
}

check_if_nfs_up() {
    # Check if external NFS server
    if [ -n "${DEPLOY_VARS["NFS_SERVER_ADDR"]:-}" ]; then
        info "Use external NFS server"
        DEPLOY_VARS["ENABLE_EXTERNAL_NFS"]="true"
        return
    fi

    if [ "${DEPLOY_VARS["RENDER_ONLY"]}" == "true" ]; then
        return
    fi

    # No Build-In NFS server
    if ! check_k8s_resource_exists "deployment" "${DEPLOY_VARS["NFS_NAME"]}"; then
        error "NFS is not deployed. Please deploy it first with: ./$(basename "$0") up nfs"
    fi

    info "Use built-in NFS server"
    fetch_current_node_ip
    DEPLOY_VARS["NFS_SERVER_ADDR"]=${DEPLOY_VARS["CURRENT_NODE_IP"]}
}

check_if_nfs_sc_up() {
    # Check if external PVC
    if [ -n "${DEPLOY_VARS["CLAW_PVC"]:-}" ]; then
        info "Use external PVC"
        DEPLOY_VARS["ENABLE_EXTERNAL_PVC"]="true"
        return
    fi

    # No Build-In NFS provider
    if ! check_k8s_resource_exists "deployment" "${DEPLOY_VARS["NFS_SC_DNAME"]}"; then
        error "NFS_SC is not deployed. Please deploy it first with: ./$(basename "$0") up nfs-sc"
    fi

    DEPLOY_VARS["CLAW_PVC"]="jiuwenclaw-pvc"
}

# 变量规则一：
# mysql: DB_NAME 拼为 <<MODULE>>_<<NAMESPACE>>（按 db 隔离各实例）
# postgresql: PG_SCHEMA 取 NAMESPACE（按 schema 隔离各实例）
# 变量规则二：
# 支持分库独立账号配置：分别定义 <<MODULE>>_DB_USER / <<MODULE>>_DB_PASSWORD
# 支持全局统一账号配置: 统一定义 DB_USER / DB_PASSWORD
# 优先级规则：若两类变量同时配置，以分库专属账号为准，全局统一账号自动失效
set_db_var() {
    local module=$1
    local db_type=$2
    local name=$3
    local namespace="${DEPLOY_VARS["NAMESPACE"]}"
    local lmodule="${module,,}"
    local full_name="${module}_${name}"

    case "$name" in
        DB_NAME)
            if [[ -z "${DEPLOY_VARS["${full_name}"]:-}" ]]; then
                if [ "${db_type}" == "mysql" ]; then
                    DEPLOY_VARS["${full_name}"]="${lmodule}_${namespace}"
                else
                    DEPLOY_VARS["${full_name}"]="${lmodule}"
                fi
            fi
            return
            ;;
        PG_SCHEMA)
            if [[ -z "${DEPLOY_VARS["${full_name}"]:-}" ]]; then
                if [ ${db_type} == "postgresql" ]; then
                    DEPLOY_VARS["${full_name}"]="${namespace}"
                fi
            fi
            return
            ;;
        DB_USER|DB_PASSWORD)
            if [ -z "${DEPLOY_VARS["${full_name}"]:-}" ]; then
                DEPLOY_VARS["${full_name}"]=${DEPLOY_VARS["${name}"]:-}
            fi
            ;;
        *)
            error "invaild var name"
            ;;
    esac

    if [ -z "${DEPLOY_VARS["${full_name}"]:-}" ]; then
        error "Please set up ${full_name} or ${name}."
    fi
}

check_if_db_up() {
    # 已经执行过检查，直接返回，避免重复校验
    if [[ "${DEPLOY_VARS["DB_CHECKED"]:-}" == "true" ]]; then
        return
    fi

    local db_type="${DEPLOY_VARS["DB_TYPE"]}"
    info "DB_TYPE: ${db_type}"
    DEPLOY_VARS["DB_CHECKED"]="true"
    if [ "${db_type}" != "mysql" ] && [ "${db_type}" != "postgresql" ]; then
        error "DB_TYPE='${db_type}' is not supported in enterprise deploy; use 'mysql' or 'postgresql'"
    fi

    local db_type_upper="${db_type^^}"
    local name="${DEPLOY_VARS["${db_type_upper}_NAME"]}"

    # Build-In DB server
    if [[ -z "${DEPLOY_VARS["DB_HOST"]:-}" || "${DEPLOY_VARS["DB_HOST"]}" == "${name}-headless.default" ]]; then
        if ! check_k8s_resource_exists "statefulset" "${name}"; then
            error "${db_type} is not deployed. Please deploy it first with: ./$(basename "$0") up ${db_type}"
        fi

        info "Use built-in ${db_type} server"
        case "${db_type}" in
            mysql)
                DEPLOY_VARS["DB_HOST"]="${name}-headless.default"
                DEPLOY_VARS["DB_PORT"]="3306"
                for module in GATEWAY WEB MANAGER IDENTITY RUNTIME
                do
                    DEPLOY_VARS["${module}_DB_USER"]="root"
                    DEPLOY_VARS["${module}_DB_PASSWORD"]=${DEPLOY_VARS["MYSQL_ROOT_PASSWORD"]}
                    for name in DB_NAME PG_SCHEMA; do
                        set_db_var "${module}" "${db_type}" "${name}"
                    done
                done
                ;;
            postgresql)
                DEPLOY_VARS["DB_HOST"]="${name}-headless.default"
                DEPLOY_VARS["DB_PORT"]="5432"
                for module in GATEWAY WEB MANAGER IDENTITY RUNTIME
                do
                    DEPLOY_VARS["${module}_DB_USER"]="postgres"
                    DEPLOY_VARS["${module}_DB_PASSWORD"]=${DEPLOY_VARS["POSTGRESQL_PASSWORD"]}
                    for name in DB_NAME PG_SCHEMA; do
                        set_db_var "${module}" "${db_type}" "${name}"
                    done
                done
                ;;
            *)
                error "check_if_db_up: unknown db '${db_type}'"
                ;;
        esac
        return
    fi

    info "Use external ${db_type} server"

    if [ -z "${DEPLOY_VARS["DB_PORT"]:-}" ]; then
        error "Please define DB_PORT in .env.custom"
    fi

    for module in GATEWAY WEB MANAGER IDENTITY RUNTIME; do
        for name in DB_USER DB_PASSWORD DB_NAME PG_SCHEMA; do
            set_db_var "${module}" "${db_type}" "${name}"
        done
    done
}

check_if_obs_up() {
    local name="${DEPLOY_VARS["MINIO_NAME"]}"

    # Check if external OBS server
    if [ -n "${DEPLOY_VARS["OBS_URL"]:-}" ]; then
        info "Use external OBS server"
        DEPLOY_VARS["ENABLE_EXTERNAL_OBS"]="true"
        return
    fi

    # No Build-In Minio server
    if ! check_k8s_resource_exists "statefulset" "${name}"; then
        error "Minio is not deployed. Please deploy it first with: ./$(basename "$0") up minio"
    fi

    info "Use built-in Minio server"
    DEPLOY_VARS["OBS_URL"]="${name}-headless.default:9000"
}

ensure_redis_up() {
    # 已经执行过检查，直接返回，避免重复校验
    if [[ "${DEPLOY_VARS["REDIS_CHECKED"]:-}" == "true" ]]; then
        return
    fi

    DEPLOY_VARS["REDIS_CHECKED"]="true"

    local namespace="${DEPLOY_VARS["NAMESPACE"]}"
    local redis_name="${DEPLOY_VARS["REDIS_NAME"]}"

    # 已设外挂 Redis，跳过
    if [ -n "${DEPLOY_VARS["REDIS_HOST"]:-}" ]; then
        info "Use external Redis server: ${DEPLOY_VARS["REDIS_HOST"]}"
        DEPLOY_VARS["ENABLE_EXTERNAL_REDIS"]="true"
        return
    fi

    DEPLOY_VARS["REDIS_HOST"]="${redis_name}.${namespace}"
    DEPLOY_VARS["REDIS_PORT"]="6379"

    # 同命名空间已有 redis，直接用
    if check_k8s_resource_exists "deployment" "${redis_name}" "${namespace}"; then
        info "Use built-in Redis server in namespace: ${namespace}"
        return
    fi

    render_redis_files

    # 渲染模式不实际部署
    if [ "${DEPLOY_VARS["RENDER_ONLY"]}" == "true" ]; then
        info "rendering Redis files for namespace: ${namespace}"
        return
    fi

    # 同命名空间没有 redis 且非外挂 → 自动启动一个
    info "Redis not found in namespace '${namespace}', auto-deploying built-in Redis..."
    deploy_redis
    success "Deploy Redis in namespace '${namespace}'"
}

check_if_jina_up() {
    local name="${DEPLOY_VARS["JINA_NAME"]}"
    local rname="${name}-reader"
    local cname="${name}-cache-proxy"

    if check_k8s_resource_exists "deployment" "${rname}" && check_k8s_resource_exists "deployment" "${cname}"; then
        DEPLOY_VARS["JINA_READER_ENDPOINT"]="http://${name}-cache-proxy-svc.default"
        info "Use built-in Jina server"
    fi
}

check_if_rabbitmq_up() {
    local name="${DEPLOY_VARS["RABBITMQ_NAME"]}"
    local user=${DEPLOY_VARS["RABBITMQ_USER"]}
    local password=${DEPLOY_VARS["RABBITMQ_PASSWORD"]}
    local url=""
    local encoded_password=$(urlencode "$password")

    # Check if external RABBITMQ server
    if [ -n "${DEPLOY_VARS["RABBITMQ_URL"]:-}" ]; then
        info "Use external RABBITMQ server"
        url="${DEPLOY_VARS["RABBITMQ_URL"]}"
        DEPLOY_VARS["MANAGER_RABBITMQ_URL"]="amqp://${user}:${encoded_password}@${url}"
        DEPLOY_VARS["ENABLE_EXTERNAL_RABBITMQ"]="true"
        return
    fi

    # No Build-In RABBITMQ server
    if ! check_k8s_resource_exists "statefulset" "${name}"; then
        error "RABBITMQ is not deployed. Please deploy it first with: ./$(basename "$0") up rabbitmq"
    fi

    info "Use built-in RABBITMQ server"
    url="${name}-headless.default:5672"
    DEPLOY_VARS["MANAGER_RABBITMQ_URL"]="amqp://${user}:${encoded_password}@${url}"
}

check_if_gateway_up() {
    if ! check_k8s_resource_exists "deployment" "${DEPLOY_VARS["GATEWAY_NAME"]}" "${DEPLOY_VARS["NAMESPACE"]}"; then
        error "GATEWAY is not deployed. Please deploy it first with: ./$(basename "$0") up gateway"
    fi
}

check_nfs_up_dependency(){
    if [ "${DEPLOY_VARS["RENDER_ONLY"]}" == "true" ]; then
        return
    fi

    local arch=$(uname -m)

    if [[ "$arch" =~ ^aarch64 || "$arch" =~ arm ]]; then
        error "ARM arch unsupported for NFS, abort deployment."
    fi
}

check_nfs_sc_up_dependency(){
    check_if_nfs_up
}

check_mysql_up_dependency(){
    local mysql_path="${DEPLOY_VARS["NFS_POD_PATH"]}/${DEPLOY_VARS["MYSQL_NAME"]}"
    local nfs_dname=${DEPLOY_VARS["NFS_NAME"]}

    check_if_nfs_up

    if [ "${DEPLOY_VARS["RENDER_ONLY"]}" == "true" ]; then
        return
    fi

    if [ "${DEPLOY_VARS["ENABLE_EXTERNAL_NFS"]}" == "false" ]; then
        info "Preparing MySQL data directory: ${mysql_path}"
        local nfs_pod=$(kubectl get pods -n default -l app=${nfs_dname} -o jsonpath='{.items[0].metadata.name}')

        info "Executing: kubectl exec ${nfs_pod} -- sh -c \"mkdir -p ${mysql_path}\""
        kubectl exec ${nfs_pod} -- sh -c "mkdir -p ${mysql_path}"
        success "MySQL directory created successfully in NFS Pod!"
    fi
}

check_postgresql_up_dependency(){
    local pg_path="${DEPLOY_VARS["NFS_POD_PATH"]}/${DEPLOY_VARS["POSTGRESQL_NAME"]}"
    local nfs_dname=${DEPLOY_VARS["NFS_NAME"]}

    check_if_nfs_up

    if [ "${DEPLOY_VARS["RENDER_ONLY"]}" == "true" ]; then
        return
    fi

    if [ "${DEPLOY_VARS["ENABLE_EXTERNAL_NFS"]}" == "false" ]; then
        info "Preparing PostgreSQL data directory: ${pg_path}"
        local nfs_pod=$(kubectl get pods -n default -l app=${nfs_dname} -o jsonpath='{.items[0].metadata.name}')

        info "Executing: kubectl exec ${nfs_pod} -- sh -c \"mkdir -p ${pg_path}\""
        kubectl exec ${nfs_pod} -- sh -c "mkdir -p ${pg_path}"
        success "PostgreSQL directory created successfully in NFS Pod!"
    fi
}

check_minio_up_dependency(){
    local minio_path="${DEPLOY_VARS["NFS_POD_PATH"]}/${DEPLOY_VARS["MINIO_NAME"]}"
    local nfs_dname=${DEPLOY_VARS["NFS_NAME"]}

    check_if_nfs_up

    if [ "${DEPLOY_VARS["RENDER_ONLY"]}" == "true" ]; then
        return
    fi

    if [ "${DEPLOY_VARS["ENABLE_EXTERNAL_NFS"]}" == "false" ]; then
        info "Preparing Minio data directory: ${minio_path}"
        local nfs_pod=$(kubectl get pods -n default -l app=${nfs_dname} -o jsonpath='{.items[0].metadata.name}')

        info "Executing: kubectl exec ${nfs_pod} -- sh -c \"mkdir -p ${minio_path}\""
        kubectl exec ${nfs_pod} -- sh -c "mkdir -p ${minio_path}"
        success "Minio directory created successfully in NFS Pod!"
    fi
}

check_rabbitmq_up_dependency(){
    local rabbit_path="${DEPLOY_VARS["NFS_POD_PATH"]}/${DEPLOY_VARS["RABBITMQ_NAME"]}"
    local nfs_dname=${DEPLOY_VARS["NFS_NAME"]}

    check_if_nfs_up

    if [ "${DEPLOY_VARS["RENDER_ONLY"]}" == "true" ]; then
        return
    fi

    if [ "${DEPLOY_VARS["ENABLE_EXTERNAL_NFS"]}" == "false" ]; then
        info "Preparing RabbitMQ data directory: ${rabbit_path}"
        local nfs_pod=$(kubectl get pods -n default -l app=${nfs_dname} -o jsonpath='{.items[0].metadata.name}')

        info "Executing: kubectl exec ${nfs_pod} -- sh -c \"mkdir -p ${rabbit_path}\""
        kubectl exec ${nfs_pod} -- sh -c "mkdir -p ${rabbit_path}"
        success "RabbitMQ directory created successfully in NFS Pod!"
    fi
}

check_log_up_dependency(){
    if [ -z "${DEPLOY_VARS["CLAW_LOG_DIR"]:-}" ]; then
        DEPLOY_VARS["CLAW_LOG_DIR"]="${HOME}/claw_logs"
    fi

    if [ "${DEPLOY_VARS["RENDER_ONLY"]}" == "true" ]; then
        return
    fi

    local log_dir="${DEPLOY_VARS["CLAW_LOG_DIR"]}"
    exec_cmd mkdir -p "${log_dir}"
    exec_cmd chmod 755 "${log_dir}"
}

check_jina_up_dependency() {
    info "JINA module has no dependencies"
}

check_proxy_up_dependency() {
    info "PROXY module has no dependencies"
}

check_gateway_up_dependency(){
    check_if_db_up
    check_if_jina_up
    ensure_redis_up
    check_if_obs_up
}

check_web_up_dependency(){
    check_if_db_up
    check_if_obs_up
    check_if_gateway_up
}

check_manager_up_dependency(){
    check_if_db_up
}

check_runtime_up_dependency(){
    local jiuwenclaw_path="${DEPLOY_VARS["NFS_POD_PATH"]}/jiuwenclaw"
    local nfs_dname=${DEPLOY_VARS["NFS_NAME"]}

    if [ "${DEPLOY_VARS["CLAW_MOUNT_TYPE"]}" == "nfs" ]; then
        check_if_nfs_up

        if [[ "${DEPLOY_VARS["RENDER_ONLY"]}" != "true" && "${DEPLOY_VARS["ENABLE_EXTERNAL_NFS"]}" == "false" ]]; then
            info "Preparing JiuwenClaw data directory: ${jiuwenclaw_path}"
            local nfs_pod=$(kubectl get pods -n default -l app=${nfs_dname} -o jsonpath='{.items[0].metadata.name}')
            info "Executing: kubectl exec ${nfs_pod} -- sh -c \"mkdir -p ${jiuwenclaw_path} && chown 1000:1000 ${jiuwenclaw_path} && chmod 777 ${jiuwenclaw_path}\""
            kubectl exec ${nfs_pod} -- sh -c "mkdir -p ${jiuwenclaw_path} && chown 1000:1000 ${jiuwenclaw_path} && chmod 777 ${jiuwenclaw_path}"
            success "JiuwenClaw directory created successfully in NFS Pod!"
        fi
    elif [ "${DEPLOY_VARS["CLAW_MOUNT_TYPE"]}" == "pvc" ]; then
        check_if_nfs_sc_up
    fi

    check_if_db_up
    ensure_redis_up

    if [[ "${DEPLOY_VARS["RENDER_ONLY"]}" != "true" && "${DEPLOY_VARS["APPLY_PATCH"]}" == "true" ]]; then
        check_if_gateway_up
        if [ -n "${DEPLOY_VARS["GATEWAY_CONFIG_HTTP_NODE_PORT"]:-}" ]; then
            info "chenhui: No need to fetch GATEWAY_CONFIG_HTTP_NODE_PORT"
            return
        fi
        local namespace="${DEPLOY_VARS["NAMESPACE"]}"
        local gw_svc="${DEPLOY_VARS["GATEWAY_NAME"]}-nodeport"
        local gw_port=$(kubectl get service "${gw_svc}" -n "${namespace}" -o jsonpath='{.spec.ports[?(@.name=="config-http")].nodePort}')
        if [ -z "${gw_port}" ]; then
            error "Failed to read nodePort (config-http) from service ${gw_svc} in namespace ${namespace}"
        fi
        DEPLOY_VARS["GATEWAY_CONFIG_HTTP_NODE_PORT"]="${gw_port}"
    fi
}

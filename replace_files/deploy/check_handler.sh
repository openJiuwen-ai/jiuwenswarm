#!/usr/bin/env bash
set -euo >/dev/null 2>&1

check_cmd() {
    if command -v "$1" >/dev/null 2>&1; then
        success "$1 is OK."
    else
        error "$1 is not installed. Please install it first."
    fi
}

check_cmds() {
    for cmd in helm docker python3 jq
    do
        check_cmd ${cmd}
    done

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

check_if_master() {
    if ! kubectl get node "$(hostname)" -o wide | awk '{print $3}' | grep -qw "master"; then
        error "This script must be run on master."
    fi
}

# ======== Check passwordless SSH connectivity from Master to Worker nodes ========
check_ssh_connectivity() {
    info "Validating passwordless SSH connectivity from Master to all Worker nodes..."
    for worker_ip in "${WORKER_NODE_IPS[@]}"; do
        if ssh -o ConnectTimeout=5 -o StrictHostKeyChecking=no ${worker_ip} "echo 'SSH connected successfully'" >/dev/null 2>&1; then
            success "Worker node ${worker_ip} - SSH passwordless connectivity is normal"
        else
            error "Worker node ${worker_ip} - SSH passwordless connectivity failed! Please configure SSH key authentication first."
        fi
    done
}

# ======== Check if the cluster has at least 2 nodes ======== 
check_cluster_has_enough_nodes() {
    info "===== Checking cluster node count ====="

    # Get ready node count (only Ready nodes)
    local node_count=$(kubectl get nodes --no-headers | grep -w "Ready" | wc -l)

    # Check if node count >= 2
    if [[ ${node_count} -lt 2 ]]; then
        error "Cluster only has ${node_count} Ready node(s), at least 2 required!"
    fi

    success "Cluster has ${node_count} Ready nodes, check passed!"
}

check_if_yr_exist()
{
    if helm list --filter "^${OYL_CHART_NAME}$" | grep -q "${OYL_CHART_NAME}"; then
        error "${OYL_CHART_NAME} is already deployed. Please uninstall it first with: ./$(basename "$0") down yr_claw"
    fi
}

check_if_yr_claw_up() {
    local pool_id=${DEPLOY_VARS["POOL_ID"]}
    local claw_deployment_name="function-agent-${pool_id}"
    local err_msg="YR_CLAW is not deployed. Please deploy it first with: ./$(basename "$0") up yr_claw"

    if ! helm list --filter "^${OYL_CHART_NAME}$" | grep -q "${OYL_CHART_NAME}"; then
        error ${err_msg}
    fi

    if ! check_k8s_resource_exists "deployment" "${claw_deployment_name}"; then
        error ${err_msg}
    fi
}

check_if_nfs_up() {
    # No external NFS server
    if [ -n "${DEPLOY_VARS["NFS_SERVER_ADDR"]:-}" ]; then
        info "Use external NFS server"
        return
    fi

    # No Build-In NFS server
    if ! check_k8s_resource_exists "deployment" "${DEPLOY_VARS["NFS_NAME"]}"; then
        error "NFS is not deployed. Please deploy it first with: ./$(basename "$0") up nfs"
    fi

    info "Use built-in NFS server"
    DEPLOY_VARS["NFS_SERVER_ADDR"]=${DEPLOY_VARS["MASTER_NODE_IP"]}
}

check_dependency(){
    check_if_master
    check_if_root
    detect_os
}

check_nfs_up_dependency(){
    check_cmds
    check_cluster_has_enough_nodes
    check_ssh_connectivity
}

check_yr_claw_up_dependency(){
    check_cmds
    check_cluster_has_enough_nodes
    check_ssh_connectivity
    check_if_nfs_up
    check_if_yr_exist
}

check_gateway_up_dependency(){
    check_cmds
    check_cluster_has_enough_nodes
    check_ssh_connectivity
    check_if_nfs_up
}



#!/usr/bin/env bash
set -euo >/dev/null 2>&1

# Select master node and label with storage=nfs for storage usage
label_master_nfs_node() {
    info "Start adding label storage=nfs for master node..."

    if kubectl label nodes "${MASTER_NODE_NAME}" storage=nfs --overwrite; then
        success "Node ${MASTER_NODE_NAME} selected and labeled successfully!"
    else
        error "Failed to add label for node ${MASTER_NODE_NAME}!"
    fi
}

# 检查 nfs-server pod 是否调度到了打了 storage=nfs 标签的 master 节点
check_nfs_pod_node() {
    info "Checking if NFS pod is running on the master node..."

    # 获取 nfs-server 的 pod 名称和所在节点
    local node_name=$(kubectl get pods -o wide -l app=${DEPLOY_VARS["NFS_NAME"]} -o jsonpath='{.items[0].metadata.name} {.items[0].spec.nodeName}' | awk '{print $2}')

    if [ "${node_name}" != "${MASTER_NODE_NAME}" ]; then
        error "NFS pod is not running on the master node"
    fi
    success "NFS pod is running on the master node"
}

install_pv_pvc() {
    render_config_template ${PV_TEMPLATE_FILE} ${PV_FILE} "DEPLOY_VARS"
    exec_cmd kubectl apply -f ${PV_FILE}

    render_config_template ${PVC_TEMPLATE_FILE} ${PVC_FILE} "DEPLOY_VARS"
    exec_cmd kubectl apply -f ${PVC_FILE}
}

uninstall_pv_pvc() {
    exec_cmd kubectl delete -f ${PVC_FILE} false
    exec_cmd kubectl delete -f ${PV_FILE} false
}

deploy_nfs() {
    label_master_nfs_node

    local nfs_path=${DEPLOY_VARS["NFS_HOST_PATH"]}
    render_config_template ${NFS_SERVER_TEMPLATE_FILE} ${NFS_SERVER_FILE} "DEPLOY_VARS"

    exec_cmd mkdir -p ${nfs_path}
    exec_cmd chmod -R 777 ${nfs_path}

    exec_cmd kubectl apply -f ${NFS_SERVER_FILE}
    wait_k8s_resource_ready "deployment" "${DEPLOY_VARS["NFS_NAME"]}"
    check_nfs_pod_node
}

uninstall_nfs() {
    local nfs_name=${DEPLOY_VARS["NFS_NAME"]}
    delete_k8s_resource "deployment" "${nfs_name}"
    wait_pod_terminated "${nfs_name}"
}

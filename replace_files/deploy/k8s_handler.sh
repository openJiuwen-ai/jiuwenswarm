#!/usr/bin/env bash
set -euo >/dev/null 2>&1

# Wait for kubernetes resource rollout ready
# Args:
#   1: resource kind
#   2: resource name
#   3: namespace (optional, default: default)
wait_k8s_resource_ready() {
    local kind="$1"
    local name="$2"
    local namespace="${3:-default}"
    
    info "Waiting for k8s resource: ${kind}/${namespace}/${name}"
    exec_cmd kubectl rollout status "${kind}/${name}" --namespace="${namespace}"
    success "${kind}/${namespace}/${name} is ready now"
}

# Check if kubernetes resource exists
# Args:
#   1: resource kind
#   2: resource name
#   3: namespace (optional, default: default)
# Return: 0 = exists, 1 = not exists
check_k8s_resource_exists() {
    local kind="$1"
    local name="$2"
    local namespace="${3:-default}"
    local args=""

    # Cluster-scoped resource: PersistentVolume
    if [[ "${kind}" == "pv" || "${kind}" == "PersistentVolume" ]]; then
        args="${kind} ${name}"
        namespace=""
    else
        args="${kind} ${name} -n ${namespace}"
    fi

    if kubectl get ${args} >/dev/null 2>&1; then
        return 0
    fi
    return 1
}

# delete all pods with specified name prefix
delete_k8s_pods() {
    local name="$1"
    local namespace="${2:-default}"

    info "Deleting all pods with prefix: ${name} (namespace: ${namespace})"
    # Get prefixed pod list and execute batch deletion
    info "Executing: kubectl get pods -n \"${namespace}\" -o name | grep -E \"^pod/${name}\" | xargs -r kubectl delete -n \"${namespace}\""
    kubectl get pods -n "${namespace}" -o name \
        | grep -E "^pod/${name}" \
        | xargs -r kubectl delete -n "${namespace}"
    success "All pods with prefix '${name}' have been deleted"
}


# Delete specified kubernetes resource
# Args:
#   1: resource kind
#   2: resource name
#   3: namespace (optional, default: default)
delete_k8s_resource() {
    local kind="$1"
    local name="$2"
    local namespace="${3:-default}"

    local cmd_args=""
    # PV is cluster-scoped, without namespace
    if [[ "${kind}" == "pv" || "${kind}" == "PersistentVolume" ]]; then
        cmd_args="${kind} ${name}"
        namespace=""
    else
        cmd_args="${kind} ${name} -n ${namespace}"
    fi

    # Check whether target resource exists
    if ! kubectl get ${cmd_args} >/dev/null 2>&1; then
        info "${kind}/${namespace}/${name} not found, skipping deletion."
        return
    fi

    info "Deleting k8s resource:  ${kind}/${namespace}/${name}"
    exec_cmd kubectl delete ${cmd_args}
    success "${kind}/${namespace}/${name} is deleted now"
}


# Wait for a pod in a given namespace to be fully terminated and removed
wait_pod_terminated() {
  local pod_name_prefix="$1"
  local namespace="${2:-default}"

  echo "=== Waiting for pod [${namespace}/${pod_name_prefix}*] to terminate completely..."

  while kubectl get pods -n "${namespace}" | grep -q "${pod_name_prefix}"; do
    info "Pod is still terminating, waiting 3 seconds..."
    sleep 3
  done

  success "Pod has been fully terminated and cleaned up!"
}

collect_k8s_cluster_info() {
    DEPLOY_VARS["MASTER_NODE_IP"]=$(kubectl get nodes \
        --selector='node-role.kubernetes.io/master' \
        -o jsonpath='{.items[*].status.addresses[?(@.type=="InternalIP")].address}')
    info "MASTER_NODE_IP: ${DEPLOY_VARS["MASTER_NODE_IP"]}"

    MASTER_NODE_NAME=$(kubectl get nodes \
        --selector='node-role.kubernetes.io/master' \
        -o jsonpath='{.items[*].metadata.name}')
    info "MASTER_NODE_NAME: ${MASTER_NODE_NAME}"

    WORKER_NODE_IPS=($(kubectl get nodes -o jsonpath='{.items[*].status.addresses[?(@.type=="InternalIP")].address}' \
        | tr ' ' '\n' \
        | grep -v "${DEPLOY_VARS["MASTER_NODE_IP"]}"))
    info "WORKER_NODE_IPS: ${WORKER_NODE_IPS[*]}"
}


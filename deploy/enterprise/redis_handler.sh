#!/usr/bin/env bash
set -euo >/dev/null 2>&1

render_redis_files() {
    local template_file="${CONFIG["REDIS_TEMPLATE_FILE"]}"
    local file="${CONFIG["REDIS_FILE"]}"

    ensure_available_port "REDIS_NODE_PORT"
    render_config_template "${template_file}" "${file}" "DEPLOY_VARS"
    apply_redis_acl_if_needed "${file}"
}

# 配置了 REDIS_USERNAME 时，为内置 Redis 注入 ACL：创建指定用户并关闭 default 用户。
#
# 密码不落进 Deployment manifest 明文：args 里写 $(REDIS_PASSWORD)，由 k8s 在容器
# 启动时用 env 中已定义的 REDIS_PASSWORD 展开（因此必须走显式 env + secretKeyRef，
# 不能只靠 envFrom）。探针里则用 $REDIS_PASSWORD 交给 sh 运行时解析，避免依赖
# k8s 的 $(VAR) 展开时序。
apply_redis_acl_if_needed() {
    local file="$1"
    local username="${DEPLOY_VARS["REDIS_USERNAME"]:-}"

    # 未配置用户名 → 保持模板原样，走 default 用户（与历史行为一致）
    if [ -z "${username}" ]; then
        return
    fi

    local password="${DEPLOY_VARS["REDIS_PASSWORD"]:-}"
    if [ -z "${password}" ]; then
        error "REDIS_USERNAME 已设置但 REDIS_PASSWORD 为空：内置 Redis 的 ACL 用户必须设置密码。" \
              "请同时设置 REDIS_PASSWORD，或清空 REDIS_USERNAME 以继续使用 default 用户。"
    fi

    local secret_name="${DEPLOY_VARS["SECRET_CM_NAME"]}"
    info "Enable ACL for built-in Redis: user='${username}', default user disabled"

    yq eval -i '
      (select(.kind == "Deployment").spec.template.spec.containers[0].env) += [
        {"name": "REDIS_USERNAME", "value": "'"${username}"'"},
        {"name": "REDIS_PASSWORD",
         "valueFrom": {"secretKeyRef": {"name": "'"${secret_name}"'", "key": "REDIS_PASSWORD"}}}
      ] |
      (select(.kind == "Deployment").spec.template.spec.containers[0].args) += [
        "--user", "'"${username}"'", "on", ">$(REDIS_PASSWORD)", "~*", "+@all",
        "--user", "default", "off"
      ] |
      (select(.kind == "Deployment").spec.template.spec.containers[0].readinessProbe.exec.command) =
        ["sh", "-c", "redis-cli --user \"$REDIS_USERNAME\" -a \"$REDIS_PASSWORD\" --no-auth-warning ping"]
    ' "${file}"
}

# 判断已存在的内置 Redis Deployment 是否已启用 ACL（args 中带 --user）。
# 用于拦截升级陷阱：存量部署的内置 Redis 没有 ACL 用户，
# 若此时新配了 REDIS_USERNAME，组件会拿新用户名去连而认证失败。
redis_deployment_has_acl() {
    local name="$1"
    local namespace="$2"
    local count
    count=$(kubectl get deployment "${name}" -n "${namespace}" \
        -o jsonpath='{.spec.template.spec.containers[0].args}' 2>/dev/null \
        | grep -c -- '--user' || true)
    [ "${count:-0}" -gt 0 ]
}

deploy_redis() {
    local namespace="${DEPLOY_VARS["NAMESPACE"]}"
    local redis_name="${DEPLOY_VARS["REDIS_NAME"]}"
    local file="${CONFIG["REDIS_FILE"]}"

    exec_cmd kubectl apply -f "${file}"
    wait_k8s_resource_ready "deployment" "${redis_name}" "${namespace}"
}

uninstall_redis() {
    local namespace="${DEPLOY_VARS["NAMESPACE"]}"
    local redis_name="${DEPLOY_VARS["REDIS_NAME"]}"
    local file="${CONFIG["REDIS_FILE"]}"

    exec_cmd kubectl delete -f "${file}" --ignore-not-found=true
    wait_pod_terminated "${redis_name}" "${namespace}"
}


# gateway / runtime 两个模块共用同一份内置 Redis，不能贸然关停。
# 只有两个模块 都关停之后，才能关停
ensure_redis_down() {
    local namespace="${DEPLOY_VARS["NAMESPACE"]}"
    local name="${DEPLOY_VARS["REDIS_NAME"]}"

    # 渲染模式不动集群
    if [ "${DEPLOY_VARS["RENDER_ONLY"]}" == "true" ]; then
        return
    fi

    # 外挂 Redis 由用户自行管理，本工具不负责卸载
    if [ "${DEPLOY_VARS["ENABLE_EXTERNAL_REDIS"]}" == "true" ]; then
        info "External Redis in use, skip shutting down built-in Redis."
        return
    fi

    # 当前命名空间内已无内置 Redis Deployment，说明已卸载或从未部署
    if ! check_k8s_resource_exists "deployment" "${name}" "${namespace}"; then
        info "Built-in Redis '${name}' not found in namespace '${namespace}', nothing to do."
        return
    fi

    for dname in ${DEPLOY_VARS["GATEWAY_NAME"]} ${DEPLOY_VARS["AGENT_RUNTIME_NAME"]}
    do
        if check_k8s_resource_exists "deployment" "${dname}" "${namespace}"; then
            info " ${dname} still running in namespace '${namespace}', keep Redis alive."
            return
        fi
    done
    uninstall_redis
    success "Built-in Redis '${name}' has been shut down."
}

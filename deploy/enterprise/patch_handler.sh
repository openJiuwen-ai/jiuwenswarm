#!/usr/bin/env bash
set -euo >/dev/null 2>&1

# 轮询 NodePort 健康端点直到 200,确认服务真正可达。死等:起不来就一直等。
# 原因: Deployment ready(readinessProbe 通过) ≠ NodePort 可达——kube-proxy 的
# iptables DNAT 规则传播有 1~2s 滞后。下发前必须确认走 NodePort 真能打通,
# 否则请求会打到尚未就绪的端口(连接被拒 / 502),却因脚本只看 curl 退出码
# 而被当成成功,导致配置/模板永远没下发、agentserver 起不来。
# 用法: wait_http_ready <host> <port> <health_path> <module>
wait_http_ready() {
    local host="$1"
    local port="$2"
    local path="$3"
    local module="${4:-service}"
    if [[ "${DEPLOY_VARS[JIUWENSWARM_LINK_MTLS_MODE]:-off}" == enforce ]]; then
        local role=gateway
        [[ "$module" != agent-runtime* ]] || role=runtime
        link_mtls_call wait "$role" "$path"
        return
    fi
    local elapsed=0
    local code="000"
    [ -n "${port}" ] || error "${module} NodePort is empty; cannot check readiness"
    info "Waiting for ${module} ready: http://${host}:${port}${path} (forever)"
    while true; do
        code=$(curl -s --max-time 3 -o /dev/null -w "%{http_code}" "http://${host}:${port}${path}" 2>/dev/null) || code="000"
        if [ "${code}" = "200" ]; then
            success "${module} ready (http=200 after ${elapsed}s)"
            return
        fi
        sleep 1
        elapsed=$((elapsed + 1))
    done
}

# POST JSON 并校验: HTTP 必须 2xx; module=runtime 时再校验响应体 .ok==true
# (只有 agent-runtime 的 config_sync 响应带顶层 .ok;gateway 的 model/agent/
# instance 接口无此字段,只卡 2xx)。
# 修掉"只看 curl 传输退出码"的坑——rc=0 只代表拿到字节,不代表业务成功
# (例如 404/502/503/409 都可能 rc=0)。
# 用法: post_and_validate <url> <data(@file 或 json 串)> <module>
post_and_validate() {
    local url="$1"
    local data="$2"
    local module="${3:-}"
    if [[ "${DEPLOY_VARS[JIUWENSWARM_LINK_MTLS_MODE]:-off}" == enforce ]]; then
        local path="${url#*://}"
        path="/${path#*/}"
        link_mtls_request "$module" "$path" "$data"
        return
    fi
    local resp_file="$(mktemp)"
    local code=$(curl -s --max-time 20 -o "${resp_file}" -w "%{http_code}" \
        -X POST "${url}" -H "Content-Type: application/json" -d "${data}" 2>/dev/null) || code="000"
    local body="$(cat "${resp_file}" 2>/dev/null || true)"
    rm -f "${resp_file}"
    if ! [[ "${code}" =~ ^2[0-9][0-9]$ ]]; then
        error "POST ${url} failed: http=${code} body=${body}"
    fi
    if [ "${module}" = "runtime" ]; then
        local ok="$(printf '%s' "${body}" | jq -r '.ok // empty' 2>/dev/null || true)"
        [ "${ok}" = "true" ] || error "POST ${url} failed: ok!=true body=${body}"
    fi
    success "POST ${url} ok (http=${code})"
}

# 按存储模式注入 data 卷(.jiuwenswarm 数据目录):模板里不再预置 PVC 卷,
# 由本函数按 CLAW_MOUNT_TYPE 二选一注入——
#   pvc: StorageClass 动态供给的持久卷(claimName=CLAW_PVC)
#   nfs: 直连 NFS 共享目录(server=NFS_SERVER_ADDR, path=NFS_SHARE_PATH/jiuwenclaw,
#        与 check_runtime_up_dependency 预创建的内置 NFS 数据目录同名)
# 用法: inject_data_volume <json_file>
inject_data_volume() {
    local json_file="$1"
    local mount_type="${DEPLOY_VARS["CLAW_MOUNT_TYPE"]}"
    local data_volume
    if [ "${mount_type}" == "pvc" ]; then
        data_volume="$(jq -n --arg claim "${DEPLOY_VARS["CLAW_PVC"]}" \
            '{name: "data", persistentVolumeClaim: {claimName: $claim}}')"
    elif [ "${mount_type}" == "nfs" ]; then
        if [ -z "${DEPLOY_VARS["NFS_SERVER_ADDR"]:-}" ]; then
            error "CLAW_MOUNT_TYPE=nfs requires NFS_SERVER_ADDR (external) or a deployed built-in NFS"
        fi
        # 规整共享根路径尾部 '/'(内置 NFS 默认 NFS_SHARE_PATH="/",避免拼出 //jiuwenclaw)
        local nfs_share="${DEPLOY_VARS["NFS_SHARE_PATH"]%/}"
        data_volume="$(jq -n --arg server "${DEPLOY_VARS["NFS_SERVER_ADDR"]}" \
            --arg path "${nfs_share}/jiuwenclaw" \
            '{name: "data", nfs: {server: $server, path: $path}}')"
    else
        error "Unsupported CLAW_MOUNT_TYPE '${mount_type}' (expect pvc/nfs)"
    fi
    jq --argjson volume "${data_volume}" \
        '.rawdata.templates[].volumes += [$volume]' \
        "${json_file}" > "${json_file}.tmp" && mv -f "${json_file}.tmp" "${json_file}"
}

# 剔除无效/不需要的 hostPath 卷(+ 引用它们的 volumeMounts):
# 规则1(两模式通用):三个代码目录变量(CLAW_CODE_PATH/RUNTIME_CODE_PATH/CORE_CODE_PATH)任一为空,
#   引用该变量的 hostPath 路径会变坏、不能挂载,一并剔除。映射(按 hostPath path 引用):
#   CLAW_CODE_PATH    -> hp-code
#   RUNTIME_CODE_PATH -> hp-rt-foundation, hp-rt-management
#   CORE_CODE_PATH    -> hp-openjiuwen
# 规则2(仅 product):agentserver 镜像内置代码,4 个 agentserver 代码挂载,1 个 jiuwenbox代码挂载
#   (hp-code/hp-rt-foundation/hp-rt-management/hp-openjiuwen)无论变量是否为空都删;
#   hp-cgroup(sidecar 系统路径)始终保留。
# 用法: drop_hostpath_volumes <json_file>
drop_hostpath_volumes() {
    local json_file="$1"
    local drop_names=()
    if [[ -z "${DEPLOY_VARS["CLAW_CODE_PATH"]:-}" ]]; then
        drop_names+=(hp-code)
    fi
    if [[ -z "${DEPLOY_VARS["RUNTIME_CODE_PATH"]:-}" ]]; then
        drop_names+=(hp-rt-foundation hp-rt-management)
    fi
    if [[ -z "${DEPLOY_VARS["CORE_CODE_PATH"]:-}" ]]; then
        drop_names+=(hp-openjiuwen)
    fi
    if [[ "${DEPLOY_VARS["MODE"]}" == "product" ]]; then
        drop_names+=(hp-code hp-rt-foundation hp-rt-management hp-openjiuwen)
    fi

    if [ "${#drop_names[@]}" -gt 0 ]; then
        local jq_drop
        jq_drop="$(printf '%s\n' "${drop_names[@]}" | jq -R . | jq -s .)"
        jq --argjson drop "${jq_drop}" \
            ".rawdata.templates[].volumes |= map(select(.name as \$n | \$drop | map(. == \$n) | any | not))" \
            "${json_file}" > "${json_file}.tmp"
        jq '(.rawdata.templates[0].volumes | map(.name)) as $keep
            | .rawdata.containers[].volumeMounts |= map(select(.name as $n | $keep | map(. == $n) | any))' \
            "${json_file}.tmp" > "${json_file}"
        rm -f "${json_file}.tmp"
    fi
}

render_patch_file() {
    local json_template="${CONFIG["AS_JSON_TEMPLATE_FILE"]}"
    local json_file="${CONFIG["AS_JSON_FILE"]}"

    info "Rendering AgentServer templates"
    render_config_template "${json_template}" "${json_file}" "DEPLOY_VARS"

    if ! jq . "${json_file}" >/dev/null 2>&1; then
        error "AgentServer JSON rendering failed, invalid JSON format: ${json_file}"
    fi

    inject_data_volume "${json_file}"
    drop_hostpath_volumes "${json_file}"

    success "AgentServer configuration rendered"
}



install_agentserver_patch() {
    local runtime_port="${DEPLOY_VARS["AGENT_RUNTIME_NODE_PORT"]}"
    local host="${DEPLOY_VARS["CURRENT_NODE_IP"]:-127.0.0.1}"
    local json_file="${CONFIG["AS_JSON_FILE"]}"

    # 下发agentserver服务配置:先确认 runtime NodePort 真正可达(等 kube-proxy
    # 规则生效),再推 config_sync,并校验 HTTP 2xx + ok==true(不再只看 curl 退出码)。
    wait_http_ready "${host}" "${runtime_port}" "/healthz" "agent-runtime NodePort"
    info "Pushing AgentServer template"
    post_and_validate "http://${host}:${runtime_port}/api/session/config_sync" \
        "@${json_file}" runtime
}

install_model_patch() {
    local gw_node_port="${DEPLOY_VARS["GATEWAY_CONFIG_HTTP_NODE_PORT"]:-}"
    local host="${DEPLOY_VARS["CURRENT_NODE_IP"]:-127.0.0.1}"
    local bot_id="default"
    local agent_template_id="default-agent"
    local model_template_id="default-model"

    # 下发前确认 gateway config NodePort 真正可达(等 kube-proxy 规则生效)。
    # gateway readinessProbe 即 /api/v1/ready on config-http(8775),与下发同端口。
    wait_http_ready "${host}" "${gw_node_port}" "/api/v1/ready" "gateway NodePort"

    # 1. 下发 model_template（数据一：模型凭据）
    info "Pushing model template (template_id=${model_template_id})"
    post_and_validate "http://${host}:${gw_node_port}/api/v1/model-templates" \
        "{
            \"template_id\":\"${model_template_id}\",
            \"template_name\":\"${DEPLOY_VARS["MODEL_NAME"]} default\",
            \"description\":\"auto-pushed\",
            \"model_type\":[\"default\"],
            \"api_base\":\"${DEPLOY_VARS["API_BASE"]}\",
            \"api_key\":\"${DEPLOY_VARS["API_KEY"]}\",
            \"model_id\":\"${DEPLOY_VARS["MODEL_NAME"]}\",
            \"model_provider\":\"${DEPLOY_VARS["MODEL_PROVIDER"]}\",
            \"timeout\":1800,
            \"retry_count\":3,
            \"enable_streaming\":true,
            \"enable_function_calling\":true,
            \"verify_ssl\":false,
            \"enabled\":true
        }" gateway

    # 2. 下发 agent_template（数据二：template_ref 指向 model_template）
    info "Pushing agent template (template_id=${agent_template_id}, ref→${model_template_id})"
    post_and_validate "http://${host}:${gw_node_port}/api/v1/agent-templates" \
        "{\"template_id\":\"${agent_template_id}\",\"template_name\":\"default agent\",\"template_ref\":{\"default_model\":[\"${model_template_id}\"]},\"enabled\":true}" \
        gateway

    # 3. 下发 instance_agent_resource（数据三：resource_id=bot_id, ref_template_id 指向 agent_template）
    info "Pushing instance agent resource (resource_id=${bot_id}, ref→${agent_template_id})"
    post_and_validate "http://${host}:${gw_node_port}/api/v1/instance-agent-resources" \
        "{\"resource_id\":\"${bot_id}\",\"ref_template_id\":\"${agent_template_id}\",\"resource_name\":\"default agent\",\"match_expr\":[],\"enabled\":true}" \
        gateway
}

install_patch()
{
    if [[ "${DEPLOY_VARS["RENDER_ONLY"]}" == "true" || "${DEPLOY_VARS["APPLY_PATCH"]}" == "false" ]]; then
        return
    fi

    install_agentserver_patch
    install_model_patch
}

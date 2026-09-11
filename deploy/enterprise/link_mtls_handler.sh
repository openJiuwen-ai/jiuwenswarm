#!/usr/bin/env bash
# Shell owns deployment/Kubernetes orchestration; the packaged foundation
# module owns PKI/database operations. Never trace private stdin/stdout.

link_mtls_check() {
    local mode="${DEPLOY_VARS[JIUWENSWARM_LINK_MTLS_MODE]:-off}" module imports
    case "$mode" in off|observe|enforce) ;; *) error 'JIUWENSWARM_LINK_MTLS_MODE must be off, observe or enforce' ;; esac
    [[ "$mode" == enforce && "$CMD" != down ]] || return 0
    for module in "${MODULES[@]}"; do
        case "$module" in GATEWAY|RUNTIME|MANAGER)
            # Import/version check only; never provision during --render-only.
            imports=$(link_mtls_call imports) || error 'Matching Runtime certificate support is required'
            DEPLOY_VARS[LINK_SYNC_CODE]=$(printf '%s' "$imports" | jq -er '.helper_code') || error 'Certificate copy helper is missing'
            return 0
        ;; esac
    done
}

link_mtls_kube() {
    kubectl --request-timeout=30s -n "${DEPLOY_VARS[NAMESPACE]}" "$@"
}

link_mtls_resolve_host() {
    local host="$1" address service namespace service_json
    service=${host%%.*}
    namespace=${DEPLOY_VARS[NAMESPACE]}
    [[ "$host" != *.* ]] || namespace=${host#*.}
    namespace=${namespace%%.*}
    if [[ "$service" =~ ^[a-z0-9][a-z0-9-]*$ && "$namespace" =~ ^[a-z0-9][a-z0-9-]*$ ]] &&
       service_json=$(kubectl --request-timeout=30s -n "$namespace" get service "$service" -o json 2>/dev/null); then
        address=$(printf '%s' "$service_json" | jq -r '.spec.clusterIP // empty') || return 1
        if [[ -z "$address" || "$address" == None ]]; then
            address=$(kubectl --request-timeout=30s -n "$namespace" get endpoints "$service" -o json |
                jq -er '[.subsets[]?.addresses[]?.ip] | if length == 1 then .[0] else error("DB requires one writable endpoint") end') || return 1
        fi
        printf '%s' "$address"
        return
    fi
    address=$(getent ahostsv4 "$host" 2>/dev/null | awk 'NR==1 {print $1}') || true
    [[ -n "$address" ]] || return 1
    printf '%s' "$address"
}

link_mtls_headless() {
    yq -o=json '.headlessService' "${SCRIPT_DIR}/templates/link-mtls.template.yaml" |
        jq --arg name "${DEPLOY_VARS[AGENT_SERVER_NAME]:-jiuwenclaw-agentserver}" \
           --arg ns "${DEPLOY_VARS[NAMESPACE]}" --argjson port "${DEPLOY_VARS[AGENT_SERVER_PORT]}" \
           '.metadata={name:$name,namespace:$ns} | .spec.ports[0] |= (.port=$port | .targetPort=$port)'
}

link_mtls_preflight() {
    local previous
    previous=$(link_mtls_kube get service "${DEPLOY_VARS[AGENT_SERVER_NAME]:-jiuwenclaw-agentserver}" --ignore-not-found -o json) || return 1
    if [[ -n "$previous" ]]; then
        printf '%s' "$previous" | jq -e '.spec.clusterIP == "None" and .spec.selector == {"jiuwenclaw-component":"agentserver"}' >/dev/null || {
            echo 'Existing AgentServer Service conflicts with the certificate binding' >&2; return 1;
        }
    fi
}

# Keep the private worker input deliberately small. DEPLOY_VARS also contains
# unrelated product configuration (including multiline integration settings),
# none of which belongs in the certificate provisioning protocol.
link_mtls_settings_json() (
    set +x
    local key
    local -a keys=(
        JIUWENSWARM_LINK_MTLS_MODE NAMESPACE
        AGENT_RUNTIME_LINK_MTLS_CLUSTER_DOMAIN AGENT_SERVER_NAME
        GATEWAY_NAME AGENT_RUNTIME_NAME GATEWAY_CONFIG_HTTP_PORT AGENT_RUNTIME_PORT
        LINK_DB_CONNECT_HOST DB_TYPE DB_HOST DB_PORT
        GATEWAY_DB_NAME GATEWAY_DB_USER GATEWAY_DB_PASSWORD GATEWAY_PG_SCHEMA
        RUNTIME_DB_NAME RUNTIME_DB_USER RUNTIME_DB_PASSWORD RUNTIME_PG_SCHEMA
        __LINK_REQUEST_BODY
    )
    for key in "${keys[@]}"; do
        [[ -n "${DEPLOY_VARS[$key]+present}" ]] || continue
        printf '%s\0%s\0' "$key" "${DEPLOY_VARS[$key]}"
    done | jq -Rs '
        split("\u0000") |
        if length > 0 and .[-1] == "" then .[:-1] else . end |
        [range(0; length; 2) as $i | {key: .[$i], value: .[$i + 1]}] |
        from_entries'
)

link_mtls_require_database_settings() {
    local key
    for key in \
        GATEWAY_DB_NAME GATEWAY_DB_USER GATEWAY_DB_PASSWORD \
        RUNTIME_DB_NAME RUNTIME_DB_USER RUNTIME_DB_PASSWORD
    do
        if [[ -z "${DEPLOY_VARS[$key]:-}" ]]; then
            echo "$key must be resolved before certificate preparation" >&2
            return 1
        fi
    done
}

link_mtls_require_serialized_database_settings() {
    local settings="$1"
    printf '%s' "$settings" | jq -e '
        . as $settings |
        [
            "GATEWAY_DB_NAME", "GATEWAY_DB_USER", "GATEWAY_DB_PASSWORD",
            "RUNTIME_DB_NAME", "RUNTIME_DB_USER", "RUNTIME_DB_PASSWORD"
        ] as $keys |
        all($keys[]; . as $key |
            ($settings[$key] | type == "string" and length > 0))' >/dev/null || {
        echo 'Serialized mTLS database settings are incomplete' >&2
        return 1
    }
}

link_mtls_worker() (
    set +x
    set -o pipefail
    local action="$1" target="${2:-}" path="${3:-}" existing='{}' role previous endpoint network=host settings
    [[ "${DEPLOY_VARS[JIUWENSWARM_LINK_MTLS_MODE]:-off}" == enforce ]] || return 1
    [[ "$action" == imports ]] || link_mtls_require_database_settings || return 1
    endpoint=${DOCKER_HOST:-}
    if [[ -n "${DOCKER_CONTEXT:-}" || -z "$endpoint" ]]; then
        endpoint=$(docker context inspect --format '{{.Endpoints.docker.Host}}') || return 1
    fi
    [[ "$endpoint" == unix://* ]] || { echo 'mTLS deployment requires local Docker' >&2; return 1; }
    if [[ "$action" == imports ]]; then network=none; else
        DEPLOY_VARS[LINK_DB_CONNECT_HOST]=$(link_mtls_resolve_host "${DEPLOY_VARS[DB_HOST]}") || return 1
    fi
    if [[ "$action" == ensure || "$action" == status ]]; then
        [[ "$action" != ensure ]] || link_mtls_preflight || return 1
        for role in gateway runtime agentserver manager; do
            previous=$(link_mtls_kube get secret "jiuwenswarm-link-$role" --ignore-not-found -o json) || return 1
            existing=$(printf '%s\n%s' "$existing" "${previous:-null}" |
                jq -cs --arg role "$role" '.[0] + {($role):.[1]}') || return 1
        done
    fi
    local -a args=(run --rm -i --user "$(id -u):$(id -g)" --read-only --cap-drop=ALL
        --security-opt=no-new-privileges --tmpfs "/tmp:rw,nosuid,nodev,size=64m"
        --workdir /tmp --entrypoint python --network "$network"
        -e PYTHONDONTWRITEBYTECODE=1 -e OPENJIUWEN_RUNTIME_LOG_FILE=disabled)
    if [[ "${DEPLOY_VARS[MODE]}" == dev ]]; then
        local source="${DEPLOY_VARS[RUNTIME_CODE_PATH]}"
        [[ "$source" == /* && "$source" != *,* && "$source" != *$'\n'* &&
           -f "$source/foundation/openjiuwen_runtime/foundation/security/link_material_deploy.py" ]] || {
            echo 'Matching Runtime foundation source is required' >&2; return 1;
        }
        args+=(--mount "type=bind,src=$source/foundation,dst=/app/link-foundation,readonly" -e PYTHONPATH=/app/link-foundation)
    fi
    args+=("${DEPLOY_VARS[AGENT_RUNTIME_IMAGE]}" -m openjiuwen_runtime.foundation.security.link_material_deploy)
    local connect_host=''
    if [[ "$action" == request || "$action" == wait ]]; then
        local service="${DEPLOY_VARS[GATEWAY_NAME]}"
        [[ "$target" != runtime ]] || service="${DEPLOY_VARS[AGENT_RUNTIME_NAME]}"
        connect_host=$(link_mtls_resolve_host "$service") || return 1
    fi
    settings=$(link_mtls_settings_json) || return 1
    link_mtls_require_serialized_database_settings "$settings" || return 1
    {
        printf '%s\n' "$settings"
        printf '\n%s' "$existing"
    } | jq -cs --arg action "$action" --arg target "$target" --arg path "$path" --arg host "$connect_host" \
        '{settings:.[0],existing:.[1],action:$action,target:$target,request_path:$path,connect_host:$host}' |
        docker "${args[@]}" 2> >(sed -n '/^mTLS deployment stopped: /p' >&2)
)

link_mtls_call() (
    set +x
    set -o pipefail
    local action="$1" result secret current
    result=$(link_mtls_worker "$@") || { echo 'mTLS preparation/request failed; no HTTP fallback' >&2; return 1; }
    if [[ "$action" == ensure ]]; then
        printf '%s' "$result" | jq -e '
            (.summary.mtls_deployment_id | type == "string" and length > 0) and
            ([.secrets[].metadata.name] | sort == ["jiuwenswarm-link-agentserver","jiuwenswarm-link-gateway","jiuwenswarm-link-manager","jiuwenswarm-link-runtime"]) and
            all(.secrets[]; .kind == "Secret" and (.data | type == "object"))' >/dev/null || {
            echo 'Invalid certificate preparation response; refusing installation' >&2; return 1;
        }
        while IFS= read -r secret; do
            # create, never apply: no annotation containing private key copies.
            if ! printf '%s' "$secret" | link_mtls_kube create -f - >/dev/null 2>&1; then
                current=$(link_mtls_kube get secret "$(printf '%s' "$secret" | jq -er '.metadata.name')" -o json) || return 1
                printf '%s\n%s' "$secret" "$current" | jq -se '.[0].data == .[1].data' >/dev/null || {
                    echo 'Secret conflicts with database material; refusing overwrite' >&2; return 1;
                }
            fi
        done < <(printf '%s' "$result" | jq -c '.secrets[]')
        link_mtls_headless | link_mtls_kube apply -f - >/dev/null || return 1
    fi
    printf '%s' "$result" | jq '.summary // .'
)

link_mtls_prepare() {
    [[ "${DEPLOY_VARS[JIUWENSWARM_LINK_MTLS_MODE]:-off}" == enforce && "${DEPLOY_VARS[RENDER_ONLY]}" != true ]] || return 0
    [[ "${DEPLOY_VARS[LINK_PREPARED]:-false}" != true ]] || return 0
    local summary
    summary=$(link_mtls_call ensure) || error 'Certificate preparation failed. No HTTP fallback.'
    DEPLOY_VARS[LINK_PREPARED]=true
    info "mTLS binding ready (DB persisted, ten-year first issuance): ${summary}"
}

link_mtls_request() (
    set +x
    local data="$3"
    [[ "$data" != @* ]] || data=$(<"${data#@}")
    DEPLOY_VARS[__LINK_REQUEST_BODY]="$data"
    link_mtls_call request "$1" "$2"
)

# Render certificate fragments with the same yq/jq toolchain as other handlers.
link_mtls_render() (
    set +x
    set -o pipefail
    local role="$1" file="$2" mode="${DEPLOY_VARS[JIUWENSWARM_LINK_MTLS_MODE]:-off}" temporary
    [[ "$mode" != off ]] || return 0
    temporary=$(mktemp "${file}.link.XXXXXX") || return 1
    trap 'rm -f "$temporary"' EXIT
    yq -o=json '.' "$file" | jq -s \
        --arg role "$role" --arg mode "$mode" --arg dev "${DEPLOY_VARS[MODE]}" \
        --arg source "${DEPLOY_VARS[RUNTIME_CODE_PATH]:-}" --arg claw "${DEPLOY_VARS[CLAW_POD_CODE_PATH]:-/app/jiuwenswarm}" \
        --arg domain "${DEPLOY_VARS[AGENT_RUNTIME_LINK_MTLS_CLUSTER_DOMAIN]:-cluster.local}" \
        --arg headless "${DEPLOY_VARS[AGENT_SERVER_NAME]:-jiuwenclaw-agentserver}" \
        --arg port "$([[ "$role" == gateway ]] && printf '%s' "${DEPLOY_VARS[GATEWAY_CONFIG_HTTP_PORT]}" || printf '%s' "${DEPLOY_VARS[AGENT_RUNTIME_PORT]}")" \
        --arg code "${DEPLOY_VARS[LINK_SYNC_CODE]:-}" \
        --slurpfile parts <(yq -o=json '.' "${SCRIPT_DIR}/templates/link-mtls.template.yaml") '
        def put($v): map(select(.name != $v.name)) + [$v];
        def env($n;$v): .env = ((.env // []) | put({name:$n,value:$v}));
        def source_mount:
            .volumeMounts = ((.volumeMounts // []) | put({name:"link-runtime-source",mountPath:"/app/link-runtime-source",readOnly:true}));
        def python_path($paths): env("PYTHONPATH"; ($paths + [.env[]? | select(.name=="PYTHONPATH") | .value | select(length>0)]) | join(":"));
        def source_volume:
            .volumes = ((.volumes // []) | put({name:"link-runtime-source",hostPath:{path:$source,type:"Directory"}}));
        if $role == "agentserver" then
            if $mode == "enforce" and $dev == "dev" then
                .[0] | .rawdata.templates |= map(source_volume) |
                [.rawdata.templates[].main_container_id] as $ids |
                .rawdata.containers |= map(if (.container_id as $id | $ids | index($id)) != null then
                    source_mount | python_path([$claw,"/app/link-runtime-source/foundation","/app/link-runtime-source/service"])
                    else . end)
            else .[0] end
        else
            map(if .kind != "Deployment" then . else
                .spec.template.spec |= (
                    . as $pod |
                    .containers |= map(if .name != ({gateway:"gateway",runtime:"agent-runtime",manager:"manager"}[$role]) then . else
                        env("JIUWENSWARM_LINK_MTLS_MODE";$mode) end) |
                    if $mode != "enforce" then . else
                        [.containers[] | select(.name==({gateway:"gateway",runtime:"agent-runtime",manager:"manager"}[$role]))] as $targets |
                        if ($targets|length) != 1 then error("ambiguous certificate target container") else . end |
                        $targets[0] as $main |
                        (($main.securityContext.runAsUser // $pod.securityContext.runAsUser) != 0 or (($pod.securityContext.fsGroup // 0) != 0)) as $copy |
                        "jiuwenswarm-link-mtls" as $volume |
                        ($volume + (if $copy then "-source" else "" end)) as $src |
                        ("/etc/jiuwenswarm/link-mtls/"+$role) as $mount |
                        .volumes = ((.volumes // []) | put({name:$src,secret:{secretName:("jiuwenswarm-link-"+$role),defaultMode:(if $copy then 292 else 256 end)}})) |
                        (if $copy then
                            .volumes |= put($parts[0].privateVolume) |
                            if any((.containers + (.initContainers // []))[]; .name=="link-material-init" or .name=="link-material-sync") then error("reserved link helper name") else . end |
                            ($parts[0].helper + {image:$main.image,imagePullPolicy:($main.imagePullPolicy // "IfNotPresent"),
                                command:["python","-c",$code],securityContext:(({runAsUser:($main.securityContext.runAsUser // $pod.securityContext.runAsUser),runAsGroup:($main.securityContext.runAsGroup // $pod.securityContext.runAsGroup)} | with_entries(select(.value != null))) + $parts[0].helper.securityContext)}) as $helper |
                            if $code=="" then error("certificate helper not available") else . end |
                            .initContainers = ((.initContainers // []) + [($helper | .name="link-material-init" | .command += ["--once"])]) |
                            .containers += [($helper | .name="link-material-sync")]
                        else . end) |
                        .containers |= map(if .name != $main.name then . else
                            .volumeMounts = ((.volumeMounts // []) | put({name:$volume,mountPath:$mount,readOnly:true})) |
                            (if $copy then env("JIUWENSWARM_LINK_MTLS_PROFILE";$mount+"/private/identity/profile.json") else . end) |
                            (if $role=="runtime" then env("AGENT_RUNTIME_LINK_MTLS_AGENTSERVER_SECRET";"jiuwenswarm-link-agentserver") | env("AGENT_RUNTIME_LINK_MTLS_HEADLESS_SERVICE";$headless) | env("AGENT_RUNTIME_LINK_MTLS_CLUSTER_DOMAIN";$domain)
                             elif $role=="gateway" then env("GATEWAY_CONFIG_PUBLIC_SCHEME";"https") else . end) |
                            (if $role=="gateway" or $role=="runtime" then
                                ("import urllib.request; from openjiuwen_runtime.foundation.security.link_profile import load_service_identity; p=load_service_identity(\u0027"+$role+"\u0027); o=urllib.request.build_opener(urllib.request.ProxyHandler({}),urllib.request.HTTPSHandler(context=p.ssl_context())); r=o.open(\u0027https://127.0.0.1:"+$port+(if $role=="gateway" then "/api/v1/ready" else "/healthz" end)+"\u0027,timeout=3); assert r.status==200") as $probe |
                                reduce ["readinessProbe","livenessProbe","startupProbe"][] as $p (. ; if has($p) then .[$p] |= (del(.httpGet,.tcpSocket,.grpc) | .exec={command:["python","-c",$probe]} | .timeoutSeconds=([(.timeoutSeconds // 1),5]|max)) else . end)
                             else . end) |
                            (if $dev=="dev" then source_mount | python_path(["/app/link-runtime-source/foundation","/app/link-runtime-source/service",(if $role=="runtime" then "/app/link-runtime-source/applications/agent_runtime/src" elif $role=="manager" then "/app/link-runtime-source/applications/manager/manager_server/src" else $claw end)]) else . end)
                        end) | if $dev=="dev" then source_volume else . end
                    end)
            end)[]
        end' | yq -p=json -P '.' > "$temporary" || return 1
    if [[ "$role" == agentserver ]]; then yq -o=json '.' "$temporary" > "$file"; else mv -f "$temporary" "$file"; fi
)

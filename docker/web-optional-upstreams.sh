#!/bin/sh
# 按 env 生成 User Web 可选自定义上游(/idp、/manager-api)的 location 片段。
#
# Missing targets only affect requests that need those services.
#
# Enterprise service names are resolved at request time so Web can start before Manager.
# Personal deployments retain the existing optional upstreams.
#
# 由 nginx 官方镜像 entrypoint 在 envsubst(20-envsubst)之后执行,
# 片段经 /etc/nginx/conf.d 主配置内的 include(位于 server 块中)生效;
# 代理通用参数(Accept-Encoding 透传、http/1.1、超时)继承主配置 server 级设置。

CONF=/etc/nginx/optional-upstreams/user-web-custom.conf
: > "$CONF"

# Each edition has exactly one listener on the existing Web service port.
port=${WEB_HTTP_PORT:-5173}
case "$port" in ''|*[!0-9]*) echo "Invalid WEB_HTTP_PORT" >&2; exit 1;; esac
conf_dir=/etc/nginx/optional-upstreams
: > "$conf_dir/user-web-redirect.inc" || exit 1
if [ "${JIUWENSWARM_EDITION:-personal}" = enterprise ]; then
    cat > "$conf_dir/user-web-listen.inc" <<EOF_LISTEN || exit 1
listen $port ssl;
http2 on;
ssl_protocols TLSv1.2 TLSv1.3;
ssl_certificate /etc/nginx/web-tls/tls.crt;
ssl_certificate_key /etc/nginx/web-tls/tls.key;
proxy_set_header X-Forwarded-Proto \$scheme;
proxy_set_header X-Forwarded-Host \$http_host;
EOF_LISTEN
    echo 'absolute_redirect off;' > "$conf_dir/user-web-redirect.inc" || exit 1
else
    printf 'listen %s;\n' "$port" > "$conf_dir/user-web-listen.inc" || exit 1
fi

qualify_service_target() {
    # nginx's resolver does not apply the Pod's DNS search suffix.
    case "$1" in
        http://localhost:*|https://localhost:*|http://localhost|https://localhost)
            printf '%s\n' "$1";;
        *)
            printf '%s\n' "$1" | sed -E "s@^(https?://)([[:alnum:]-]+)([:/]|$)@\1\2.$service_domain\3@";;
    esac
}

web_surface_header=
idp_target=${USER_WEB_IDP_TARGET:-}
if [ "${JIUWENSWARM_EDITION:-personal}" = "enterprise" ]; then
    dns_resolver=${USER_WEB_RESOLVER:-}
    if [ -z "$dns_resolver" ] || [ "$dns_resolver" = "auto" ]; then
        dns_resolver=$(awk '/^nameserver[[:space:]]/ { print $2; exit }' /etc/resolv.conf)
        case "$dns_resolver" in *:*) dns_resolver="[$dns_resolver]";; esac
    fi
    if [ -n "$dns_resolver" ]; then
        printf 'resolver %s valid=10s ipv6=off;\n' "$dns_resolver" >> "$CONF"
    fi
    manager_target=${USER_WEB_MANAGER_TARGET:-}
    service_domain=$(awk '$1 == "search" {
        for (i = 2; i <= NF; i++) {
            if ($i ~ /^[^.]+\.svc\./) { print $i; exit }
        }
    }' /etc/resolv.conf)
    backend_target=${USER_WEB_BACKEND_TARGET:-}
    if [ -n "$service_domain" ]; then
        idp_target=$(qualify_service_target "$idp_target")
        manager_target=$(qualify_service_target "$manager_target")
        backend_target=$(qualify_service_target "$backend_target")
    fi
    if [ -n "$backend_target" ]; then
        printf 'set $user_backend_target "%s";\n' "${backend_target%/}" >> "$CONF"
        cat >> "$CONF" <<'EOF_BACKEND'
location ^~ /idp/ {
    proxy_pass $user_backend_target;
}
location ^~ /manager-api/v1/user-console/ {
    rewrite ^/manager-api/(.*)$ /api/$1 break;
    proxy_pass $user_backend_target;
}
location = /manager-api { return 404; }
location ^~ /manager-api/ { return 404; }
location = /file-api { return 307 /file-api/; }
location = /share-api { return 307 /share-api/; }
EOF_BACKEND
        for file in user-http.inc user-webchannel-http.inc user-ws.inc; do
            printf 'proxy_pass $user_backend_target;\n' > "/etc/nginx/optional-upstreams/$file"
        done
        # The WebSocket locations set Upgrade locally and do not inherit server headers.
        printf '%s\n' 'proxy_set_header X-Forwarded-Proto $scheme;' \
            'proxy_set_header X-Forwarded-Host $http_host;' >> /etc/nginx/optional-upstreams/user-ws.inc
        exit 0
    fi
    cat >> "$CONF" <<EOF_TARGETS
set \$user_idp_target "${idp_target%/}";
set \$user_manager_target "${manager_target%/}";
EOF_TARGETS
    if [ -n "$idp_target" ]; then
        idp_target='$user_idp_target'
        web_surface_header="proxy_set_header X-Jiuwen-Web-Surface user;"
    fi
fi

if [ -n "$idp_target" ]; then
    cat >> "$CONF" <<EOF
location = /idp {
    rewrite ^ / break;
    ${web_surface_header}
    proxy_pass ${idp_target};
}
location ^~ /idp/ {
    rewrite ^/idp/(.*)\$ /\$1 break;
    ${web_surface_header}
    proxy_pass ${idp_target};
}
EOF
else
    cat >> "$CONF" <<'EOF'
location = /idp { return 502; }
location ^~ /idp/ { return 502; }
EOF
fi

if [ "${JIUWENSWARM_EDITION:-personal}" = "enterprise" ]; then
    manager_proxy='return 503;'
    if [ -n "$manager_target" ]; then
        manager_proxy='rewrite ^/manager-api/(.*)$ /api/$1 break;
    proxy_pass $user_manager_target;'
    fi
    cat >> "$CONF" <<EOF_MANAGER
location ^~ /manager-api/v1/user-console/ {
    ${manager_proxy}
}
location = /manager-api { return 404; }
location ^~ /manager-api/ { return 404; }
location = /file-api { return 307 /file-api/; }
location = /share-api { return 307 /share-api/; }
EOF_MANAGER
elif [ -n "$USER_WEB_MANAGER_TARGET" ]; then
    cat >> "$CONF" <<EOF
location = /manager-api {
    rewrite ^ /api break;
    proxy_pass ${USER_WEB_MANAGER_TARGET};
}
location ^~ /manager-api/ {
    rewrite ^/manager-api/(.*)\$ /api/\$1 break;
    proxy_pass ${USER_WEB_MANAGER_TARGET};
}
EOF
else
    cat >> "$CONF" <<'EOF'
location = /manager-api { return 502; }
location ^~ /manager-api/ { return 502; }
EOF
fi

# The enterprise origin forwards business requests only after the shared
# Manager verifier has accepted the user session and selected its instance.
HTTP_CONF=/etc/nginx/optional-upstreams/user-http.inc
WEBCHANNEL_HTTP_CONF=/etc/nginx/optional-upstreams/user-webchannel-http.inc
WS_CONF=/etc/nginx/optional-upstreams/user-ws.inc
if [ "${JIUWENSWARM_EDITION:-personal}" = "enterprise" ]; then
    if [ -z "$manager_target" ]; then
        printf 'return 503;\n' > "$HTTP_CONF"
        printf 'return 503;\n' > "$WEBCHANNEL_HTTP_CONF"
        printf 'return 503;\n' > "$WS_CONF"
        exit 0
    fi
    cat >> "$CONF" <<EOF_AUTH
location = /_resolve_user_face {
    internal;
    proxy_pass \$user_manager_target/api/v1/user-console/user-face-upstream;
    proxy_pass_request_body off;
    proxy_set_header Content-Length "";
    proxy_set_header Authorization \$user_authz;
    proxy_set_header Cookie \$http_cookie;
}
location @user_auth401 {
    default_type application/json;
    return 401 '{"detail":"Not authenticated"}';
}
EOF_AUTH
    cat > "$HTTP_CONF" <<'EOF_HTTP'
auth_request /_resolve_user_face;
auth_request_set $gateway_http $upstream_http_x_gateway_http_upstream;
error_page 401 = @user_auth401;
proxy_pass $gateway_http;
EOF_HTTP
    printf 'include %s;\n' "$HTTP_CONF" > "$WEBCHANNEL_HTTP_CONF"
    cat > "$WS_CONF" <<'EOF_WS'
auth_request /_resolve_user_face;
auth_request_set $gateway_ws $upstream_http_x_gateway_ws_upstream;
error_page 401 = @user_auth401;
proxy_pass $gateway_ws;
EOF_WS
else
    printf 'proxy_pass %s;\n' "$GATEWAY_WEB_HTTP_URL" > "$HTTP_CONF"
    printf 'proxy_pass %s;\n' "$GATEWAY_URL" > "$WEBCHANNEL_HTTP_CONF"
    printf 'proxy_pass %s;\n' "$GATEWAY_URL" > "$WS_CONF"
fi

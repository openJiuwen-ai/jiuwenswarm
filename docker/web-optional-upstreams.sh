#!/bin/sh
# 按 env 生成 User Web 可选自定义上游(/idp、/manager-api)的 location 片段。
#
# 语义对齐 jiuwenswarm/channels/web/app_web.py 的 _dispatch_proxy/_proxy_named_http:
#   - /idp   精确 → 上游 /;      /idp/x   → 上游 /x(前缀剥离)
#   - /manager-api 精确 → 上游 /api;/manager-api/x → 上游 /api/x
#   - target 未设置/为空 → 502 "proxy target not configured"(逐请求,Python 同款)
#   - 查询串由 nginx 自动透传;所有方法均代理
#
# 这两个上游是客户自定义钩子(自定义认证中心/管理面),不参与产品默认接线:
#   - env 未设置或为空
#     → 生成 502 兜底 location,保证 nginx 可启动
#   - env 配置了自定义地址 → 生成反向代理 location(域名在 nginx 启动时解析)
#
# 由 nginx 官方镜像 entrypoint 在 envsubst(20-envsubst)之后执行,
# 片段经 /etc/nginx/conf.d 主配置内的 include(位于 server 块中)生效;
# 代理通用参数(Accept-Encoding 透传、http/1.1、超时)继承主配置 server 级设置。

CONF=/etc/nginx/optional-upstreams/user-web-custom.conf
: > "$CONF"

if [ -n "$USER_WEB_IDP_TARGET" ]; then
    cat >> "$CONF" <<EOF
location = /idp {
    rewrite ^ / break;
    proxy_pass ${USER_WEB_IDP_TARGET};
}
location ^~ /idp/ {
    rewrite ^/idp/(.*)\$ /\$1 break;
    proxy_pass ${USER_WEB_IDP_TARGET};
}
EOF
else
    cat >> "$CONF" <<'EOF'
location = /idp { return 502; }
location ^~ /idp/ { return 502; }
EOF
fi

if [ -n "$USER_WEB_MANAGER_TARGET" ]; then
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

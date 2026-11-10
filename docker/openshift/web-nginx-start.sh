#!/bin/bash
# 启动 nginx:
#   1) /etc/nginx/templates/*.template 经 envsubst 渲染至 /opt/app-root/etc/nginx.d/
#      (主配置 http 块 include);仅替换环境中已定义的 ${VAR},$http_upgrade 等
#      nginx 变量保持原样,模板要求的变量须由部署期提供
#   2) 依次执行 /docker-entrypoint.d/*.sh
#   3) exec /usr/libexec/s2i/run 前台启动 nginx,日志输出至 stdout/stderr
set -euo pipefail

TPL_DIR=/etc/nginx/templates
OUT_DIR=/opt/app-root/etc/nginx.d

mkdir -p "$OUT_DIR"
for tpl in "$TPL_DIR"/*.template; do
    [ -e "$tpl" ] || continue
    out="$OUT_DIR/$(basename "$tpl" .template)"
    # 仅保留环境中已定义的变量(gettext 0.21 的 --variables 不支持 stdin)
    defined_envs=$(envsubst --variables "$(cat "$tpl")" \
        | while IFS= read -r v; do
              eval "[ \${$v+set} ]" && printf '${%s} ' "$v" || true
          done)
    envsubst "$defined_envs" < "$tpl" > "$out"
done

for script in /docker-entrypoint.d/*.sh; do
    [ -e "$script" ] || continue
    if [ -x "$script" ]; then "$script"; else . "$script"; fi
done

exec /usr/libexec/s2i/run

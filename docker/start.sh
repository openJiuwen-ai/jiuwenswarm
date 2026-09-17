#!/usr/bin/env bash
# 容器启动入口：根据 ROLE 环境变量初始化 workspace 并启动对应服务。
#   ROLE=gateway     -> jiuwenswarm-init + jiuwenswarm-gateway
#   ROLE=agentserver -> jiuwenswarm-init + jiuwenswarm-agentserver
#   其他/未设置      -> 打印提示后退出
echo "[start.sh] ROLE='${ROLE}', JIUWENSWARM_EDITION='${JIUWENSWARM_EDITION}'"
case "${ROLE}" in
    gateway)
        echo "[start.sh] initializing workspace"
        jiuwenswarm-init
        echo "[start.sh] starting jiuwenswarm-gateway"
        exec jiuwenswarm-gateway
        ;;
    agentserver)
        echo "[start.sh] initializing workspace"
        jiuwenswarm-init
        echo "[start.sh] starting jiuwenswarm-agentserver"
        exec jiuwenswarm-agentserver
        ;;
    *)
        echo "[start.sh] ROLE not set or unknown ('${ROLE}'), nothing to do."
        exit 0
        ;;
esac

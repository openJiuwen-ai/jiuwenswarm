#!/usr/bin/env bash
# 容器启动入口（仅由 agentserver 容器调用）：
#   生成企业版配置 → 初始化 workspace → 启动 agentserver
# JIUWENSWARM_CONFIG_DIR 等环境变量由 k8s envFrom 在进程启动前注入，
# 到本脚本执行时已就绪。

# 新建文件/目录保持「同组可写」(目录 0775 / 文件 0664)，由本脚本的全部子进程
# 继承（update_conf.py、agentserver 及其 Worker）。
# 原因：domain 分区是多副本共享的 PVC，而 OpenShift restricted-v2 SCC 按
# namespace 分配随机 uid、进程以 gid 0 运行。默认 umask 022 建出的 0755/0644
# 只有属主可写，换一个 uid 的 Pod（重建、换 namespace）就只能读不能改，
# 共享工作区会随时间逐步变为只读。umask 0002 让同属 gid 0 的后继 Pod 始终可写。
# 对固定 uid 的通用 k8s 部署无副作用（组即 app 自身的组）。
umask 0002

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
echo "[start.sh] JIUWENSWARM_EDITION='${JIUWENSWARM_EDITION}'"

# 生成企业版配置（app 身份执行 → 覆盖上一步稀疏版；失败即退出，不 startup）
echo "[start.sh] generating config via update_conf.py"
echo "[start.sh] JIUWENSWARM_CONFIG_DIR='${JIUWENSWARM_CONFIG_DIR}'"
# 用绝对路径调用：UBI 镜像的 BASH_ENV 会激活 /opt/app-root venv，
# 裸 python3 会命中 venv（无业务依赖）；/usr/local/bin/python3 恒为系统 3.11
if ! /usr/local/bin/python3 "${SCRIPT_DIR}/update_conf.py"; then
    echo "[start.sh] update_conf.py FAILED" >&2
    exit 1
fi

echo "[start.sh] starting jiuwenswarm-agentserver"
exec "jiuwenswarm-agentserver"

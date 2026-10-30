#!/bin/sh
# jiuwenbox 沙箱容器 entrypoint。
#
# 背景:OCI 运行时(runc/containerd)默认把 /proc/sys 以只读方式挂载,
# 而 isolated 网络模式的沙箱创建(jiuwenbox supervisor/network.py 的
# setup_network_uplink)第一步就要写 /proc/sys/net/ipv4/ip_forward。
# 容器已具备 SYS_ADMIN(capabilities_add),启动时 remount 为 rw 即可,
# 无需 privileged。
#
# remount 失败仅告警不阻断:服务照常启动,退化为"沙箱创建失败但
# box-server 可用"的原状态,便于区分"网络 setup 失败"与"服务起不来"。
#
# exec "$@" 保持 launcher 为 PID 1,保留 jiuwenbox 的
# PR_SET_CHILD_SUBREAPER / SIGCHLD 兜底逻辑。
mount -o remount,rw /proc/sys 2>/dev/null || \
    echo '[jiuwenbox-entrypoint] WARN: failed to remount /proc/sys rw, sandbox network setup may fail'
exec "$@"

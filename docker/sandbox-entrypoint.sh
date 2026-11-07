#!/bin/sh
# jiuwenbox 沙箱容器 entrypoint。

# 沙箱启动时要读 /home/app 下的文件（bind 源路径）。沙箱里的 bwrap 开了
# user namespace，root 的特权对这个目录不生效，只能按普通权限检查——
# 所以 /home/app 必须允许其他人进入（x 位），这里统一改回 755。
chmod 755 /home/app 2>/dev/null || \
    echo '[jiuwenbox-entrypoint] WARN: failed to chmod 755 /home/app, sandbox bind source traversal may fail'

# isolated 网络模式的沙箱创建（supervisor/network.py 的 setup_network_uplink）
# 第一步要写 /proc/sys/net/ipv4/ip_forward，而 OCI 运行时默认把 /proc/sys 挂成只读。
# 容器已具备 SYS_ADMIN（capabilities_add），启动时 remount 为 rw 即可，无需 privileged。
mount -o remount,rw /proc/sys 2>/dev/null || \
    echo '[jiuwenbox-entrypoint] WARN: failed to remount /proc/sys rw, sandbox network setup may fail'

# exec "$@" 使 launcher 成为 PID 1，保留 jiuwenbox 的
# PR_SET_CHILD_SUBREAPER / SIGCHLD 兜底逻辑。
exec "$@"

#!/bin/sh
# jiuwenbox 沙箱容器入口。

# bwrap 进入 user namespace 之后，访问 /home/app 只检查文件权限，root 不能绕过。
# openEuler 的 useradd 受 UMASK 077 影响，家目录是 0700，其他人进不去。
# 改成 0755，沙箱才能遍历作为 bind 源的这个目录。失败只告警，不阻止启动。
chmod 755 /home/app 2>/dev/null || \
    echo '[jiuwenbox-entrypoint] WARN: failed to chmod 755 /home/app, sandbox bind source traversal may fail'

# 容器运行时默认把 /proc/sys 挂成只读。isolated 网络建立 uplink 时要写
# /proc/sys/net/ipv4/ip_forward。容器已有 SYS_ADMIN，这里把它重新挂成可写。
# 失败只告警：服务继续启动，沙箱网络若因此失败会在创建时报错。
mount -o remount,rw /proc/sys 2>/dev/null || \
    echo '[jiuwenbox-entrypoint] WARN: failed to remount /proc/sys rw, sandbox network setup may fail'

# exec 让后面的进程成为 PID 1，jiuwenbox 才能回收自己拉起的子进程。
exec "$@"

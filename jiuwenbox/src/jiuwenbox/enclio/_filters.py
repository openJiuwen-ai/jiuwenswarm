# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Fixed transport filter for the host-side proxy."""

import ctypes
import errno
import functools
import os
import platform
import socket

from ._linux import PR_SET_NO_NEW_PRIVS, prctl


class _Comparison(ctypes.Structure):
    _fields_ = [
        ("arg", ctypes.c_uint), ("op", ctypes.c_uint),
        ("datum_a", ctypes.c_uint64), ("datum_b", ctypes.c_uint64),
    ]


@functools.lru_cache(maxsize=1)
def _libseccomp():
    library = ctypes.CDLL("libseccomp.so.2")
    library.seccomp_init.argtypes = [ctypes.c_uint32]
    library.seccomp_init.restype = ctypes.c_void_p
    library.seccomp_release.argtypes = [ctypes.c_void_p]
    library.seccomp_load.argtypes = [ctypes.c_void_p]
    library.seccomp_attr_set.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint32]
    library.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    library.seccomp_rule_add_array.argtypes = [
        ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint, ctypes.POINTER(_Comparison),
    ]
    return library


def _eq(arg: int, value: int) -> _Comparison:
    return _Comparison(arg, 4, value, 0)


def _masked(arg: int, mask: int, value: int) -> _Comparison:
    return _Comparison(arg, 7, mask, value)


_PROXY_DENY = """
execve execveat mount umount2 pivot_root chroot unshare setns ptrace process_vm_readv
process_vm_writev pidfd_getfd pidfd_send_signal bpf perf_event_open userfaultfd
keyctl add_key request_key io_uring_setup io_uring_enter io_uring_register
open_by_handle_at name_to_handle_at kexec_load kexec_file_load reboot init_module
finit_module delete_module swapon swapoff fsopen fsconfig fsmount move_mount open_tree
mount_setattr creat openat2 unlink unlinkat rename renameat renameat2 mkdir mkdirat
rmdir link linkat symlink symlinkat mknod mknodat truncate ftruncate chmod fchmod
fchmodat fchmodat2 chown fchown lchown fchownat utime utimes utimensat futimesat
setxattr lsetxattr fsetxattr removexattr lremovexattr fremovexattr fanotify_init
acct quotactl ioctl kill tkill tgkill rt_sigqueueinfo rt_tgsigqueueinfo
setuid setgid setreuid setregid setresuid setresgid setfsuid setfsgid setgroups
pidfd_open open_tree fspick process_madvise process_mrelease
"""


def install_proxy_filter() -> None:
    # 给proxy安装seccomp过滤器，防止被漏洞利用
    # 1. 无条件拒绝的系统调用 _PROXY_DENY
    # 2. 限制 clone3 参数，不支持 CLONE_NEWxxxx，传入namespace标志
    # 3. 限制 open/openat 参数，只支持 O_RDONLY
    # 4. 限制 socket/socketpair 参数，只支持地址族 AF_UNIX/AF_INET/AF_INET6/AF_NETLINK
    # 5. 设置 NoNewPrivs
    lib = _libseccomp()
    allow = 0x7FFF0000
    denied = 0x00050000 | errno.EPERM
    context = lib.seccomp_init(allow)
    if not context:
        raise OSError("seccomp_init")

    def rule(name: str, action: int, *conditions: _Comparison) -> None:
        nr = lib.seccomp_syscall_resolve_name(name.encode("ascii"))
        if nr == -1 and name == "fchmodat2" and platform.machine() in ("aarch64", "x86_64"):
            nr = 452
        if nr >= 0:
            rules = (_Comparison * len(conditions))(*conditions)
            if lib.seccomp_rule_add_array(context, action, nr, len(rules), rules) < 0:
                raise OSError("seccomp_rule")

    try:
        if lib.seccomp_attr_set(context, 2, 0x80000000) < 0:  # Kill a foreign ABI.
            raise OSError("seccomp_arch")
        for name in set(_PROXY_DENY.split()):
            rule(name, denied)
        rule("clone3", 0x00050000 | errno.ENOSYS)
        for bit in (1 << n for n in range(32) if 0x7e020080 & (1 << n)):
            rule("clone", denied, _masked(0, bit, bit))
        for name, arg in (("open", 1), ("openat", 2)):
            for value in (os.O_WRONLY, os.O_RDWR):
                rule(name, denied, _masked(arg, os.O_ACCMODE, value))
            for bit in (os.O_CREAT, os.O_TRUNC, os.O_TMPFILE & ~os.O_DIRECTORY):
                rule(name, denied, _masked(arg, bit, bit))
        families = (socket.AF_UNIX, socket.AF_INET, socket.AF_INET6, socket.AF_NETLINK)
        for name in ("socket", "socketpair"):
            for family in set(range(46)) - set(families):
                rule(name, denied, _eq(0, family))
            rule(name, denied, _Comparison(0, 5, 46, 0))  # SCMP_CMP_GE
        for family in (socket.AF_INET, socket.AF_INET6):
            rule("socket", denied, _eq(0, family), _masked(1, 0xF, socket.SOCK_RAW))
        prctl(PR_SET_NO_NEW_PRIVS, 1)
        if lib.seccomp_load(context) < 0:
            raise OSError("seccomp_load")
    finally:
        lib.seccomp_release(context)

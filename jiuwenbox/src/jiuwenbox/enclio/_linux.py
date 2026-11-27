# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Linux bootstrap primitives shared with the isolated PID 1."""

import array
import ctypes
import fcntl
import json
import os
import select
import signal
import socket

LIBC = ctypes.CDLL(None, use_errno=True)
PR_SET_PDEATHSIG = 1
PR_SET_DUMPABLE = 4
PR_SET_CHILD_SUBREAPER = 36
PR_SET_NO_NEW_PRIVS = 38
PROXY_PORT = 18080
BOOTSTRAP_ENV = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}
_BOOTSTRAP_LIMIT = 4 * 1024 * 1024


def prctl(option: int, value: int) -> None:
    if LIBC.prctl(option, value, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "prctl")


def parent_death(parent: int) -> None:
    # 防孤儿进程：只要父进程退出，就给子进程发 SIGKILL
    prctl(PR_SET_PDEATHSIG, signal.SIGKILL)
    if os.getppid() != parent:
        getattr(os, "_exit")(125)


def close_extra(*keep: int) -> None:
    for name in os.listdir("/proc/self/fd"):
        fd = int(name)
        if fd > 2 and fd not in keep:
            try:
                os.close(fd)
            except OSError:
                pass


def anonymous_file(value: bytes) -> int:
    fd = os.memfd_create("enclio", os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING)
    try:
        with os.fdopen(os.dup(fd), "wb") as file:
            file.write(value)
        os.lseek(fd, 0, os.SEEK_SET)
        fcntl.fcntl(fd, fcntl.F_ADD_SEALS, (
            fcntl.F_SEAL_WRITE | fcntl.F_SEAL_GROW | fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_SEAL
        ))
    except BaseException:
        os.close(fd)
        raise
    return fd


def pack(data: dict) -> int:
    return anonymous_file(json.dumps(data).encode())


def unpack(fd: int) -> dict:
    with os.fdopen(fd, "rb") as file:
        data = file.read(_BOOTSTRAP_LIMIT + 1)
    if len(data) > _BOOTSTRAP_LIMIT:
        raise ValueError("bootstrap_budget")
    return json.loads(data)


def socket_pair() -> tuple[socket.socket, socket.socket]:
    return socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)


def send_fd(channel: socket.socket, message: bytes, fd: int) -> None:
    # 把句柄fd共享给对端
    if channel.sendmsg([message], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", [fd]))]) != len(message):
        raise OSError("short control packet")


def receive_fd(channel: socket.socket, message: bytes) -> int:
    item_size = array.array("i").itemsize
    payload, ancillary, flags, _ = channel.recvmsg(
        len(message), socket.CMSG_SPACE(2 * item_size), socket.MSG_CMSG_CLOEXEC,
    )
    fds = []
    try:
        for level, kind, raw in ancillary:
            if (level, kind) != (socket.SOL_SOCKET, socket.SCM_RIGHTS):
                raise ValueError("unexpected_control_data")
            values = array.array("i")
            values.frombytes(raw[:len(raw) - len(raw) % item_size])
            fds.extend(values)
        if payload != message or flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC) or len(fds) != 1:
            raise ValueError("invalid_descriptor_packet")
        return fds.pop()
    finally:
        for fd in fds:
            os.close(fd)


def pidfd_alive(fd: int) -> bool:
    return not select.select([fd], [], [], 0)[0]


def kill_pidfd(fd: int) -> None:
    try:
        signal.pidfd_send_signal(fd, signal.SIGKILL)
    except ProcessLookupError:
        pass


def close_stdio() -> None:
    fd = os.open("/dev/null", os.O_RDWR, mode=0o600)
    for target in range(3):
        os.dup2(fd, target)
    if fd > 2:
        os.close(fd)



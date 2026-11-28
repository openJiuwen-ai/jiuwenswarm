# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Trusted PID 1 launched inside bubblewrap."""

import errno
import importlib.util
import mmap
import os
import resource
import select
import signal
import socket
import sys

_spec = importlib.util.spec_from_file_location("_linux", os.path.join(os.path.dirname(__file__), "_linux.py"))
if _spec is None or _spec.loader is None:
    raise ImportError("trusted Linux bootstrap is unavailable")
_linux = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_linux)
PROXY_PORT, PR_SET_DUMPABLE = _linux.PROXY_PORT, _linux.PR_SET_DUMPABLE
close_extra, prctl, send_fd, unpack = _linux.close_extra, _linux.prctl, _linux.send_fd, _linux.unpack


def _verify_view(payload: dict) -> None:
    # init pid需要是1的原因：
    # 1. 接管工具派生的子进程，init等待回收子进程
    # 2. 当supervisor主动终止init时，可以向同命名空间内的其余进程发送SIGKILL，终止整个沙箱进程树
    if os.getpid() != 1 or (os.getuid(), os.getgid()) != tuple(payload["identity"]):
        raise ValueError("invalid_namespace_identity")
    with open("/proc/self/status", encoding="ascii") as file:
        status = dict(line.split(":", 1) for line in file if ":" in line)
    if any(int(status[field], 16) for field in ("CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb")):
        raise ValueError("unexpected_capabilities")
    if int(status["NoNewPrivs"]) != 1:
        raise ValueError("no_new_privs_absent")


def _handoff(fd: int, run_id: str, timeout: float) -> None:
    with socket.socket(fileno=fd) as channel, socket.socket() as listener:
        channel.settimeout(timeout)
        listener.bind(("127.0.0.1", PROXY_PORT))
        listener.listen(32)
        send_fd(channel, b"EL1/" + run_id.encode("ascii"), listener.fileno())
        if channel.recv(2) != b"A":
            raise ValueError("invalid_proxy_ack")


def _target(payload: dict, state: mmap.mmap, handshake: int) -> None:
    try:
        os.set_inheritable(handshake, False)
        close_extra(handshake)
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)
        command = payload["command"]
        argv, env = command["argv"], payload["env"]
        # BPF is already inherited from bubblewrap. The shared page and
        # close-on-exec pipe distinguish an attempted exec from setup.
        state[0] = 1
        os.execvpe(argv[0], argv, env)
    except BaseException:
        state[0] = 2
        try:
            os.write(handshake, b"E")
        except OSError:
            pass
    getattr(os, "_exit")(125)


def _run(payload: dict, channel: socket.socket) -> None:
    _verify_view(payload)
    try:
        os.chdir(payload["command"]["cwd"])
    except OSError as exc:
        # Report only chdir failures here; namespace/filter failures stay fatal.
        try:
            os.write(2, f"failed to start command: {exc}\n".encode("utf-8", "replace"))
        except OSError:
            # A policy denying write must not lose the structured cwd failure.
            pass
        channel.send(f"Ecwd{exc.errno or errno.EIO}".encode("ascii"))
        return
    if payload["proxy_fd"] >= 0:
        _handoff(payload["proxy_fd"], payload["id"], payload["startup"])
    pidfd = os.pidfd_open(os.getpid())
    try:
        # init 把 pidfd 发给 supervisor，supervisor再发给engine，在cleanup中遍历pid，一次发送SIGKILL
        send_fd(channel, b"K", pidfd)
        channel.settimeout(payload["startup"])
        if channel.recv(2) != b"A":
            raise ValueError("init_registration_failed")
        channel.settimeout(None)
    finally:
        os.close(pidfd)
    state = mmap.mmap(-1, 1, flags=mmap.MAP_SHARED)
    handshake_read, handshake_write = os.pipe()
    # Mark the possible-execution window before a child can run.
    channel.send(b"P")
    pid = os.fork()
    if pid == 0:
        os.close(handshake_read)
        _target(payload, state, handshake_write)
    os.close(handshake_write)
    # Poll the shared error byte so a policy denying write and exit can still
    # be reported and terminated.
    poller = select.poll()
    poller.register(handshake_read, select.POLLIN | select.POLLHUP)
    while state[0] != 2 and not poller.poll(10):
        pass
    failed = state[0] != 1 or bool(os.read(handshake_read, 1))
    os.close(handshake_read)
    if failed:
        channel.send(b"Eexec")
        os.kill(pid, signal.SIGKILL)
    else:
        channel.send(b"S")
    while True:
        done, status = os.waitpid(-1, 0)
        if done == pid:
            break
    if not failed:
        channel.send(f"X{os.waitstatus_to_exitcode(status)}".encode("ascii"))
    # Namespace teardown also kills remaining members if the supervisor aborts.
    try:
        os.kill(-1, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        while True:
            os.waitpid(-1, 0)
    except ChildProcessError:
        pass


def main() -> None:
    data_fd, status_fd, stderr_fd = map(int, sys.argv[1:])
    with socket.socket(fileno=status_fd) as channel:
        try:
            payload = unpack(data_fd)
            os.dup2(stderr_fd, 2)
            close_extra(status_fd, payload["proxy_fd"])
            prctl(PR_SET_DUMPABLE, 0)
            resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
            _run(payload, channel)
        except BaseException:
            try:
                channel.send(b"Einit")
            except OSError:
                pass
            getattr(os, "_exit")(125)
    getattr(os, "_exit")(0)


if __name__ == "__main__":
    main()

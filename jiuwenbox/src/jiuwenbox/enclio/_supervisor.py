# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Single-threaded supervisor for one sandbox run."""

import errno
import json
import os
import platform
import selectors
import signal
import socket
import stat
import subprocess
import sys
import time
from contextlib import ExitStack

from jiuwenbox.models.policy import CgroupPolicy
from jiuwenbox.supervisor import cgroup

from ._resources import RunResources, _verify_limits
from ._linux import (
    BOOTSTRAP_ENV, PR_SET_CHILD_SUBREAPER, PR_SET_DUMPABLE, anonymous_file, close_extra, close_stdio,
    pack, parent_death, pidfd_alive, prctl, receive_fd, send_fd, socket_pair, unpack,
)
from ._mounts import build_mounts
from ._policy import PROXY_ENV
from ._proxy import serve


class _Stop(Exception):
    def __init__(self, status: str, stage: str | None = None, code: str | None = None):
        self.status, self.stage, self.code = status, stage, code


def _setup_error(stage: str, code: str) -> _Stop:
    return _Stop("setup_error", stage, code)


class Supervisor:
    def __init__(
        self, payload: dict, control: socket.socket, registry: socket.socket, caller: int,
        target_stdout: int, target_stderr: int,
    ):
        self.payload, self.control, self.registry = payload, control, registry
        self.stdout, self.stderr = target_stdout, target_stderr
        self.config = payload["deployment"]
        self.limits = CgroupPolicy.model_validate_json(payload["plan"]["limits_json"])
        self.deadline = payload["deadline"]
        self.start_deadline = min(self.deadline or float("inf"), time.monotonic() + self.config["startup"])
        self.watched = selectors.DefaultSelector()
        self.watched.register(control, selectors.EVENT_READ, "control")
        # caller 进程退出时，supervisor的selector可以发现事件，在_events函数里处理
        self.watched.register(caller, selectors.EVENT_READ, "caller")
        self.stack = ExitStack()
        self.resources = RunResources(payload["id"], self.limits)
        self.roles = {}
        self.handle = None
        self.oom = 0
        self.command_started = False
        self.exit_status = None
        self.finished_bwrap = False
        self.bwrap: subprocess.Popen | None = None
        self.proxy_writer = None
        self.watched.register(registry, selectors.EVENT_READ, "registry")

    def _control(self) -> None:
        packet = self.control.recv(4)
        if packet == b"L":
            raise _Stop("resource_limit", "output", "output_limit")
        if packet == b"D":
            raise _Stop("timed_out")
        if packet == b"S":
            raise _setup_error("startup", "startup_timeout")
        raise _Stop("cancelled")

    def _events(self, *, startup: bool):
        end = self.start_deadline if startup else self.deadline
        timeout = None if end is None else max(0, end - time.monotonic())
        events = self.watched.select(timeout)
        for key, _ in events:
            if key.data == "control":
                self._control()
            if key.data == "caller":
                # caller进程退出，抛出"cancelled"，supervisor.run捕获
                raise _Stop("cancelled")
        if self.deadline is not None and time.monotonic() >= self.deadline:
            raise _Stop("timed_out")
        if startup and time.monotonic() >= self.start_deadline:
            raise _setup_error("startup", "startup_timeout")
        return sorted((key for key, _ in events), key=lambda key: key.data != "status")

    def _register(self, packet: bytes, fd: int | None = None) -> None:
        if fd is None:
            self.registry.send(packet)
        else:
            send_fd(self.registry, packet, fd)
        while True:
            for key in self._events(startup=True):
                if key.data == "registry":
                    if self.registry.recv(2) != b"A":
                        raise _setup_error("registration", "resource_registration_failed")
                    return

    def _setup_resources(self) -> None:
        # 把清理句柄共享给引擎
        self.resources.lifetime, self.proxy_writer = os.pipe2(os.O_NONBLOCK | os.O_CLOEXEC)
        # 把lifetime的读端发给engine，让engine在清理进程里可以感知proxy worker的状态，详见cleanup函数
        self._register(b"V", self.resources.lifetime)
        if self.limits.is_empty():
            return
        if not self.resources.prepare_groups():
            raise _setup_error("cgroup", "group_collision")
        self._register(b"R")
        # 创建cgroup控制节点，并写入限额
        try:
            self.handle = cgroup.setup(self.payload["id"], self.limits)
            _verify_limits(self.handle, self.limits)
            self.oom = cgroup.oom_kill_count(self.handle)
        except (OSError, ValueError, cgroup.CgroupSetupError) as exc:
            raise _setup_error("cgroup", "resource_setup_failed") from exc

    def _track_role(self, name: str, pid: int) -> None:
        try:
            fd = os.pidfd_open(pid)
        except OSError:
            # This child cannot have its PID reused before we reap it.
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            raise
        self.roles[name] = (pid, fd)
        self.resources.tracked.append(fd)
        self._register(b"T", fd)
        if not pidfd_alive(fd):
            raise _setup_error(name, "launcher_died")
        self.watched.register(fd, selectors.EVENT_READ, name)

    def _start_proxy(self, channel: socket.socket, data: dict) -> None:
        parent = os.getpid()
        pid = os.fork()
        if pid == 0:
            try:
                parent_death(parent)
                close_extra(channel.fileno(), self.proxy_writer)
                close_stdio()
                serve(channel.fileno(), self.proxy_writer, data)
            except BaseException:
                getattr(os, "_exit")(125)
            getattr(os, "_exit")(0)
        # serve() waits for init's listener handoff. No workers exist yet.
        self._track_role("proxy", pid)

    def _start_bwrap(self, argv: list[str], fds: tuple[int, ...]) -> None:
        parent = os.getpid()

        def prepare() -> None:
            parent_death(parent)
            if self.handle is not None:
                cgroup.attach(self.handle, os.getpid())

        try:
            # This supervisor has one thread. Join before bwrap can fork.
            self.bwrap = subprocess.Popen(
                argv, stdout=self.stdout, stderr=subprocess.DEVNULL,
                env=BOOTSTRAP_ENV, pass_fds=fds, preexec_fn=prepare,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise _setup_error("bwrap", "launch_failed") from exc
        # init registers its pidfd before it forks the business command.
        self._track_role("bwrap", self.bwrap.pid)

    def _start_sandbox(self) -> None:
        plan = self.payload["plan"]
        with ExitStack() as start:
            try:
                bwrap = build_mounts(self.config, plan)
            except (OSError, ValueError) as exc:
                raise _setup_error("mounts", "source_unavailable") from exc
            supervisor_status, init_status = socket_pair()
            self.stack.enter_context(supervisor_status)
            start.enter_context(init_status)
            proxy_channel = -1
            if plan["mode"] == "isolated":
                init_proxy, proxy = socket_pair()
                start.enter_context(init_proxy)
                start.enter_context(proxy)
                proxy_channel = init_proxy.fileno()
                data = {
                    "id": self.payload["id"], "startup": self.config["startup"],
                    "deadline": self.deadline, "endpoints": plan["endpoints"],
                }
                self._start_proxy(proxy, data)
                proxy.close()

            command = self.payload["command"]
            env = {
                "PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8", "TMPDIR": "/tmp",
                **self.payload["environment"], **command["env"],
            }
            if plan["mode"] == "isolated":
                env.update(PROXY_ENV)
                env.update({name.upper(): value for name, value in PROXY_ENV.items()})
            init_data = pack({
                "id": self.payload["id"], "command": command, "env": env,
                "proxy_fd": proxy_channel,
                "startup": self.config["startup"], "identity": plan["identity"],
            })
            start.callback(os.close, init_data)
            bwrap.command = [
                "--", self.config["sandbox_python"], "-I", "-B", "/jiuwenbox/enclio/_init.py",
                str(init_data), str(init_status.fileno()), str(self.stderr),
            ]
            bpf_fd = anonymous_file(bytes.fromhex(self.payload["bpf"]))
            start.callback(os.close, bpf_fd)
            bwrap.seccomp_fd = bpf_fd
            argv = bwrap.to_args()
            argv[0] = self.config["bwrap"]
            fds = (bpf_fd, init_data, init_status.fileno(), self.stderr)
            if proxy_channel >= 0:
                fds += (proxy_channel,)

            self._start_bwrap(argv, fds)
            supervisor_status.setblocking(False)
            self.watched.register(supervisor_status, selectors.EVENT_READ, "status")

    def _consume_status(self, channel: socket.socket) -> bool:
        if channel.recv(1, socket.MSG_PEEK) == b"K":
            init_fd = receive_fd(channel, b"K")
            self.roles["init"] = (None, init_fd)
            self.resources.tracked.append(init_fd)
            self._register(b"T", init_fd)
            # After A, init can fork before we consume P/S. A simultaneous
            # cancellation must report unknown rather than falsely safe to retry.
            self.command_started = None
            channel.send(b"A")
            return True
        packet = channel.recv(64)
        if not packet:
            self.watched.unregister(channel)
            return False
        elif packet == b"P":
            self.command_started = None
        elif packet == b"S":
            self.command_started = True
            try:
                self.control.send(b"S", socket.MSG_DONTWAIT)
            except OSError:
                pass
        elif packet[:1] == b"X":
            self.exit_status = int(packet[1:])
        elif packet == b"Eexec":
            self.command_started = False
            raise _setup_error("exec", "target_exec_failed")
        elif packet.startswith(b"Ecwd") and packet[4:].isdigit():
            number = int(packet[4:])
            if not 0 < number < 4096:
                raise _setup_error("init", "sandbox_view_or_filter_failed")
            if self.command_started is not False or "init" in self.roles:
                raise _setup_error("init", "sandbox_view_or_filter_failed")
            code = {
                errno.ENOENT: "workdir_not_found",
                errno.EACCES: "workdir_permission_denied",
                errno.ENOTDIR: "workdir_not_directory",
            }.get(number, "workdir_unavailable")
            raise _setup_error("cwd", code)
        else:
            raise _setup_error("init", "sandbox_view_or_filter_failed")
        return True

    def _run(self) -> str:
        uid, gid = self.config["uid"], self.config["gid"]
        if (os.getuid(), os.geteuid(), os.getgid(), os.getegid()) != (uid, uid, gid, gid):
            raise _setup_error("identity", "service_identity_mismatch")
        try:
            info = os.stat(self.config["bwrap"])
        except OSError as exc:
            raise _setup_error("bwrap", "bubblewrap_unavailable") from exc
        if not stat.S_ISREG(info.st_mode) or info.st_mode & (stat.S_ISUID | stat.S_ISGID | 0o022):
            raise _setup_error("bwrap", "untrusted_binary_mode")
        if "security.capability" in os.listxattr(self.config["bwrap"]):
            raise _setup_error("bwrap", "bwrap_file_capabilities")
        if platform.machine() not in ("aarch64", "x86_64"):
            raise _setup_error("platform", "unsupported_abi")
        try:
            self._setup_resources()
            self._start_sandbox()
        finally:
            os.close(self.stdout)
            os.close(self.stderr)
            close_stdio()
            if self.proxy_writer is not None:
                os.close(self.proxy_writer)
        while True:
            for key in self._events(startup=self.command_started is not True):
                role = key.data
                if role == "status":
                    while True:
                        try:
                            if not self._consume_status(key.fileobj):
                                break
                        except BlockingIOError:
                            break
                elif role == "registry":
                    raise _Stop("cancelled")
                elif role == "proxy" and self.exit_status is None:
                    raise _setup_error("proxy", "proxy_interrupted")
                elif role == "bwrap":
                    self.finished_bwrap = True
            if self.finished_bwrap:
                if self.exit_status is None:
                    raise _setup_error("bwrap", "sandbox_interrupted")
                return "exited" if self.exit_status >= 0 else "signalled"

    def _finish(self) -> tuple[bool, int]:
        complete, oom = True, self.oom
        budget = self.config["cleanup"]
        end = time.monotonic() + budget
        if self.handle is not None:
            oom = cgroup.oom_kill_count(self.handle)
        if self.bwrap is not None:
            self.bwrap.kill()
        complete = self.resources.cleanup(max(0, end - time.monotonic()))
        if self.bwrap is not None:
            try:
                self.bwrap.wait(timeout=max(0, end - time.monotonic()))
            except subprocess.TimeoutExpired:
                complete = False

        while True:
            try:
                pid, _ = os.waitpid(-1, os.WNOHANG)
            except ChildProcessError:
                return complete, oom
            if pid == 0:
                if time.monotonic() >= end:
                    return False, oom
                time.sleep(0.01)

    def run(self) -> dict:
        stage, code, limit = None, None, None
        try:
            status = self._run()
        except _Stop as exc:
            status, stage, code = exc.status, exc.stage, exc.code
        except Exception:
            status, stage, code = "setup_error", "supervisor", "startup_failed"
        try:
            complete, oom = self._finish()
        except Exception:
            complete, oom = False, self.oom
        finally:
            self.registry.close()
            self.resources.close()
            self.watched.close()
            self.stack.close()
        if status == "resource_limit":
            limit = "output"
        if oom > self.oom and status in ("exited", "signalled", "setup_error"):
            status, limit = "resource_limit", "memory"
        initial = None
        if not complete:
            initial, status = status, "cleanup_error"
            stage = stage or "cleanup"
            code = code or "cleanup_unconfirmed"
        return {
            "status": status,
            "exit_code": self.exit_status if self.exit_status is not None and self.exit_status >= 0 else None,
            "term_signal": -self.exit_status if self.exit_status is not None and self.exit_status < 0 else None,
            "command_started": self.command_started, "cleanup_complete": complete,
            "limit_reason": limit, "error_stage": stage, "error_code": code, "initial_status": initial,
        }


def _send_result(control: socket.socket, result: dict) -> None:
    # A terminal result is already committed. Reject late notifications, then
    # consume queued packets: closing SOCK_SEQPACKET with unread input resets
    # the peer and can discard the result even after send() succeeds.
    control.shutdown(socket.SHUT_RD)
    while control.recv(4096):
        pass
    control.send(json.dumps(result).encode())


def main(target_stdout: int, target_stderr: int) -> None:
    bootstrap, control_fd, registry_fd, caller = map(int, sys.argv[1:])
    # control 是控制句柄，用户下发取消/超时指令
    # supervisor 通过 registry 把清理句柄共享给引擎
    with socket.socket(fileno=control_fd) as control, socket.socket(fileno=registry_fd) as registry:
        # 防反调试
        prctl(PR_SET_DUMPABLE, 0)
        # 孤儿进程全部归属到 supervisor，确保清理阶段确认沙箱内没有进程逃出监督范围
        prctl(PR_SET_CHILD_SUBREAPER, 1)
        # 从 memfd 读回引擎写入的 JSON 快照（deployment/plan/command/bpf/deadline/environment）
        payload = unpack(bootstrap)
        result = Supervisor(payload, control, registry, caller, target_stdout, target_stderr).run()
        try:
            # 通过socket回传结果， _collect->_read_result读取结果
            _send_result(control, result)
        except OSError:
            pass

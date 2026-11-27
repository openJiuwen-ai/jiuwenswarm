# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Async entry point and bounded protocol relay for the one-run supervisor."""

import asyncio
import functools
import json
import os
import platform
import socket
import time
import uuid
from contextlib import ExitStack
from dataclasses import asdict, replace
from pathlib import Path

from jiuwenbox.models.policy import CgroupPolicy, SyscallPolicy
from jiuwenbox.supervisor.environment import build_process_env
from jiuwenbox.supervisor.seccomp import build_seccomp_filter

from ._linux import BOOTSTRAP_ENV, kill_pidfd, pack, receive_fd, socket_pair
from ._resources import RunResources
from ._policy import (
    CommandRequest, Deployment, Plan, RunResult, SandboxPolicy, compile_plan, snapshot_command,
    snapshot_deployment,
)

_BOOTSTRAP = """
# 纯标准库跳板：在导入 supervisor 之前先完成 fd 调包与路径设置。
# 1) 备份进程的 stdout/stderr（引擎的 PIPE 最终承载沙箱命令的输出，
#    由 supervisor 显式交给沙箱命令使用，见 _supervisor 的 main/stdout=self.stdout）。
# 2) 把自身 1/2 重定向到 /dev/null：后续 import 链（jiuwenbox、pydantic 等）
#    的任何警告或回溯都不会混入命令输出、挤占输出预算。
# 3) BOOTSTRAP_ENV excludes PYTHONPATH; add the trusted package_root explicitly.
#    -B avoids writing .pyc files into a possibly read-only package_root.
import os
import sys

package = sys.argv.pop(1)
target_stdout, target_stderr = os.dup(1), os.dup(2)
null = os.open("/dev/null", os.O_WRONLY, mode=0o600)
os.dup2(null, 1)
os.dup2(null, 2)
os.close(null)
sys.path.insert(0, package)
from jiuwenbox.enclio._supervisor import main
main(target_stdout, target_stderr)
"""


async def _register_resources(channel: socket.socket, resources: RunResources) -> None:
    """Retain cleanup handles in the caller before allowing children to start."""
    loop = asyncio.get_running_loop()
    while True:
        try:
            packet = channel.recv(1, socket.MSG_PEEK)
        except BlockingIOError:
            ready = loop.create_future()

            def readable() -> None:
                if not ready.done():
                    ready.set_result(None)

            loop.add_reader(channel.fileno(), readable)
            try:
                await ready
            finally:
                loop.remove_reader(channel.fileno())
            continue
        if not packet:
            return
        if packet == b"R":
            channel.recv(1)
            accepted = resources.prepare_groups()
        elif packet in (b"T", b"V"):
            fd = receive_fd(channel, packet)
            if packet == b"T":
                resources.tracked.append(fd)
            elif resources.lifetime is None:
                resources.lifetime = fd
            else:
                os.close(fd)
                raise ValueError("duplicate_lifetime")
            accepted = True
        else:
            raise ValueError("invalid_resource_registration")
        await loop.sock_sendall(channel, b"A" if accepted else b"N")


class _Output:
    def __init__(self, maximum: int, control: socket.socket):
        self.remaining = maximum
        self.control = control
        self.exceeded = False

    async def drain(self, stream: asyncio.StreamReader, output: bytearray) -> None:
        while True:
            data = await stream.read(65536)
            if not data:
                break
            size = min(len(data), self.remaining)
            output.extend(data[:size])
            self.remaining -= size
            if size < len(data) and not self.exceeded:
                self.exceeded = True
                _notify(self.control, b"L")


def _notify(channel: socket.socket, value: bytes) -> None:
    try:
        channel.send(value, socket.MSG_DONTWAIT)
    except OSError:
        pass


async def _stdin(writer: asyncio.StreamWriter, data: bytes) -> None:
    try:
        for offset in range(0, len(data), 65536):
            writer.write(data[offset:offset + 65536])
            await writer.drain()
    except (BrokenPipeError, ConnectionResetError):
        pass
    finally:
        writer.close()


class _SupervisorTimeout(Exception):
    def __init__(self, reason: str, started: bool):
        self.reason = reason
        self.started = started


async def _read_result(
    channel: socket.socket, deadline: float | None, startup_deadline: float, cleanup: float,
    stop: asyncio.Event,
) -> tuple[dict, bool | None]:
    loop = asyncio.get_running_loop()
    started = False
    cancelled = asyncio.create_task(stop.wait())
    receive = asyncio.create_task(loop.sock_recv(channel, 16384))
    try:
        while True:
            end = deadline if started else min(startup_deadline, deadline or float("inf"))
            delay = None if end is None else max(0, end - time.monotonic()) + 0.05
            done, _ = await asyncio.wait(
                (receive, cancelled), timeout=delay, return_when=asyncio.FIRST_COMPLETED,
            )
            if receive in done:
                packet = receive.result()
                if packet != b"S":
                    return (json.loads(packet) if packet else {}), (True if started else None)
                started = True
                receive = asyncio.create_task(loop.sock_recv(channel, 16384))
                continue

            if cancelled in done:
                notification, reason = b"C", "cancelled"
            elif deadline is not None and time.monotonic() >= deadline:
                notification, reason = b"D", "timed_out"
            else:
                notification, reason = b"S", "setup_error"
            _notify(channel, notification)
            try:
                async with asyncio.timeout(cleanup + 0.5):
                    while True:
                        packet = await receive
                        if packet != b"S":
                            break
                        started = True
                        receive = asyncio.create_task(loop.sock_recv(channel, 16384))
            except TimeoutError as exc:
                raise _SupervisorTimeout(reason, started) from exc
            if not packet:
                raise _SupervisorTimeout(reason, started)
            return json.loads(packet), (True if started else None)
    finally:
        receive.cancel()
        cancelled.cancel()


def create_bpf_compiler():
    """Create a bounded compiler cache owned by one engine instance."""
    @functools.lru_cache(maxsize=64)
    def compile_filter(machine: str, policy_json: str, protect_daemon_signals: bool) -> str:
        return build_seccomp_filter(
            SyscallPolicy.model_validate_json(policy_json), protect_daemon_signals=protect_daemon_signals,
        ).hex()

    return compile_filter


class LiteSandboxEngine:
    """Execute argv in a new Linux sandbox and wait for cleanup on each call."""

    def __init__(self, deployment: Deployment):
        package_root = str(Path(__file__).resolve().parents[2])
        self._deployment = snapshot_deployment(deployment, package_root)
        # engine的最大并发数
        self._slots = asyncio.Semaphore(self._deployment["max_concurrent"])
        self._cleanup_failed = False
        # The cache is local to one immutable deployment and compiler version.
        self._compile = create_bpf_compiler()

    async def run(
        self,
        policy: SandboxPolicy,
        command: CommandRequest,
        *,
        cancel: asyncio.Event | None = None,
    ) -> RunResult:
        """Run an authorized policy. Cleanup failure takes precedence over cancellation."""
        start = time.monotonic()
        run_id = "lite-" + uuid.uuid4().hex
        if cancel is not None and cancel.is_set():
            return RunResult(run_id, "cancelled")
        try:
            plan = compile_plan(policy, self._deployment)
        except (ValueError, TypeError, AttributeError, OverflowError):
            return RunResult(run_id, "invalid_policy", error_stage="policy", error_code="invalid_policy")
        try:
            request = snapshot_command(command, plan, self._deployment["max_input"])
        except (ValueError, TypeError, AttributeError, OverflowError):
            return RunResult(run_id, "invalid_request", error_stage="request", error_code="invalid_request")
        if platform.system() != "Linux":
            return RunResult(run_id, "setup_error", error_stage="platform", error_code="linux_required")
        deadline = None if command.timeout_seconds is None else start + command.timeout_seconds
        # Own a separate task from submission to reaping, including the window
        # where asyncio is still creating the supervisor subprocess.
        stop = asyncio.Event()
        worker = asyncio.create_task(self._limited(run_id, plan, request, command.stdin, deadline, stop))
        relay = asyncio.create_task(self._relay(cancel, stop)) if cancel is not None else None
        try:
            return await asyncio.shield(worker)
        except asyncio.CancelledError:
            stop.set()
            while not worker.done():
                try:
                    await asyncio.shield(worker)
                except asyncio.CancelledError:
                    continue
            result = worker.result()
            if result.status == "cleanup_error" or not result.cleanup_complete:
                return result
            raise
        finally:
            if relay is not None:
                relay.cancel()

    @staticmethod
    async def _relay(cancel: asyncio.Event, stop: asyncio.Event) -> None:
        await cancel.wait()
        stop.set()

    async def _limited(
        self, run_id: str, plan: Plan, request: dict, data: bytes, deadline: float | None, stop: asyncio.Event,
    ) -> RunResult:
        claim = asyncio.create_task(self._slots.acquire())
        cancellation = asyncio.create_task(stop.wait())
        try:
            timeout = None if deadline is None else max(0, deadline - time.monotonic())
            await asyncio.wait((claim, cancellation), timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
            if stop.is_set():
                return RunResult(run_id, "cancelled")
            if not claim.done() or deadline is not None and time.monotonic() >= deadline:
                return RunResult(run_id, "timed_out")
            if self._cleanup_failed:
                return RunResult(run_id, "setup_error", error_stage="engine", error_code="cleanup_required")
            try:
                bpf = self._compile(platform.machine(), plan.syscall_json, False)
            except (OSError, ValueError):
                return RunResult(run_id, "setup_error", error_stage="seccomp", error_code="compile_failed")
            environment = dict(plan.environment)
            service_env = build_process_env(environment)
            if "HOME" in service_env:
                environment["HOME"] = service_env["HOME"]
            payload = {
                "id": run_id,
                "deployment": {**self._deployment, "runtime": [asdict(row) for row in self._deployment["runtime"]]},
                "plan": asdict(plan),
                "command": request,
                "bpf": bpf,
                "deadline": deadline,
                "environment": environment,
            }
            result = await self._execute(payload, data, stop)
            if result.status == "cleanup_error" or not result.cleanup_complete:
                # Close admission before releasing the slot to any queued run.
                self._cleanup_failed = True
            return result
        finally:
            cancellation.cancel()
            if claim.done() and not claim.cancelled():
                self._slots.release()
            else:
                claim.cancel()

    async def _execute(self, payload: dict, data: bytes, stop: asyncio.Event) -> RunResult:
        run_id = payload["id"]
        config = self._deployment
        try:
            with ExitStack() as stack:
                channel, child = map(stack.enter_context, socket_pair())
                registry, child_registry = map(stack.enter_context, socket_pair())
                resources = RunResources(run_id, CgroupPolicy.model_validate_json(payload["plan"]["limits_json"]))
                stack.callback(resources.close)
                fd = pack(payload)
                stack.callback(os.close, fd)
                # 把caller信息发给supervisor，让supervisor做死亡监听
                caller = os.pidfd_open(os.getpid())
                stack.callback(os.close, caller)
                channel.setblocking(False)
                registry.setblocking(False)
                startup_deadline = time.monotonic() + config["startup"]
                proc = await asyncio.create_subprocess_exec(
                    config["python"], "-B", "-c", _BOOTSTRAP, config["package_root"],
                    str(fd), str(child.fileno()), str(child_registry.fileno()), str(caller),
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE, cwd="/", env=BOOTSTRAP_ENV,
                    pass_fds=(fd, child.fileno(), child_registry.fileno(), caller), start_new_session=True,
                )
                child.close()
                child_registry.close()
                supervisor_fd = None
                if proc.returncode is None:
                    try:
                        candidate = os.pidfd_open(proc.pid)
                    except ProcessLookupError:
                        pass
                    else:
                        stack.callback(os.close, candidate)
                        if proc.returncode is None:
                            supervisor_fd = candidate
                return await self._collect(
                    proc, supervisor_fd, channel, registry, resources, payload, data, startup_deadline, stop,
                )
        except (OSError, ValueError, NotImplementedError):
            return RunResult(run_id, "setup_error", error_stage="supervisor", error_code="launch_failed")

    async def _collect(
        self, proc: asyncio.subprocess.Process, supervisor_fd: int | None,
        channel: socket.socket, registry: socket.socket, resources: RunResources,
        payload: dict, data: bytes, startup_deadline: float, stop: asyncio.Event,
    ) -> RunResult:
        cleanup = self._deployment["cleanup"]
        stdout, stderr = bytearray(), bytearray()
        output = _Output(self._deployment["max_output"], channel)
        # 接收清理句柄
        registration = asyncio.create_task(_register_resources(registry, resources))
        outgoing = asyncio.create_task(_stdin(proc.stdin, data))
        # 持续读取 stdout/stderr，stdout/stderr传递路径bootstrap->supervisor->bwrap->沙箱init->工具
        drains = (asyncio.create_task(output.drain(proc.stdout, stdout)),
                  asyncio.create_task(output.drain(proc.stderr, stderr)))
        result, started = {}, None
        status = "setup_error"
        try:
            result, started = await _read_result(channel, payload["deadline"], startup_deadline, cleanup, stop)
        except _SupervisorTimeout as exc:
            status, started = exc.reason, True if exc.started else None
            if supervisor_fd is not None:
                kill_pidfd(supervisor_fd)
        except (ValueError, OSError):
            if supervisor_fd is not None:
                kill_pidfd(supervisor_fd)
        finally:
            outgoing.cancel()
        protocol_complete = True
        try:
            async with asyncio.timeout(cleanup + 0.5):
                await registration
        except (OSError, ValueError):
            protocol_complete = False
            registration.cancel()
            if supervisor_fd is not None:
                kill_pidfd(supervisor_fd)
        finally:
            await asyncio.gather(registration, outgoing, return_exceptions=True)

        # cleanup() has its own monotonic budget. Await its worker to finish
        # before closing the pidfds, even when the control protocol failed.
        clean = await asyncio.to_thread(resources.cleanup, cleanup)
        if not result:
            result = {
                "status": status,
                "command_started": started, "cleanup_complete": clean,
                "error_stage": "supervisor", "error_code": "supervisor_interrupted",
            }
        if not clean or not protocol_complete:
            result = {
                **result, "initial_status": result.get("initial_status") or result["status"],
                "status": "cleanup_error", "cleanup_complete": False,
                "error_stage": "cleanup", "error_code": "cleanup_unconfirmed",
            }
        try:
            async with asyncio.timeout(cleanup + 0.5):
                await asyncio.gather(*drains, proc.wait())
        except OSError:
            if supervisor_fd is not None:
                kill_pidfd(supervisor_fd)
            for task in drains:
                task.cancel()
            await asyncio.gather(*drains, return_exceptions=True)
            result = {
                **result, "status": "cleanup_error", "cleanup_complete": False,
                "initial_status": result.get("initial_status") or result.get("status"),
                "error_stage": "cleanup", "error_code": "cleanup_unconfirmed",
            }
        outcome = RunResult(payload["id"], stdout=bytes(stdout), stderr=bytes(stderr), **result)
        if output.exceeded and outcome.status in ("exited", "signalled"):
            return replace(outcome, status="resource_limit", limit_reason="output",
                           error_stage="output", error_code="output_limit")
        return outcome

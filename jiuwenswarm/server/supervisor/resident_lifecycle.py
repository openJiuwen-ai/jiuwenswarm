# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Validated retirement of a resident Worker before replacement or upgrade."""
from __future__ import annotations

import asyncio
import os
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

import psutil
from filelock import FileLock, Timeout

from jiuwenswarm.server.supervisor.identity import data_scope, deployment_scope


@asynccontextmanager
async def startup_lock(socket_path: str, deadline: float):
    path = Path(socket_path + ".startup.lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = FileLock(str(path), timeout=0, thread_local=False)
    while True:
        try:
            lock.acquire()
            break
        except Timeout:
            if time.monotonic() >= deadline:
                raise TimeoutError("worker election timed out") from None
            await asyncio.sleep(.025)
    try:
        yield
    finally:
        lock.release()


def worker_process(hello: dict, socket_path: str, *, allow_legacy: bool = False) -> tuple[psutil.Process, float]:
    """Establish ownership from the OS as well as the peer's HELLO fields."""
    if not isinstance(hello, dict) or hello.get("kind") != "hello" or hello.get("protocol") not in (1, 2):
        raise RuntimeError("Worker lifecycle refused an unknown IPC protocol")
    legacy = hello.get("deployment_scope") is None
    if legacy and not allow_legacy:
        raise RuntimeError("Worker lifecycle requires a deployment identity")
    if not legacy and (hello.get("deployment_scope") != deployment_scope()
                       or hello.get("data_scope") != data_scope()):
        raise RuntimeError("Worker lifecycle refused a different deployment")
    if legacy and hello.get("data_scope") not in (None, data_scope()):
        raise RuntimeError("Worker lifecycle refused a different data scope")
    pid = hello.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool):
        raise RuntimeError("Worker lifecycle refused an invalid PID")
    if pid <= 0 or pid == os.getpid():
        raise RuntimeError("Worker lifecycle refused an invalid PID")
    try:
        process = psutil.Process(pid)
        created = process.create_time()
        if process.username() != psutil.Process().username():
            raise RuntimeError("Worker lifecycle refused a different user")
        executable = os.path.normcase(str(Path(process.exe()).resolve()))
        expected = {os.path.normcase(str(Path(sys.executable).resolve()))}
        if not getattr(sys, "frozen", False):
            expected.add(os.path.normcase(str(Path(getattr(sys, "_base_executable", sys.executable)).resolve())))
        argv = process.cmdline()
        if executable not in expected or argv[1:3] != ["-m", "jiuwenswarm.server.worker.main"]:
            raise RuntimeError("Worker lifecycle refused an unrelated process")
        if ("--socket" not in argv
                or argv[argv.index("--socket") + 1:argv.index("--socket") + 2] != [socket_path]):
            raise RuntimeError("Worker lifecycle refused an unrelated process")
        if legacy and not getattr(sys, "frozen", False):
            # One interpreter can import either a source checkout or a wheel.
            # Neither cwd nor the caller's package location proves the peer's
            # loaded deployment. Frozen peers are bound to their OS executable.
            raise RuntimeError(
                "Legacy Python Worker deployment could not be established; automatic termination refused. "
                "Stop it using its original source/WHL service before restarting."
            )
        if hello.get("process_created") not in (None, created):
            raise RuntimeError("Worker PID identity changed")
        return process, created
    except (psutil.Error, OSError) as exc:
        raise RuntimeError("Worker process ownership could not be established") from exc


def _alive(process: psutil.Process, created: float) -> bool:
    try:
        if psutil.Process(process.pid).create_time() != created:
            raise RuntimeError("Worker PID identity changed")
        return process.is_running() and process.status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


async def _wait_exit(process: psutil.Process, created: float, deadline: float) -> None:
    while _alive(process, created):
        if time.monotonic() >= deadline:
            raise TimeoutError("Worker process did not exit before the shutdown deadline")
        await asyncio.sleep(min(.025, max(.001, deadline - time.monotonic())))


async def retire_worker(connection, hello: dict, socket_path: str, *, deadline: float,
                        allow_legacy: bool = False) -> dict:
    """Send shutdown, observe actual exit and refuse incomplete persistence."""
    process, created = worker_process(hello, socket_path, allow_legacy=allow_legacy)
    grace = deadline - min(1.0, max(0.0, deadline - time.monotonic()) / 5)
    outcome = None
    error = None
    try:
        await asyncio.wait_for(connection.send({"kind": "shutdown"}), max(.001, grace - time.monotonic()))
        while time.monotonic() < grace:
            frame = await asyncio.wait_for(connection.recv(), max(.001, grace - time.monotonic()))
            if frame.get("kind") == "shutdown.done":
                outcome = frame
                break
    except (OSError, RuntimeError, asyncio.IncompleteReadError) as exc:
        error = exc
    try:
        await _wait_exit(process, created, grace)
    except TimeoutError:
        if _alive(process, created):
            try:
                children = process.children(recursive=True)
            except (psutil.Error, OSError):
                # Failure to inspect descendants must not skip the owned root.
                children = []
            for child in reversed(children):
                try:
                    born = child.create_time()
                    if _alive(child, born):
                        child.kill()
                except (psutil.Error, OSError, RuntimeError):
                    continue
            if _alive(process, created):
                process.kill()
        await _wait_exit(process, created, deadline)
        raise RuntimeError("Worker forced to exit; graceful shutdown/persistence incomplete") from error
    if outcome is not None and outcome.get("ok") is not True:
        raise RuntimeError(f"Worker reported incomplete shutdown: {outcome.get('error', '')}")
    if outcome is None and hello.get("protocol") != 1:
        raise RuntimeError("Worker exited without a shutdown acknowledgement") from error
    # The Worker releases this lifetime lease before exit. Hold the startup
    # lock in the caller until the replacement has also passed HELLO validation.
    lease = FileLock(socket_path + ".owner.lock", timeout=0, thread_local=False)
    try:
        lease.acquire()
        lease.release()
    except Timeout as exc:
        raise RuntimeError("Worker IPC lifetime lease is still held") from exc
    return outcome or {"kind": "shutdown.done", "ok": True, "legacy": True}

# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Retire this deployment's Worker for an explicit system plugin restore."""

from __future__ import annotations

import argparse
import asyncio
import logging
import math
import os
import subprocess
import sys
import time
from pathlib import Path

import psutil
from filelock import FileLock, Timeout

logger = logging.getLogger(__name__)
RECOVERY_WORKER_FLAG = "--system-plugin-recovery-stop-worker"


def _process_running(pid: int, created: float) -> bool:
    """Observe a captured process without granting authority to a recycled PID."""
    try:
        process = psutil.Process(pid)
        return (
            process.create_time() == created
            and process.is_running()
            and process.status() != psutil.STATUS_ZOMBIE
        )
    except psutil.NoSuchProcess:
        return False


def _lease_released(socket_path: str) -> bool:
    lease = FileLock(socket_path + ".owner.lock", timeout=0, thread_local=False)
    try:
        with lease:
            return True
    except Timeout:
        return False


async def _stop_recovery_worker(timeout: float) -> None:
    """Stop a validated Worker after its Front and Gateway have both exited."""
    from jiuwenswarm.server.ipc.client import connect, endpoint_is_listening, try_connect
    from jiuwenswarm.server.supervisor.resident_lifecycle import (
        WorkerShutdownIncomplete,
        retire_worker,
        startup_lock,
        worker_process,
    )
    from jiuwenswarm.server.supervisor.shutdown import _pending_worker_alive, _pending_workers
    from jiuwenswarm.server.supervisor.worker_process import default_socket_path

    socket_path = default_socket_path()
    deadline = time.monotonic() + timeout
    # Match the normal shutdown path's booting-Worker check. A not-yet-bound
    # endpoint does not prove that the previous Worker is absent.
    pending = await asyncio.to_thread(_pending_workers, [socket_path])
    async with startup_lock(socket_path, deadline):
        connection = await try_connect(socket_path, timeout=min(.05, timeout))
        while connection is None:
            if (
                not endpoint_is_listening(socket_path)
                and not _pending_worker_alive(pending[socket_path])
                and _lease_released(socket_path)
            ):
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Recovery could not acquire the detached Worker IPC")
            try:
                connection = await asyncio.wait_for(connect(socket_path), min(.2, remaining))
            except (OSError, RuntimeError):
                await asyncio.sleep(min(.025, max(0.0, deadline - time.monotonic())))

        try:
            hello = await asyncio.wait_for(connection.recv(), max(.001, deadline - time.monotonic()))
            # Recovery is not permission to stop an unversioned or unrelated
            # deployment. Reuse the normal OS, executable and scope validation.
            process, created = worker_process(hello, socket_path)
            descendants = []
            for child in process.children(recursive=True):
                try:
                    descendants.append((child.pid, child.create_time()))
                except psutil.NoSuchProcess:
                    continue
            verification_budget = min(3.0, timeout / 5)
            try:
                await retire_worker(
                    connection,
                    hello,
                    socket_path,
                    deadline=deadline - verification_budget,
                )
            except WorkerShutdownIncomplete:
                # An explicit restore interrupts running work by design. A
                # forced exit may continue only after the checks below; normal
                # application shutdown retains its strict persistence contract.
                logger.warning(
                    "[SystemPluginRecovery] Worker persistence was incomplete; verifying cold restart",
                    exc_info=True,
                )
        finally:
            await asyncio.wait_for(connection.close(), max(.001, deadline - time.monotonic()))

        if _process_running(process.pid, created):
            raise RuntimeError("Recovery refused to restart while the previous Worker is alive")
        # A plugin may have left detached tools behind. Stop only the exact
        # descendants captured while the validated Worker still owned them.
        for pid, born in reversed(descendants):
            if _process_running(pid, born):
                try:
                    child = psutil.Process(pid)
                    if child.create_time() == born:
                        child.kill()
                except psutil.NoSuchProcess:
                    continue
        while True:
            if (
                not _process_running(process.pid, created)
                and not any(_process_running(pid, born) for pid, born in descendants)
                and not _pending_worker_alive(pending[socket_path])
                and _lease_released(socket_path)
                and not endpoint_is_listening(socket_path)
            ):
                return
            if time.monotonic() >= deadline:
                raise TimeoutError("Recovery could not confirm Worker and IPC retirement")
            await asyncio.sleep(.025)


def stop_recovery_worker(*, workspace: Path | None = None, timeout: float = 30.0) -> None:
    """Confirm Worker retirement before restarting the restored backend pair.

    Args:
        workspace: Optional instance data directory, resolved in a subprocess.
        timeout: Total bounded Worker shutdown and exit-verification budget.

    Raises:
        RuntimeError: Ownership or process retirement could not be confirmed.
        TimeoutError: The previous Worker could not be retired before the deadline.
    """
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("Recovery Worker timeout must be positive and finite")
    if workspace is None:
        asyncio.run(_stop_recovery_worker(timeout))
        return

    command = [sys.executable]
    if getattr(sys, "frozen", False):
        command.append(RECOVERY_WORKER_FLAG)
    else:
        command.extend(["-P", "-m", "jiuwenswarm.common.system_plugin_recovery_restart"])
    command.extend(["--workspace", str(workspace), "--timeout", str(timeout)])
    env = os.environ.copy()
    env["JIUWENSWARM_DATA_DIR"] = str(workspace)
    package_root = str(Path(__file__).resolve().parents[2])
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [package_root, env.get("PYTHONPATH", "")]))
    completed = subprocess.run(
        command,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout + 5,
        check=False,
        start_new_session=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"Recovery Worker shutdown failed (exit={completed.returncode}): {completed.stderr[-2000:]}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Retire the Worker for system plugin recovery")
    parser.add_argument("--workspace", type=Path)
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args()
    if args.workspace is not None:
        workspace = args.workspace.expanduser().resolve()
        os.environ["JIUWENSWARM_DATA_DIR"] = str(workspace)
        from jiuwenswarm.dotenv_early import load_dotenv_runtime

        bootstrap = workspace / ".env"
        if bootstrap.is_file():
            load_dotenv_runtime(bootstrap, override=True)
        os.environ["JIUWENSWARM_DATA_DIR"] = str(workspace)
        from jiuwenswarm.common.utils import get_env_file

        load_dotenv_runtime(get_env_file(), override=True)
        os.environ["JIUWENSWARM_DATA_DIR"] = str(workspace)
    stop_recovery_worker(timeout=args.timeout)


if __name__ == "__main__":
    main()

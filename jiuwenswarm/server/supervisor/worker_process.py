# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Spawn and stop the Runtime Worker process."""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)


def default_socket_path() -> str:
    override = os.environ.get("JIUWENSWARM_RUNTIME_SOCKET", "").strip()
    if override:
        return override
    from jiuwenswarm.common.utils import get_user_workspace_dir
    from jiuwenswarm.server.supervisor.identity import data_scope

    suffix = "pipe" if os.name == "nt" else "sock"
    path = get_user_workspace_dir() / "run" / f"runtime-{data_scope()[:16]}.{suffix}"
    if os.name != "nt" and len(os.fsencode(path)) >= 100:
        import tempfile
        directory = Path(tempfile.gettempdir()) / f"jiuwenswarm-runtime-{os.getuid()}"
        directory.mkdir(mode=0o700, exist_ok=True)
        if directory.is_symlink() or directory.stat().st_uid != os.getuid():
            raise RuntimeError("Runtime IPC directory has an unexpected owner")
        directory.chmod(0o700)
        path = directory / f"runtime-{data_scope()[:16]}.sock"
    return str(path)


def legacy_socket_paths() -> list[str]:
    """Historical endpoints in this data scope; explicit overrides are isolated."""
    if os.environ.get("JIUWENSWARM_RUNTIME_SOCKET", "").strip():
        return []
    from jiuwenswarm.common.utils import get_user_workspace_dir

    suffix = "pipe" if os.name == "nt" else "sock"
    return [str(get_user_workspace_dir() / "run" / f"runtime-worker.{suffix}")]


def build_worker_command(
    socket_path: str,
    *,
    host: str,
    port: int,
    stub: bool = False,
    stub_ready_delay: float = 0.0,
    stub_exit_on_request: bool = False,
) -> list[str]:
    command = [
        sys.executable,
        "-m",
        "jiuwenswarm.server.worker.main",
        "--socket",
        socket_path,
        "--host",
        host,
        "--port",
        str(port),
    ]
    if stub:
        command.append("--stub")
        command.extend(["--stub-ready-delay", str(stub_ready_delay)])
        if stub_exit_on_request:
            command.append("--stub-exit-on-request")
    return command


async def request_resident_worker_shutdown(socket_path: str, *, hello_timeout: float = 5.0) -> int | None:
    """Ask the Worker listening on ``socket_path`` to shut down.

    A Worker outlives the Front that spawned it, so stopping the service
    process tree leaves it running. This connects as a fresh supervisor,
    validates the Worker, sends ``shutdown``, and confirms process exit. The
    caller must stop every Front first; otherwise a Front supervisor would
    spawn a replacement Worker.

    Args:
        socket_path: Worker IPC endpoint, see ``default_socket_path``.
        hello_timeout: Total handshake/exit deadline; name retained for compatibility.

    Returns:
        The pid of the Worker asked to shut down, or ``None`` when no Worker
        is listening on the endpoint.

    Raises:
        RuntimeError: The endpoint answered with something other than a
            Worker hello.
    """
    from jiuwenswarm.server.supervisor.shutdown import shutdown_resident_worker

    result = await shutdown_resident_worker(timeout=hello_timeout, socket_path=socket_path)
    return result.get("pid")



class WorkerProcess:
    """A Worker subprocess owned by this supervisor."""

    def __init__(
        self,
        socket_path: str,
        *,
        host: str,
        port: int,
        stub: bool = False,
        stub_ready_delay: float = 0.0,
        stub_exit_on_request: bool = False,
    ) -> None:
        self.socket_path = socket_path
        self._host = host
        self._port = port
        self._stub = stub
        self._stub_ready_delay = stub_ready_delay
        self._stub_exit_on_request = stub_exit_on_request
        self._proc: subprocess.Popen[bytes] | None = None
        self._stderr_path = (
            Path(socket_path).parent
            / "worker-logs"
            / f"runtime-worker-{uuid.uuid4().hex}.stderr.log"
        )

    @property
    def stderr_path(self) -> Path:
        return self._stderr_path

    @property
    def pid(self) -> int | None:
        if self._proc is None:
            return None
        return self._proc.pid

    @property
    def returncode(self) -> int | None:
        if self._proc is None:
            return None
        return self._proc.poll()

    async def spawn(self) -> None:
        from jiuwenswarm.server.supervisor.identity import code_identity, launch_identity_payload

        # Verify source once before the spawn, then hand that exact observation
        # to the child. Reattached resident Workers still require fresh checks.
        self.launch_identity = await asyncio.to_thread(code_identity)
        payload = launch_identity_payload(self.launch_identity)
        command = build_worker_command(
            self.socket_path,
            host=self._host,
            port=self._port,
            stub=self._stub,
            stub_ready_delay=self._stub_ready_delay,
            stub_exit_on_request=self._stub_exit_on_request,
        )
        command.append("--identity-from-parent")
        logger.info("[Supervisor] spawning runtime worker: %s", " ".join(command))
        # Popen, not asyncio's subprocess transport: that transport kills the
        # child when the Front event loop closes. A new session keeps the
        # Worker alive across Front restarts.
        self._stderr_path.parent.mkdir(parents=True, exist_ok=True)
        with self._stderr_path.open("ab") as stderr:
            self._proc = subprocess.Popen(
                command,
                env=os.environ.copy(),
                start_new_session=True,
                stdin=subprocess.PIPE,
                stderr=stderr,
            )
        try:
            self._proc.stdin.write(payload)
            self._proc.stdin.close()
        except OSError:
            self._proc.kill()
            await self.wait(timeout=5)
            raise
        logger.info("[Supervisor] worker pid=%s stderr=%s", self.pid, self._stderr_path)

    async def wait(self, *, timeout: float | None = None) -> int | None:
        """Return the exit code, or ``None`` when ``timeout`` elapses first."""
        proc = self._proc
        if proc is None:
            return 0
        deadline = time.monotonic() + timeout if timeout is not None else None
        while True:
            code = proc.poll()
            if code is not None:
                return code
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                await asyncio.sleep(min(0.05, remaining))
            else:
                await asyncio.sleep(0.05)

    async def stop(self, *, timeout: float = 5.0) -> None:
        proc = self._proc
        if proc is None or proc.poll() is not None:
            return
        deadline = time.monotonic() + max(0.0, timeout)
        proc.terminate()
        grace = deadline - min(1.0, max(0.0, timeout) / 2)
        code = await self.wait(timeout=max(0.0, grace - time.monotonic()))
        if code is None:
            logger.warning("[Supervisor] worker did not exit after SIGTERM; killing")
            proc.kill()
            if await self.wait(timeout=max(0.0, deadline - time.monotonic())) is None:
                logger.error("[Supervisor] worker exit could not be observed within the shutdown deadline")

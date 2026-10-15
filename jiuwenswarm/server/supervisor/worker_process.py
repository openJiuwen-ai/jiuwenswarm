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

from jiuwenswarm.server.ipc.client import try_connect
from jiuwenswarm.server.ipc.protocol import KIND_HELLO, KIND_SHUTDOWN

logger = logging.getLogger(__name__)


def default_socket_path() -> str:
    override = os.environ.get("JIUWENSWARM_RUNTIME_SOCKET", "").strip()
    if override:
        return override
    home = os.environ.get("JIUWENSWARM_HOME") or os.path.expanduser("~")
    name = "runtime-worker.pipe" if os.name == "nt" else "runtime-worker.sock"
    return str(Path(home) / ".jiuwenswarm" / "run" / name)


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
    reads the hello to learn the Worker pid, then sends ``shutdown``. The
    caller must stop every Front first; otherwise a Front supervisor would
    spawn a replacement Worker.

    Args:
        socket_path: Worker IPC endpoint, see ``default_socket_path``.
        hello_timeout: Seconds to wait for the Worker hello frame.

    Returns:
        The pid of the Worker asked to shut down, or ``None`` when no Worker
        is listening on the endpoint.

    Raises:
        RuntimeError: The endpoint answered with something other than a
            Worker hello.
    """
    conn = await try_connect(socket_path)
    if conn is None:
        return None
    try:
        hello = await asyncio.wait_for(conn.recv(), timeout=hello_timeout)
        if not isinstance(hello, dict) or hello.get("kind") != KIND_HELLO:
            raise RuntimeError(f"expected worker hello on {socket_path}")
        pid = hello.get("pid")
        if not isinstance(pid, int):
            raise RuntimeError(f"worker hello on {socket_path} carries no pid")
        await conn.send({"kind": KIND_SHUTDOWN})
        return pid
    finally:
        await conn.close()


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
        command = build_worker_command(
            self.socket_path,
            host=self._host,
            port=self._port,
            stub=self._stub,
            stub_ready_delay=self._stub_ready_delay,
            stub_exit_on_request=self._stub_exit_on_request,
        )
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
                stderr=stderr,
            )
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
        proc.terminate()
        code = await self.wait(timeout=timeout)
        if code is None:
            logger.warning("[Supervisor] worker did not exit after SIGTERM; killing")
            proc.kill()
            await self.wait()

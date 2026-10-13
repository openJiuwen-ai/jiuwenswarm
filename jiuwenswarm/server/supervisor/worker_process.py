# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Spawn and stop the Runtime Worker process."""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
from pathlib import Path

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
        self._proc = subprocess.Popen(
            command,
            env=os.environ.copy(),
            start_new_session=True,
        )

    async def wait(self, *, timeout: float | None = None) -> int | None:
        """Return the exit code, or ``None`` when ``timeout`` elapses first."""
        proc = self._proc
        if proc is None:
            return 0
        try:
            return await asyncio.to_thread(proc.wait, timeout)
        except subprocess.TimeoutExpired:
            return None

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

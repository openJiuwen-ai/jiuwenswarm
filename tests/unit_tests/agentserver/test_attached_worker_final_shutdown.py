# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Final shutdown must observe attached process exit as well as its ACK."""

import asyncio
import os
from pathlib import Path

import psutil
import pytest

from jiuwenswarm.server.lifecycle import Readiness
from jiuwenswarm.server.supervisor import runtime_supervisor
from jiuwenswarm.server.supervisor.resident_lifecycle import WorkerShutdownIncomplete
from jiuwenswarm.server.supervisor.runtime_supervisor import RuntimeSupervisor


async def test_attached_ack_does_not_hide_a_live_dependency_thread(monkeypatch, tmp_path):
    shim = tmp_path / "linger"
    shim.mkdir()
    (shim / "sitecustomize.py").write_text(
        "import sys\n"
        "if 'jiuwenswarm.server.worker.main' in getattr(sys, 'orig_argv', []):\n"
        "    from jiuwenswarm.server.worker import stub\n"
        "    original = stub._serve_one\n"
        "    async def serve(connection, **kwargs):\n"
        "        result = await original(connection, **kwargs)\n"
        "        if result is True:\n"
        "            import threading, time\n"
        "            threading.Thread(target=lambda: time.sleep(60), daemon=False).start()\n"
        "        return result\n"
        "    stub._serve_one = serve\n", encoding="utf-8")
    source = Path(__file__).resolve().parents[3]
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join([str(shim), str(source)]))
    monkeypatch.setenv("JIUWEN_HARMONY_RUNTIME", "1")
    monkeypatch.setenv("JIUWENSWARM_RUNTIME_IPC", "front_loopback")
    supervisor = RuntimeSupervisor(Readiness(), stub=True, max_restarts=0, connect_timeout=8)
    process = None
    try:
        await asyncio.wait_for(supervisor.run_until_ipc_ready(), 15)
        assert await supervisor._readiness.wait_agent_ready(5)
        pid = supervisor.worker_pid
        process = supervisor._process
        monkeypatch.setattr(runtime_supervisor, "_FINAL_SHUTDOWN_TIMEOUT", 1.5)
        with pytest.raises(WorkerShutdownIncomplete, match="forced"):
            await supervisor.stop()
        assert await process.wait(timeout=2) is not None
        assert not psutil.pid_exists(pid)
    finally:
        await supervisor.stop()
        if process is not None:
            await process.stop()

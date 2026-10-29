# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Upgrade isolation and explicit, verifiable retirement of old Python Workers."""
import asyncio
import os
from pathlib import Path
import subprocess
import sys

import psutil
import pytest

from jiuwenswarm.common.utils import get_user_workspace_dir
from jiuwenswarm.server.ipc.client import connect_with_retry
from jiuwenswarm.server.lifecycle import Readiness
from jiuwenswarm.server.supervisor.runtime_supervisor import RuntimeSupervisor
from jiuwenswarm.server.supervisor.shutdown import shutdown_resident_worker
from jiuwenswarm.server.supervisor.worker_process import default_socket_path

SOURCE = Path(__file__).resolve().parents[3]
LEGACY_SHIM = """import sys
if 'jiuwenswarm.server.worker.main' in getattr(sys, 'orig_argv', []):
    from jiuwenswarm.server.worker import stub
    original = stub._serve_one
    class LegacyConnection:
        def __init__(self, connection): self.connection = connection
        def __getattr__(self, name): return getattr(self.connection, name)
        async def send(self, message):
            message = dict(message)
            if message.get('kind') == 'hello':
                message['protocol'] = 1
                for key in ['data_scope', 'deployment_scope', 'process_created', 'code_identity', 'shutdown_ack']:
                    message.pop(key, None)
            if message.get('kind') != 'shutdown.done':
                await self.connection.send(message)
    async def legacy(connection, **kwargs):
        return await original(LegacyConnection(connection), **kwargs)
    stub._serve_one = legacy
"""


@pytest.mark.asyncio
async def test_unknown_legacy_worker_does_not_block_startup_and_requires_explicit_pid(monkeypatch, tmp_path):
    monkeypatch.delenv("JIUWENSWARM_RUNTIME_SOCKET", raising=False)
    monkeypatch.setenv("JIUWENSWARM_IDENTITY_CACHE_DIR", str(tmp_path / "identity"))
    shim = tmp_path / "shim"
    shim.mkdir()
    (shim / "sitecustomize.py").write_text(LEGACY_SHIM, encoding="utf-8")
    suffix = "pipe" if os.name == "nt" else "sock"
    path = get_user_workspace_dir() / "run" / f"runtime-worker.{suffix}"
    path.parent.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(shim), str(SOURCE)])}
    with (tmp_path / "legacy.stderr.log").open("wb") as stderr:
        process = subprocess.Popen([sys.executable, "-m", "jiuwenswarm.server.worker.main", "--socket", str(path), "--stub"],
            env=env, cwd=SOURCE, stdout=stderr, stderr=stderr)
    supervisor = RuntimeSupervisor(Readiness(), stub=True, max_restarts=0)
    worker = None
    try:
        connection = await connect_with_retry(str(path), timeout=10)
        hello = await asyncio.wait_for(connection.recv(), 5)
        await connection.close()
        worker = psutil.Process(hello["pid"])
        born = worker.create_time()
        await asyncio.wait_for(supervisor.run_until_ipc_ready(), 15)
        assert supervisor.worker_pid != worker.pid
        assert worker.is_running()
        await supervisor.stop(terminate_worker=True)
        with pytest.raises(RuntimeError, match="not found"):
            await shutdown_resident_worker(timeout=2, include_legacy=True, legacy_worker_pid=worker.pid + 1)
        assert worker.create_time() == born and worker.is_running()
        outcome = await shutdown_resident_worker(timeout=5, include_legacy=True, legacy_worker_pid=worker.pid)
        assert outcome["ok"] is True and outcome["pid"] == worker.pid
        assert not psutil.pid_exists(worker.pid)
    finally:
        if not supervisor._stopping:
            await supervisor.stop(terminate_worker=True)
        if worker is not None and worker.is_running() and worker.create_time() == born:
            worker.kill()
            await asyncio.to_thread(worker.wait, 5)
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)


@pytest.mark.asyncio
async def test_changed_installed_dependency_replaces_resident_worker(monkeypatch, tmp_path):
    dependencies = tmp_path / "dependencies"
    distribution = dependencies / "worker_probe-1.0.dist-info"
    distribution.mkdir(parents=True)
    metadata = distribution / "METADATA"
    metadata.write_text("Name: worker-probe\nVersion: 1.0\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(dependencies))
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join([str(dependencies), str(SOURCE)]))
    monkeypatch.setenv("JIUWENSWARM_IDENTITY_CACHE_DIR", str(tmp_path / "identity"))
    path = str(tmp_path / "runtime.pipe")
    first = RuntimeSupervisor(Readiness(), socket_path=path, stub=True, max_restarts=0)
    second = None
    owned = None
    try:
        await asyncio.wait_for(first.run_until_ipc_ready(), 15)
        owned = first._process
        pid = first.worker_pid
        await first.stop()
        metadata.write_text("Name: worker-probe\nVersion: 2.0\n", encoding="utf-8")
        second = RuntimeSupervisor(Readiness(), socket_path=path, stub=True, max_restarts=0)
        await asyncio.wait_for(second.run_until_ipc_ready(), 15)
        assert second.worker_pid != pid
        assert not psutil.pid_exists(pid)
    finally:
        if second is not None:
            await second.stop(terminate_worker=True)
        if not first._stopping:
            await first.stop(terminate_worker=True)
        if owned is not None and owned.returncode is None:
            await owned.stop()


def test_failed_stop_preserves_debug_state_for_retry(monkeypatch, tmp_path):
    from jiuwenswarm import debug_launcher as launcher

    monkeypatch.setattr(launcher, "REPO_ROOT", tmp_path)
    state = launcher.DebugState(pid=999999, started_at=0, log_file="", command=[], cwd=str(tmp_path))
    launcher.write_debug_state(state)
    monkeypatch.setattr(launcher, "is_debug_service_alive", lambda _: False)
    monkeypatch.setattr(launcher, "_stop_resident_worker", lambda **_: False)
    assert launcher.stop_debug_service() == 1
    assert launcher.read_debug_state() == state
    monkeypatch.setattr(launcher, "_stop_resident_worker", lambda **_: True)
    assert launcher.stop_debug_service() == 0
    assert launcher.read_debug_state() is None


def test_different_installations_do_not_share_runtime_endpoint(monkeypatch):
    monkeypatch.delenv("JIUWENSWARM_RUNTIME_SOCKET", raising=False)
    monkeypatch.setattr(sys, "prefix", str(Path("environment-one").resolve()))
    first = default_socket_path()
    monkeypatch.setattr(sys, "prefix", str(Path("environment-two").resolve()))
    assert default_socket_path() != first


@pytest.mark.asyncio
async def test_shutdown_ack_does_not_hide_a_surviving_worker_thread(monkeypatch, tmp_path):
    shim = tmp_path / "blocked-exit"
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
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join([str(shim), str(SOURCE)]))
    monkeypatch.setenv("JIUWENSWARM_IDENTITY_CACHE_DIR", str(tmp_path / "identity"))
    path = str(tmp_path / "runtime.pipe")
    front = RuntimeSupervisor(Readiness(), stub=True, socket_path=path, max_restarts=0)
    owned = None
    try:
        await asyncio.wait_for(front.run_until_ipc_ready(), 15)
        owned = front._process
        pid = front.worker_pid
        await front.stop()
        with pytest.raises(RuntimeError, match="forced to exit.*incomplete"):
            await shutdown_resident_worker(socket_path=path, timeout=.7)
        assert not psutil.pid_exists(pid)
    finally:
        if not front._stopping:
            await front.stop(terminate_worker=True)
        if owned is not None:
            await owned.stop()


@pytest.mark.asyncio
async def test_stop_does_not_claim_a_different_installations_historical_endpoint(monkeypatch, tmp_path):
    monkeypatch.delenv("JIUWENSWARM_RUNTIME_SOCKET", raising=False)
    monkeypatch.setenv("JIUWENSWARM_IDENTITY_CACHE_DIR", str(tmp_path / "identity"))
    suffix = "pipe" if os.name == "nt" else "sock"
    path = get_user_workspace_dir() / "run" / f"runtime-worker.{suffix}"
    path.parent.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "PYTHONPATH": str(SOURCE), "JIUWENSWARM_RUNTIME_PROFILE": "foreign-installation"}
    with (tmp_path / "foreign.stderr.log").open("wb") as stderr:
        process = subprocess.Popen([sys.executable, "-m", "jiuwenswarm.server.worker.main", "--socket", str(path), "--stub"],
            env=env, cwd=SOURCE, stdout=stderr, stderr=stderr)
    worker = None
    try:
        connection = await connect_with_retry(str(path), timeout=10)
        hello = await asyncio.wait_for(connection.recv(), 5)
        await connection.close()
        worker = psutil.Process(hello["pid"])
        born = worker.create_time()
        result = await shutdown_resident_worker(timeout=3, include_legacy=True)
        assert result["ok"] is True and result["absent"] is True
        assert worker.create_time() == born and worker.is_running()
        with pytest.raises(RuntimeError, match="different deployment"):
            await shutdown_resident_worker(timeout=3, legacy_worker_pid=worker.pid)
        assert worker.is_running()
    finally:
        if worker is not None and worker.is_running() and worker.create_time() == born:
            worker.kill()
            await asyncio.to_thread(worker.wait, 5)
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)

# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Upgrade isolation and explicit, verifiable retirement of old Python Workers."""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace

import psutil
import pytest

from jiuwenswarm.common.utils import get_user_workspace_dir
from jiuwenswarm.common import utils
from jiuwenswarm.server.ipc.client import connect_with_retry
from jiuwenswarm.server.lifecycle import Readiness
from jiuwenswarm.server.supervisor import identity, worker_process
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


@pytest.fixture
def legacy_workspace(monkeypatch):
    """Keep historical Unix socket paths below the OS limit, including under xdist."""
    from jiuwenswarm.common import utils

    with tempfile.TemporaryDirectory(prefix="jw-old-") as directory:
        with monkeypatch.context() as patch:
            workspace = Path(directory)
            patch.setattr(utils, "_workspace_base_dir", workspace)
            patch.setenv("JIUWENSWARM_DATA_DIR", str(workspace))
            yield workspace


@pytest.mark.asyncio
async def test_unknown_legacy_worker_does_not_block_startup_and_requires_explicit_pid(
    monkeypatch, tmp_path, legacy_workspace,
):
    monkeypatch.delenv("JIUWENSWARM_RUNTIME_SOCKET", raising=False)
    monkeypatch.setenv("JIUWENSWARM_IDENTITY_CACHE_DIR", str(tmp_path / "identity"))
    shim = tmp_path / "shim"
    shim.mkdir()
    (shim / "sitecustomize.py").write_text(LEGACY_SHIM, encoding="utf-8")
    suffix = "pipe" if os.name == "nt" else "sock"
    path = legacy_workspace / "run" / f"runtime-worker.{suffix}"
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
        cli = await asyncio.to_thread(subprocess.run,
            [sys.executable, "-m", "jiuwenswarm.server.supervisor.shutdown", "--shutdown-worker", "--timeout", "3"],
            cwd=SOURCE, env=env, capture_output=True, text=True, timeout=15, check=False)
        assert cli.returncode != 0
        assert "no deployment identity" in cli.stderr
        assert worker.create_time() == born and worker.is_running()
        with pytest.raises(RuntimeError, match="not found"):
            await shutdown_resident_worker(timeout=2, include_legacy=True, legacy_worker_pid=worker.pid + 1)
        assert worker.create_time() == born and worker.is_running()
        outcome = await shutdown_resident_worker(timeout=5, include_legacy=True, legacy_worker_pid=worker.pid)
        assert outcome["ok"] is True and outcome["pid"] == worker.pid
        # Reap our child before checking PID existence; POSIX retains zombies.
        await asyncio.to_thread(process.wait, 5)
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
        assert await owned.wait(timeout=3) is not None
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


@pytest.mark.parametrize("instance_variable", ["HOSTNAME", "JIUWENSWARM_RUNTIME_INSTANCE"])
async def test_shared_data_instances_keep_independent_resident_workers(monkeypatch, tmp_path, instance_variable):
    from jiuwenswarm.server.supervisor.identity import deployment_scope

    monkeypatch.delenv("JIUWENSWARM_RUNTIME_SOCKET", raising=False)
    monkeypatch.delenv("JIUWENSWARM_RUNTIME_INSTANCE", raising=False)
    monkeypatch.setenv("JIUWENSWARM_DATA_DIR", str(get_user_workspace_dir()))
    monkeypatch.setenv("JIUWENSWARM_IDENTITY_CACHE_DIR", str(tmp_path / "identity"))
    monkeypatch.setenv("PYTHONPATH", str(SOURCE))
    monkeypatch.setenv(instance_variable, "pod-one")
    first_scope = deployment_scope()
    first = RuntimeSupervisor(Readiness(), stub=True, max_restarts=0)
    second = None
    try:
        await asyncio.wait_for(first.run_until_ipc_ready(), 15)
        assert await first._readiness.wait_agent_ready(5)
        first_process = psutil.Process(first.worker_pid)
        first_created = first_process.create_time()
        monkeypatch.setenv(instance_variable, "pod-two")
        assert deployment_scope() != first_scope
        second = RuntimeSupervisor(Readiness(), stub=True, max_restarts=0)
        assert first._socket_path != second._socket_path
        await asyncio.wait_for(second.run_until_ipc_ready(), 15)
        assert await second._readiness.wait_agent_ready(5)
        assert second.worker_pid != first.worker_pid
        second_pid = second.worker_pid
        await second.stop(terminate_worker=True)
        assert not psutil.pid_exists(second_pid)
        assert first_process.create_time() == first_created and first_process.is_running()
    finally:
        if second is not None:
            await second.stop(terminate_worker=True)
        monkeypatch.setenv(instance_variable, "pod-one")
        await first.stop(terminate_worker=True)


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
        assert await owned.wait(timeout=3) is not None
        assert not psutil.pid_exists(pid)
    finally:
        if not front._stopping:
            await front.stop(terminate_worker=True)
        if owned is not None:
            await owned.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("foreign_identity_key", ["JIUWENSWARM_RUNTIME_PROFILE", "JIUWENSWARM_RUNTIME_INSTANCE"])
async def test_stop_does_not_claim_a_different_installations_historical_endpoint(
    monkeypatch, tmp_path, foreign_identity_key, legacy_workspace,
):
    monkeypatch.delenv("JIUWENSWARM_RUNTIME_SOCKET", raising=False)
    monkeypatch.setenv("JIUWENSWARM_IDENTITY_CACHE_DIR", str(tmp_path / "identity"))
    suffix = "pipe" if os.name == "nt" else "sock"
    path = legacy_workspace / "run" / f"runtime-worker.{suffix}"
    path.parent.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "PYTHONPATH": str(SOURCE), foreign_identity_key: "foreign-installation"}
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
        cli = await asyncio.to_thread(subprocess.run,
            [sys.executable, "-m", "jiuwenswarm.server.supervisor.shutdown", "--shutdown-worker", "--timeout", "3"],
            cwd=SOURCE, env={**os.environ, "PYTHONPATH": str(SOURCE)},
            capture_output=True, text=True, timeout=15, check=False)
        assert cli.returncode == 0, cli.stderr
        assert json.loads(cli.stdout)["absent"] is True
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


@pytest.mark.asyncio
async def test_final_shutdown_cli_retires_owned_historical_endpoint_before_front_start(
    monkeypatch, tmp_path, legacy_workspace,
):
    from jiuwenswarm.server.ipc.client import endpoint_is_listening

    monkeypatch.delenv("JIUWENSWARM_RUNTIME_SOCKET", raising=False)
    monkeypatch.setenv("JIUWENSWARM_IDENTITY_CACHE_DIR", str(tmp_path / "identity"))
    suffix = "pipe" if os.name == "nt" else "sock"
    path = legacy_workspace / "run" / f"runtime-worker.{suffix}"
    path.parent.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "PYTHONPATH": str(SOURCE)}
    worker = None
    with (tmp_path / "owned-historical.stderr.log").open("wb") as stderr:
        process = subprocess.Popen(
            [sys.executable, "-m", "jiuwenswarm.server.worker.main", "--socket", str(path), "--stub"],
            env=env, cwd=SOURCE, stdout=stderr, stderr=stderr)
    try:
        peer = await connect_with_retry(str(path), timeout=15)
        hello = await asyncio.wait_for(peer.recv(), 5)
        await peer.close()
        worker = psutil.Process(hello["pid"])
        born = worker.create_time()
        assert hello["deployment_scope"] == identity.deployment_scope()
        assert not endpoint_is_listening(default_socket_path())
        cli = await asyncio.to_thread(subprocess.run,
            [sys.executable, "-m", "jiuwenswarm.server.supervisor.shutdown", "--shutdown-worker", "--timeout", "5"],
            cwd=SOURCE, env=env, capture_output=True, text=True, timeout=15, check=False)
        assert cli.returncode == 0, cli.stderr
        assert json.loads(cli.stdout)["pid"] == worker.pid
        assert await asyncio.to_thread(process.wait, 5) == 0
        assert not psutil.pid_exists(worker.pid)
    finally:
        if worker is not None and worker.is_running() and worker.create_time() == born:
            worker.kill()
            await asyncio.to_thread(worker.wait, 5)
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)


@pytest.mark.parametrize("instance", ["", "pod-one"])
def test_legacy_posix_paths_include_historical_temp_fallback(monkeypatch, tmp_path, instance):
    monkeypatch.delenv("JIUWENSWARM_RUNTIME_SOCKET", raising=False)
    monkeypatch.delenv("HOSTNAME", raising=False)
    monkeypatch.setenv("JIUWENSWARM_RUNTIME_INSTANCE", instance)
    workspace = tmp_path / ("deep-" * 25)
    temp = tmp_path / "os-temp"
    monkeypatch.setattr(utils, "_workspace_base_dir", workspace)
    monkeypatch.setattr(identity, "data_scope", lambda: "a" * 64)
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(temp))
    monkeypatch.setattr(worker_process, "os", SimpleNamespace(name="posix", environ=os.environ, getuid=lambda: 42))
    paths = worker_process.legacy_socket_paths()
    prefix = "runtime-" + "a" * 16
    assert str(temp / "jiuwenswarm-runtime-42" / (prefix + ".sock")) in paths
    token = worker_process._runtime_instance_token()
    if token:
        assert str(temp / "jiuwenswarm-runtime-42" / (prefix + "-" + token + ".sock")) in paths
    assert str(workspace / "run/runtime-worker.sock") in paths
    assert len(paths) == len(set(paths))
    assert not temp.exists(), "Endpoint discovery must not create a directory"


def test_explicit_socket_override_excludes_all_historical_locations(monkeypatch):
    monkeypatch.setenv("JIUWENSWARM_RUNTIME_SOCKET", "owned-custom-endpoint")
    assert worker_process.legacy_socket_paths() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("historical", [False, True])
async def test_instance_shutdown_loads_target_environment_without_touching_callers_worker(
    monkeypatch, tmp_path, legacy_workspace, historical,
):
    from jiuwenswarm.server.supervisor.shutdown import shutdown_application_worker

    monkeypatch.delenv("JIUWENSWARM_RUNTIME_SOCKET", raising=False)
    monkeypatch.setenv("JIUWENSWARM_IDENTITY_CACHE_DIR", str(tmp_path / "identity"))
    other = tmp_path / "other"
    (legacy_workspace / "config").mkdir()
    (legacy_workspace / ".env").write_text(f"JIUWENSWARM_DATA_DIR={other}\n", encoding="utf-8")
    (legacy_workspace / "config/.env").write_text(
        "JIUWENSWARM_RUNTIME_PROFILE=target\nJIUWENSWARM_RUNTIME_INSTANCE=target\n", encoding="utf-8")
    workers = []
    try:
        for workspace, label in [(legacy_workspace, "target"), (other, "other")]:
            monkeypatch.setattr(utils, "_workspace_base_dir", workspace)
            monkeypatch.setenv("JIUWENSWARM_DATA_DIR", str(workspace))
            monkeypatch.setenv("JIUWENSWARM_RUNTIME_PROFILE", label)
            monkeypatch.setenv("JIUWENSWARM_RUNTIME_INSTANCE", label)
            path = default_socket_path()
            if historical and label == "target":
                path = str(workspace / "run" / ("runtime-worker.pipe" if os.name == "nt" else "runtime-worker.sock"))
            process = worker_process.WorkerProcess(path, host="127.0.0.1", port=0, stub=True)
            await process.spawn()
            connection = await connect_with_retry(path, timeout=15)
            hello = await asyncio.wait_for(connection.recv(), 5)
            await connection.close()
            native = psutil.Process(hello["pid"])
            workers.append((process, native, native.create_time()))
        before = {key: os.environ.get(key) for key in
            ["JIUWENSWARM_DATA_DIR", "JIUWENSWARM_RUNTIME_PROFILE", "JIUWENSWARM_RUNTIME_INSTANCE"]}
        assert await asyncio.to_thread(shutdown_application_worker, workspace=legacy_workspace, timeout=5)
        assert await workers[0][0].wait(timeout=3) == 0
        assert not psutil.pid_exists(workers[0][1].pid)
        assert workers[1][1].is_running() and workers[1][1].create_time() == workers[1][2]
        assert before == {key: os.environ.get(key) for key in before}
    finally:
        for process, native, born in workers:
            if native.is_running() and native.create_time() == born:
                assert native.cmdline()[1:3] == ["-m", "jiuwenswarm.server.worker.main"]
                native.kill()
            await process.wait(timeout=5)


@pytest.mark.asyncio
async def test_final_shutdown_waits_for_a_worker_still_booting_before_ipc(monkeypatch, tmp_path, legacy_workspace):
    from jiuwenswarm.server.supervisor.shutdown import shutdown_application_worker

    monkeypatch.delenv("JIUWENSWARM_RUNTIME_SOCKET", raising=False)
    monkeypatch.setenv("JIUWENSWARM_IDENTITY_CACHE_DIR", str(tmp_path / "identity"))
    shim = tmp_path / "boot-delay"
    shim.mkdir()
    (shim / "sitecustomize.py").write_text(
        "import sys, time\n"
        "if 'jiuwenswarm.server.worker.main' in getattr(sys, 'orig_argv', []): time.sleep(2)\n",
        encoding="utf-8")
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join([str(shim), str(SOURCE)]))
    path = default_socket_path()
    process = worker_process.WorkerProcess(path, host="127.0.0.1", port=0, stub=True)
    await process.spawn()
    try:
        assert await asyncio.to_thread(shutdown_application_worker, workspace=legacy_workspace, timeout=5)
        assert await process.wait(timeout=1) == 0
    finally:
        if await process.wait(timeout=.05) is None:
            connection = await connect_with_retry(path, timeout=5)
            await asyncio.wait_for(connection.recv(), 5)
            await connection.close()
            await shutdown_resident_worker(timeout=5, socket_path=path)
            assert await process.wait(timeout=2) == 0

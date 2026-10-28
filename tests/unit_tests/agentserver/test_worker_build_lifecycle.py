# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Artifact/source identity and actual resident-process replacement regressions."""
from __future__ import annotations

import asyncio
import json
import os
import sys
import subprocess
import shutil
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from zipfile import ZipFile

import pytest

from jiuwenswarm.common.worker_build_identity import read_build_identity, write_build_identity
from jiuwenswarm.server.supervisor import dependency_identity, identity, resident_lifecycle
from jiuwenswarm.server.supervisor.worker_process import WorkerProcess


def build_source(root):
    (root / "jiuwenswarm").mkdir(parents=True)
    (root / "jiuwenswarm/main.py").write_text("value = 1\n", encoding="utf-8")
    (root / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    return root


def test_separate_release_checkout_injects_one_identity_and_checks_reuse(tmp_path):
    source = build_source(tmp_path / "source")
    release = tmp_path / "release"
    release.mkdir()
    artifact = tmp_path / "build" / "first.json"
    first = write_build_identity(artifact, source, packaging_root=release)
    second = write_build_identity(tmp_path / "build" / "second.json", source, packaging_root=release)
    assert first["backend_build_id"] != second["backend_build_id"]
    rebuilt = write_build_identity(tmp_path / "rebuilt" / "identity.json", source, identity_input=artifact)
    another = write_build_identity(tmp_path / "another" / "identity.json", source, identity_input=artifact)
    assert len({first["backend_build_id"], second["backend_build_id"], rebuilt["backend_build_id"],
                another["backend_build_id"]}) == 4
    assert rebuilt["input_build_id"] == another["input_build_id"] == first["backend_build_id"]
    assert read_build_identity(artifact) == first
    assert not (source / "jiuwenswarm/_worker_build_identity.json").exists()
    (source / "jiuwenswarm/main.py").write_text("value = 2\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="does not match"):
        write_build_identity(tmp_path / "wrong.json", source, identity_input=artifact)


def test_wheel_identity_observes_actual_environment_and_rejects_corrupt_metadata(tmp_path, monkeypatch):
    source = build_source(tmp_path / "source")
    path = source / "jiuwenswarm/_worker_build_identity.json"
    write_build_identity(path, source)
    monkeypatch.setattr(identity, "__file__", str(source / "jiuwenswarm/server/supervisor/identity.py"))
    monkeypatch.setattr(dependency_identity, "environment_identity", lambda: "environment-one")
    first = identity.code_identity()
    monkeypatch.setattr(dependency_identity, "environment_identity", lambda: "environment-two")
    assert identity.code_identity() != first
    path.write_text("{broken", encoding="utf-8")
    with pytest.raises(RuntimeError, match="Invalid Worker build identity"):
        identity.code_identity()


def test_source_runs_without_release_files_and_detects_dependency_changes(tmp_path, monkeypatch):
    source = build_source(tmp_path / "source")
    monkeypatch.setattr(identity, "__file__", str(source / "jiuwenswarm/server/supervisor/identity.py"))
    monkeypatch.setenv("JIUWENSWARM_IDENTITY_CACHE_DIR", str(tmp_path / "cache"))
    environment = {"value": "one"}
    monkeypatch.setattr(dependency_identity, "environment_identity", lambda: environment["value"])
    first = identity.code_identity()
    assert identity.code_identity() == first
    environment["value"] = "two"
    assert identity.code_identity() != first
    assert not list(source.glob("scripts/*"))


@pytest.mark.parametrize("layout", ["flat", "src", "module"])
def test_dependency_identity_detects_same_version_reinstall_and_editable_changes(tmp_path, monkeypatch, layout):
    root = tmp_path / "sdk %20 with spaces"
    code = (root / "src/example/__init__.py" if layout == "src" else
            root / "example.py" if layout == "module" else root / "example/__init__.py")
    code.parent.mkdir(parents=True)
    code.write_text("value = 1\n", encoding="utf-8")
    metadata = {"METADATA": "Name: example\nVersion: 1", "RECORD": "first",
                "top_level.txt": "example", "direct_url.json": json.dumps({
                    "url": root.as_uri(), "dir_info": {"editable": True}})}
    distribution = SimpleNamespace(metadata={"Name": "example"}, version="1", read_text=metadata.get)
    monkeypatch.setattr(dependency_identity.importlib.metadata, "distributions", lambda: [distribution])
    first = dependency_identity.environment_identity()
    metadata["RECORD"] = "second"
    second = dependency_identity.environment_identity()
    assert second != first
    code.write_text("value = 2\n", encoding="utf-8")
    assert dependency_identity.environment_identity() != second


@pytest.fixture
def isolated_worker(tmp_path, monkeypatch):
    from jiuwenswarm.common import utils

    data = tmp_path / "data"
    monkeypatch.setenv("JIUWENSWARM_DATA_DIR", str(data))
    monkeypatch.setattr(utils, "_workspace_base_dir", data)
    monkeypatch.setenv("JIUWENSWARM_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("JIUWENSWARM_IDENTITY_CACHE_DIR", str(tmp_path / "identities"))
    monkeypatch.setenv("PYTHONPATH", str(Path(__file__).resolve().parents[3]))
    return str(tmp_path / "worker")


@pytest.mark.asyncio
async def test_real_resident_worker_is_replaced_then_reused(isolated_worker, monkeypatch):
    from jiuwenswarm.server.ipc.client import connect_with_retry
    from jiuwenswarm.server.lifecycle import Readiness
    from jiuwenswarm.server.supervisor.runtime_supervisor import RuntimeSupervisor
    from jiuwenswarm.server.supervisor.shutdown import shutdown_resident_worker

    monkeypatch.setenv("JIUWENSWARM_BUILD_ID", "old-build")
    old = WorkerProcess(isolated_worker, host="127.0.0.1", port=0, stub=True)
    supervisor = RuntimeSupervisor(Readiness(), socket_path=isolated_worker, stub=True, connect_timeout=15)
    monkeypatch.setattr(supervisor, "_start_side_tasks", Mock())
    try:
        await old.spawn()
        connection = await connect_with_retry(isolated_worker, timeout=15)
        previous = await asyncio.wait_for(connection.recv(), 5)
        await connection.close()
        monkeypatch.setenv("JIUWENSWARM_BUILD_ID", "new-build")
        await supervisor._bring_up()
        assert await old.wait(timeout=5) == 0
        assert supervisor._worker_pid != previous["pid"]
        assert supervisor._worker_instance != previous["instance"]
        replacement_pid = supervisor._worker_pid
        await supervisor._conn.close()
        supervisor._conn = None
        await supervisor._bring_up()
        assert supervisor._worker_pid == replacement_pid
        await supervisor._conn.close()
        supervisor._conn = None
        result = await shutdown_resident_worker(socket_path=isolated_worker, timeout=10)
        assert result["ok"] is True
        import psutil
        assert not psutil.pid_exists(replacement_pid) or psutil.Process(replacement_pid).status() == psutil.STATUS_ZOMBIE
    finally:
        if supervisor._conn is not None:
            await supervisor._conn.close()
        if supervisor._process is not None:
            await supervisor._process.stop(timeout=2)
        await old.stop(timeout=2)


@pytest.mark.asyncio
async def test_foreign_deployment_is_rejected_without_shutdown(isolated_worker, monkeypatch):
    from jiuwenswarm.server.lifecycle import Readiness
    from jiuwenswarm.server.supervisor import runtime_supervisor as module

    connection = SimpleNamespace(recv=AsyncMock(return_value={
        "kind": "hello", "protocol": 2, "data_scope": identity.data_scope(),
        "deployment_scope": "another-installation", "code_identity": "old", "stub": False,
    }), close=AsyncMock(), send=AsyncMock())
    monkeypatch.setattr(module, "try_connect", AsyncMock(return_value=connection))
    supervisor = module.RuntimeSupervisor(Readiness(), socket_path=isolated_worker)
    with pytest.raises(RuntimeError, match="different deployment"):
        await supervisor._bring_up()
    connection.send.assert_not_awaited()
    assert supervisor._conn is None


def test_retirement_refuses_self_and_reused_pid(monkeypatch):
    hello = {"kind": "hello", "protocol": 2, "data_scope": identity.data_scope(),
             "deployment_scope": identity.deployment_scope(), "pid": os.getpid()}
    with pytest.raises(RuntimeError, match="invalid PID"):
        resident_lifecycle.worker_process(hello, "unused")
    process = SimpleNamespace(pid=123, create_time=lambda: 1)
    monkeypatch.setattr(resident_lifecycle.psutil, "Process", lambda pid: SimpleNamespace(create_time=lambda: 2))
    with pytest.raises(RuntimeError, match="PID identity changed"):
        resident_lifecycle._alive(process, 1)


@pytest.mark.parametrize("protocol", [1, 2])
@pytest.mark.parametrize("cwd", ["source", "arbitrary"])
def test_legacy_python_worker_refuses_inferred_deployment(tmp_path, monkeypatch, protocol, cwd):
    real_process = resident_lifecycle.psutil.Process
    username = real_process().username()
    pid = os.getpid() + 10_000_000
    process = SimpleNamespace(create_time=lambda: 1, username=lambda: username, exe=lambda: sys.executable,
        cmdline=lambda: [sys.executable, "-m", "jiuwenswarm.server.worker.main", "--socket", "worker"],
        parent=lambda: None, cwd=lambda: str(Path(resident_lifecycle.__file__).resolve().parents[3]
                                          if cwd == "source" else tmp_path))
    monkeypatch.setattr(resident_lifecycle.psutil, "Process", lambda value=None: process if value else real_process())
    hello = {"kind": "hello", "protocol": protocol, "pid": pid}
    with pytest.raises(RuntimeError, match="automatic termination refused"):
        resident_lifecycle.worker_process(hello, "worker", allow_legacy=True)


@pytest.mark.parametrize("peer", ["own", "other-app", "python"])
@pytest.mark.parametrize("protocol", [1, 2])
def test_legacy_desktop_ownership_requires_its_os_executable(tmp_path, monkeypatch, peer, protocol):
    real_process = resident_lifecycle.psutil.Process
    username = real_process().username()
    pid = os.getpid() + 10_000_000
    desktop = str(tmp_path / "WorkSwarm.app/Contents/Resources/backend/workswarm")
    executable = {"own": desktop, "other-app": str(tmp_path / "Other.app/Contents/Resources/backend/workswarm"),
                  "python": sys.executable}[peer]
    process = SimpleNamespace(create_time=lambda: 1, username=lambda: username, exe=lambda: executable,
        cmdline=lambda: [executable, "-m", "jiuwenswarm.server.worker.main", "--socket", "worker"])
    monkeypatch.setattr(resident_lifecycle.psutil, "Process", lambda value=None: process if value else real_process())
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", desktop)
    hello = {"kind": "hello", "protocol": protocol, "pid": pid}
    if peer == "own":
        assert resident_lifecycle.worker_process(hello, "worker", allow_legacy=True) == (process, 1)
    else:
        with pytest.raises(RuntimeError, match="unrelated process"):
            resident_lifecycle.worker_process(hello, "worker", allow_legacy=True)


def test_lifecycle_does_not_signal_an_unrelated_native_process(isolated_worker):
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        hello = {"kind": "hello", "protocol": 2, "pid": process.pid,
                 "data_scope": identity.data_scope(), "deployment_scope": identity.deployment_scope()}
        with pytest.raises(RuntimeError, match="unrelated process"):
            resident_lifecycle.worker_process(hello, isolated_worker)
        assert process.poll() is None
    finally:
        process.terminate()
        process.wait(timeout=5)


@pytest.mark.asyncio
async def test_owned_protocol_one_without_shutdown_ack_waits_for_exit(isolated_worker):
    from jiuwenswarm.server.ipc.client import connect_with_retry

    worker = WorkerProcess(isolated_worker, host="127.0.0.1", port=0, stub=True)
    connection = None
    try:
        await worker.spawn()
        connection = await connect_with_retry(isolated_worker, timeout=15)
        hello = await connection.recv()
        # Protocol-one exit handling does not waive the deployment check.
        hello["protocol"] = 1

        async def legacy_recv():
            frame = await connection.recv()
            if frame.get("kind") == "shutdown.done":
                raise asyncio.IncompleteReadError(b"", 1)
            return frame

        legacy = SimpleNamespace(send=connection.send, recv=legacy_recv)
        result = await resident_lifecycle.retire_worker(
            legacy, hello, isolated_worker, deadline=time.monotonic() + 10, allow_legacy=True)
        assert result["legacy"] is True
        assert await worker.wait(timeout=1) == 0
    finally:
        if connection is not None:
            await connection.close()
        await worker.stop(timeout=2)


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", [1, 2])
@pytest.mark.parametrize("operation", ["startup", "reattach", "shutdown"])
async def test_unknown_python_deployment_stays_alive_without_shutdown_or_replacement(
    isolated_worker, monkeypatch, protocol, operation,
):
    from jiuwenswarm.server.ipc.client import connect_with_retry
    from jiuwenswarm.server.lifecycle import Readiness
    from jiuwenswarm.server.supervisor import runtime_supervisor, shutdown

    worker = WorkerProcess(isolated_worker, host="127.0.0.1", port=0, stub=True)
    connection = None
    try:
        await worker.spawn()
        connection = await connect_with_retry(isolated_worker, timeout=15)
        hello = await asyncio.wait_for(connection.recv(), 5)
        legacy = {key: value for key, value in hello.items() if key not in {
            "deployment_scope", "process_created",
        }}
        legacy.update(protocol=protocol, code_identity=(hello["code_identity"]
                      if operation == "reattach" else "different-build"))
        peer = SimpleNamespace(recv=AsyncMock(return_value=legacy), send=AsyncMock(wraps=connection.send),
                               close=AsyncMock(wraps=connection.close))
        constructor = Mock(side_effect=AssertionError("An unowned Worker must not be replaced"))
        monkeypatch.setattr(runtime_supervisor, "WorkerProcess", constructor)
        if operation in {"startup", "reattach"}:
            monkeypatch.setattr(runtime_supervisor, "try_connect", AsyncMock(return_value=peer))
            front = runtime_supervisor.RuntimeSupervisor(Readiness(), socket_path=isolated_worker, stub=True)
            with pytest.raises(RuntimeError, match="automatic termination refused"):
                await front._bring_up()
            assert front._conn is None
            assert front._fatal
        else:
            monkeypatch.setattr(shutdown, "try_connect", AsyncMock(return_value=peer))
            with pytest.raises(RuntimeError, match="automatic termination refused"):
                await shutdown.shutdown_resident_worker(socket_path=isolated_worker, timeout=5)
        peer.send.assert_not_awaited()
        peer.close.assert_awaited_once()
        constructor.assert_not_called()
        assert worker.returncode is None
    finally:
        if connection is not None:
            await connection.close()
        await worker.stop(timeout=2)


@pytest.mark.asyncio
async def test_concurrent_fronts_replace_once_and_busy_front_cannot_spawn(isolated_worker, monkeypatch):
    from jiuwenswarm.server.ipc.client import connect_with_retry
    from jiuwenswarm.server.lifecycle import Readiness
    from jiuwenswarm.server.supervisor import runtime_supervisor as module
    from jiuwenswarm.server.supervisor.shutdown import shutdown_resident_worker

    monkeypatch.setenv("JIUWENSWARM_BUILD_ID", "old")
    old = WorkerProcess(isolated_worker, host="127.0.0.1", port=0, stub=True)
    fronts = [module.RuntimeSupervisor(Readiness(), socket_path=isolated_worker, stub=True, connect_timeout=10)
              for _ in range(2)]
    for front in fronts:
        monkeypatch.setattr(front, "_start_side_tasks", Mock())
    try:
        await old.spawn()
        connection = await connect_with_retry(isolated_worker, timeout=15)
        await connection.recv()
        await connection.close()
        monkeypatch.setenv("JIUWENSWARM_BUILD_ID", "new")
        monkeypatch.setattr(module, "_HELLO_TIMEOUT", .5)
        constructor = Mock(wraps=WorkerProcess)
        monkeypatch.setattr(module, "WorkerProcess", constructor)
        results = await asyncio.gather(*(front._bring_up() for front in fronts), return_exceptions=True)
        assert sum(result is None for result in results) == 1
        assert sum(isinstance(result, TimeoutError) for result in results) == 1
        assert constructor.call_count == 1
        assert await old.wait(timeout=1) == 0
        for front in fronts:
            if front._conn is not None:
                await front._conn.close()
                front._conn = None
        assert (await shutdown_resident_worker(socket_path=isolated_worker, timeout=10))["ok"] is True
    finally:
        for front in fronts:
            if front._conn is not None:
                await front._conn.close()
            if front._process is not None:
                await front._process.stop(timeout=2)
        await old.stop(timeout=2)


def test_named_service_stop_uses_selected_package_and_instance_data(tmp_path, monkeypatch):
    from jiuwenswarm import start_services
    from jiuwenswarm.instance_manager.config import InstanceConfig

    run = Mock(return_value=SimpleNamespace(returncode=0))
    monkeypatch.setattr(start_services.subprocess, "run", run)
    config = InstanceConfig("example", tmp_path / "instance")
    assert start_services._stop_instance_worker(config)
    args = run.call_args
    assert args.args[0][:3] == [sys.executable, "-m", "jiuwenswarm.server.supervisor.shutdown"]
    assert args.kwargs["env"]["JIUWENSWARM_DATA_DIR"] == str(config.workspace)
    run.return_value.returncode = 1
    assert not start_services._stop_instance_worker(config)


@pytest.mark.asyncio
async def test_shutdown_ack_cannot_hide_a_process_that_keeps_running(isolated_worker, monkeypatch):
    import psutil

    executable = getattr(sys, "_base_executable", sys.executable)
    process = subprocess.Popen([executable, "-c", "import time; time.sleep(30)"])
    observed = psutil.Process(process.pid)
    monkeypatch.setattr(resident_lifecycle, "worker_process", lambda *args, **kwargs: (observed, observed.create_time()))
    connection = SimpleNamespace(send=AsyncMock(), recv=AsyncMock(return_value={"kind": "shutdown.done", "ok": True}))
    try:
        with pytest.raises(RuntimeError, match="persistence incomplete"):
            await resident_lifecycle.retire_worker(connection, {"protocol": 2}, isolated_worker,
                                                  deadline=time.monotonic() + .5)
        assert process.wait(timeout=1) is not None
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=1)


def test_wheel_build_without_release_scripts_stamps_only_the_artifact(tmp_path):
    """Exercise setuptools rather than assuming package-data includes generated files."""
    root = Path(__file__).resolve().parents[3]
    source = tmp_path / "source"
    (source / "jiuwenswarm/common").mkdir(parents=True)
    for package in (source / "jiuwenswarm", source / "jiuwenswarm/common"):
        (package / "__init__.py").write_text("", encoding="utf-8")
    shutil.copyfile(root / "setup.py", source / "setup.py")
    shutil.copyfile(root / "jiuwenswarm/common/worker_build_identity.py",
                    source / "jiuwenswarm/common/worker_build_identity.py")
    (source / "pyproject.toml").write_text('''[project]
name = "workswarm"
version = "1.2.3"
[tool.setuptools.packages.find]
where = ["."]
[tool.setuptools.package-data]
jiuwenswarm = ["_worker_build_identity.json", "resources/*"]
''', encoding="utf-8")
    build = tmp_path / "build"
    (build / "egg").mkdir(parents=True)
    env = os.environ.copy()
    for name in ("WORKSWARM_SOURCE_ROOT", "WORKSWARM_PACKAGING_ROOT", "WORKSWARM_WORKER_IDENTITY"):
        env.pop(name, None)
    identity_input = build / "input.json"
    supplied = write_build_identity(identity_input, source)
    env["WORKSWARM_WORKER_IDENTITY"] = str(identity_input)
    identities = []
    for attempt in ("first", "second"):
        output = build / attempt
        result = subprocess.run([
            sys.executable, "setup.py", "egg_info", "--egg-base", str(build / "egg"),
            "build", "--build-base", str(build / "lib"), "bdist_wheel",
            "--dist-dir", str(output), "--bdist-dir", str(build / "bdist"),
        ], cwd=source, env=env, capture_output=True, text=True, encoding="utf-8", timeout=60)
        assert result.returncode == 0, result.stdout + result.stderr
        with ZipFile(next(output.glob("*.whl"))) as wheel:
            value = json.loads(wheel.read("jiuwenswarm/_worker_build_identity.json"))
            identities.append(value["backend_build_id"])
            assert value["schema"] == 2
            assert value["input_build_id"] == supplied["backend_build_id"]
            assert json.loads(wheel.read("jiuwenswarm/resources/build-metadata.json"))["version"] == "1.2.3"
    assert len({*identities, supplied["backend_build_id"]}) == 3
    assert not (source / "scripts").exists()
    assert not (source / "jiuwenswarm/_worker_build_identity.json").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", [False, True])
async def test_dmg_cannot_retire_python_or_whl_worker(isolated_worker, tmp_path, monkeypatch, legacy):
    from jiuwenswarm.server.ipc.client import connect_with_retry

    worker = WorkerProcess(isolated_worker, host="127.0.0.1", port=0, stub=True)
    connection = None
    try:
        await worker.spawn()
        connection = await connect_with_retry(isolated_worker, timeout=15)
        hello = await connection.recv()
        if legacy:
            hello.pop("deployment_scope")
            hello["protocol"] = 1
        connection.send = AsyncMock(wraps=connection.send)
        with monkeypatch.context() as desktop:
            desktop.setattr(sys, "frozen", True, raising=False)
            desktop.setattr(sys, "executable", str(tmp_path / "WorkSwarm.app/Contents/Resources/backend/workswarm"))
            with pytest.raises(RuntimeError, match="different deployment|unrelated process"):
                await resident_lifecycle.retire_worker(connection, hello, isolated_worker,
                    deadline=time.monotonic() + 2, allow_legacy=True)
        connection.send.assert_not_awaited()
        assert worker.returncode is None
        await connection.send({"kind": "shutdown"})
        if hello["protocol"] != 1:
            while (await connection.recv()).get("kind") != "shutdown.done":
                pass
        assert await worker.wait(timeout=5) == 0
    finally:
        if connection is not None:
            await connection.close()
        await worker.stop(timeout=2)


@pytest.mark.asyncio
async def test_startup_migrates_legacy_endpoint_even_without_owner_lease_detection(isolated_worker, monkeypatch):
    from jiuwenswarm.server.ipc.client import connect_with_retry
    from jiuwenswarm.server.lifecycle import Readiness
    from jiuwenswarm.server.supervisor import runtime_supervisor as module, worker_process
    from jiuwenswarm.server.supervisor.shutdown import shutdown_resident_worker

    legacy_path = isolated_worker + "-legacy"
    legacy = WorkerProcess(legacy_path, host="127.0.0.1", port=0, stub=True)
    current_path = worker_process.default_socket_path()
    front = module.RuntimeSupervisor(Readiness(), socket_path=current_path, stub=True, connect_timeout=15)
    monkeypatch.setattr(front, "_start_side_tasks", Mock())
    monkeypatch.setattr(worker_process, "legacy_socket_paths", lambda: [legacy_path])
    monkeypatch.setattr(module, "endpoint_is_listening", lambda path: False)
    try:
        await legacy.spawn()
        connection = await connect_with_retry(legacy_path, timeout=15)
        old_hello = await connection.recv()
        await connection.close()
        await front._bring_up()
        assert await legacy.wait(timeout=1) == 0
        assert front._worker_pid != old_hello["pid"]
        await front._conn.close()
        front._conn = None
        assert (await shutdown_resident_worker(socket_path=current_path, timeout=10))["ok"] is True
    finally:
        if front._conn is not None:
            await front._conn.close()
        if front._process is not None:
            await front._process.stop(timeout=2)
        await legacy.stop(timeout=2)

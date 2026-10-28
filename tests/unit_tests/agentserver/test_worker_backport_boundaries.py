# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Exercise the production boundaries added to the protocol-1 backport."""

from __future__ import annotations

import ast
import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import psutil
import pytest

from jiuwenswarm.server.lifecycle import Readiness
from jiuwenswarm.server.supervisor import dependency_identity, identity, shutdown
from jiuwenswarm.server.supervisor.runtime_supervisor import RuntimeSupervisor

ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.parametrize("layout", ["checkout", "site-packages"])
@pytest.mark.parametrize("changed", ["server", "lock"])
def test_content_fallback_observes_real_code_and_root_lock(tmp_path, monkeypatch, layout, changed):
    root = tmp_path / layout
    package = root / "jiuwenswarm"
    code = package / "server" / "probe.py"
    code.parent.mkdir(parents=True)
    code.write_text("value = 1\n", encoding="utf-8")
    lock = root / "uv.lock"
    lock.write_text("version = 1\n", encoding="utf-8")
    monkeypatch.setattr(identity, "__file__", str(package / "server/supervisor/identity.py"))
    monkeypatch.setattr(dependency_identity, "environment_identity", lambda: "same-environment")
    before = identity.code_identity()
    (code if changed == "server" else lock).write_text("value = 2\n", encoding="utf-8")
    assert identity.code_identity() != before
    assert not (root / "scripts").exists()
    assert not (package / "_worker_build_identity.json").exists()


def _entry_functions(path, names):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
    namespace = {"sys": sys, "Path": Path, "_ensure_stdio": lambda: None}
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id.startswith("_DESKTOP"):
                    namespace[target.id] = node.value.value
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), "exec"), namespace)
    return namespace


def test_frozen_lifecycle_dispatch_enforces_receipt_then_runs_real_cli(tmp_path, monkeypatch, capsys):
    entry = _entry_functions(ROOT / "scripts/jiuwenswarm_exe_entry.py", {"_dispatch", "_pop_flag"})
    worker_shutdown = AsyncMock(return_value={"ok": True, "absent": True})
    monkeypatch.setattr(shutdown, "shutdown_resident_worker", worker_shutdown)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    receipt = tmp_path / "receipt"
    argv = ["workswarm.exe", "--desktop-run-worker-lifecycle", "--shutdown-worker",
            "--shutdown-receipt", str(receipt)]
    monkeypatch.setattr(sys, "argv", argv.copy())
    with pytest.raises(RuntimeError):
        entry["_dispatch"]()
    worker_shutdown.assert_not_awaited()
    receipt.write_text("ok\n", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", argv.copy())
    assert entry["_dispatch"]() is None
    worker_shutdown.assert_awaited_once()
    assert json.loads(capsys.readouterr().out) == {"ok": True, "absent": True}


@pytest.mark.asyncio
async def test_unobserved_reattached_exit_fails_and_keeps_identity_for_retry(monkeypatch):
    process = SimpleNamespace(create_time=lambda: 1.0, kill=Mock())
    process.wait = Mock(side_effect=psutil.TimeoutExpired(0))
    monkeypatch.setattr(psutil, "Process", lambda pid: process)
    peer = SimpleNamespace(send=AsyncMock(), close=AsyncMock())
    front = RuntimeSupervisor(Readiness())
    front._conn = front._validated_conn = peer
    front._worker_pid, front._worker_create_time = 987654, 1.0
    with pytest.raises(RuntimeError, match="exit could not be observed"):
        await front.stop(terminate_worker=True)
    assert front._worker_pid == 987654 and front._worker_create_time == 1.0
    assert front._process is None
    process.kill.assert_called_once()
    process.wait.return_value, process.wait.side_effect = 0, None
    await front.stop(terminate_worker=True)
    assert not front._owns_process


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform != "win32", reason="Windows descendant lifetime regression")
@pytest.mark.parametrize("method", ["desktop", "cli", "force_cli"])
async def test_windows_front_tree_exit_preserves_worker_until_validated_ipc_stop(tmp_path, monkeypatch, method):
    from jiuwenswarm.common import utils

    data = tmp_path / "data"
    monkeypatch.setenv("PYTHONPATH", str(ROOT))
    monkeypatch.setenv("JIUWENSWARM_DATA_DIR", str(data))
    monkeypatch.setattr(utils, "_workspace_base_dir", data)
    marker = tmp_path / "worker.json"
    socket = str(tmp_path / "worker")
    script = """
import asyncio,json,psutil,sys
print('Front process initialized',flush=True)
from pathlib import Path
from jiuwenswarm.server.supervisor.worker_process import WorkerProcess
from jiuwenswarm.server.ipc.client import connect_with_retry
async def run():
    worker=WorkerProcess(sys.argv[1],host='127.0.0.1',port=0,stub=True)
    await worker.spawn()
    peer=await connect_with_retry(sys.argv[1],timeout=15)
    hello=await peer.recv()
    await peer.close()
    children=[{'pid':p.pid,'created':p.create_time()} for p in psutil.Process().children(recursive=True)]
    Path(sys.argv[2]).write_text(json.dumps({'hello':hello,'children':children}),encoding='utf-8')
    await asyncio.Event().wait()
asyncio.run(run())
"""
    with (tmp_path / "front.log").open("wb") as log:
        front = subprocess.Popen([sys.executable, "-c", script, socket, str(marker), "--desktop-run-agent"],
            env=os.environ.copy(), stdout=log, stderr=subprocess.STDOUT)
    observed = None
    try:
        deadline = time.monotonic() + 60
        while not marker.exists() and time.monotonic() < deadline:
            if front.poll() is not None:
                pytest.fail((tmp_path / "front.log").read_text(encoding="utf-8", errors="replace"))
            await asyncio.sleep(.05)
        assert marker.exists(), (tmp_path / "front.log").read_text(encoding="utf-8", errors="replace")
        observed = json.loads(marker.read_text(encoding="utf-8"))
        native = psutil.Process(observed["hello"]["pid"])
        if method == "desktop":
            entry = _entry_functions(ROOT / "jiuwenswarm/channels/desktop/desktop_app.py", {"_psutil_terminate"})
            entry["_psutil_terminate"](front.pid)
        else:
            command = [sys.executable, "-m", "jiuwenswarm.server.supervisor.shutdown", "--detach-front", str(front.pid)]
            if method == "force_cli":
                command.append("--force")
            await asyncio.to_thread(subprocess.run, command, env=os.environ.copy(), check=True,
                                    capture_output=True, timeout=10)
        await asyncio.to_thread(front.wait, 3)
        assert native.is_running(), "Front tree termination must retain its resident Worker"
        result = await shutdown.shutdown_resident_worker(socket_path=socket, timeout=5)
        assert result["ok"] is True
        await asyncio.to_thread(native.wait, 3)
        assert not native.is_running()
    finally:
        if front.poll() is None:
            front.kill()
            await asyncio.to_thread(front.wait, 3)
        for child in observed["children"] if observed else []:
            try:
                process = psutil.Process(child["pid"])
                if process.create_time() == child["created"]:
                    process.kill()
                    await asyncio.to_thread(process.wait, 3)
            except psutil.NoSuchProcess:
                pass

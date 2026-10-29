# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Failure paths from the build ownership and desktop lifecycle review."""
import ast
import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import psutil
import pytest

from jiuwenswarm.common import update_shutdown
from jiuwenswarm.server.supervisor import dependency_identity, resident_lifecycle, shutdown


@pytest.mark.parametrize("failure", ["cmdline", "ppid", "terminate", "trailing_m"])
def test_python_desktop_tree_reports_failure_or_cleans_all_known_processes(monkeypatch, failure):
    front = SimpleNamespace(pid=11, cmdline=Mock(return_value=["python", "-m", "agentserver"]),
                            ppid=Mock(return_value=10), terminate=Mock(), kill=Mock())
    parent = SimpleNamespace(pid=10, children=lambda **kw: [front], terminate=Mock(), kill=Mock())
    if failure == "trailing_m":
        front.cmdline.return_value = ["python", "-m"]
    else:
        getattr(front, failure).side_effect = psutil.AccessDenied(11)
    monkeypatch.setattr(psutil, "Process", lambda pid: parent)
    path = Path(__file__).resolve().parents[3] / "jiuwenswarm/channels/desktop/desktop_app.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    method = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "_psutil_terminate")
    namespace = {}
    exec(compile(ast.Module(body=[method], type_ignores=[]), str(path), "exec"), namespace)
    if failure == "trailing_m":
        namespace[method.name](10)
        front.terminate.assert_called_once()
        parent.terminate.assert_called_once()
    else:
        with pytest.raises(RuntimeError, match="ownership could not be established|termination incomplete"):
            namespace[method.name](10)
        parent.terminate.assert_not_called()


def test_receipt_replace_failure_cleans_temporary_and_preserves_pending(tmp_path, monkeypatch):
    receipt = update_shutdown.create_shutdown_receipt(tmp_path)
    monkeypatch.setattr(update_shutdown.os, "replace", Mock(side_effect=OSError("read only")))
    with pytest.raises(OSError, match="read only"):
        update_shutdown.complete_shutdown_receipt(receipt, ok=True)
    assert list(tmp_path.iterdir()) == [receipt]
    assert receipt.read_bytes() == b"pending\n"
    with pytest.raises(RuntimeError, match="Update refused"):
        update_shutdown.require_clean_shutdown(receipt)


def test_corrupt_dependency_is_named_and_never_silently_omitted(monkeypatch):
    distribution = SimpleNamespace(metadata={"Name": "example"}, version="1", read_text=lambda _: "{broken")
    monkeypatch.setattr(dependency_identity.importlib.metadata, "distributions", lambda: [distribution])
    with pytest.raises(RuntimeError, match="Dependency identity could not be verified: example") as result:
        dependency_identity.environment_identity()
    assert isinstance(result.value.__cause__, json.JSONDecodeError)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["enumerate", "child_birth"])
async def test_retirement_still_reaps_owned_root_when_descendant_disappears(tmp_path, monkeypatch, failure):
    child = subprocess.Popen([getattr(sys, "_base_executable", sys.executable), "-c", "import time; time.sleep(60)"])
    process = psutil.Process(child.pid)
    created = process.create_time()
    monkeypatch.setattr(resident_lifecycle, "worker_process", lambda *a, **kw: (process, created))
    if failure == "enumerate":
        monkeypatch.setattr(process, "children", Mock(side_effect=psutil.NoSuchProcess(child.pid)))
    else:
        descendant = SimpleNamespace(create_time=Mock(side_effect=psutil.NoSuchProcess(999999)))
        monkeypatch.setattr(process, "children", lambda **kw: [descendant])
    connection = SimpleNamespace(send=AsyncMock(), recv=AsyncMock(side_effect=asyncio.IncompleteReadError(b"", 1)))
    try:
        with pytest.raises(RuntimeError, match="Worker forced to exit"):
            await resident_lifecycle.retire_worker(
                connection, {"protocol": 2}, str(tmp_path / "worker"), deadline=time.monotonic() + 1,
            )
        assert child.wait(timeout=2) is not None
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=2)


@pytest.mark.parametrize("force", [False, True])
def test_detach_keeps_unreadable_tool_in_verified_worker_subtree(monkeypatch, force):
    def native(pid, argv, ppid):
        return SimpleNamespace(pid=pid, cmdline=Mock(return_value=argv), ppid=lambda: ppid,
                               terminate=Mock(), kill=Mock())

    parent = native(10, ["launcher"], 1)
    front = native(11, ["python", "-m", "jiuwenswarm.server.app_agentserver"], 10)
    malformed = native(12, ["python", "-m"], 10)
    unknown = native(13, ["python", "-m", "jiuwenswarm.server.worker.main"], 10)
    tool = native(14, ["tool"], 13)
    tool.cmdline.side_effect = psutil.AccessDenied(14)
    parent.children = lambda **kw: [front, malformed, unknown, tool]
    monkeypatch.setattr(psutil, "Process", lambda pid: parent)
    monkeypatch.setattr(psutil, "wait_procs", lambda targets, timeout: (targets, []))
    shutdown.detach_front_process(10, force=force)
    for process in (unknown, tool):
        process.terminate.assert_not_called()
        process.kill.assert_not_called()
    for process in (parent, front, malformed):
        (process.kill if force else process.terminate).assert_called_once()
        (process.terminate if force else process.kill).assert_not_called()


@pytest.mark.parametrize("failure", ["cmdline", "ppid", "terminate", "alive"])
def test_detach_never_reports_success_for_unverified_or_surviving_front(monkeypatch, failure):
    front = SimpleNamespace(pid=11, cmdline=Mock(return_value=["python", "-m", "jiuwenswarm.server.app_agentserver"]),
                            ppid=Mock(return_value=10), terminate=Mock(), kill=Mock())
    parent = SimpleNamespace(pid=10, cmdline=lambda: ["uv", "run", "python", "-m",
                                                   "jiuwenswarm.server.app_agentserver"],
                             children=lambda **kw: [front], terminate=Mock(), kill=Mock())
    if failure in {"cmdline", "ppid", "terminate"}:
        getattr(front, failure).side_effect = psutil.AccessDenied(11)
    monkeypatch.setattr(psutil, "Process", lambda pid: parent)
    monkeypatch.setattr(psutil, "wait_procs", lambda targets, timeout: ([], [front]))
    with pytest.raises(RuntimeError, match="Front detach incomplete"):
        shutdown.detach_front_process(parent.pid)
    parent.terminate.assert_not_called()
    if failure in {"cmdline", "ppid"}:
        front.terminate.assert_not_called()


def test_shutdown_cli_reports_non_ok_as_nonzero(monkeypatch, capsys):
    monkeypatch.setattr(shutdown, "shutdown_resident_worker", AsyncMock(return_value={"ok": False}))
    monkeypatch.setattr(sys, "argv", ["shutdown", "--shutdown-worker"])
    with pytest.raises(SystemExit) as result:
        shutdown.main()
    assert result.value.code == 1
    assert json.loads(capsys.readouterr().out) == {"ok": False}


@pytest.mark.parametrize("error,exists", [(121, True), (231, True), (2, False)])
def test_pipe_probe_reads_error_from_the_same_native_call(monkeypatch, error, exists):
    import ctypes
    from jiuwenswarm.server.ipc.client import _windows_pipe_is_listening

    wait = Mock(return_value=0)
    loader = Mock(return_value=SimpleNamespace(WaitNamedPipeW=wait))
    monkeypatch.setattr(ctypes, "WinDLL", loader, raising=False)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: error, raising=False)
    assert _windows_pipe_is_listening("test-pipe") is exists
    loader.assert_called_once_with("kernel32", use_last_error=True)
    wait.assert_called_once_with("test-pipe", 1)

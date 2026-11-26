# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Bounded incident files survive Worker replacement without mixed stderr."""

import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from jiuwenswarm.server.supervisor.diagnostics import dump_runtime_state, process_resources


def test_resource_collection_failure_is_not_a_supervisor_failure(monkeypatch):
    import psutil

    def unavailable():
        raise OSError("OS counters unavailable")

    monkeypatch.setattr(psutil, "virtual_memory", unavailable)
    assert process_resources(os.getpid()) == {"resources_unavailable": "OSError"}


def test_diagnostic_storage_error_does_not_break_worker_heartbeat(monkeypatch):
    from jiuwenswarm.server.worker import diagnostics

    monkeypatch.setattr(diagnostics, "_enabled", True)
    monkeypatch.setattr(diagnostics, "_last_arm", 0.0)
    monkeypatch.setattr(diagnostics, "_fault_output", None)

    def unavailable(*args, **kwargs):
        raise OSError("disk unavailable")

    monkeypatch.setattr(diagnostics.faulthandler, "dump_traceback_later", unavailable)
    diagnostics.arm_stall_dump(beat_age=0.0)
    assert diagnostics._last_arm > 0


@pytest.mark.asyncio
async def test_supervisor_dump_retention_and_resource_evidence(tmp_path):
    for index in range(15):
        (tmp_path / f"supervisor-old-{index}.txt").write_text("old", encoding="utf-8")
    snapshot = {"reason": "runtime worker closed", "resources": process_resources(os.getpid())}
    path = dump_runtime_state(tmp_path, snapshot)
    assert len(list(tmp_path.glob("supervisor-*.txt"))) == 12
    first = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert first["resources"]["rss"] > 0
    assert first["resources"]["threads"] > 0
    assert "command_line" not in first["resources"]


@pytest.mark.parametrize("fail_enable", [False, True])
def test_fault_file_is_closed_after_handlers_or_failed_enable(monkeypatch, tmp_path, fail_enable):
    from contextlib import ExitStack
    from jiuwenswarm.common import paths
    from jiuwenswarm.server.worker import diagnostics

    monkeypatch.setattr(paths, "get_agent_logs_dir", lambda: tmp_path)
    monkeypatch.setattr(diagnostics, "_enabled", False)
    monkeypatch.setattr(diagnostics, "_fault_output", None)
    monkeypatch.setattr(diagnostics, "_fault_resources", ExitStack())
    monkeypatch.setattr(diagnostics, "_last_arm", 0.0)
    opened = []
    events = []
    original_open = Path.open

    def open_file(path, *args, **kwargs):
        output = original_open(path, *args, **kwargs)
        opened.append(output)
        return output

    def enable(*, file=None, all_threads=True):
        if file is not None and fail_enable:
            raise OSError("fault handler unavailable")
        events.append("enable")

    def disable():
        assert all(not output.closed for output in opened) or fail_enable
        events.append("disable")

    monkeypatch.setattr(Path, "open", open_file)
    monkeypatch.setattr(diagnostics.faulthandler, "enable", enable)
    monkeypatch.setattr(diagnostics.faulthandler, "disable", disable)
    monkeypatch.setattr(diagnostics.faulthandler, "cancel_dump_traceback_later", lambda: events.append("cancel"))
    try:
        diagnostics.enable_worker_diagnostics()
        diagnostics.enable_worker_diagnostics()
        assert len(opened) == 1
        assert opened[0].closed is fail_enable
        assert diagnostics._enabled is True
    finally:
        diagnostics.disable_worker_diagnostics()
    assert events == ["enable", "cancel", "disable"]
    assert opened[0].closed
    assert diagnostics._fault_output is None
    assert diagnostics._enabled is False
    diagnostics.disable_worker_diagnostics()


def test_native_process_abort_leaves_its_own_fault_file(tmp_path):
    env = dict(os.environ)
    env["JIUWENSWARM_DATA_DIR"] = str(tmp_path / "data")
    old_directory = tmp_path / "data" / "logs" / "agent" / "runtime_diagnostics"
    old_directory.mkdir(parents=True)
    for index in range(10):
        (old_directory / f"worker-old-{index}.fault.log").write_text("old", encoding="utf-8")
    source = Path(__file__).resolve().parents[3]
    env["PYTHONPATH"] = str(source)
    # Disable Windows' interactive crash dialog only in this expendable child.
    code = (
        "import os, sys\n"
        "if sys.platform == 'win32':\n"
        " import ctypes; ctypes.windll.kernel32.SetErrorMode(0x0002)\n"
        "from jiuwenswarm.server.worker.diagnostics import enable_worker_diagnostics\n"
        "enable_worker_diagnostics()\n"
        "os.abort()\n"
    )
    completed = subprocess.run([sys.executable, "-X", "utf8", "-c", code], env=env,
                               cwd=tmp_path, capture_output=True, timeout=60)
    assert completed.returncode != 0
    files = list((tmp_path / "data").rglob("worker-*.fault.log"))
    assert len(files) == 8
    current = [path for path in files if not path.name.startswith("worker-old-")]
    assert len(current) == 1
    assert "Fatal Python error" in current[0].read_text(encoding="utf-8")

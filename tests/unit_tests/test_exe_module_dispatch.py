# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Frozen executable's argument dispatch contract."""

from __future__ import annotations

import argparse
import importlib.util
import os
from pathlib import Path
import runpy
import sys


def test_frozen_module_receives_only_its_own_arguments(monkeypatch) -> None:
    entry_path = Path(__file__).resolve().parents[2] / "scripts" / "jiuwenswarm_exe_entry.py"
    spec = importlib.util.spec_from_file_location("test_exe_module_dispatch_entry", entry_path)
    assert spec is not None and spec.loader is not None
    entry = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(entry)

    original_argv = [
        "workswarm.exe",
        "-m",
        "jiuwenswarm.server.worker.main",
        "--socket",
        "worker.pipe",
    ]
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "argv", original_argv)
    monkeypatch.setattr(entry, "_ensure_stdio", lambda: None)

    def run_module(module_name: str, *, run_name: str, alter_sys: bool) -> None:
        assert module_name == "jiuwenswarm.server.worker.main"
        assert run_name == "__main__"
        assert alter_sys is True
        parser = argparse.ArgumentParser()
        parser.add_argument("--socket", required=True)
        assert parser.parse_args(sys.argv[1:]).socket == "worker.pipe"

    monkeypatch.setattr(runpy, "run_module", run_module)
    assert entry._dispatch() == 0
    assert sys.argv is original_argv


def test_frozen_interpreter_flags_stripped_and_pyz_module_resolved(monkeypatch) -> None:
    """Frozen exe strips -I/-S flags and resolves PYZ-embedded scripts (issue #6973).

    The SDK's ``wrap_posix_command`` launches
    ``sys.executable -I -S <_posix_command.py> /bin/sh -c <cmd>``.
    In a frozen exe, ``sys.executable`` is the desktop binary and
    ``_posix_command.py`` lives inside the PYZ archive, not on disk.
    The entry must strip the interpreter flags and resolve the script as
    a module from the frozen bundle.
    """
    entry_path = Path(__file__).resolve().parents[2] / "scripts" / "jiuwenswarm_exe_entry.py"
    spec = importlib.util.spec_from_file_location("test_exe_module_dispatch_entry", entry_path)
    assert spec is not None and spec.loader is not None
    entry = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(entry)

    meipass = "/fake/meipass"
    script_path = os.path.join(
        meipass, "openjiuwen", "core", "sys_operation", "_posix_command.py"
    )

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", meipass, raising=False)
    monkeypatch.setattr(
        sys, "argv",
        ["workswarm.exe", "-I", "-S", script_path, "/bin/sh", "-c", "echo hello"],
    )
    monkeypatch.setattr(entry, "_ensure_stdio", lambda: None)

    # Simulate the module being found inside the PYZ archive.
    monkeypatch.setattr(
        importlib.util, "find_spec",
        lambda name: object() if name == "openjiuwen.core.sys_operation._posix_command" else None,
    )

    captured: dict = {}

    def run_module(module_name: str, *, run_name: str, alter_sys: bool) -> None:
        captured["module_name"] = module_name
        captured["run_name"] = run_name
        captured["alter_sys"] = alter_sys
        # Verify -I and -S were stripped from argv.
        assert "-I" not in sys.argv
        assert "-S" not in sys.argv
        # Verify the script path was removed from argv.
        assert script_path not in sys.argv
        # Verify the remaining args are the command to exec.
        assert sys.argv[1:] == ["/bin/sh", "-c", "echo hello"]

    monkeypatch.setattr(runpy, "run_module", run_module)

    assert entry._dispatch() == 0
    assert captured["module_name"] == "openjiuwen.core.sys_operation._posix_command"
    assert captured["run_name"] == "__main__"
    assert captured["alter_sys"] is True


def test_frozen_unrecognized_args_return_error(monkeypatch) -> None:
    """Frozen exe returns non-zero for unrecognized arguments (issue #6973).

    Previously the catch-all ``return 0`` caused callers to mistake
    non-execution for success.
    """
    entry_path = Path(__file__).resolve().parents[2] / "scripts" / "jiuwenswarm_exe_entry.py"
    spec = importlib.util.spec_from_file_location("test_exe_module_dispatch_entry", entry_path)
    assert spec is not None and spec.loader is not None
    entry = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(entry)

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "argv", ["workswarm.exe", "--totally-unknown-flag"])
    monkeypatch.setattr(entry, "_ensure_stdio", lambda: None)
    # Suppress error output written to fd 2.
    monkeypatch.setattr(os, "write", lambda fd, data: len(data))

    assert entry._dispatch() == 1

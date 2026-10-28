# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Frozen executable's ``-m`` argument contract."""

from __future__ import annotations

import argparse
import importlib.util
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

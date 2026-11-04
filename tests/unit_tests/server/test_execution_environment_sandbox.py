# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Sandbox transport preserves deletion markers until the execution host."""

import importlib.util
import sys
from pathlib import Path

import pytest


def test_sandbox_daemon_removes_inherited_values(monkeypatch: pytest.MonkeyPatch) -> None:
    root = Path(__file__).resolve().parents[3]
    path = root / "jiuwenbox/src/jiuwenbox/supervisor/sandbox_daemon.py"
    spec = importlib.util.spec_from_file_location("execution_environment_daemon_test", path)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    monkeypatch.setenv("JW_REMOVE_ME", "host-value")
    overlay = module._normalize_env({"JW_REMOVE_ME": None, "JW_MEMBER": "alice"})
    assert overlay["JW_REMOVE_ME"] is None
    child = module.build_child_env(overlay)
    assert "JW_REMOVE_ME" not in child
    assert child["JW_MEMBER"] == "alice"

# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""skill-omni-creation environment gate: interpreter re-exec per platform."""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from jiuwenswarm.common.utils import get_builtin_skills_dir


def _load_environment_gate() -> ModuleType:
    path = get_builtin_skills_dir() / "skill-omni-creation" / "scripts" / "environment_gate.py"
    spec = importlib.util.spec_from_file_location("_skill_omni_environment_gate", path)
    assert spec is not None and spec.loader is not None, path
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def gate(monkeypatch, tmp_path) -> ModuleType:
    module = _load_environment_gate()
    monkeypatch.setattr(module, "_STATUS_DIR", tmp_path)
    monkeypatch.setattr(module, "_STATUS_FILE", tmp_path / "environment_status.json")
    # The re-exec marks os.environ; setenv makes monkeypatch restore it afterwards.
    monkeypatch.setenv(module._REEXEC_FLAG, "0")
    return module


def _fake_os(name: str, **overrides) -> SimpleNamespace:
    # Simulate only the gate's platform; pathlib and pytest need the real OS.
    return SimpleNamespace(
        name=name, environ=os.environ, path=os.path, fspath=os.fspath, **overrides
    )


def _expected_argv(selected: Path) -> list[str]:
    return [str(selected), str(Path(sys.argv[0]).resolve()), *sys.argv[1:]]


class _ProcessHandoff(Exception):
    """Stands in for os._exit, which never returns."""


def test_windows_reexec_waits_for_the_selected_interpreter(gate, monkeypatch, tmp_path) -> None:
    selected = tmp_path / "project-venv-python"
    calls: list[list[str]] = []
    exit_codes: list[int] = []

    def fail_execv(*_args) -> None:
        raise AssertionError("os.execv orphans the worker on Windows")

    def fake_exit(code: int) -> None:
        exit_codes.append(code)
        raise _ProcessHandoff

    monkeypatch.setattr(gate, "os", _fake_os("nt", execv=fail_execv, _exit=fake_exit))
    monkeypatch.setattr(gate, "subprocess", SimpleNamespace(call=lambda argv: calls.append(argv) or 3))

    with pytest.raises(_ProcessHandoff):
        gate._reexec_with_selected(selected)

    # The worker's exit code is forwarded verbatim through the wrapper.
    assert exit_codes == [3]
    assert calls == [_expected_argv(selected)]
    assert os.environ[gate._REEXEC_FLAG] == "1"


def test_posix_reexec_still_replaces_the_process(gate, monkeypatch, tmp_path) -> None:
    selected = tmp_path / "project-venv-python"
    execv_calls: list[tuple[str, list[str]]] = []

    def fail_call(_argv) -> int:
        raise AssertionError("POSIX keeps os.execv")

    monkeypatch.setattr(
        gate, "os", _fake_os("posix", execv=lambda exe, argv: execv_calls.append((exe, argv)))
    )
    monkeypatch.setattr(gate, "subprocess", SimpleNamespace(call=fail_call))

    gate._reexec_with_selected(selected)

    assert execv_calls == [(str(selected), _expected_argv(selected))]

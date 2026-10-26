"""Managed Windows startup declares Git from service PATH, not command input."""

import json
from unittest.mock import Mock

import pytest

from jiuwenswarm.server.sandbox import runtime_tools as runner


@pytest.mark.parametrize("layout", ["cmd", "bin", "mingw64/bin", "mingw32/bin"])
def test_service_git_layout_declares_only_its_runtime_root(tmp_path, layout):
    root = tmp_path / "runtime" / "git-win"
    executable = root / layout / "git.exe"
    bash = root / "usr" / "bin" / "bash.exe"
    for path in (executable, bash):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    (root / "bin").mkdir(exist_ok=True)
    (root / "cmd").mkdir(exist_ok=True)
    env = {"PATH": str(executable.parent)}
    runner.declare_windows_runtime_tools(env)
    assert json.loads(env["JIUWENBOX_TOOL_PATHS"]) == {
        "read_roots": [str(root)],
        "executable_dirs": [str(root / name) for name in ("cmd", "bin", "usr/bin")],
    }


@pytest.mark.parametrize("declaration", ["", "{}", "invalid-json", '{"read_roots":[],"executable_dirs":[]}'])
def test_explicit_host_declaration_is_not_replaced(monkeypatch, declaration):
    which = Mock(side_effect=AssertionError("must not discover additional tools"))
    monkeypatch.setattr(runner, "_find_executable", which)
    env = {"PATH": "service-path", "JIUWENBOX_TOOL_PATHS": declaration}
    runner.declare_windows_runtime_tools(env)
    assert env["JIUWENBOX_TOOL_PATHS"] == declaration


@pytest.mark.parametrize("path", ["", "relative/cmd"])
def test_missing_or_relative_git_does_not_grant_access(path):
    env = {"PATH": path}
    runner.declare_windows_runtime_tools(env)
    assert "JIUWENBOX_TOOL_PATHS" not in env


@pytest.mark.parametrize("layout,has_bash", [("unrelated/cli", True), ("cmd", False)])
def test_unknown_layout_or_incomplete_git_does_not_grant_ancestors(tmp_path, layout, has_bash):
    root = tmp_path / "user-data"
    executable = root / layout / "git.exe"
    executable.parent.mkdir(parents=True)
    executable.touch()
    if has_bash:
        bash = root / "usr" / "bin" / "bash.exe"
        bash.parent.mkdir(parents=True)
        bash.touch()
    env = {"PATH": str(executable.parent)}
    runner.declare_windows_runtime_tools(env)
    assert "JIUWENBOX_TOOL_PATHS" not in env

# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""The installer must authorize the Python selected for the sandbox runner."""

import pytest

from jiuwenbox.models.policy import SecurityPolicy
from jiuwenbox.server import policy_reader
from jiuwenbox.supervisor.win_setup import collect_preinstall_paths


@pytest.mark.parametrize("runner_state", ["valid", "missing", "unset", "renamed"])
def test_detected_python_matches_runner(tmp_path, monkeypatch, runner_state):
    service_dir = tmp_path / "venv" / "Scripts"
    runner_dir = tmp_path / "Python312"
    for directory in (service_dir, runner_dir):
        directory.mkdir(parents=True)
        (directory / "python.exe").touch()
    if runner_state == "renamed":
        (runner_dir / "python.exe").rename(runner_dir / "python3.exe")
    monkeypatch.setattr(policy_reader.sys, "platform", "win32")
    monkeypatch.setattr(policy_reader.sys, "executable", str(service_dir / "python.exe"))
    monkeypatch.setattr(policy_reader.shutil, "which", lambda _: None)
    if runner_state == "unset":
        monkeypatch.delenv("JIUWENBOX_RUNNER_PYTHON", raising=False)
    else:
        executable = runner_dir / {"valid": "python.exe", "renamed": "python3.exe"}.get(runner_state, "missing.exe")
        monkeypatch.setenv("JIUWENBOX_RUNNER_PYTHON", str(executable))

    policy = policy_reader._resolve_tool_paths(SecurityPolicy())
    expected = runner_dir if runner_state in {"valid", "renamed"} else service_dir
    assert policy.windows.filesystem.tool_paths.python_dir == str(expected.resolve())
    assert str(expected.resolve()) in collect_preinstall_paths(policy)
    if runner_state == "valid":
        assert str(service_dir.resolve()) not in collect_preinstall_paths(policy)


@pytest.mark.parametrize("platform", ["win32", "linux"])
def test_explicit_python_directory_is_preserved(tmp_path, monkeypatch, platform):
    monkeypatch.setattr(policy_reader.sys, "platform", platform)
    monkeypatch.setattr(policy_reader.shutil, "which", lambda _: None)
    monkeypatch.setenv("JIUWENBOX_RUNNER_PYTHON", str(tmp_path / "another" / "python.exe"))
    configured = str(tmp_path / "configured")
    policy = SecurityPolicy.model_validate({
        "windows": {"filesystem": {"tool_paths": {"python_dir": configured}}},
    })
    assert policy_reader._resolve_tool_paths(policy).windows.filesystem.tool_paths.python_dir == configured

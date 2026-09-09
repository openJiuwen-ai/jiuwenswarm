# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Frozen entrypoint startup-error propagation tests."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


@pytest.fixture
def exe_entry():
    entry_path = Path(__file__).resolve().parents[2] / "scripts" / "jiuwenswarm_exe_entry.py"
    spec = importlib.util.spec_from_file_location("test_jiuwenswarm_exe_entry", entry_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_child_error_uses_correct_custom_data_directory(
    exe_entry, tmp_path: Path, monkeypatch
) -> None:
    data_dir = tmp_path / "custom-data"
    diagnostics_dir = tmp_path / "session"
    monkeypatch.setenv("JIUWENSWARM_DATA_DIR", str(data_dir))
    monkeypatch.setenv("JIUWENSWARM_STARTUP_DIAGNOSTICS_DIR", str(diagnostics_dir))
    exe_entry._ENTRY_ARGV = ("jiuwenswarm.exe", "--desktop-run-agent")

    try:
        raise ImportError("DLL load failed while importing cygrpc")
    except ImportError as exc:
        exe_entry._write_child_error(exc)

    assert (data_dir / "logs" / exe_entry.ERROR_LOG_NAME).is_file()
    assert len(list(diagnostics_dir.glob("failure-*.json"))) == 1


def test_nonzero_child_system_exit_is_recorded(
    exe_entry, monkeypatch
) -> None:
    recorded = []
    exe_entry._ENTRY_ARGV = ("jiuwenswarm.exe", "--desktop-run-app")
    monkeypatch.setattr(
        exe_entry,
        "_dispatch",
        lambda: (_ for _ in ()).throw(SystemExit(1)),
    )
    monkeypatch.setattr(exe_entry, "_write_child_error", recorded.append)

    with pytest.raises(SystemExit) as raised:
        exe_entry.main()

    assert raised.value.code == 1
    assert len(recorded) == 1


def test_nonzero_child_return_code_is_recorded(exe_entry, monkeypatch) -> None:
    recorded = []
    exe_entry._ENTRY_ARGV = ("jiuwenswarm.exe", "--desktop-run-app")
    monkeypatch.setattr(exe_entry, "_dispatch", lambda: 10)
    monkeypatch.setattr(exe_entry, "_write_child_error", recorded.append)

    with pytest.raises(SystemExit) as raised:
        exe_entry.main()

    assert raised.value.code == 10
    assert len(recorded) == 1

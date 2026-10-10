"""Runner interpreter selection must not silently switch environments."""

import sys

import pytest

from jiuwenbox.supervisor import win_exec


def test_missing_explicit_runner_python_is_rejected(monkeypatch, tmp_path):
    monkeypatch.setenv("JIUWENBOX_RUNNER_PYTHON", str(tmp_path / "missing" / "python.exe"))

    with pytest.raises(RuntimeError, match="JIUWENBOX_RUNNER_PYTHON"):
        win_exec._build_runner_command("sb", "C:\\ws", 60080, 60089, 60100)


def test_existing_explicit_runner_python_is_used(monkeypatch, tmp_path):
    runner_python = tmp_path / "python.exe"
    runner_python.write_bytes(b"")
    monkeypatch.setenv("JIUWENBOX_RUNNER_PYTHON", str(runner_python))

    assert str(runner_python) in win_exec._build_runner_command("sb", "C:\\ws", 60080, 60089, 60100)


def test_unset_runner_python_uses_current_interpreter(monkeypatch):
    monkeypatch.delenv("JIUWENBOX_RUNNER_PYTHON", raising=False)

    assert sys.executable in win_exec._build_runner_command("sb", "C:\\ws", 60080, 60089, 60100)

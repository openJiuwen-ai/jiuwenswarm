from __future__ import annotations

import subprocess
from pathlib import Path

from jiuwenswarm.common.git_safe_directory import (
    is_dubious_ownership_error,
    safe_directory_hint,
    safe_directory_value,
)


def _cp(args: list[str], returncode: int, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess(
        args=args,
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


def test_detects_dubious_ownership_error():
    result = _cp(
        ["git", "status"],
        128,
        stderr=(
            "fatal: detected dubious ownership in repository at "
            "'/storage/Users/currentUser'"
        ),
    )

    assert is_dubious_ownership_error(result) is True


def test_safe_directory_value_uses_absolute_posix_path(tmp_path):
    assert safe_directory_value(str(tmp_path)) == Path(tmp_path).resolve().as_posix()


def test_safe_directory_hint_contains_manual_command(tmp_path):
    expected_path = Path(tmp_path).resolve().as_posix()

    assert safe_directory_hint(str(tmp_path)) == (
        "请在终端执行: "
        f"git config --global --add safe.directory {expected_path}"
    )

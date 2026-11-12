# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""One-shot CLI output is readable while full diagnostics remain available."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[3]
FAKE_APP = """
import os
import subprocess
import sys
import types

async def run(args, *, stdout, stderr):
    print('PYTHON_DIAGNOSTIC', flush=True)
    os.write(1, b'NATIVE_STDOUT_DIAGNOSTIC\\n')
    os.write(2, b'NATIVE_STDERR_DIAGNOSTIC\\n')
    subprocess.run(
        [sys.executable, '-c', 'import os; os.write(2, b"CHILD_DIAGNOSTIC")'],
        check=True,
    )
    if os.environ.get('FAIL_CLI_TEST') == '1':
        raise RuntimeError('private startup detail')
    stdout.write('ANSWER\\n')
    stdout.flush()
    return 0

module = types.ModuleType('jiuwenswarm.channels.process_cli.app')
module.run = run
sys.modules[module.__name__] = module
from jiuwenswarm.channels.process_cli.main import main
main()
"""


def _run(tmp_path: Path, *flags: str, fail: bool = False) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["JIUWENSWARM_DATA_DIR"] = str(tmp_path)
    env.pop("JIUWENSWARM_PROCESS_DEBUG", None)
    env["PYTHONUTF8"] = "1"
    if fail:
        env["FAIL_CLI_TEST"] = "1"
    return subprocess.run(
        [sys.executable, "-c", FAKE_APP, "hello", *flags],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=PROJECT_ROOT,
        env=env,
        timeout=20,
        check=False,
    )


def _logs(tmp_path: Path) -> list[Path]:
    return list((tmp_path / "agent" / "process_cli_logs").glob("*.log"))


def test_one_shot_hides_python_native_and_child_diagnostics(tmp_path: Path) -> None:
    result = _run(tmp_path)

    assert result.returncode == 0, result.stderr
    assert result.stdout == "ANSWER\n"
    assert result.stderr == ""
    logs = _logs(tmp_path)
    assert len(logs) == 1
    detail = logs[0].read_text(encoding="utf-8")
    for marker in (
        "PYTHON_DIAGNOSTIC",
        "NATIVE_STDOUT_DIAGNOSTIC",
        "NATIVE_STDERR_DIAGNOSTIC",
        "CHILD_DIAGNOSTIC",
    ):
        assert marker in detail


def test_failed_one_shot_shows_summary_and_preserves_traceback(tmp_path: Path) -> None:
    result = _run(tmp_path, fail=True)

    assert result.returncode == 1
    assert result.stdout == ""
    assert "启动失败（RuntimeError）" in result.stderr
    assert "private startup detail" not in result.stderr
    logs = _logs(tmp_path)
    assert len(logs) == 1
    detail = logs[0].read_text(encoding="utf-8")
    assert "RuntimeError: private startup detail" in detail


def test_debug_shows_diagnostics_in_terminal(tmp_path: Path) -> None:
    result = _run(tmp_path, "--debug")

    assert result.returncode == 0, result.stderr
    assert "ANSWER" in result.stdout
    assert "PYTHON_DIAGNOSTIC" in result.stderr
    assert "NATIVE_STDERR_DIAGNOSTIC" in result.stderr
    assert _logs(tmp_path) == []


def test_log_directory_failure_reports_to_stderr(tmp_path: Path) -> None:
    log_directory = tmp_path / "agent" / "process_cli_logs"
    log_directory.parent.mkdir()
    log_directory.write_text("not a directory", encoding="utf-8")

    result = _run(tmp_path)

    assert result.returncode == 0
    assert result.stderr.count("jiuwenswarm-process: 无法写入诊断日志：") == 1
    assert "ANSWER" in result.stdout
    assert log_directory.is_file()

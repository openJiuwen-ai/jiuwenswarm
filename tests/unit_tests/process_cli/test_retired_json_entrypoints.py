# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Retired CLI flags fail before input, workspace changes or Runtime startup."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from jiuwenswarm.channels.process_cli import main as cli_main


ROOT = Path(__file__).resolve().parents[3]
REMOVED = (
    ["--query-json", "-"],
    ["--query-json", "request.json"],
    ["--query-json=request.json"],
    ["--query-j", "-"],
    ["--run-json", "-"],
    ["--run-json", "request.json"],
    ["--run-json=request.json"],
    ["--run-j", "-"],
    ["--run-jsonl"],
)


@pytest.mark.parametrize("arguments", REMOVED)
def test_removed_json_flags_fail_before_reading_input_or_changing_cwd(
    arguments: list[str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class UnreadableInput:
        def __getattr__(self, name: str) -> None:
            raise AssertionError(f"retired entrypoint accessed stdin: {name}")

    def unexpected_setup(*_args: object, **_kwargs: object) -> None:
        pytest.fail("retired entrypoint reached workspace/model setup")

    monkeypatch.setattr(sys, "stdin", UnreadableInput())
    monkeypatch.setattr(cli_main, "_activate_requested_cwd", unexpected_setup)
    monkeypatch.setattr(cli_main, "prepare_model_options", unexpected_setup)
    monkeypatch.setattr(sys, "argv", ["jiuwenswarm-process", *arguments])
    with pytest.raises(SystemExit) as caught:
        cli_main.main()
    assert caught.value.code == 2
    output = capsys.readouterr()
    assert output.out == ""
    assert arguments[0].partition("=")[0] in output.err
    assert "Traceback" not in output.err


@pytest.mark.parametrize("flag", ["--query-json", "--run-json", "--run-jsonl"])
def test_removed_entrypoints_exit_while_stdin_is_open(flag: str, tmp_path: Path) -> None:
    script = """
import importlib.abc
import sys

class NoExecution(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.startswith(('jiuwenswarm.runtime', 'jiuwenswarm.common.config',
                                'jiuwenswarm.channels.process_cli.client',
                                'jiuwenswarm.channels.process_cli.machine_entry',
                                'jiuwenswarm.channels.process_cli.query_entry',
                                'jiuwenswarm.channels.process_cli.repl')):
            raise AssertionError('unexpected execution import: ' + fullname)

sys.meta_path.insert(0, NoExecution())
from jiuwenswarm.channels.process_cli.main import main
main()
"""
    arguments = [flag] if flag == "--run-jsonl" else [flag, "-"]
    data = tmp_path / "uncreated-data"
    process = subprocess.Popen(
        [sys.executable, "-c", script, *arguments],
        cwd=ROOT,
        env={**os.environ, "PYTHONUTF8": "1", "JIUWENSWARM_DATA_DIR": str(data)},
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        process.wait(timeout=10)
        assert process.stdin is not None and not process.stdin.closed
        process.stdin.close()
        process.stdin = None
        stdout, stderr = process.communicate(timeout=5)
        assert process.returncode == 2
        assert stdout == b""
        assert flag.encode() in stderr
        assert b"unexpected execution import" not in stderr
        assert not data.exists()
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)


def test_help_omits_json_inputs_and_keeps_text_execution_options() -> None:
    parser = cli_main.build_parser()
    help_text = parser.format_help()
    for flag in ("--query-json", "--run-json", "--run-jsonl"):
        assert flag not in help_text
    for flag in ("--model", "--api-base", "--session", "--output", "--list-sessions"):
        assert flag in help_text
    args = parser.parse_args(['{"input":"literal task text"}', "--output", "jsonl"])
    assert args.prompt == '{"input":"literal task text"}'
    assert args.output == "jsonl"

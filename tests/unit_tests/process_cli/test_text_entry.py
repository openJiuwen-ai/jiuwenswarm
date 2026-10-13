# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The public command accepts task text and never starts a machine protocol."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import pytest

from jiuwenswarm.channels.process_cli.main import build_parser

ROOT = Path(__file__).resolve().parents[3]
GUARDED_MAIN = """
import builtins
import sys
original = builtins.__import__
def guarded(name, *args, **kwargs):
    if name.startswith(('jiuwenswarm.runtime', 'jiuwenswarm.server')) or name == 'jiuwenswarm.channels.process_cli.app':
        raise AssertionError('unexpected Runtime import: ' + name)
    return original(name, *args, **kwargs)
builtins.__import__ = guarded
from jiuwenswarm.channels.process_cli.main import main
sys.argv[0] = 'jiuwenswarm-process'
main()
"""


def _run(arguments):
    return subprocess.run(
        [sys.executable, "-c", GUARDED_MAIN, *arguments],
        cwd=ROOT,
        input=b"",
        capture_output=True,
        timeout=10,
        env=dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8"),
    )


@pytest.mark.parametrize("flag", ["-h", "--help"])
def test_help_lists_only_text_commands_without_loading_runtime(flag):
    result = _run([flag])
    help_text = result.stdout.decode("utf-8")
    assert result.returncode == 0
    assert result.stderr == b""
    for option in (
        "--session",
        "--cwd",
        "--project-dir",
        "--trusted-dir",
        "--mode",
        "--work-mode",
        "--timeout",
        "--show-tools",
        "--show-reasoning",
        "--debug",
    ):
        assert option in help_text
    for retired in ("--query-json", "--run-json", "--run-jsonl", "--output"):
        assert retired not in help_text


@pytest.mark.parametrize(
    "arguments",
    [
        ["--query-json", "-"],
        ["--query-json=query.txt"],
        ["--run-json", "-"],
        ["--run-json=request.txt"],
        ["--run-jsonl"],
        ["task", "--output", "json"],
        ["task", "--output", "jsonl"],
        ["task", "--output", "human"],
    ],
)
def test_retired_flags_fail_before_runtime_or_stdin_read(arguments):
    result = _run(arguments)
    assert result.returncode == 2
    assert result.stdout == b""
    assert "无法识别的参数" in result.stderr.decode("utf-8")
    assert b"AssertionError" not in result.stderr


def test_text_prompt_and_configuration_are_parsed_without_a_request_file():
    args = build_parser().parse_args(
        [
            "只回复 CLI_DEFAULT_OK",
            "--session",
            "process_cli_demo",
            "--cwd",
            "D:/workspace with spaces",
            "--project-dir",
            "D:/project",
            "--trusted-dir",
            "D:/shared",
            "--trusted-dir",
            "D:/other",
            "--mode",
            "agent.work.normal",
            "--work-mode",
            "work",
            "--timeout",
            "120",
            "--show-tools",
            "--show-reasoning",
            "--debug",
        ]
    )
    assert args.prompt == "只回复 CLI_DEFAULT_OK"
    assert args.session == "process_cli_demo"
    assert args.cwd == "D:/workspace with spaces"
    assert args.trusted_dir == ["D:/shared", "D:/other"]
    assert args.timeout == 120
    assert args.mode == "agent.work.normal"
    assert args.show_tools and args.show_reasoning and args.debug
    assert not any(
        hasattr(args, name)
        for name in ("run_json", "run_jsonl", "query_json", "output")
    )

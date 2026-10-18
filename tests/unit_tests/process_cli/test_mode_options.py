# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import contextlib
import io
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from jiuwenswarm.channels.process_cli import main as cli_main


RESTRICTED_MODES = (
    "agent.code.plan",
    "agent.work.plan",
    "code.plan",
    "agent.plan",
    "team.work.normal",
    "team.code.normal",
    "team.work.plan",
    "team.code.plan",
    "team",
    "team.work",
    "team.normal",
    "team.code",
    "code.team",
    "team.plan",
    "team.plan.normal",
    "team.plan.code",
    " CODE.PLAN ",
    " TEAM ",
    "code.plan.extra",
    "code.team.extra",
    "team.unknown",
)


@pytest.mark.parametrize(
    "arguments",
    (
        [],
        ["hello"],
        ["--run-json", "-"],
        ["--run-jsonl"],
        ["--query-json", "-"],
    ),
)
def test_removed_work_mode_flag_is_rejected(arguments, capsys):
    parser = cli_main.build_parser()
    assert "--work-mode" not in parser.format_help()
    with pytest.raises(SystemExit) as exc:
        parser.parse_args([*arguments, "--work-mode", "work"])
    assert exc.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "无法识别的参数" in captured.err
    assert "--work-mode" in captured.err


@pytest.mark.parametrize("mode", RESTRICTED_MODES)
def test_noninteractive_modes_rejected_before_setup(monkeypatch, capsys, mode):
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "jiuwenswarm-process",
            "hello",
            "--mode",
            mode,
            "--session",
            "existing-session",
        ],
    )

    def unexpected_setup(*args, **kwargs):
        pytest.fail("invalid mode must be rejected before workspace or model setup")

    monkeypatch.setattr(cli_main, "_activate_requested_cwd", unexpected_setup)
    monkeypatch.setattr(cli_main, "validate_model_options", unexpected_setup)
    monkeypatch.setattr(cli_main, "prepare_model_options", unexpected_setup)
    with pytest.raises(SystemExit) as exc:
        cli_main.main()
    assert exc.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "非交互式命令仅支持单 Agent 普通模式" in captured.err


def test_prompt_file_cannot_bypass_mode_restriction(monkeypatch, tmp_path):
    prompt_file = tmp_path / "prompt.txt"
    prompt_file.write_text("hello", encoding="utf-8")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "jiuwenswarm-process",
            "--_prompt-file",
            str(prompt_file),
            "--mode",
            "agent.code.plan",
        ],
    )
    with pytest.raises(SystemExit) as exc:
        cli_main.main()
    assert exc.value.code == 2


@pytest.mark.parametrize(
    "mode",
    (
        "agent.code.normal",
        "agent.work.normal",
        "agent.code",
        "agent.work",
        "agent",
        "agent.fast",
        "code",
        "code.normal",
    ),
)
def test_normal_single_agent_modes_reach_execution(monkeypatch, mode):
    _assert_handoff(monkeypatch, ["hello", "--mode", mode], expected="run")


@pytest.mark.parametrize("mode", RESTRICTED_MODES[:16])
def test_interactive_modes_reach_repl_and_worker(monkeypatch, mode):
    _assert_handoff(monkeypatch, ["--mode", mode], expected="repl")
    _assert_handoff(
        monkeypatch,
        [
            "hello",
            "--mode",
            mode,
            "--_interactive-worker",
        ],
        expected="run",
    )


def _assert_handoff(monkeypatch, flags, *, expected):
    calls = []

    async def run(args, **kwargs):
        calls.append("run")
        return 0

    async def run_repl(args):
        calls.append("repl")
        return 0

    for name, function_name, function in (
        ("app", "run", run),
        ("repl", "run_repl", run_repl),
    ):
        module = ModuleType(f"jiuwenswarm.channels.process_cli.{name}")
        setattr(module, function_name, function)
        monkeypatch.setitem(sys.modules, module.__name__, module)
    monkeypatch.setattr(sys, "argv", ["jiuwenswarm-process", *flags])
    monkeypatch.setattr(cli_main, "validate_model_options", lambda args: None)
    monkeypatch.setattr(cli_main, "prepare_model_options", lambda *args: None)
    monkeypatch.setattr(
        cli_main, "runtime_model_config", lambda *args: contextlib.nullcontext()
    )
    monkeypatch.setattr(
        cli_main,
        "capture_diagnostics",
        lambda **kwargs: contextlib.nullcontext(
            SimpleNamespace(stdout=io.StringIO(), stderr=io.StringIO(), log_path=None),
        ),
    )
    with pytest.raises(SystemExit) as exc:
        cli_main.main()
    assert exc.value.code == 0
    assert calls == [expected]


@pytest.mark.parametrize(
    "mode", ("agent.code.plan", "agent.work.plan", "team", "team.code")
)
def test_real_entrypoint_rejects_modes_without_loading_runtime(tmp_path, mode):
    script = """
import importlib.abc
import sys
class Guard(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.startswith(('jiuwenswarm.runtime', 'jiuwenswarm.channels.process_cli.app')):
            raise AssertionError('Runtime loaded for invalid mode')
sys.meta_path.insert(0, Guard())
from jiuwenswarm.channels.process_cli.main import main
main()
"""
    result = subprocess.run(
        [sys.executable, "-c", script, "hello", "--mode", mode],
        input="",
        capture_output=True,
        encoding="utf-8",
        timeout=15,
        cwd=Path(__file__).resolve().parents[3],
        env=dict(os.environ, PYTHONUTF8="1", JIUWENSWARM_DATA_DIR=str(tmp_path)),
    )
    assert result.returncode == 2
    assert result.stdout == ""
    assert "非交互式命令仅支持单 Agent 普通模式" in result.stderr
    assert not list(tmp_path.iterdir())

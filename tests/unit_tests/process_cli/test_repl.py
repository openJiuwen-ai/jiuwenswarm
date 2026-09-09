# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import argparse
import asyncio
import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from jiuwenswarm.channels.process_cli import app, main as main_module, repl
from jiuwenswarm.channels.process_cli.display_context import (
    select_configured_model_name,
)
from jiuwenswarm.channels.process_cli.main import build_parser


def _args(**overrides) -> argparse.Namespace:
    values = {
        "session": None,
        "cwd": None,
        "project_dir": None,
        "trusted_dir": [],
        "mode": "code.normal",
        "work_mode": "code",
        "output": "human",
        "timeout": None,
        "show_reasoning": False,
        "show_tools": False,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def test_parser_enters_interactive_mode_without_prompt() -> None:
    args = build_parser().parse_args([])

    assert args.prompt is None
    assert args.output == "human"


def test_process_cli_help_uses_chinese_labels() -> None:
    help_text = build_parser().format_help()

    assert help_text.startswith("用法：")
    assert "位置参数：" in help_text
    assert "选项：" in help_text
    assert "显示帮助信息并退出" in help_text


def test_invalid_choice_error_is_fully_chinese(capsys) -> None:
    with pytest.raises(SystemExit, match="2"):
        build_parser().parse_args(["--work-mode", "invalid", "task"])

    error = capsys.readouterr().err
    assert "参数 --work-mode 的值无效" in error
    assert "可选值：'code', 'work'" in error
    assert "invalid choice" not in error


@pytest.mark.asyncio
async def test_interactive_prompt_keeps_existing_text(monkeypatch) -> None:
    prompts: list[str] = []

    def fake_input(prompt: str) -> str:
        prompts.append(prompt)
        return "/exit"

    monkeypatch.setattr("builtins.input", fake_input)

    assert await repl._read_prompt(None) == "/exit"
    assert prompts == ["jiuwenswarm> "]


def test_worker_command_uses_a_fresh_process_entry_and_runtime_session() -> None:
    command = repl._worker_command(
        _args(timeout=30.0, trusted_dir=["D:/trusted"]),
        prompt_file="D:/temp/prompt.txt",
        session_id="process_cli_session_1",
        session_result_file="D:/temp/session.txt",
    )

    assert command[:3] == [
        sys.executable,
        "-m",
        "jiuwenswarm.channels.process_cli.main",
    ]
    assert "--_interactive-worker" in command
    assert command[command.index("--session") + 1] == "process_cli_session_1"
    assert command[command.index("--mode") + 1] == "code.normal"
    assert command[command.index("--work-mode") + 1] == "code"
    assert command[command.index("--_prompt-file") + 1] == "D:/temp/prompt.txt"
    assert "inspect this project" not in command


def test_worker_entry_reads_prompt_from_internal_file(monkeypatch, tmp_path) -> None:
    prompt_path = tmp_path / "prompt.txt"
    prompt_path.write_text("prompt from file", encoding="utf-8")
    observed: dict[str, str] = {}

    async def fake_run(args, **_kwargs) -> int:
        observed["prompt"] = args.prompt
        return 0

    monkeypatch.setattr(app, "run", fake_run)
    monkeypatch.setattr(
        sys,
        "argv",
        ["jiuwenswarm-process", "--_prompt-file", str(prompt_path)],
    )

    with pytest.raises(SystemExit, match="0"):
        main_module.main()

    assert observed["prompt"] == "prompt from file"


@pytest.mark.skipif(os.name != "nt", reason="CTRL_BREAK is Windows-only")
def test_windows_worker_sigbreak_runs_async_cleanup_and_exits_130(tmp_path) -> None:
    ready_path = tmp_path / "ready.txt"
    cleanup_path = tmp_path / "cleanup.txt"
    script = textwrap.dedent(
        f"""
        import asyncio
        from pathlib import Path

        from jiuwenswarm.channels.process_cli.main import (
            _WindowsWorkerInterruptController,
        )

        controller = _WindowsWorkerInterruptController(enabled=True)
        controller.install()

        async def operation():
            try:
                Path({str(ready_path)!r}).write_text("ready", encoding="utf-8")
                await asyncio.Event().wait()
            finally:
                Path({str(cleanup_path)!r}).write_text("cleaned", encoding="utf-8")

        try:
            code = asyncio.run(controller.run(operation))
        except asyncio.CancelledError:
            code = 130 if controller.interrupted else 1
        finally:
            controller.restore()
        raise SystemExit(code)
        """
    )
    process = subprocess.Popen(
        [sys.executable, "-c", script],
        cwd=Path.cwd(),
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
    )
    try:
        deadline = time.monotonic() + 10.0
        while not ready_path.exists() and process.poll() is None:
            if time.monotonic() >= deadline:
                pytest.fail("worker did not become ready for CTRL_BREAK")
            time.sleep(0.05)
        assert process.poll() is None

        process.send_signal(signal.CTRL_BREAK_EVENT)
        assert process.wait(timeout=10.0) == 130
        assert cleanup_path.read_text(encoding="utf-8") == "cleaned"
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5.0)


@pytest.mark.asyncio
async def test_runtime_log_drain_accepts_a_line_larger_than_stream_limit() -> None:
    reader = asyncio.StreamReader(limit=64 * 1024)
    reader.feed_data(b"x" * (70 * 1024) + b"\nlast line\n")
    reader.feed_eof()

    tail = await repl._drain_runtime_logs(reader)

    assert tail[-1] == "last line"
    assert tail[-2].endswith("x" * 100)


@pytest.mark.asyncio
async def test_worker_keeps_prompt_off_argv_and_reports_late_failure(
    monkeypatch,
    capsys,
) -> None:
    prompt = "secret " + ("x" * (40 * 1024))
    observed: dict[str, object] = {}

    class FakeProcess:
        returncode = 1

        def __init__(self) -> None:
            self.stderr = asyncio.StreamReader()
            self.stderr.feed_data(b"worker failed after session creation\n")
            self.stderr.feed_eof()

        async def wait(self) -> int:
            return self.returncode

    async def fake_create_subprocess_exec(*command, **_kwargs):
        command_list = list(command)
        observed["command"] = command_list
        prompt_path = Path(command_list[command_list.index("--_prompt-file") + 1])
        result_path = Path(
            command_list[command_list.index("--_session-result-file") + 1]
        )
        observed["prompt"] = prompt_path.read_text(encoding="utf-8")
        result_path.write_text("runtime-session", encoding="utf-8")
        return FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    return_code, session_id = await repl._run_worker(
        _args(),
        prompt=prompt,
        session_id=None,
    )

    assert return_code == 1
    assert session_id == "runtime-session"
    assert observed["prompt"] == prompt
    assert prompt not in observed["command"]
    error = capsys.readouterr().err
    assert "工作进程退出码：1" in error
    assert "worker failed after session creation" in error


@pytest.mark.asyncio
async def test_worker_cancellation_returns_to_repl(monkeypatch) -> None:
    wait_started = asyncio.Event()

    class FakeProcess:
        returncode = None

        def __init__(self) -> None:
            self.stderr = asyncio.StreamReader()

        async def wait(self) -> int:
            wait_started.set()
            await asyncio.Event().wait()
            return 0

    process = FakeProcess()

    async def fake_create_subprocess_exec(*_command, **_kwargs):
        return process

    async def fake_interrupt_worker(interrupted_process) -> None:
        interrupted_process.returncode = 130
        interrupted_process.stderr.feed_eof()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setattr(repl, "_interrupt_worker", fake_interrupt_worker)

    task = asyncio.create_task(
        repl._run_worker(_args(), prompt="cancel me", session_id=None)
    )
    await wait_started.wait()
    task.cancel()

    assert await task == (130, None)


@pytest.mark.asyncio
async def test_worker_interrupt_has_bounded_terminate_and_kill_fallback(
    monkeypatch,
) -> None:
    calls: list[str] = []

    class StubbornProcess:
        returncode = None

        def send_signal(self, _signal) -> None:
            calls.append("signal")

        def terminate(self) -> None:
            calls.append("terminate")

        def kill(self) -> None:
            calls.append("kill")

        async def wait(self) -> int:
            await asyncio.Event().wait()
            return 0

    monkeypatch.setattr(repl, "_INTERRUPT_GRACE_SECONDS", 0.01)
    monkeypatch.setattr(repl, "_TERMINATE_GRACE_SECONDS", 0.01)
    monkeypatch.setattr(repl, "_KILL_GRACE_SECONDS", 0.01)

    await repl._interrupt_worker(StubbornProcess())

    assert calls == ["signal", "terminate", "kill"]


def test_select_configured_model_name_uses_first_preview_candidate() -> None:
    entries = [
        {"model_client_config": {"model_name": "model-a"}},
        {
            "model_client_config": {"model_name": "model-b"},
            "is_default": True,
        },
    ]

    assert select_configured_model_name(entries) == "model-a"


def test_select_configured_model_name_falls_back_to_first_valid_entry() -> None:
    entries = [
        {"model_client_config": {}},
        {"model_client_config": {"model_name": "model-a"}},
        {"model_client_config": {"model_name": "model-b"}},
    ]

    assert select_configured_model_name(entries) == "model-a"


def test_configured_model_name_reads_cli_config_without_runtime_imports(
    monkeypatch,
    tmp_path,
) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "config.yaml").write_text(
        """\
models:
  defaults:
    - model_client_config:
        model_name: ${MODEL_NAME:-fallback-model}
      is_default: true
""",
        encoding="utf-8",
    )
    (config_dir / ".env").write_text("MODEL_NAME=dotenv-model\n", encoding="utf-8")
    monkeypatch.setenv("JIUWENSWARM_CONFIG_DIR", str(config_dir))

    assert repl._resolve_configured_model_name() == "dotenv-model"


@pytest.mark.parametrize(
    ("mode", "work_mode", "expected"),
    [
        ("code.normal", "code", "code.normal"),
        ("agent", "work", "agent"),
        ("agent", "code", "code.normal"),
        ("agent.plan", "code", "code.plan"),
    ],
)
def test_display_mode_collapses_mode_and_work_mode(
    mode: str,
    work_mode: str,
    expected: str,
) -> None:
    assert repl._resolve_display_mode(mode, work_mode) == expected


@pytest.mark.asyncio
async def test_repl_runs_every_instruction_in_a_new_worker_and_reuses_session(
    monkeypatch,
    capsys,
) -> None:
    prompts = iter(("first", "second", "/new", "third", "/session", "/exit"))
    calls: list[tuple[str, str | None]] = []

    async def fake_read_prompt(_session) -> str:
        return next(prompts)

    async def fake_run_worker(
        args,
        *,
        prompt: str,
        session_id: str | None,
    ) -> tuple[int, str]:
        calls.append((prompt, session_id))
        return 0, session_id or f"runtime-session-{len(calls)}"

    monkeypatch.setattr(repl, "_read_prompt", fake_read_prompt)
    monkeypatch.setattr(repl, "_create_prompt_session", lambda: None)
    monkeypatch.setattr(repl, "_run_worker", fake_run_worker)
    monkeypatch.setattr(
        repl,
        "_resolve_configured_model_name",
        lambda: "gpt-5.6-sol",
    )

    result = await repl.run_repl(_args())

    assert result == 0
    assert calls == [
        ("first", None),
        ("second", "runtime-session-1"),
        ("third", None),
    ]
    output = capsys.readouterr().out
    assert ">_ JiuwenSwarm" in output
    assert "模型（配置推断）：  gpt-5.6-sol" in output
    assert "模式（请求推断）：  code.normal" in output
    assert "工作模式" not in output
    assert "进程式 CLI · 本地 Runtime" in output
    assert "每条指令均在独立进程中运行" in output
    assert "下一条指令将创建新的 Runtime 会话" in output
    assert "runtime-session-3" in output


@pytest.mark.asyncio
async def test_repl_interrupts_only_current_worker_and_continues(
    monkeypatch,
    capsys,
) -> None:
    prompts = iter(("first", "second", "/exit"))
    calls: list[str] = []

    async def fake_read_prompt(_session) -> str:
        return next(prompts)

    async def fake_run_worker(args, *, prompt: str, session_id: str | None):
        calls.append(prompt)
        return (130 if prompt == "first" else 0), session_id

    monkeypatch.setattr(repl, "_read_prompt", fake_read_prompt)
    monkeypatch.setattr(repl, "_create_prompt_session", lambda: None)
    monkeypatch.setattr(repl, "_run_worker", fake_run_worker)
    monkeypatch.setattr(repl, "_resolve_configured_model_name", lambda: "model")

    assert await repl.run_repl(_args()) == 0
    assert calls == ["first", "second"]
    assert "已中断当前指令，可以继续输入" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_repl_interrupts_only_current_prompt_and_continues(
    monkeypatch,
    capsys,
) -> None:
    reads = 0

    async def fake_read_prompt(_session) -> str:
        nonlocal reads
        reads += 1
        if reads == 1:
            raise asyncio.CancelledError
        return "/exit"

    monkeypatch.setattr(repl, "_read_prompt", fake_read_prompt)
    monkeypatch.setattr(repl, "_create_prompt_session", lambda: None)
    monkeypatch.setattr(repl, "_resolve_configured_model_name", lambda: "model")

    assert await repl.run_repl(_args()) == 0
    assert reads == 2
    assert "已取消当前输入，可以继续输入" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_repl_refreshes_display_metadata_between_turns(monkeypatch) -> None:
    prompts = iter(("first", "/exit"))
    models = iter(("initial", "first-turn", "second-turn"))
    seen: list[str] = []

    async def fake_read_prompt(_session) -> str:
        return next(prompts)

    async def fake_run_worker(args, *, prompt: str, session_id: str | None):
        return 0, session_id

    class FakeUI:
        def startup(self, **_kwargs) -> None:
            return

        def status(self, **kwargs) -> None:
            seen.append(kwargs["model_name"])

    monkeypatch.setattr(repl, "_read_prompt", fake_read_prompt)
    monkeypatch.setattr(repl, "_create_prompt_session", lambda: None)
    monkeypatch.setattr(repl, "_run_worker", fake_run_worker)
    monkeypatch.setattr(repl, "_resolve_configured_model_name", lambda: next(models))
    monkeypatch.setattr(repl, "ProcessCliUI", FakeUI)

    assert await repl.run_repl(_args()) == 0
    assert seen == ["first-turn", "second-turn"]

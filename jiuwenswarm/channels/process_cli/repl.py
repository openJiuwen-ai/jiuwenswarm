# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Interactive launcher for one-command/one-Runtime worker processes."""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import tempfile
from collections import deque
from pathlib import Path
from typing import TYPE_CHECKING

from jiuwenswarm.channels.process_cli.commands import resolve_slash_command
from jiuwenswarm.channels.process_cli.display_context import (
    resolve_configured_model_name as _resolve_configured_model_name,
)
from jiuwenswarm.channels.process_cli.display_context import (
    resolve_display_mode as _resolve_display_mode,
)
from jiuwenswarm.channels.process_cli.prompt import (
    create_prompt_session as _create_prompt_session,
)
from jiuwenswarm.channels.process_cli.prompt import read_prompt as _read_prompt
from jiuwenswarm.channels.process_cli.ui import ProcessCliUI, resolved_cwd

if TYPE_CHECKING:
    import argparse
    from asyncio.subprocess import Process

_EXIT_COMMANDS = frozenset({"exit", "quit"})
_LOG_LINE_TAIL_BYTES = 64 * 1024
_TRUNCATED_LOG_MARKER = b"[...truncated...] "
_INTERRUPT_GRACE_SECONDS = 15.0
_TERMINATE_GRACE_SECONDS = 5.0
_KILL_GRACE_SECONDS = 5.0


def _clear_current_task_cancellation() -> None:
    task = asyncio.current_task()
    if task is not None:
        task.uncancel()


def _worker_command(
    args: argparse.Namespace,
    *,
    prompt_file: str,
    session_id: str | None,
    session_result_file: str,
) -> list[str]:
    command = [
        sys.executable,
        "-m",
        "jiuwenswarm.channels.process_cli.main",
        "--output",
        "human",
        "--mode",
        args.mode,
        "--work-mode",
        args.work_mode,
        "--_interactive-worker",
        "--_session-result-file",
        session_result_file,
        "--_prompt-file",
        prompt_file,
    ]
    if session_id:
        command.extend(("--session", session_id))
    if args.cwd:
        command.extend(("--cwd", args.cwd))
    if args.project_dir:
        command.extend(("--project-dir", args.project_dir))
    for trusted_dir in args.trusted_dir:
        command.extend(("--trusted-dir", trusted_dir))
    if args.timeout is not None:
        command.extend(("--timeout", str(args.timeout)))
    if args.show_reasoning:
        command.append("--show-reasoning")
    if args.show_tools:
        command.append("--show-tools")
    return command


async def _drain_runtime_logs(reader: asyncio.StreamReader) -> deque[str]:
    tail: deque[str] = deque(maxlen=20)
    pending = b""
    while True:
        chunk = await reader.read(64 * 1024)
        if not chunk:
            if pending:
                tail.append(pending.decode(errors="replace").rstrip())
            return tail
        pending += chunk
        while b"\n" in pending:
            line, pending = pending.split(b"\n", 1)
            tail.append(line.decode(errors="replace").rstrip("\r"))
        if len(pending) > _LOG_LINE_TAIL_BYTES:
            pending = _TRUNCATED_LOG_MARKER + pending[-_LOG_LINE_TAIL_BYTES:]


async def _wait_for_worker_exit(process: Process, *, timeout: float) -> bool:
    if process.returncode is not None:
        return True
    try:
        await asyncio.wait_for(process.wait(), timeout=timeout)
    except (ProcessLookupError, asyncio.TimeoutError):
        return process.returncode is not None
    return True


async def _interrupt_worker(process: Process) -> None:
    if process.returncode is not None:
        return
    try:
        process.send_signal(
            signal.CTRL_BREAK_EVENT if os.name == "nt" else signal.SIGINT
        )
    except OSError:
        pass
    else:
        if await _wait_for_worker_exit(
            process,
            timeout=_INTERRUPT_GRACE_SECONDS,
        ):
            return

    if process.returncode is None:
        try:
            process.terminate()
        except OSError:
            pass
        else:
            if await _wait_for_worker_exit(
                process,
                timeout=_TERMINATE_GRACE_SECONDS,
            ):
                return

    if process.returncode is None:
        try:
            process.kill()
        except OSError:
            return
        await _wait_for_worker_exit(process, timeout=_KILL_GRACE_SECONDS)


async def _run_worker(
    args: argparse.Namespace,
    *,
    prompt: str,
    session_id: str | None,
) -> tuple[int, str | None]:
    with tempfile.TemporaryDirectory(prefix="jiuwenswarm-process-repl-") as temp_dir:
        result_path = Path(temp_dir) / "session-id.txt"
        prompt_path = Path(temp_dir) / "prompt.txt"
        prompt_path.write_text(prompt, encoding="utf-8")
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        process = await asyncio.create_subprocess_exec(
            *_worker_command(
                args,
                prompt_file=str(prompt_path),
                session_id=session_id,
                session_result_file=str(result_path),
            ),
            stderr=asyncio.subprocess.PIPE,
            creationflags=creationflags,
        )
        process_stderr = process.stderr
        if process_stderr is None:
            raise RuntimeError("process CLI worker stderr pipe is unavailable")
        log_task = asyncio.create_task(_drain_runtime_logs(process_stderr))
        try:
            return_code = await process.wait()
        except asyncio.CancelledError:
            _clear_current_task_cancellation()
            await _interrupt_worker(process)
            return_code = 130
        finally:
            if process.returncode is None:
                log_task.cancel()
            try:
                log_tail = await log_task
            except asyncio.CancelledError:
                log_tail = deque()

        next_session = session_id
        if result_path.exists():
            value = result_path.read_text(encoding="utf-8").strip()
            if value:
                next_session = value
        if return_code not in (0, 130):
            diagnostics = [f"工作进程退出码：{return_code}", *log_tail]
            ProcessCliUI(stream=sys.stderr).diagnostics(diagnostics)
        return return_code, next_session


async def run_repl(args: argparse.Namespace) -> int:
    """Run a UI-only shell; every instruction owns a fresh worker process."""
    ui = ProcessCliUI()
    session_id = args.session
    cwd = resolved_cwd(args.cwd)
    model_name = _resolve_configured_model_name()
    display_mode = _resolve_display_mode(args.mode, args.work_mode)
    prompt_session = _create_prompt_session()
    ui.startup(
        model_name=model_name,
        mode=display_mode,
        cwd=cwd,
        session_id=session_id,
    )
    while True:
        # These values are best-effort previews for the next fresh worker.
        # Refresh them every turn so configuration changes are not displayed
        # indefinitely after the worker would observe a newer configuration.
        model_name = _resolve_configured_model_name()
        display_mode = _resolve_display_mode(args.mode, args.work_mode)
        ui.status(
            model_name=model_name,
            mode=display_mode,
            cwd=cwd,
            session_id=session_id,
        )
        try:
            prompt = (await _read_prompt(prompt_session)).strip()
        except asyncio.CancelledError:
            _clear_current_task_cancellation()
            ui.notice("已取消当前输入，可以继续输入。")
            continue
        except KeyboardInterrupt:
            ui.notice("已取消当前输入，可以继续输入。")
            continue
        except EOFError:
            ui.blank_line()
            return 0
        if not prompt:
            continue
        lowered = prompt.lower()
        slash_command = resolve_slash_command(lowered)
        if lowered in _EXIT_COMMANDS or slash_command == "/exit":
            return 0
        if slash_command == "/help":
            ui.help()
            continue
        if slash_command == "/new":
            session_id = None
            ui.notice("下一条指令将创建新的 Runtime 会话。")
            continue
        if slash_command == "/session":
            ui.session(session_id)
            continue
        return_code, session_id = await _run_worker(
            args,
            prompt=prompt,
            session_id=session_id,
        )
        if return_code == 130:
            ui.notice("已中断当前指令，可以继续输入。")


__all__ = ["run_repl"]

# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Interactive launcher for one-command/one-Runtime worker processes."""

from __future__ import annotations

import asyncio
import codecs
import json
import os
import signal
import subprocess
import sys
import tempfile
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from jiuwenswarm.channels.process_cli.commands import (
    ParsedSlashCommand,
    parse_slash_command,
    resolve_mode_target,
)
from jiuwenswarm.channels.process_cli.display_context import (
    resolve_cli_work_mode as _resolve_cli_work_mode,
)
from jiuwenswarm.channels.process_cli.display_context import (
    resolve_configured_model_name as _resolve_configured_model_name,
)
from jiuwenswarm.channels.process_cli.display_context import (
    resolve_display_mode as _resolve_display_mode,
)
from jiuwenswarm.channels.process_cli.live_layout import (
    FORWARDED_RECEIPT_PREFIX,
    LiveTurnLayout,
)
from jiuwenswarm.channels.process_cli.prompt import (
    create_live_prompt_session as _create_live_prompt_session,
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
_SESSION_CREATE_OPERATION = "session.create"
_SESSION_SWITCH_OPERATION = "session.switch"
_SESSION_FORK_OPERATION = "session.fork"
_SESSION_DELETE_OPERATION = "session.delete"
_STATEFUL_WORKER_OPERATIONS = frozenset(
    {
        _SESSION_CREATE_OPERATION,
        _SESSION_SWITCH_OPERATION,
        _SESSION_FORK_OPERATION,
        _SESSION_DELETE_OPERATION,
    }
)


def _worker_environment() -> dict[str, str]:
    """Use one explicit wire encoding for the parent/worker stdio pipes."""

    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    return env


@dataclass(slots=True)
class _ReplState:
    session_id: str | None
    cwd: str
    model_name: str
    display_mode: str


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
    worker_result_file: str | None = None,
    operation: str = "chat",
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
    if worker_result_file:
        command.extend(("--_worker-result-file", worker_result_file))
    if operation != "chat":
        command.extend(("--_operation", operation))
    else:
        command.append("--_forwarded-live-input")
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


async def _drain_runtime_logs(
    reader: asyncio.StreamReader,
    *,
    on_receipt=None,
) -> deque[str]:
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
            decoded = line.decode("utf-8", errors="replace").rstrip("\r")
            if decoded.startswith(FORWARDED_RECEIPT_PREFIX):
                try:
                    prefix_length = len(FORWARDED_RECEIPT_PREFIX)
                    receipt = json.loads(decoded[prefix_length:])
                except json.JSONDecodeError:
                    tail.append("工作进程返回了无效的补充输入回执")
                else:
                    if on_receipt is not None and isinstance(receipt, dict):
                        on_receipt(str(receipt.get("status") or "unknown"))
                continue
            tail.append(decoded)
        if len(pending) > _LOG_LINE_TAIL_BYTES:
            pending = _TRUNCATED_LOG_MARKER + pending[-_LOG_LINE_TAIL_BYTES:]


def _write_parent_output(text: str) -> None:
    """Write worker text through the parent terminal stream."""

    stream = sys.stdout
    stream.write(text)
    stream.flush()


async def _relay_worker_output(
    reader: asyncio.StreamReader,
    *,
    layout: LiveTurnLayout | None,
) -> None:
    """Append worker output to the model-output region of one live turn."""

    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")

    while True:
        chunk = await reader.read(64 * 1024)
        if not chunk:
            text = decoder.decode(b"", final=True)
            if layout is not None:
                layout.append_output(text)
            elif text:
                _write_parent_output(text)
            return
        text = decoder.decode(chunk)
        if layout is not None:
            layout.append_output(text)
        elif text:
            _write_parent_output(text)


async def _read_live_prompt(
    prompt_session,
    layout: LiveTurnLayout,
) -> str:
    return await prompt_session.prompt_async(message=layout.message)


async def _forward_live_input(
    process: Process,
    *,
    prompt_session,
    layout: LiveTurnLayout,
) -> None:
    """Forward terminal lines from the REPL process to its Runtime worker."""

    writer = process.stdin
    if writer is None:
        raise RuntimeError("process CLI worker stdin pipe is unavailable")
    try:
        while process.returncode is None:
            try:
                text = await _read_live_prompt(prompt_session, layout)
            except EOFError:
                return
            except KeyboardInterrupt:
                await _interrupt_worker(process)
                return
            if process.returncode is not None:
                return
            layout.add_supplement(text)
            writer.write((text + "\n").encode("utf-8"))
            await writer.drain()
    except (BrokenPipeError, ConnectionResetError):
        return
    finally:
        if not writer.is_closing():
            writer.close()
            try:
                await writer.wait_closed()
            except (BrokenPipeError, ConnectionResetError):
                pass


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
    operation: str = "chat",
    prompt_session=None,
) -> tuple[int, str | None]:
    live_layout = (
        LiveTurnLayout(prompt)
        if operation == "chat" and prompt_session is not None
        else None
    )
    live_prompt_session = None
    if live_layout is not None:
        live_prompt_session = _create_live_prompt_session()
        live_layout.bind(live_prompt_session)
    with tempfile.TemporaryDirectory(prefix="jiuwenswarm-process-repl-") as temp_dir:
        result_path = Path(temp_dir) / "session-id.txt"
        worker_result_path = Path(temp_dir) / "worker-result.json"
        prompt_path = Path(temp_dir) / "prompt.txt"
        prompt_path.write_text(prompt, encoding="utf-8")
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        process = await asyncio.create_subprocess_exec(
            *_worker_command(
                args,
                prompt_file=str(prompt_path),
                session_id=session_id,
                session_result_file=str(result_path),
                worker_result_file=str(worker_result_path),
                operation=operation,
            ),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            creationflags=creationflags,
            env=_worker_environment(),
        )
        process_stdout = getattr(process, "stdout", None)
        process_stderr = process.stderr
        if process_stdout is None:
            # Lightweight process doubles used by channel lifecycle tests do
            # not expose a stdout reader.
            process_stdout = asyncio.StreamReader()
            process_stdout.feed_eof()
        if process_stderr is None:
            raise RuntimeError("process CLI worker stderr pipe is unavailable")
        log_task = asyncio.create_task(
            _drain_runtime_logs(
                process_stderr,
                on_receipt=(
                    live_layout.apply_receipt if live_layout is not None else None
                ),
            )
        )
        output_task = asyncio.create_task(
            _relay_worker_output(
                process_stdout,
                layout=live_layout,
            )
        )
        input_task = (
            asyncio.create_task(
                _forward_live_input(
                    process,
                    prompt_session=live_prompt_session,
                    layout=live_layout,
                )
            )
            if live_layout is not None and live_prompt_session is not None
            else None
        )
        try:
            return_code = await process.wait()
        except asyncio.CancelledError:
            _clear_current_task_cancellation()
            await _interrupt_worker(process)
            return_code = 130
        finally:
            try:
                await output_task
            finally:
                if input_task is not None:
                    if (
                        live_prompt_session is not None
                        and live_prompt_session.app.is_running
                    ):
                        live_prompt_session.default_buffer.reset()
                        live_prompt_session.app.erase_when_done = True
                        live_prompt_session.app.exit(result="")
                        await asyncio.gather(input_task, return_exceptions=True)
                    else:
                        input_task.cancel()
                        await asyncio.gather(input_task, return_exceptions=True)
                else:
                    process_stdin = getattr(process, "stdin", None)
                    if process_stdin is not None and not process_stdin.is_closing():
                        process_stdin.close()
            if process.returncode is None:
                log_task.cancel()
            try:
                log_tail = await log_task
            except asyncio.CancelledError:
                log_tail = deque()
            if live_layout is not None:
                _write_parent_output(live_layout.final_text())

        next_session = session_id
        worker_result: dict[str, object] | None = None
        setattr(args, "_last_worker_result", None)
        if worker_result_path.exists():
            try:
                loaded = json.loads(worker_result_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                log_tail.append(f"工作进程结果无效：{exc}")
            else:
                if isinstance(loaded, dict) and loaded.get("operation") == operation:
                    worker_result = loaded
                    setattr(args, "_last_worker_result", dict(loaded))
                    value = loaded.get("session_id")
                    if isinstance(value, str) and value.strip():
                        next_session = value.strip()
                    mode = loaded.get("mode")
                    if isinstance(mode, str) and mode.strip():
                        args.mode = mode.strip()
                    work_mode = loaded.get("work_mode")
                    if isinstance(work_mode, str) and work_mode.strip() in {
                        "code",
                        "work",
                    }:
                        args.work_mode = work_mode.strip()
                    project_dir = loaded.get("project_dir")
                    if isinstance(project_dir, str):
                        args.project_dir = project_dir.strip()
                else:
                    log_tail.append("工作进程结果与当前操作不匹配")
        if worker_result is None and result_path.exists() and operation == "chat":
            value = result_path.read_text(encoding="utf-8").strip()
            if value:
                next_session = value
        if (
            return_code == 0
            and operation in _STATEFUL_WORKER_OPERATIONS
            and worker_result is None
        ):
            return_code = 1
            log_tail.append("工作进程未返回已提交的 Session 结果")
        if return_code not in (0, 130):
            diagnostics = [f"工作进程退出码：{return_code}", *log_tail]
            ProcessCliUI(stream=sys.stderr).diagnostics(diagnostics)
        return return_code, next_session


def _handle_mode_command(
    args: argparse.Namespace,
    *,
    arguments: str,
    display_mode: str,
    ui: ProcessCliUI,
) -> str:
    if not arguments:
        ui.notice(f"当前模式：{display_mode}")
        return display_mode
    next_mode = resolve_mode_target(arguments)
    if next_mode is None:
        ui.notice("用法：/mode <agent.work|agent.code|team.work|team.code>")
        return display_mode
    args.mode = next_mode
    args.work_mode = _resolve_cli_work_mode(next_mode, args.work_mode)
    updated_display_mode = _resolve_display_mode(args.mode, args.work_mode)
    ui.notice(f"已切换模式：{updated_display_mode}")
    return updated_display_mode


async def _handle_skills_command(
    args: argparse.Namespace,
    *,
    arguments: str,
    session_id: str | None,
    ui: ProcessCliUI,
) -> None:
    if arguments.lower() not in {"", "list"}:
        ui.notice("用法：/skills list")
        return
    return_code, _unchanged_session = await _run_worker(
        args,
        prompt="/skills list",
        session_id=session_id,
        operation="skills.list",
    )
    if return_code == 130:
        ui.notice("已中断当前指令，可以继续输入。")


def _worker_delivered(args: argparse.Namespace, operation: str) -> bool:
    result = getattr(args, "_last_worker_result", None)
    return isinstance(result, dict) and result.get("operation") == operation


async def _handle_new_command(
    args: argparse.Namespace,
    *,
    arguments: str,
    state: _ReplState,
    ui: ProcessCliUI,
) -> None:
    if arguments.lower() not in {"", "--persist", "--persist-session"}:
        ui.notice("用法：/new [--persist|--persist-session]")
        return
    return_code, next_session = await _run_worker(
        args,
        prompt=arguments,
        session_id=state.session_id,
        operation=_SESSION_CREATE_OPERATION,
    )
    if _worker_delivered(args, _SESSION_CREATE_OPERATION) and next_session:
        state.session_id = next_session
    if return_code == 130:
        ui.notice("已中断当前指令，可以继续输入。")


async def _handle_resume_command(
    args: argparse.Namespace,
    *,
    arguments: str,
    state: _ReplState,
    ui: ProcessCliUI,
) -> None:
    parts = arguments.split()
    if len(parts) != 1:
        ui.notice("用法：/resume <session-id>")
        return
    return_code, next_session = await _run_worker(
        args,
        prompt=parts[0],
        session_id=state.session_id,
        operation=_SESSION_SWITCH_OPERATION,
    )
    if _worker_delivered(args, _SESSION_SWITCH_OPERATION) and next_session:
        state.session_id = next_session
    if return_code == 130:
        ui.notice("已中断当前指令，可以继续输入。")


async def _handle_branch_command(
    args: argparse.Namespace,
    *,
    arguments: str,
    state: _ReplState,
    ui: ProcessCliUI,
) -> None:
    if not state.session_id:
        ui.notice("当前没有可创建分支的会话。")
        return
    return_code, next_session = await _run_worker(
        args,
        prompt=arguments,
        session_id=state.session_id,
        operation=_SESSION_FORK_OPERATION,
    )
    if _worker_delivered(args, _SESSION_FORK_OPERATION) and next_session:
        state.session_id = next_session
    if return_code == 130:
        ui.notice("已中断当前指令，可以继续输入。")


async def _confirm_delete(target: str) -> bool:
    answer = await asyncio.to_thread(
        input,
        f"确认删除会话 {target}？此操作不可恢复。[y/N] ",
    )
    return answer.strip().lower() in {"y", "yes"}


async def _handle_delete_command(
    args: argparse.Namespace,
    *,
    arguments: str,
    state: _ReplState,
    ui: ProcessCliUI,
) -> None:
    parts = arguments.split()
    if len(parts) != 1:
        ui.notice("用法：/delete <session-id>")
        return
    target = parts[0]
    if not await _confirm_delete(target):
        ui.notice("已取消删除。")
        return
    return_code, _unchanged_session = await _run_worker(
        args,
        prompt=target,
        session_id=state.session_id,
        operation=_SESSION_DELETE_OPERATION,
    )
    if _worker_delivered(args, _SESSION_DELETE_OPERATION):
        if state.session_id == target:
            state.session_id = None
    if return_code == 130:
        ui.notice("已中断当前指令，可以继续输入。")


async def _handle_slash_command(
    args: argparse.Namespace,
    command: ParsedSlashCommand,
    ui: ProcessCliUI,
    state: _ReplState,
) -> bool:
    """Handle one recognized command and report whether the REPL should exit."""
    if command.name == "/mode":
        state.display_mode = _handle_mode_command(
            args,
            arguments=command.arguments,
            display_mode=state.display_mode,
            ui=ui,
        )
        return False
    if command.name == "/skills":
        await _handle_skills_command(
            args,
            arguments=command.arguments,
            session_id=state.session_id,
            ui=ui,
        )
        return False
    if command.name == "/new":
        await _handle_new_command(
            args,
            arguments=command.arguments,
            state=state,
            ui=ui,
        )
        return False
    if command.name == "/resume":
        await _handle_resume_command(
            args,
            arguments=command.arguments,
            state=state,
            ui=ui,
        )
        return False
    if command.name == "/branch":
        await _handle_branch_command(
            args,
            arguments=command.arguments,
            state=state,
            ui=ui,
        )
        return False
    if command.name == "/delete":
        await _handle_delete_command(
            args,
            arguments=command.arguments,
            state=state,
            ui=ui,
        )
        return False
    return _handle_simple_slash_command(command, ui, state)


def _handle_simple_slash_command(
    command: ParsedSlashCommand,
    ui: ProcessCliUI,
    state: _ReplState,
) -> bool:
    if command.arguments:
        ui.notice(f"用法：{command.name}")
        return False
    if command.name == "/exit":
        return True
    if command.name == "/status":
        ui.status(
            model_name=state.model_name,
            mode=state.display_mode,
            cwd=state.cwd,
            session_id=state.session_id,
        )
    elif command.name == "/help":
        ui.help()
    elif command.name == "/session":
        ui.session(state.session_id)
    return False


async def run_repl(args: argparse.Namespace) -> int:
    """Run a UI-only shell; every instruction owns a fresh worker process."""
    args.work_mode = _resolve_cli_work_mode(args.mode, args.work_mode)
    ui = ProcessCliUI()
    state = _ReplState(
        session_id=args.session,
        cwd=resolved_cwd(args.cwd),
        model_name=_resolve_configured_model_name(),
        display_mode=_resolve_display_mode(args.mode, args.work_mode),
    )
    prompt_session = _create_prompt_session()
    ui.startup(
        model_name=state.model_name,
        mode=state.display_mode,
        cwd=state.cwd,
        session_id=state.session_id,
    )
    while True:
        # These values are best-effort previews for the next fresh worker.
        # Refresh them every turn so configuration changes are not displayed
        # indefinitely after the worker would observe a newer configuration.
        state.model_name = _resolve_configured_model_name()
        state.display_mode = _resolve_display_mode(args.mode, args.work_mode)
        ui.status(
            model_name=state.model_name,
            mode=state.display_mode,
            cwd=state.cwd,
            session_id=state.session_id,
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
        slash_command = parse_slash_command(prompt)
        if lowered in _EXIT_COMMANDS:
            return 0
        if slash_command is not None:
            if await _handle_slash_command(args, slash_command, ui, state):
                return 0
            continue
        worker_kwargs = {
            "prompt": prompt,
            "session_id": state.session_id,
        }
        if prompt_session is not None:
            worker_kwargs["prompt_session"] = prompt_session
        return_code, state.session_id = await _run_worker(args, **worker_kwargs)
        if return_code == 130:
            ui.notice("已中断当前指令，可以继续输入。")


__all__ = ["run_repl"]

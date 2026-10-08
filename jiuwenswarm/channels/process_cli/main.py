# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Entry point for the process-style JiuwenSwarm CLI."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import re
import signal
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, NoReturn, TextIO

from jiuwenswarm.channels.process_cli.display_context import resolve_cli_work_mode
from jiuwenswarm.channels.process_cli.diagnostics import capture_diagnostics


def _configure_forwarded_stdio(args: argparse.Namespace) -> None:
    """Keep the internal REPL worker pipe contract UTF-8 on Windows."""

    if not getattr(args, "_forwarded_live_input", False):
        return
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")


class _WindowsWorkerInterruptController:
    """Turn the REPL worker's CTRL_BREAK into cancellable async cleanup."""

    def __init__(self, *, enabled: bool) -> None:
        self.enabled = enabled and os.name == "nt" and hasattr(signal, "SIGBREAK")
        self.interrupted = False
        self._installed = False
        self._previous_handler: Any = None
        self._task: asyncio.Task[int] | None = None

    def install(self) -> None:
        if not self.enabled or self._installed:
            return
        self._previous_handler = signal.signal(signal.SIGBREAK, self._handle)
        self._installed = True

    def restore(self) -> None:
        if not self._installed:
            return
        signal.signal(signal.SIGBREAK, self._previous_handler)
        self._installed = False

    def _handle(self, _signum, _frame) -> None:
        self.interrupted = True
        task = self._task
        if task is not None and not task.done():
            task.cancel()

    async def run(self, operation: Callable[[], Awaitable[int]]) -> int:
        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("process CLI worker has no active asyncio task")
        self._task = task
        try:
            if self.interrupted:
                raise asyncio.CancelledError
            return await operation()
        finally:
            self._task = None


def _translate_argparse_error(message: str) -> str:
    invalid_choice = re.fullmatch(
        r"argument (?P<argument>\S+): invalid choice: (?P<value>.+) "
        r"\(choose from (?P<choices>.+)\)",
        message,
    )
    if invalid_choice:
        return (
            f"参数 {invalid_choice.group('argument')} 的值无效："
            f"{invalid_choice.group('value')}"
            f"（可选值：{invalid_choice.group('choices')}）"
        )

    required = re.fullmatch(r"the following arguments are required: (.+)", message)
    if required:
        return f"缺少必需参数：{required.group(1)}"

    unrecognized = re.fullmatch(r"unrecognized arguments: (.+)", message)
    if unrecognized:
        return f"无法识别的参数：{unrecognized.group(1)}"

    expected = re.fullmatch(r"argument (\S+): expected one argument", message)
    if expected:
        return f"参数 {expected.group(1)} 需要一个值"

    invalid_value = re.fullmatch(
        r"argument (\S+): invalid (\S+) value: (.+)",
        message,
    )
    if invalid_value:
        return f"参数 {invalid_value.group(1)} 的值无效：{invalid_value.group(3)}"

    return message


class ChineseArgumentParser(argparse.ArgumentParser):
    """Keep argparse behavior while presenting its fixed labels in Chinese."""

    def format_usage(self) -> str:
        return super().format_usage().replace("usage:", "用法：", 1)

    def format_help(self) -> str:
        return (
            super()
            .format_help()
            .replace("usage:", "用法：", 1)
            .replace("positional arguments:", "位置参数：", 1)
            .replace("options:", "选项：", 1)
            .replace(
                "show this help message and exit",
                "显示帮助信息并退出。",
                1,
            )
        )

    def error(self, message: str) -> NoReturn:
        self.print_usage(sys.stderr)
        self.exit(2, f"{self.prog}: 错误：{_translate_argparse_error(message)}\n")


def build_parser() -> argparse.ArgumentParser:
    parser = ChineseArgumentParser(
        prog="jiuwenswarm-process",
        description="在独立的本地 Runtime 进程中运行 JiuwenSwarm 指令。",
    )
    parser.add_argument(
        "prompt",
        nargs="?",
        help="要执行的指令；省略后进入交互式 CLI。",
    )
    parser.add_argument("--session", help="恢复已有的 Runtime 会话 ID。")
    parser.add_argument(
        "--query-json",
        metavar="FILE|-",
        help="执行一次只读 Runtime 查询，输出 query_result 后关闭 Runtime 并退出。",
    )
    parser.add_argument(
        "--run-json",
        metavar="FILE|-",
        help="执行一份机器 JSON 请求并输出版本化 JSONL；- 从标准输入读取至 EOF。",
    )
    parser.add_argument(
        "--run-jsonl",
        action="store_true",
        help="一次进程内按行接收 run、answer、cancel；任务结束即退出。",
    )
    parser.add_argument("--cwd", help="工作目录；默认为当前目录。")
    parser.add_argument("--project-dir", help="稳定的项目目录；默认与工作目录相同。")
    parser.add_argument(
        "--trusted-dir",
        action="append",
        default=[],
        help="可信目录；可重复指定多个目录。",
    )
    parser.add_argument("--mode", default="code.normal", help="Runtime 运行模式。")
    parser.add_argument(
        "--work-mode",
        choices=("code", "work"),
        default="code",
        help="Runtime 工作模式配置。",
    )
    parser.add_argument(
        "--output",
        choices=("human", "json", "jsonl"),
        default="human",
        help="Runtime 事件流的输出格式。",
    )
    parser.add_argument("--timeout", type=float, help="总执行超时时间，单位为秒。")
    parser.add_argument("--show-reasoning", action="store_true", help="显示思考过程。")
    parser.add_argument(
        "--show-tools", action="store_true", help="显示工具调用和结果。"
    )
    parser.add_argument(
        "--debug", action="store_true", help="单次命令在终端显示详细诊断日志。"
    )
    parser.add_argument(
        "--_interactive-worker",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--_forwarded-live-input",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--_session-result-file",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--_worker-result-file",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--_model-selection", dest="model_selection", help=argparse.SUPPRESS
    )
    parser.add_argument(
        "--_prompt-file",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--_operation",
        choices=(
            "chat",
            "skills.list",
            "session.create",
            "session.switch",
            "session.fork",
            "session.delete",
        ),
        default="chat",
        help=argparse.SUPPRESS,
    )
    return parser


def _activate_requested_cwd(
    args: argparse.Namespace,
    parser: argparse.ArgumentParser,
) -> None:
    """Apply ``--cwd`` to the whole local process before Runtime imports."""
    requested = str(args.cwd or "").strip()
    if not requested:
        return
    try:
        target = Path(requested).expanduser().resolve(strict=True)
        if not target.is_dir():
            raise NotADirectoryError(target)
        os.chdir(target)
    except OSError as exc:
        parser.error(f"--cwd 无法访问：{exc}")
    args.cwd = str(target)


def _report_machine_failure(
    code: int, stderr: TextIO, log_path: Path | None
) -> None:
    if code == 0:
        return
    detail = f"；诊断日志：{log_path}" if log_path else ""
    _write_stderr(
        stderr,
        f"jiuwenswarm-process: 请求失败（退出码 {code}）；"
        f"详见标准输出的错误记录{detail}",
    )


def _write_stderr(stream: TextIO, message: str) -> None:
    """Write a user-visible CLI message independently of diagnostic logging."""
    stream.write(f"{message}\n")
    stream.flush()


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    _configure_forwarded_stdio(args)
    if args.query_json is not None:
        standalone = sys.argv[1:] in (
            ["--query-json", args.query_json],
            [f"--query-json={args.query_json}"],
        )
        with capture_diagnostics(capture_stdout=False, debug=args.debug) as streams:
            from jiuwenswarm.channels.process_cli.query_entry import (
                execute_query_source,
            )

            code = execute_query_source(
                args.query_json, conflicting_arguments=not standalone
            )
            _report_machine_failure(code, streams.stderr, streams.log_path)
        sys.exit(code)
    if args.run_jsonl:
        with capture_diagnostics(capture_stdout=False, debug=args.debug) as streams:
            from jiuwenswarm.channels.process_cli.machine_entry import execute_source

            code = execute_source(
                "-",
                json_lines=True,
                conflicting_arguments=sys.argv[1:] != ["--run-jsonl"],
            )
            _report_machine_failure(code, streams.stderr, streams.log_path)
        sys.exit(code)
    if args.run_json is not None:
        command_args = sys.argv[1:]
        standalone = command_args == ["--run-json", args.run_json]
        standalone = standalone or command_args == [f"--run-json={args.run_json}"]
        conflicting = not standalone
        with capture_diagnostics(capture_stdout=False, debug=args.debug) as streams:
            from jiuwenswarm.channels.process_cli.machine_entry import execute_source

            code = execute_source(args.run_json, conflicting_arguments=conflicting)
            _report_machine_failure(code, streams.stderr, streams.log_path)
        sys.exit(code)
    args.work_mode = resolve_cli_work_mode(args.mode, args.work_mode)
    worker_interrupt = _WindowsWorkerInterruptController(
        enabled=bool(getattr(args, "_interactive_worker", False)),
    )
    worker_interrupt.install()
    diagnostic_log_path: Path | None = None
    try:
        prompt_file = getattr(args, "_prompt_file", None)
        if prompt_file:
            if args.prompt is not None:
                parser.error("内部 prompt 文件不能与位置参数同时使用")
            args.prompt = Path(prompt_file).read_text(encoding="utf-8")
        _activate_requested_cwd(args, parser)
        if args.timeout is not None and args.timeout <= 0:
            parser.error("--timeout 必须大于零")
        if args.prompt is None and args.output != "human":
            parser.error("交互模式仅支持 --output human")
        if args.prompt is None and args.debug:
            parser.error("--debug 仅支持非交互式运行")
        if args.prompt is None:
            from jiuwenswarm.channels.process_cli.repl import run_repl

            code = asyncio.run(run_repl(args))
            sys.exit(code)

        # The REPL worker has a separate stderr pipe and receipt protocol.
        # Standalone commands capture both Python and native diagnostics.
        worker = bool(getattr(args, "_interactive_worker", False))
        with capture_diagnostics(
            capture_stdout=not worker,
            debug=args.debug or worker,
        ) as streams:
            diagnostic_log_path = streams.log_path
            with contextlib.redirect_stdout(sys.stderr):
                from jiuwenswarm.channels.process_cli.app import run

                async def run_command() -> int:
                    return await run(
                        args,
                        stdout=streams.stdout,
                        stderr=streams.stdout if worker else streams.stderr,
                    )

                code = asyncio.run(worker_interrupt.run(run_command))
            if code != 0 and streams.log_path is not None:
                _write_stderr(streams.stderr, f"诊断日志：{streams.log_path}")
    except KeyboardInterrupt:
        code = 130
    except asyncio.CancelledError:
        if not worker_interrupt.interrupted:
            raise
        code = 130
    except Exception as exc:  # noqa: BLE001 - command-line boundary
        detail = f"；详细日志：{diagnostic_log_path}" if diagnostic_log_path else ""
        _write_stderr(
            sys.stderr,
            f"jiuwenswarm-process: 启动失败（{type(exc).__name__}）{detail}",
        )
        code = 1
    finally:
        worker_interrupt.restore()
    sys.exit(code)


if __name__ == "__main__":
    main()

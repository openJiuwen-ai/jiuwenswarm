# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Terminal presentation helpers for the process-style CLI."""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import unicodedata
from collections.abc import Iterable
from pathlib import Path
from typing import TextIO

from jiuwenswarm.channels.process_cli.commands import SLASH_COMMANDS

_ANSI_RESET = "\033[0m"
_ANSI_BOLD_CYAN = "\033[1;36m"
_ANSI_CYAN = "\033[36m"
_ANSI_GREEN = "\033[32m"
_ANSI_YELLOW = "\033[33m"
_ANSI_RED = "\033[31m"
_ANSI_DIM = "\033[2m"


def _display_width(value: str) -> int:
    width = 0
    for character in value:
        if unicodedata.combining(character):
            continue
        width += 2 if unicodedata.east_asian_width(character) in {"W", "F"} else 1
    return width


def _truncate(value: str, width: int) -> str:
    if width <= 0:
        return ""
    if _display_width(value) <= width:
        return value
    marker = "…" if width > 1 else ""
    target = width - _display_width(marker)
    result: list[str] = []
    used = 0
    for character in value:
        character_width = _display_width(character)
        if used + character_width > target:
            break
        result.append(character)
        used += character_width
    return "".join(result) + marker


def _wrap_display(value: str, width: int) -> list[str]:
    if width <= 0:
        return [""]
    lines: list[str] = []
    current: list[str] = []
    used = 0
    for character in value:
        character_width = _display_width(character)
        if current and used + character_width > width:
            lines.append("".join(current))
            current = []
            used = 0
        current.append(character)
        used += character_width
    lines.append("".join(current))
    return lines


def _supports_unicode(stream: TextIO) -> bool:
    encoding = getattr(stream, "encoding", None) or "utf-8"
    try:
        "╭─╮│╰╯✓×⠋…".encode(encoding)
    except (LookupError, UnicodeEncodeError):
        return False
    return True


def _supports_color(stream: TextIO) -> bool:
    if os.getenv("NO_COLOR") is not None or os.getenv("TERM") == "dumb":
        return False
    isatty = getattr(stream, "isatty", None)
    return bool(callable(isatty) and isatty())


class ProcessCliUI:
    """Render the REPL shell without owning any Runtime behavior."""

    def __init__(
        self,
        stream: TextIO | None = None,
        *,
        columns: int | None = None,
    ) -> None:
        self.stream = stream or sys.stdout
        self.columns = columns or shutil.get_terminal_size(fallback=(80, 24)).columns
        self.unicode = _supports_unicode(self.stream)
        self.color = _supports_color(self.stream)

    def startup(
        self,
        *,
        model_name: str,
        mode: str,
        cwd: str,
        session_id: str | None,
    ) -> None:
        session = session_id or "尚未创建"
        model = model_name or "未配置"
        if self.columns < 48:
            self._write_wrapped(">_ JiuwenSwarm", style=_ANSI_BOLD_CYAN)
            self._write_wrapped("进程式 CLI · 本地 Runtime")
            self._write_wrapped(f"模型（配置推断）：{model}")
            self._write_wrapped(f"目录：{cwd}")
            self._write_wrapped(f"模式（请求推断）：{mode}")
            self._write_wrapped(f"会话：{session}")
            self._write("\n")
        else:
            lines = [
                (">_ JiuwenSwarm", _ANSI_BOLD_CYAN),
                ("进程式 CLI · 本地 Runtime", _ANSI_DIM),
                ("", ""),
                (f"模型（配置推断）：  {model}", ""),
                (f"目录：  {cwd}", ""),
                (f"模式（请求推断）：  {mode}", ""),
                (f"会话：  {session}", ""),
            ]
            self._card(lines)
            self._write("\n")

        self._write_wrapped(
            "每条指令均在独立进程中运行，Runtime 会话将在不同轮次间保留。",
            indent="  ",
        )
        self._write_wrapped(
            "输入 / 查看可用命令。",
            style=_ANSI_DIM,
            indent="  ",
        )
        self._write("\n")

    def help(self, *, compact: bool = False) -> None:
        if compact:
            self._write_wrapped("常用命令：", style=_ANSI_DIM, indent="  ")
            self._write("\n")
        else:
            self._write("\n")
            self._write_wrapped("可用命令：", style=_ANSI_BOLD_CYAN)
            self._write("\n")
        labels = [
            f"{command.name:<11}{command.description}" for command in SLASH_COMMANDS
        ]
        if self.columns >= 68:
            for index in range(0, len(labels), 2):
                left = labels[index]
                right = labels[index + 1] if index + 1 < len(labels) else ""
                gap = " " * max(4, 36 - _display_width(left))
                self._write_wrapped(f"{left}{gap}{right}", indent="  ")
            self._write("\n")
        else:
            for label in labels:
                self._write_wrapped(label, indent="  ")
            self._write("\n")

    def status(
        self,
        *,
        model_name: str,
        mode: str,
        cwd: str,
        session_id: str | None,
    ) -> None:
        session = (
            f"会话 {self.short_session(session_id)}" if session_id else "尚未创建会话"
        )
        content = f"  {model_name or '未配置'} · {mode} · {session} · {cwd}"
        self._write(
            self._styled(_truncate(content, max(self.columns - 1, 1)), _ANSI_DIM)
        )
        self._write("\n\n")

    def notice(self, message: str) -> None:
        self._write(self._styled(f"\n! {message}\n\n", _ANSI_YELLOW))

    def blank_line(self) -> None:
        self._write("\n")

    def diagnostics(self, lines: Iterable[str]) -> None:
        self._write("\n工作进程诊断信息：\n")
        self._write("\n".join(lines))
        self._write("\n")

    def session(self, session_id: str | None) -> None:
        if session_id:
            self._write(f"\n当前 Runtime 会话：{session_id}\n\n")
        else:
            self._write("\n尚未创建 Runtime 会话。\n\n")

    @staticmethod
    def short_session(session_id: str | None) -> str:
        value = str(session_id or "")
        return value if len(value) <= 12 else value[:12] + "…"

    def _card(self, lines: list[tuple[str, str]]) -> None:
        width = min(72, max(46, self.columns - 2))
        content_width = width - 4
        if self.unicode:
            top_left, horizontal, top_right = "╭", "─", "╮"
            vertical = "│"
            bottom_left, bottom_right = "╰", "╯"
        else:
            top_left = top_right = bottom_left = bottom_right = "+"
            horizontal = "-"
            vertical = "|"
        self._write(f"{top_left}{horizontal * (width - 2)}{top_right}\n")
        for text, style in lines:
            clipped = _truncate(text, content_width)
            padding = " " * max(content_width - _display_width(clipped), 0)
            self._write(
                f"{vertical} {self._styled(clipped, style)}{padding} {vertical}\n"
            )
        self._write(f"{bottom_left}{horizontal * (width - 2)}{bottom_right}\n")

    def _styled(self, value: str, style: str) -> str:
        if not self.color or not style:
            return value
        return f"{style}{value}{_ANSI_RESET}"

    def _write_wrapped(
        self,
        value: str,
        *,
        style: str = "",
        indent: str = "",
    ) -> None:
        indent_width = _display_width(indent)
        available = max(self.columns - indent_width, 1)
        for line in _wrap_display(value, available):
            self._write(f"{indent}{self._styled(line, style)}\n")

    def _write(self, value: str) -> None:
        self.stream.write(value)
        self.stream.flush()


class HumanRunUI:
    """TTY-only decoration for a single human-readable Runtime execution."""

    def __init__(self, stdout: TextIO, stderr: TextIO) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.enhanced = _supports_color(stdout) or (
            bool(getattr(stdout, "isatty", lambda: False)())
            and _supports_unicode(stdout)
        )
        self.unicode = _supports_unicode(stdout)
        self.color = _supports_color(stdout)
        self._status_visible = False
        self._status_text = ""
        self._spinner_task: asyncio.Task[None] | None = None
        self._frame_index = 0
        self._assistant_visible = False

    @staticmethod
    def start() -> None:
        """Keep Runtime initialization silent until request processing begins."""

    def working(self) -> None:
        if not self.enhanced:
            return
        self._set_status("正在处理……")

    def begin_assistant(self) -> None:
        self.clear_status()
        if self._assistant_visible or not self.enhanced:
            return
        self.stdout.write(self._styled("• JiuwenSwarm\n\n", _ANSI_BOLD_CYAN))
        self.stdout.flush()
        self._assistant_visible = True

    def clear_status(self) -> None:
        if not self._status_visible:
            return
        self._cancel_spinner()
        self.stdout.write("\r\033[2K" if self.color else "\n")
        self.stdout.flush()
        self._status_visible = False
        self._status_text = ""

    def completed(self, session_id: str) -> None:
        if not self.enhanced:
            return
        self.clear_status()
        marker = "✓" if self.unicode else "+"
        session = ProcessCliUI.short_session(session_id)
        self.stdout.write(
            self._styled(f"\n{marker} 执行完成 · 会话 {session}\n", _ANSI_GREEN)
        )
        self.stdout.flush()

    def failed(self, message: str) -> None:
        self.clear_status()
        marker = "×" if self.unicode else "x"
        self.stderr.write(self._styled(f"\n{marker} 执行失败\n\n", _ANSI_RED))
        self.stderr.write(f"  {message}\n")
        self.stderr.flush()

    def interrupted(self) -> None:
        self.clear_status()
        self.stderr.write(self._styled("\n! 已中断\n", _ANSI_YELLOW))
        self.stderr.flush()

    def reasoning(self, text: str) -> None:
        self.clear_status()
        branch = "├─" if self.unicode else "+-"
        self.stderr.write(self._styled(f"\n  {branch} 思考\n", _ANSI_DIM))
        self.stderr.write(f"  │  {text}\n" if self.unicode else f"     {text}\n")
        self.stderr.flush()

    def tool(self, label: str, text: str) -> None:
        self.clear_status()
        branch = "├─" if self.unicode else "+-"
        self.stderr.write(self._styled(f"\n  {branch} {label}\n", _ANSI_DIM))
        self.stderr.write(f"  │  {text}\n" if self.unicode else f"     {text}\n")
        self.stderr.flush()

    def _styled(self, value: str, style: str) -> str:
        if not self.color:
            return value
        return f"{style}{value}{_ANSI_RESET}"

    def _set_status(self, text: str) -> None:
        self._status_text = text
        if self.color:
            if not self._status_visible:
                self.stdout.write("\n")
                self._status_visible = True
            self._render_status()
            self._start_spinner()
            return
        if self._status_visible:
            self.stdout.write("\n")
        marker = "⠋" if self.unicode else "*"
        self.stdout.write(self._styled(f"  {marker} {text}", _ANSI_CYAN))
        self.stdout.flush()
        self._status_visible = True

    def _render_status(self) -> None:
        frames = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏" if self.unicode else "|/-\\"
        frame = frames[self._frame_index % len(frames)]
        self.stdout.write(
            "\r\033[2K" + self._styled(f"  {frame} {self._status_text}", _ANSI_CYAN)
        )
        self.stdout.flush()

    def _start_spinner(self) -> None:
        if self._spinner_task is not None and not self._spinner_task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._spinner_task = loop.create_task(self._animate())
        self._spinner_task.add_done_callback(self._consume_spinner_result)

    async def _animate(self) -> None:
        while self._status_visible:
            await asyncio.sleep(0.12)
            if not self._status_visible:
                return
            self._frame_index += 1
            self._render_status()

    def _cancel_spinner(self) -> None:
        task = self._spinner_task
        self._spinner_task = None
        if task is not None and not task.done():
            task.cancel()

    @staticmethod
    def _consume_spinner_result(task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        task.exception()


def resolved_cwd(value: str | None) -> str:
    return str(Path(value or os.getcwd()).expanduser().resolve())


__all__ = ["HumanRunUI", "ProcessCliUI", "resolved_cwd"]

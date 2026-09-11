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
from typing import Any, TextIO

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

    def skills(self, items: list[Any]) -> None:
        """Render the Runtime ``skills.list`` payload without changing it."""
        skills = [item for item in items if isinstance(item, dict)]
        installed = [item for item in skills if item.get("installed") is True]
        available = [item for item in skills if item.get("installed") is not True]

        self._write("\n")
        self._write_wrapped(
            f"已安装技能（{len(installed)}）" if installed else "暂无已安装技能",
            style=_ANSI_BOLD_CYAN,
        )
        for item in installed:
            self._write_skill(item)
        if available:
            self._write("\n")
            self._write_wrapped(
                f"可安装技能（{len(available)}）",
                style=_ANSI_BOLD_CYAN,
            )
            for item in available:
                self._write_skill(item)
        self._write("\n")

    def sessions(
        self,
        items: list[Any],
        *,
        current_session_id: str = "",
    ) -> None:
        """Render a channel-owned Runtime Session catalog."""
        sessions = [item for item in items if isinstance(item, dict)]
        self._write("\n")
        self._write_wrapped(
            f"进程式 CLI 会话（{len(sessions)}）"
            if sessions
            else "暂无进程式 CLI 会话",
            style=_ANSI_BOLD_CYAN,
        )
        for item in sessions:
            session_id = str(item.get("session_id") or "?")
            title = str(item.get("title") or "").strip()
            mode = str(item.get("mode") or "unknown").strip()
            marker = "*" if session_id == current_session_id else "-"
            current = " [当前]" if marker == "*" else ""
            suffix = f" · {title}" if title else ""
            self._write_wrapped(
                f"{marker} {session_id}{current} · {mode}{suffix}",
                indent="  ",
            )
        self._write("\n")

    def models(
        self,
        items: list[Any],
        *,
        current_selection: str = "",
    ) -> None:
        """Render the credential-free Runtime model catalog."""
        models = [item for item in items if isinstance(item, dict)]
        self._write("\n")
        self._write_wrapped(
            f"可选聊天模型（{len(models)}）" if models else "暂无可用聊天模型",
            style=_ANSI_BOLD_CYAN,
        )
        for item in models:
            selection_key = str(item.get("selection_key") or "?")
            display_name = str(item.get("display_name") or selection_key)
            provider = str(item.get("provider") or "").strip()
            marker = "*" if selection_key == current_selection else "-"
            current = " [当前]" if marker == "*" else ""
            agentos = " [agentos]" if item.get("is_agentos") is True else ""
            provider_text = f" · {provider}" if provider else ""
            key_text = (
                f" · 选择键 {selection_key}" if selection_key != display_name else ""
            )
            self._write_wrapped(
                f"{marker} {display_name}{current}{agentos}{provider_text}{key_text}",
                indent="  ",
            )
        self._write("\n")

    def model_selected(self, item: dict[str, Any], *, persisted: bool) -> None:
        selection_key = str(item.get("selection_key") or "?")
        display_name = str(item.get("display_name") or selection_key)
        scope = "当前会话" if persisted else "下一次请求"
        self.notice(f"已为{scope}选择模型：{display_name}（{selection_key}）")

    def context_compacted(self, payload: dict[str, Any]) -> None:
        """Render one terminal compact result without transport semantics."""
        result = str(payload.get("result") or "noop").strip().lower()
        stats = payload.get("stats")
        if result == "busy":
            self.notice("当前会话正在执行其他上下文压缩，请稍后重试。")
            return
        if result != "compressed":
            self.notice("当前上下文无需压缩。")
            return
        before = after = 0
        if isinstance(stats, dict):
            before = int(stats.get("raw_total_tokens") or 0)
            after = int(stats.get("total_tokens") or 0)
        if before > 0:
            saved = round((before - after) / before * 100, 1)
            self.notice(
                f"上下文压缩完成：{after / 1000:.1f}K/"
                f"{before / 1000:.1f}K tokens（节省 {saved:.1f}%）"
            )
            return
        self.notice("上下文压缩完成。")

    def rewind_turns(self, items: list[Any]) -> None:
        """Render Runtime-owned rewind targets without storage knowledge."""
        turns = [item for item in items if isinstance(item, dict)]
        self._write("\n")
        self._write_wrapped(
            f"可回退轮次（{len(turns)}）" if turns else "当前会话暂无可回退轮次",
            style=_ANSI_BOLD_CYAN,
        )
        for item in turns:
            turn_index = int(item.get("turn_index") or 0)
            preview = str(item.get("content_preview") or "").strip() or "（无文本）"
            stats = item.get("stats")
            file_count = (
                int(stats.get("filesChanged") or 0) if isinstance(stats, dict) else 0
            )
            suffix = f" · {file_count} 个文件" if file_count else ""
            self._write_wrapped(
                f"{turn_index}. {preview}{suffix}",
                indent="  ",
            )
        if turns:
            self._write_wrapped(
                "使用 /rewind <轮次> [conversation|all|files] 执行回退。",
                style=_ANSI_DIM,
                indent="  ",
            )
        self._write("\n")

    def session_rewound(self, payload: dict[str, Any]) -> None:
        """Render one completed Runtime rewind mutation."""
        turn_index = int(payload.get("turn_index") or 0)
        action = str(payload.get("action") or "conversation")
        action_names = {
            "conversation": "对话",
            "conversation_and_files": "对话与文件",
            "files_only": "文件",
        }
        message = f"已将{action_names.get(action, action)}回退到第 {turn_index} 轮"
        restored = payload.get("restored_files")
        deleted = payload.get("deleted_files")
        restored_count = len(restored) if isinstance(restored, list) else 0
        deleted_count = len(deleted) if isinstance(deleted, list) else 0
        if restored_count or deleted_count:
            message += f"（恢复 {restored_count}，删除 {deleted_count} 个文件）"
        self.notice(message)

    def memory_files(self, items: list[Any]) -> None:
        """Render read-only Memory source metadata."""
        files = [item for item in items if isinstance(item, dict)]
        self._write("\n")
        self._write_wrapped(
            f"记忆文件（{len(files)}）" if files else "当前范围内暂无记忆文件",
            style=_ANSI_BOLD_CYAN,
        )
        for item in files:
            relative_path = str(item.get("relative_path") or item.get("path") or "?")
            kind = str(item.get("kind") or "unknown")
            size = int(item.get("size") or 0)
            self._write_wrapped(
                f"- {relative_path} [{kind}] · {size} 字节",
                indent="  ",
            )
        self._write("\n")

    def memory_status(self, payload: dict[str, Any]) -> None:
        """Render side-effect-free configured Memory status."""
        enabled = "开启" if payload.get("enabled") is True else "关闭"
        proactive = "开启" if payload.get("proactive") is True else "关闭"
        auto = "开启" if payload.get("auto_memory_enabled") is True else "关闭"
        mode = str(payload.get("current_mode") or "unknown")
        engine = str(payload.get("engine") or "builtin")
        storage = str(payload.get("storage_mode") or "local")
        self._write("\n")
        self._write_wrapped("记忆状态", style=_ANSI_BOLD_CYAN)
        self._write_wrapped(f"模式：{mode}", indent="  ")
        self._write_wrapped(f"记忆：{enabled} · 主动记忆：{proactive}", indent="  ")
        self._write_wrapped(f"自动记忆：{auto}", indent="  ")
        self._write_wrapped(f"引擎：{engine} · 存储：{storage}", indent="  ")
        self._write_wrapped(
            "未启动索引服务；此命令只读取配置。",
            style=_ANSI_DIM,
            indent="  ",
        )
        self._write("\n")

    def memory_locations(self, payload: dict[str, Any]) -> None:
        """Display Memory paths without launching an external program."""
        labels = (
            ("项目记忆", "project_memory_dir"),
            ("编码记忆", "coding_memory_dir"),
            ("Agent 记忆", "agent_memory_dir"),
            ("用户记忆", "user_memory_dir"),
        )
        self._write("\n")
        self._write_wrapped("记忆目录", style=_ANSI_BOLD_CYAN)
        for label, key in labels:
            value = str(payload.get(key) or "").strip()
            if value:
                self._write_wrapped(f"{label}：{value}", indent="  ")
        self._write("\n")

    def mcp_servers(self, items: list[Any]) -> None:
        """Render safe static MCP descriptors."""
        servers = [item for item in items if isinstance(item, dict)]
        self._write("\n")
        self._write_wrapped(
            f"MCP 服务（{len(servers)}）" if servers else "暂无已配置的 MCP 服务",
            style=_ANSI_BOLD_CYAN,
        )
        for item in servers:
            name = str(item.get("name") or "?")
            transport = str(item.get("transport") or "unknown")
            state = str(item.get("connection_state") or "unknown")
            enabled = "启用" if item.get("default_enabled") is True else "禁用"
            self._write_wrapped(
                f"- {name} [{enabled}] · {transport} · {state}",
                indent="  ",
            )
        self._write("\n")

    def mcp_server(self, item: dict[str, Any]) -> None:
        """Render one static MCP descriptor without endpoint data."""
        self._write("\n")
        self._write_wrapped("MCP 配置详情", style=_ANSI_BOLD_CYAN)
        labels = (
            ("名称", "name"),
            ("传输", "transport"),
            ("连接状态", "connection_state"),
            ("集成类型", "integration_type"),
            ("超时（秒）", "timeout_seconds"),
        )
        for label, key in labels:
            value = item.get(key)
            if value not in (None, ""):
                self._write_wrapped(f"{label}：{value}", indent="  ")
        enabled = "启用" if item.get("default_enabled") is True else "禁用"
        self._write_wrapped(f"默认状态：{enabled}", indent="  ")
        self._write_wrapped(
            "静态查看不会探测端点或启动 MCP 工具。",
            style=_ANSI_DIM,
            indent="  ",
        )
        self._write("\n")

    def agent_definitions(self, items: list[Any]) -> None:
        """Render safe custom-Agent catalog metadata."""
        agents = [item for item in items if isinstance(item, dict)]
        self._write("\n")
        self._write_wrapped(
            f"Agent 定义（{len(agents)}）" if agents else "暂无 Agent 定义",
            style=_ANSI_BOLD_CYAN,
        )
        for item in agents:
            name = str(item.get("name") or "?")
            source = str(item.get("source") or "unknown")
            description = str(item.get("description") or "").strip()
            shadowed_by = str(item.get("shadowed_by") or "").strip()
            shadowed = f" · 被 {shadowed_by} 覆盖" if shadowed_by else ""
            enabled_value = item.get("enabled")
            enabled = (
                " · 启用"
                if enabled_value is True
                else " · 禁用"
                if enabled_value is False
                else ""
            )
            suffix = f" · {description}" if description else ""
            self._write_wrapped(
                f"- {name} [{source}]{enabled}{shadowed}{suffix}",
                indent="  ",
            )
        self._write("\n")

    def agent_definition(self, item: dict[str, Any]) -> None:
        """Render one Agent definition without prompt or backing path."""
        self._write("\n")
        self._write_wrapped("Agent 配置详情", style=_ANSI_BOLD_CYAN)
        labels = (
            ("名称", "name"),
            ("描述", "description"),
            ("来源", "source"),
            ("模型", "model"),
            ("权限模式", "permission_mode"),
            ("记忆范围", "memory_scope"),
            ("调用时机", "when_to_use"),
            ("最大迭代", "max_iterations"),
        )
        for label, key in labels:
            value = item.get(key)
            if value not in (None, ""):
                self._write_wrapped(f"{label}：{value}", indent="  ")
        for label, key in (
            ("工具", "tools"),
            ("禁用工具", "disallowed_tools"),
            ("技能", "skills"),
        ):
            values = item.get(key)
            if isinstance(values, list) and values:
                self._write_wrapped(
                    f"{label}：{', '.join(str(value) for value in values)}",
                    indent="  ",
                )
        self._write("\n")

    def agent_tools(self, items: list[Any]) -> None:
        """Render static tool choices for custom Agent definitions."""
        tools = [item for item in items if isinstance(item, dict)]
        self._write("\n")
        self._write_wrapped(
            f"Agent 可配置工具（{len(tools)}）" if tools else "暂无可配置工具",
            style=_ANSI_BOLD_CYAN,
        )
        for item in tools:
            name = str(item.get("name") or item.get("internal_name") or "?")
            internal = str(item.get("internal_name") or "").strip()
            group = str(item.get("group") or "").strip()
            description = str(item.get("description") or "").strip()
            key = f" ({internal})" if internal and internal != name else ""
            suffix = " · ".join(value for value in (group, description) if value)
            suffix = f" · {suffix}" if suffix else ""
            self._write_wrapped(f"- {name}{key}{suffix}", indent="  ")
        self._write("\n")

    def permission_snapshot(self, payload: dict[str, Any]) -> None:
        """Render the effective read-only permission snapshot."""
        scope = str(payload.get("scope") or "host")
        session_id = str(payload.get("session_id") or "").strip()
        effective = payload.get("effective")
        effective = effective if isinstance(effective, dict) else {}
        enabled_value = effective.get("enabled")
        enabled = (
            "开启"
            if enabled_value is True
            else "关闭"
            if enabled_value is False
            else "未显式配置"
        )
        self._write("\n")
        self._write_wrapped("权限快照（只读）", style=_ANSI_BOLD_CYAN)
        scope_text = f"会话 {session_id}" if scope == "session" else "主机"
        self._write_wrapped(f"范围：{scope_text} · 权限系统：{enabled}", indent="  ")

        tools = effective.get("tools")
        tool_items = tools if isinstance(tools, list) else []
        if tool_items:
            self._write_wrapped("工具权限：", indent="  ")
            for item in tool_items:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name") or "?")
                level = str(item.get("level") or "?")
                self._write_wrapped(f"- {name}: {level}", indent="    ")
        else:
            self._write_wrapped("工具权限：暂无显式配置", indent="  ")

        rules = effective.get("rules")
        rule_items = rules if isinstance(rules, list) else []
        if rule_items:
            self._write_wrapped("规则：", indent="  ")
            for item in rule_items:
                if not isinstance(item, dict):
                    continue
                rule_id = str(item.get("id") or "未命名")
                action = str(item.get("action") or "未设置")
                pattern = str(item.get("pattern") or "").strip()
                suffix = f" · {pattern}" if pattern else ""
                self._write_wrapped(
                    f"- {rule_id}: {action}{suffix}",
                    indent="    ",
                )
        else:
            self._write_wrapped("规则：暂无显式配置", indent="  ")
        self._write_wrapped(
            "此命令不会修改权限、执行审批或重新加载 Agent。",
            style=_ANSI_DIM,
            indent="  ",
        )
        self._write("\n")

    def _write_skill(self, item: dict[str, Any]) -> None:
        name = str(item.get("name") or "?")
        if item.get("is_builtin_source") is True or item.get("is_builtin") is True:
            source = "内置"
        else:
            source = str(item.get("source") or "项目")
        description = str(item.get("description") or "").strip()
        suffix = f" · {description}" if description else ""
        self._write_wrapped(f"- {name} [{source}]{suffix}", indent="  ")

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

    def skills(self, items: list[Any]) -> None:
        """Render a skills list after clearing transient progress output."""
        self.clear_status()
        ProcessCliUI(self.stdout).skills(items)

    def sessions(
        self,
        items: list[Any],
        *,
        current_session_id: str = "",
    ) -> None:
        """Render a Session catalog after clearing transient progress output."""
        self.clear_status()
        ProcessCliUI(self.stdout).sessions(
            items,
            current_session_id=current_session_id,
        )

    def models(
        self,
        items: list[Any],
        *,
        current_selection: str = "",
    ) -> None:
        self.clear_status()
        ProcessCliUI(self.stdout).models(
            items,
            current_selection=current_selection,
        )

    def model_selected(self, item: dict[str, Any], *, persisted: bool) -> None:
        self.clear_status()
        ProcessCliUI(self.stdout).model_selected(item, persisted=persisted)

    def context_compacted(self, payload: dict[str, Any]) -> None:
        self.clear_status()
        ProcessCliUI(self.stdout).context_compacted(payload)

    def rewind_turns(self, items: list[Any]) -> None:
        self.clear_status()
        ProcessCliUI(self.stdout).rewind_turns(items)

    def session_rewound(self, payload: dict[str, Any]) -> None:
        self.clear_status()
        ProcessCliUI(self.stdout).session_rewound(payload)

    def memory_files(self, items: list[Any]) -> None:
        self.clear_status()
        ProcessCliUI(self.stdout).memory_files(items)

    def memory_status(self, payload: dict[str, Any]) -> None:
        self.clear_status()
        ProcessCliUI(self.stdout).memory_status(payload)

    def memory_locations(self, payload: dict[str, Any]) -> None:
        self.clear_status()
        ProcessCliUI(self.stdout).memory_locations(payload)

    def mcp_servers(self, items: list[Any]) -> None:
        self.clear_status()
        ProcessCliUI(self.stdout).mcp_servers(items)

    def mcp_server(self, item: dict[str, Any]) -> None:
        self.clear_status()
        ProcessCliUI(self.stdout).mcp_server(item)

    def agent_definitions(self, items: list[Any]) -> None:
        self.clear_status()
        ProcessCliUI(self.stdout).agent_definitions(items)

    def agent_definition(self, item: dict[str, Any]) -> None:
        self.clear_status()
        ProcessCliUI(self.stdout).agent_definition(item)

    def agent_tools(self, items: list[Any]) -> None:
        self.clear_status()
        ProcessCliUI(self.stdout).agent_tools(items)

    def permission_snapshot(self, payload: dict[str, Any]) -> None:
        self.clear_status()
        ProcessCliUI(self.stdout).permission_snapshot(payload)

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

    def session_event(self, event_type: str, payload: dict[str, Any]) -> None:
        """Render one successful Runtime Session lifecycle result."""
        self.clear_status()
        session_id = str(payload.get("session_id") or "")
        if event_type == "session.created":
            message = f"已创建并切换到会话 {session_id}"
        elif event_type == "session.switched":
            message = f"已恢复会话 {session_id}"
        elif event_type == "session.forked":
            title = str(payload.get("title") or "").strip()
            suffix = f" · {title}" if title else ""
            message = f"已创建并切换到会话分支 {session_id}{suffix}"
        elif event_type == "session.deleted":
            message = f"已删除会话 {session_id}"
        else:
            return
        marker = "✓" if self.unicode else "+"
        self.stdout.write(self._styled(f"\n{marker} {message}\n", _ANSI_GREEN))
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

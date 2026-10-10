# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""AgentMdPromptRail — attach the nearest project instruction file.

Searches the session working directory upward (at most three levels) for a
project instruction file such as ``agent.md`` / ``claude.md`` and publishes
its content as a prompt attachment (``project.instructions``). The prompt
attachment pipeline guarantees the delivery semantics: the first effective
context carries a full snapshot, unchanged content appends nothing, and a
snapshot lost to context compaction is re-sent in full on the next turn.

The rail itself only keeps the attachment section in sync with the file
found on disk: create/update the section when the file (or its content)
changes, clear it when the file disappears. Injection text stays in English
regardless of the configured response language.
"""
from __future__ import annotations

import logging
import os
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Sequence

from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.harness.prompts.prompt_attachment_manager import (
    PromptAttachmentKind,
)
from openjiuwen.harness.rails.base import DeepAgentRail

logger = logging.getLogger(__name__)


class ProjectInstructionFile(str, Enum):
    """Candidate project-instruction filenames.

    Declaration order is the same-directory match priority. Matching is
    case-insensitive on every platform so ``agent.md`` / ``AGENT.md`` /
    ``Agent.md`` behave identically on Windows and Linux.
    """

    AGENT_MD = "agent.md"
    CLAUDE_MD = "claude.md"


#: 查找层级上限：会话工作目录本身 + 两层祖先（对齐“最多查找三层”）。
DEFAULT_MAX_LEVELS = 3
#: 单条 attachment 的内容上限，与 PromptAttachmentManager 渲染期单条截断一致。
DEFAULT_MAX_CHARS = 12_000
SECTION_NAME = "project.instructions"
SECTION_SOURCE = "jiuwenswarm.agent_md_prompt_rail"
#: 排序值：attachment 按 (priority, source, section) 排序，早于 runtime.setting(95)。
SECTION_PRIORITY = 90

#: bash 工具（openjiuwen harness BashTool）入参里指定工作目录的字段名。
BASH_TOOL_NAME = "bash"
BASH_WORKDIR_PARAM = "workdir"

_TOOL_NOTICE_OPEN = "<system-reminder>"
_TOOL_NOTICE_CLOSE = "</system-reminder>"

_TRUNCATION_NOTICE = (
    "\n\n[Project instructions truncated: content exceeded {max_chars} characters.]"
)


def _same_path(left: Path | None, right: Path | None) -> bool:
    """Windows 大小写不敏感的路径等价比较；任一为 None 返回 False。"""
    if left is None or right is None:
        return False
    if os.name == "nt":
        return os.path.normcase(str(left)) == os.path.normcase(str(right))
    return str(left) == str(right)


def find_project_instruction_file(
    cwd: str | Path,
    *,
    categories: Sequence[ProjectInstructionFile] = tuple(ProjectInstructionFile),
    max_levels: int = DEFAULT_MAX_LEVELS,
) -> Path | None:
    """Return the nearest project instruction file under *cwd*, or None.

    Scans ``cwd`` first, then parent directories, up to *max_levels*
    directories in total. Within one directory, category order (the enum
    declaration order) decides which file wins. Filename matching is
    case-insensitive; entries that are not regular files are skipped.
    """
    if max_levels < 1:
        return None
    try:
        root = Path(cwd).expanduser().resolve(strict=False)
    except OSError:
        return None
    directories = [root, *root.parents][:max_levels]
    for directory in directories:
        try:
            entries = sorted(directory.iterdir(), key=lambda item: item.name.lower())
        except OSError:
            continue
        by_lower_name: dict[str, Path] = {}
        for entry in entries:
            if entry.is_file():
                by_lower_name.setdefault(entry.name.lower(), entry)
        for category in categories:
            hit = by_lower_name.get(category.value)
            if hit is not None:
                return hit
    return None


class AgentMdPromptRail(DeepAgentRail):
    """Keep the ``project.instructions`` attachment in sync with the file on disk."""

    # 与 ProjectMemoryRail.WRITE_LIKE_TOOLS 对齐：写类工具调用后重读文件内容。
    WRITE_LIKE_TOOLS = frozenset(
        {
            "write_file",
            "edit_file",
            "write_text_file",
            "write",
            "delete_file",
            "delete",
            "move_file",
            "rename_file",
        }
    )

    def __init__(
        self,
        *,
        categories: Iterable[ProjectInstructionFile] | None = None,
        max_levels: int = DEFAULT_MAX_LEVELS,
        max_chars: int = DEFAULT_MAX_CHARS,
    ) -> None:
        super().__init__()
        self._categories: tuple[ProjectInstructionFile, ...] = tuple(
            categories or ProjectInstructionFile
        )
        self._max_levels = max_levels
        self._max_chars = max_chars
        self._cwd: str | None = None
        self._attachment_manager: Any = None
        # (resolved_path, mtime_ns, size) → 已解码内容；未变化时跳过重读。
        self._read_cache: tuple[tuple[str, int, int], str] | None = None
        # 会话 attachment 当前命中的文件路径；用于 bash 目录注入去重。
        self._attached_path: str | None = None
        # 已向 bash 工具结果追加过指令的目录（同目录只追加一次）。
        self._appended_tool_cwds: set[str] = set()

    def init(self, agent: Any) -> None:
        self._attachment_manager = getattr(agent, "prompt_attachment_manager", None)

    def uninit(self, agent: Any) -> None:
        self._attachment_manager = None
        self._read_cache = None
        self._attached_path = None
        self._appended_tool_cwds.clear()

    def set_runtime_paths(self, *, cwd: str | None = None) -> None:
        """Per-request session working directory (parallel to RuntimePromptRail)."""
        normalized = str(cwd).strip() if cwd else ""
        next_cwd = normalized or None
        if next_cwd != self._cwd:
            self._cwd = next_cwd
            self._read_cache = None

    async def before_model_call(self, ctx: AgentCallbackContext) -> None:
        if self._attachment_manager is None:
            return
        writer = self._attachment_manager.bind_context(ctx)
        resolved = (
            find_project_instruction_file(
                self._cwd,
                categories=self._categories,
                max_levels=self._max_levels,
            )
            if self._cwd
            else None
        )
        if resolved is None:
            self._attached_path = None
            await self._clear_section(writer)
            return
        content = self._load_content(resolved)
        if content is None:
            self._attached_path = None
            await self._clear_section(writer)
            return
        try:
            await writer.add_section(
                SECTION_NAME,
                content,
                PromptAttachmentKind.FILE,
                SECTION_SOURCE,
                priority=SECTION_PRIORITY,
                content_kind="text/markdown",
            )
        except ValueError as exc:
            logger.warning(
                "[AgentMdPromptRail] skip prompt attachment section=%s: %s",
                SECTION_NAME,
                exc,
            )
            return
        self._attached_path = str(resolved)

    async def after_tool_call(self, ctx: AgentCallbackContext) -> None:
        tool_name = str(getattr(ctx.inputs, "tool_name", "") or "").strip()
        if tool_name in self.WRITE_LIKE_TOOLS:
            self._read_cache = None
        if tool_name == BASH_TOOL_NAME:
            self._append_bash_cwd_instructions(ctx)

    def _append_bash_cwd_instructions(self, ctx: AgentCallbackContext) -> None:
        """bash 指定了与会话 cwd 不同的 workdir 时，把该目录的 agent.md 追加进工具结果。

        与会话 attachment 的层级覆盖查找不同：这里严格只看指定目录本身一层，
        不向上扫描；同一目录在一次会话里只追加一次；与会话 attachment 当前
        命中同一文件时跳过，避免重复注入。
        """
        inputs = getattr(ctx, "inputs", None)
        if inputs is None:
            return
        tool_msg = getattr(inputs, "tool_msg", None)
        if tool_msg is None or not isinstance(getattr(tool_msg, "content", None), str):
            return
        tool_args = getattr(inputs, "tool_args", None)
        raw_workdir = (
            str(tool_args.get(BASH_WORKDIR_PARAM, "")).strip()
            if isinstance(tool_args, dict)
            else ""
        )
        if not raw_workdir:
            return
        try:
            workdir = Path(raw_workdir).expanduser().resolve(strict=False)
        except OSError:
            return
        if not workdir.is_dir():
            return
        if _same_path(workdir, Path(self._cwd) if self._cwd else None):
            return
        if str(workdir) in self._appended_tool_cwds:
            return
        resolved_file = find_project_instruction_file(
            workdir,
            categories=self._categories,
            max_levels=1,
        )
        if resolved_file is None:
            return
        if _same_path(resolved_file, Path(self._attached_path) if self._attached_path else None):
            return
        content = self._load_content(resolved_file)
        if content is None:
            return
        suffix = f"\n\n{_TOOL_NOTICE_OPEN}\n{content}\n{_TOOL_NOTICE_CLOSE}"
        tool_msg.content = tool_msg.content + suffix
        tool_result = getattr(inputs, "tool_result", None)
        if isinstance(tool_result, str):
            inputs.tool_result = tool_result + suffix
        self._appended_tool_cwds.add(str(workdir))
        logger.info(
            "[AgentMdPromptRail] appended project instructions to bash result: %s",
            resolved_file,
        )

    def _load_content(self, path: Path) -> str | None:
        """Read the instruction file, capped at ``_max_chars`` characters."""
        try:
            stat_result = path.stat()
            stamp = (str(path), stat_result.st_mtime_ns, stat_result.st_size)
        except OSError as exc:
            logger.warning("[AgentMdPromptRail] stat failed for %s: %s", path, exc)
            return None
        if self._read_cache is not None and self._read_cache[0] == stamp:
            return self._read_cache[1]
        # UTF-8 每字符至多 4 字节，按字节上限读取即可覆盖 max_chars 字符。
        read_limit = max(self._max_chars, 1) * 4
        try:
            with open(path, "rb") as handle:
                raw = handle.read(read_limit)
        except OSError as exc:
            logger.warning("[AgentMdPromptRail] read failed for %s: %s", path, exc)
            return None
        text = raw.decode("utf-8", "ignore").strip()
        if not text:
            return None
        if len(text) > self._max_chars:
            text = text[: self._max_chars] + _TRUNCATION_NOTICE.format(
                max_chars=self._max_chars
            )
        content = (
            f"# Project instructions (from `{path}`)\n\n"
            f"{text}"
        )
        self._read_cache = (stamp, content)
        return content

    async def _clear_section(self, writer: Any) -> None:
        try:
            await writer.clear_section(SECTION_NAME)
        except ValueError as exc:
            logger.warning(
                "[AgentMdPromptRail] skip clearing prompt attachment section=%s: %s",
                SECTION_NAME,
                exc,
            )


__all__ = [
    "AgentMdPromptRail",
    "BASH_TOOL_NAME",
    "BASH_WORKDIR_PARAM",
    "DEFAULT_MAX_CHARS",
    "DEFAULT_MAX_LEVELS",
    "ProjectInstructionFile",
    "SECTION_NAME",
    "find_project_instruction_file",
]

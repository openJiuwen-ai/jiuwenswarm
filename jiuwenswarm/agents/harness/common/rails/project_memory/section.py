# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""PromptSection factory for project memory."""
from __future__ import annotations

from typing import Optional

from openjiuwen.harness.prompts import PromptSection

# Section name used by both the factory and the Rail to add/remove.
# Using a string literal (not ``SectionName.*``) so we don't need to
# modify agent-core's ``SectionName`` enum.
SECTION_NAME = "project_memory"

_HEADER_CN = "## 项目记忆（ProjectMemoryRail 自动加载）"
_HEADER_EN = "## Project Memory (auto-loaded by ProjectMemoryRail)"

_NOTE_CN = (
    "以下内容来自项目根、用户目录、本地私有文件的合并。"
    "修改磁盘文件即可在下一轮对话生效。"
)
_NOTE_EN = (
    "The following is merged from project root, user home, and local private files. "
    "Edits take effect on the next turn."
)


def build_project_memory_section(
    content: str,
    *,
    language: str = "cn",  # kept for backward compat; both languages are always populated
    priority: int = 120,
) -> Optional[PromptSection]:
    """Build the ``project_memory`` :class:`PromptSection`, or ``None`` when content is empty.

    Both ``cn`` and ``en`` content keys are always populated so that whichever
    language the agent renders with, the header/note prose stays in the right
    language. The ``language`` parameter is kept for API stability but no longer
    affects which keys are included.
    """
    del language  # accepted for API compat, not used.
    if not content or not content.strip():
        return None
    body_text = content.strip()
    body_cn = f"{_HEADER_CN}\n\n{_NOTE_CN}\n\n{body_text}\n"
    body_en = f"{_HEADER_EN}\n\n{_NOTE_EN}\n\n{body_text}\n"
    return PromptSection(
        name=SECTION_NAME,
        content={"cn": body_cn, "en": body_en},
        priority=priority,
    )


__all__ = ["build_project_memory_section", "SECTION_NAME"]

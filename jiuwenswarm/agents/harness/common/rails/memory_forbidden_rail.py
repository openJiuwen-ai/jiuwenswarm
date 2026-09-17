# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Execution-time guard for sensitive information written to memory."""

from __future__ import annotations

import json
from pathlib import PurePath
from typing import Any

from openjiuwen.core.foundation.llm import ToolMessage
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.harness.prompts import PromptSection
from openjiuwen.harness.rails.base import DeepAgentRail

from jiuwenswarm.agents.harness.common.memory.forbidden import (
    contains_forbidden_memory_content,
    get_disabled_memory_filter_prompt,
    get_forbidden_memory_prompt,
)
from jiuwenswarm.common.utils import logger


_MEMORY_WRITE_TOOLS = frozenset(
    {
        "write_memory",
        "edit_memory",
        "coding_memory_write",
        "coding_memory_edit",
        "experience_learn",
    }
)

_FILE_WRITE_TOOLS = frozenset(
    {
        "write_file",
        "write_text_file",
        "write",
        "create_file",
        "create",
        "edit_file",
        "edit",
        "search_replace",
        "str_replace",
    }
)

_NEW_CONTENT_KEYS = (
    "content",
    "new_string",
    "new_text",
    "newText",
    "new_content",
    "text",
    "memory",
    "experience",
    "lesson",
)

_MEMORY_FILE_NAMES = frozenset(
    {
        "user.md",
        "identity.md",
        "memory.md",
        "jiuwenswarm.md",
        "jiuwenswarm.local.md",
    }
)

_MEMORY_PATH_PARTS = frozenset({"memory", "coding_memory"})

_DENIAL_MESSAGE = (
    "[SENSITIVE_MEMORY_BLOCKED] 检测到敏感信息，已阻止写入记忆；请删除或脱敏后再保存。"
)

_PROMPT_SECTION_NAME = "memory_forbidden"
_PROMPT_PRIORITY = 113

# agent-core's MemoryRail and CodingMemoryRail both ship a fixed instruction
# that sensitive information must never be saved.  The product switch owns
# that policy, so remove only that clause from the base memory section and let
# ``memory_forbidden`` below add the current on/off rule.  The remaining base
# memory guidance (temporary data, user opt-out, conflicts, etc.) is preserved.
_BASE_SENSITIVE_MEMORY_RULES = (
    ("不要记录敏感信息、", "不要记录"),
    ("- 敏感信息、", "- "),
    ("Do not record sensitive information, ", "Do not record "),
    ("- Sensitive information, ", "- "),
)


def _parse_tool_args(raw_args: Any) -> dict[str, Any]:
    if isinstance(raw_args, dict):
        return raw_args
    if isinstance(raw_args, str):
        try:
            parsed = json.loads(raw_args)
        except (json.JSONDecodeError, TypeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _tool_call_data(ctx: AgentCallbackContext) -> tuple[str, dict[str, Any]]:
    inputs = ctx.inputs
    if isinstance(inputs, dict):
        tool_name = inputs.get("tool_name", "")
        raw_args = inputs.get("tool_args", {})
    else:
        tool_name = getattr(inputs, "tool_name", "")
        raw_args = getattr(inputs, "tool_args", {})
    return str(tool_name or "").lower(), _parse_tool_args(raw_args)


def _is_memory_file_path(raw_path: Any) -> bool:
    if not isinstance(raw_path, (str, PurePath)):
        return False
    normalized = str(raw_path).strip().strip("\"'").replace("\\", "/")
    if not normalized:
        return False
    parts = tuple(
        part.casefold() for part in normalized.split("/") if part not in ("", ".")
    )
    if not parts:
        return False
    if parts[-1] in _MEMORY_FILE_NAMES:
        return True
    # A source package can legitimately be named ``memory``.  Generic file
    # tools are therefore treated as memory writes only for Markdown artifacts;
    # native memory tools are identified independently by tool name.
    return parts[-1].endswith(".md") and bool(_MEMORY_PATH_PARTS.intersection(parts))


def _targets_memory(tool_name: str, tool_args: dict[str, Any]) -> bool:
    if tool_name in _MEMORY_WRITE_TOOLS:
        return True
    if tool_name not in _FILE_WRITE_TOOLS:
        return False
    return any(
        _is_memory_file_path(tool_args.get(key))
        for key in ("file_path", "path", "filename", "target_path")
    )


def _new_content(tool_args: dict[str, Any]) -> tuple[str, ...]:
    values: list[str] = []
    for key in _NEW_CONTENT_KEYS:
        value = tool_args.get(key)
        if isinstance(value, str) and value:
            values.append(value)
    return tuple(values)


def _remove_unconditional_sensitive_memory_rule(builder: Any) -> None:
    """Make the base memory prompt neutral about sensitive information."""
    get_section = getattr(builder, "get_section", None)
    if not callable(get_section):
        return
    section = get_section("memory")
    if section is None:
        return

    original_content = getattr(section, "content", None)
    if not isinstance(original_content, dict):
        return

    updated_content: dict[str, str] = {}
    changed = False
    for language, content in original_content.items():
        updated = content
        if isinstance(updated, str):
            for source, replacement in _BASE_SENSITIVE_MEMORY_RULES:
                updated = updated.replace(source, replacement)
        updated_content[language] = updated
        changed = changed or updated != content

    if changed:
        builder.add_section(
            PromptSection(
                name=getattr(section, "name", "memory"),
                content=updated_content,
                priority=getattr(section, "priority", 100),
            )
        )


class MemoryForbiddenRail(DeepAgentRail):
    """Block only sensitive writes to memory targets while filtering is on."""

    priority: int = 100

    async def before_model_call(self, ctx: AgentCallbackContext) -> None:
        builder = getattr(
            getattr(self, "_deep_agent", None) or ctx.agent,
            "system_prompt_builder",
            None,
        )
        if builder is None:
            return

        _remove_unconditional_sensitive_memory_rule(builder)
        builder.remove_section(_PROMPT_SECTION_NAME)
        language = getattr(builder, "language", "cn") or "cn"
        content = get_forbidden_memory_prompt(language)
        if not content:
            content = get_disabled_memory_filter_prompt(language)
        if not content:
            return
        builder.add_section(
            PromptSection(
                name=_PROMPT_SECTION_NAME,
                content={language: content},
                priority=_PROMPT_PRIORITY,
            )
        )

    async def before_tool_call(self, ctx: AgentCallbackContext) -> None:
        tool_name, tool_args = _tool_call_data(ctx)
        if not _targets_memory(tool_name, tool_args):
            return
        if not any(
            contains_forbidden_memory_content(value)
            for value in _new_content(tool_args)
        ):
            return

        logger.warning(
            "[MemoryForbiddenRail] blocked sensitive memory write: tool=%s",
            tool_name,
        )
        self._reject_tool(ctx)

    @staticmethod
    def _reject_tool(ctx: AgentCallbackContext) -> None:
        inputs = ctx.inputs
        tool_call = (
            inputs.get("tool_call")
            if isinstance(inputs, dict)
            else getattr(inputs, "tool_call", None)
        )
        tool_call_id = getattr(tool_call, "id", "") if tool_call else ""
        ctx.extra["_skip_tool"] = True
        if isinstance(inputs, dict):
            inputs["tool_result"] = _DENIAL_MESSAGE
            inputs["tool_msg"] = ToolMessage(
                content=_DENIAL_MESSAGE, tool_call_id=tool_call_id
            )
        else:
            inputs.tool_result = _DENIAL_MESSAGE
            inputs.tool_msg = ToolMessage(
                content=_DENIAL_MESSAGE, tool_call_id=tool_call_id
            )

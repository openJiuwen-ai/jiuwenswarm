# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Permission suggestion builders for allow-always persistence."""

from __future__ import annotations

import logging
import os
import re
import shlex
from dataclasses import dataclass
from typing import Any

from jiuwenclaw.agentserver.permissions.shell_ast import (
    ShellAstParseResult,
    parse_shell_for_permission,
)
from jiuwenclaw.agentserver.permissions.shell_tools import (
    extract_shell_command,
    is_shell_permission_tool,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PermissionSuggestion:
    tools: tuple[str, ...]
    match_type: str
    pattern: str
    action: str = "allow"
    scope: str = "head"
    reason: str | None = None


def build_permission_suggestions(
    tool_name: str,
    tool_args: dict[str, Any],
    shell_ast_result: ShellAstParseResult | None = None,
    *,
    ask_subcommands: list[str] | None = None,
    existing_patterns: set[str] | None = None,
) -> list[PermissionSuggestion]:
    if not is_shell_permission_tool(tool_name):
        return []
    command = extract_shell_command(tool_args)
    if not command:
        return []
    return build_shell_permission_suggestions(
        tool_name,
        command,
        shell_ast_result=shell_ast_result,
        ask_subcommands=ask_subcommands,
        existing_patterns=existing_patterns,
    )


def build_shell_permission_suggestions(
    tool_name: str,
    command: str,
    *,
    shell_ast_result: ShellAstParseResult | None = None,
    ask_subcommands: list[str] | None = None,
    existing_patterns: set[str] | None = None,
) -> list[PermissionSuggestion]:
    shell_ast_result = shell_ast_result or parse_shell_for_permission(command)
    selected_texts = [text.strip() for text in (ask_subcommands or []) if isinstance(text, str) and text.strip()]

    if selected_texts:
        heads = [_extract_command_head(text) for text in selected_texts]
    elif shell_ast_result.kind == "parse_unavailable":
        return _dedupe_suggestions([
            PermissionSuggestion(
                tools=(tool_name,),
                match_type="command",
                pattern=command.strip(),
                scope="exact",
                reason=shell_ast_result.reason or "parse_unavailable_exact_command",
            )
        ], existing_patterns=existing_patterns)
    else:
        heads = list(shell_ast_result.all_command_heads)
        if not heads and shell_ast_result.kind == "simple":
            heads = [
                _extract_command_head(subcommand.text)
                for subcommand in shell_ast_result.subcommands
                if subcommand.text
            ]
        if not heads and shell_ast_result.kind != "parse_unavailable":
            heads = [_extract_command_head(command)]

    suggestions = [
        PermissionSuggestion(
            tools=(tool_name,),
            match_type="command",
            pattern=f"{head} *",
            reason="command_head",
        )
        for head in heads
        if head
    ]
    return _dedupe_suggestions(suggestions, existing_patterns=existing_patterns)


def _extract_command_head(command: str) -> str:
    text = (command or "").strip()
    if not text:
        return ""
    dynamic_head = _extract_dynamic_command_head(text)
    if dynamic_head:
        return dynamic_head
    try:
        argv = shlex.split(text, posix=(os.name != "nt"))
    except ValueError:
        argv = text.split()
    return str(argv[0]).strip() if argv else ""


def _extract_dynamic_command_head(text: str) -> str:
    if text.startswith("$("):
        depth = 0
        for idx, ch in enumerate(text):
            if ch == "$" and idx + 1 < len(text) and text[idx + 1] == "(":
                depth += 1
            elif ch == ")" and depth:
                depth -= 1
                if depth == 0:
                    return text[:idx + 1].strip()
        return ""
    if text.startswith("${"):
        end = text.find("}")
        return text[:end + 1].strip() if end > 2 else ""
    if text.startswith("$"):
        match = re.match(r"^\$[A-Za-z_][A-Za-z0-9_]*", text)
        return match.group(0) if match else ""
    if text.startswith("%"):
        match = re.match(r"^%[^%\s]+%", text)
        return match.group(0) if match else ""
    return ""


def _dedupe_suggestions(
    suggestions: list[PermissionSuggestion],
    *,
    existing_patterns: set[str] | None = None,
) -> list[PermissionSuggestion]:
    existing_patterns = existing_patterns or set()
    seen: set[tuple[tuple[str, ...], str, str, str]] = set()
    result: list[PermissionSuggestion] = []
    for suggestion in suggestions:
        if suggestion.pattern in existing_patterns:
            continue
        signature = (
            suggestion.tools,
            suggestion.match_type,
            suggestion.pattern,
            suggestion.action,
        )
        if signature in seen:
            continue
        seen.add(signature)
        result.append(suggestion)
    return result

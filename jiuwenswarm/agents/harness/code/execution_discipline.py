# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Pure heuristics for keeping Code mode on an implementation path.

The policy is adapted from iCode's implementation gate, but deliberately has
no dependency on iCode's SessionHost, EventBus, or mutation store.
"""

from __future__ import annotations

import json
import re
from typing import Any

MUTATING_TOOLS = frozenset(
    {"edit_file", "write_file", "Edit", "Write", "edit", "write"}
)
EXPLORE_TOOLS = frozenset(
    {
        "grep",
        "glob",
        "read_file",
        "list_dir",
        "list_files",
        "todo_create",
        "todo_get",
        "todo_list",
        "todo_modify",
        "memory_search",
    }
)
SHELL_TOOLS = frozenset({"bash", "shell", "execute_cmd", "shell.execute_cmd"})

_IMPLEMENT_HINTS = (
    "implement",
    "fix ",
    "fix the",
    "add ",
    "edit ",
    "modify ",
    "refactor",
    "patch",
    "create ",
    "update ",
    "change the code",
    "修改",
    "修复",
    "实现",
    "新增",
    "重构",
    "写代码",
)
_VERIFY_PATTERNS = (
    r"\bpytest\b",
    r"\bpython(?:3(?:\.\d+)?)?\s+-m\s+(?:pytest|unittest)\b",
    r"\bnpm\s+(?:run\s+)?(?:test|build|lint)\b",
    r"\b(?:pnpm|yarn)\s+(?:run\s+)?(?:test|build|lint)\b",
    r"\b(?:jest|vitest|mocha|ava|deno\s+test)\b",
    r"\bgo\s+(?:test|build|vet)\b",
    r"\bcargo\s+(?:test|check|clippy|build)\b",
    r"\b(?:mvn|gradle)\s+test\b",
    r"\bmake(?:\s|$)",
    r"\btsc(?:\s|$)",
    r"\bmypy\b",
    r"\bruff\s+check\b",
)
_MUTATION_FAILURE_MARKERS = (
    "error applying patch",
    "could not find",
    "no such file or directory",
    "old_string not found",
    "failed to edit",
    "permission denied",
    "not unique",
    "multiple matches",
    "string not found",
)


def looks_like_implementation_task(text: str) -> bool:
    """Return whether a user message requests a concrete code change."""

    lower = str(text or "").strip().lower()
    return bool(lower) and any(hint in lower for hint in _IMPLEMENT_HINTS)


def extract_shell_command(tool_args: Any) -> str:
    """Best-effort extraction of a command from a shell tool payload."""

    args = _as_mapping(tool_args)
    if args is None:
        return str(tool_args or "").strip() if isinstance(tool_args, str) else ""
    for key in ("command", "cmd", "script"):
        value = args.get(key)
        if value:
            return str(value).strip()
    return ""


def looks_like_verification_command(command: str) -> bool:
    """Return whether a shell command runs a build, test, lint, or type check."""

    subject = re.split(r"\s(?:\||&&|;)\s", str(command or ""), maxsplit=1)[0]
    return any(
        re.search(pattern, subject, re.IGNORECASE) for pattern in _VERIFY_PATTERNS
    )


def tool_result_payload(tool_result: Any) -> tuple[Any, bool | None]:
    """Extract result content and an explicit success flag."""

    if tool_result is None:
        return "", None
    success = getattr(tool_result, "success", None)
    if success is None and isinstance(tool_result, dict):
        success = tool_result.get("success")
    content = getattr(tool_result, "content", None)
    if content is None and isinstance(tool_result, dict):
        content = tool_result.get("content")
    if content is None:
        content = tool_result
    return content, success if success is None else bool(success)


def mutation_succeeded(
    tool_name: str,
    tool_result: Any,
    *,
    tool_success: bool | None = None,
) -> bool:
    """Conservatively decide whether a mutating tool changed the worktree."""

    if tool_name not in MUTATING_TOOLS or tool_success is False:
        return False
    text = str(tool_result or "")
    if any(marker in text.lower() for marker in _MUTATION_FAILURE_MARKERS):
        return False
    return bool(tool_success is True or text.strip())


def shell_result_succeeded(
    result: Any,
    *,
    tool_success: bool | None = None,
) -> bool | None:
    """Return a shell exit outcome, or ``None`` when it is not yet known."""

    text = str(result or "")
    if tool_success is not None:
        return bool(tool_success)
    match = re.search(r"(?:Exit Code|exit_code)\s*[:=]\s*(\d+)", text, re.IGNORECASE)
    if match:
        return int(match.group(1)) == 0
    return None


def primary_user_text(ctx: Any) -> str:
    """Read the first substantive user message from a callback context."""

    messages = getattr(getattr(ctx, "inputs", None), "messages", None) or []
    fallback = ""
    for message in messages:
        role = getattr(message, "role", None)
        if role is None and isinstance(message, dict):
            role = message.get("role")
        if role != "user":
            continue
        content = getattr(message, "content", None)
        if content is None and isinstance(message, dict):
            content = message.get("content")
        text = _content_text(content)
        if text and not fallback:
            fallback = text
        if looks_like_implementation_task(text):
            return text
    return fallback


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                value = item.get("text") or item.get("content")
                if value:
                    parts.append(str(value))
            else:
                value = getattr(item, "text", None) or getattr(item, "content", None)
                if value:
                    parts.append(str(value))
        return "\n".join(parts).strip()
    return str(content or "").strip()


def _as_mapping(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


__all__ = [
    "EXPLORE_TOOLS",
    "MUTATING_TOOLS",
    "SHELL_TOOLS",
    "extract_shell_command",
    "looks_like_implementation_task",
    "looks_like_verification_command",
    "mutation_succeeded",
    "primary_user_text",
    "shell_result_succeeded",
    "tool_result_payload",
]

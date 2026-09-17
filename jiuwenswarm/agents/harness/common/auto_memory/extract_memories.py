# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Extract memories from conversation after it ends.

This module provides utility functions for memory extraction:
- scan_memory_files: Scan existing memory files in project memory directory
- _check_coding_memory_write_in_history: Mutex detection for coding_memory_write
- _convert_messages_to_base_messages: Message conversion helper

The main extraction logic is in extraction_runner.py.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# Regex patterns for memory file frontmatter
_FRONTMATTER_PATTERN = re.compile(
    r"^---\s*\n"
    r"name:\s*(.+)\s*\n"
    r"description:\s*(.+)\s*\n"
    r"type:\s*(.+)\s*\n"
    r"---\s*\n",
    re.MULTILINE,
)


def scan_memory_files(memory_dir: Path) -> list[dict[str, Any]]:
    """Scan existing memory files in the memory directory.

    Args:
        memory_dir: Path to the project memory directory.

    Returns:
        List of memory file metadata (name, description, type, path).
    """
    if not memory_dir.exists():
        return []

    memories: list[dict[str, Any]] = []

    for md_file in memory_dir.glob("*.md"):
        if md_file.name == "MEMORY.md":
            continue  # Skip index file

        try:
            content = md_file.read_text(encoding="utf-8")
            match = _FRONTMATTER_PATTERN.match(content)
            if match:
                memories.append({
                    "name": match.group(1).strip(),
                    "description": match.group(2).strip(),
                    "type": match.group(3).strip(),
                    "path": str(md_file),
                    "file_name": md_file.name,
                })
        except Exception as e:
            logger.warning(f"[auto_memory] Failed to read memory file {md_file}: {e}")

    return memories


def _check_coding_memory_write_in_history(history_list: list) -> bool:
    """Check if coding_memory_write tool was actually called by assistant.

    Only checks tool_calls field, not message content, to avoid false positives
    when user mentions the tool name in their message.

    Args:
        history_list: List of message dicts with role/content/tool_calls fields.

    Returns:
        True if coding_memory_write was called, False otherwise.
    """
    for msg in history_list:
        # Only check assistant messages with actual tool_calls
        role = msg.get("role", "")
        if role != "assistant":
            continue
        tool_calls = msg.get("tool_calls", [])
        for tc in tool_calls:
            if isinstance(tc, dict):
                func_name = tc.get("function", {}).get("name", "")
                if func_name == "coding_memory_write":
                    return True
    return False


def _convert_messages_to_base_messages(messages: list[dict[str, Any] | Any]) -> list[Any]:
    """Convert dict messages to BaseMessage objects.

    Handles dict messages with role/content fields, BaseMessage-like objects,
    and unknown formats (converted to UserMessage with string content).

    Args:
        messages: List of messages (dict or BaseMessage-like objects).

    Returns:
        List of BaseMessage objects.
    """
    from openjiuwen.core.foundation.llm.schema.message import (
        UserMessage, AssistantMessage, SystemMessage, ToolMessage
    )

    converted = []
    for msg in messages:
        if isinstance(msg, dict):
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if role == "user":
                converted.append(UserMessage(content=str(content)))
            elif role == "assistant":
                converted.append(AssistantMessage(content=str(content)))
            elif role == "system":
                converted.append(SystemMessage(content=str(content)))
            elif role == "tool":
                tool_call_id = msg.get("tool_call_id", "")
                converted.append(ToolMessage(content=str(content), tool_call_id=tool_call_id))
            else:
                # Fallback to user message
                converted.append(UserMessage(content=str(content)))
        elif hasattr(msg, "role") and hasattr(msg, "content"):
            # Already a BaseMessage-like object
            converted.append(msg)
        else:
            # Unknown format, convert to string
            converted.append(UserMessage(content=str(msg)))

    return converted



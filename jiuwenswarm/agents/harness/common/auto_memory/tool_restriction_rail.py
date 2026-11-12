# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""AutoMemoryToolRestrictionRail - restrict tool calls for extract_memories subagent.

Similar to Claude Code's canUseTool mechanism:
- Allows Read/Grep/Glob tools (unrestricted)
- Allows Bash read-only commands (ls, cat, git status, etc.)
- Allows Write/Edit only in memory_dir
- Denies other tools by setting ctx.extra["_skip_tool"] = True

This enables cache sharing: the subagent inherits parent's tools,
but runtime restriction prevents unauthorized tool use.
"""

from __future__ import annotations

import logging
import re
import shlex
from pathlib import Path
from typing import Any, Dict

from openjiuwen.core.single_agent.rail.base import AgentRail, AgentCallbackContext

logger = logging.getLogger(__name__)


# Tool names that are allowed without restrictions
UNRESTRICTED_READ_TOOLS = (
    "read_file",
    "read",
    "grep",
    "glob",
    "list_files",
    "search",
)

# Bash commands that are read-only (allowed)
READ_ONLY_BASH_COMMANDS = (
    "ls",
    "cat",
    "head",
    "tail",
    "grep",
    "find",
    "git status",
    "git log",
    "git diff",
    "git show",
    "pwd",
    "echo",
    "wc",
    "stat",
)

# Shell syntax that turns an otherwise read-only command into a writer: a
# separator chains a second command, and the redirect/substitution tokens let
# the shell itself create or overwrite files. A bare ``$`` is rejected as well:
# ``shlex.split`` performs no expansion, so ``${IFS}`` survives as a single word
# and the shell would split it into a write flag after this check passed.
_SHELL_SEGMENT_SEPARATOR_RE = re.compile(r"\|\||&&|\||;")
_SHELL_UNSAFE_TOKENS = (">", "<", "`", "$", "&", "\n", "\r")
# `find` is read-only by name but not by flag: these turn it into a writer.
_FIND_WRITE_ARGS = frozenset(
    {"-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fprint0", "-fprintf", "-fls"}
)


def _is_read_only_shell_segment(segment: str) -> bool:
    """Return whether a single shell segment is a read-only invocation.

    Args:
        segment: One command from a chain such as ``cat a && b``.

    Returns:
        True only if the segment neither redirects nor runs a non-allowlisted
        command (and, for ``find``, carries no write flag).
    """
    if any(token in segment for token in _SHELL_UNSAFE_TOKENS):
        return False
    try:
        words = shlex.split(segment)
    except ValueError:
        return False
    if not words:
        return False
    command = words[0].lower()
    if command == "git":
        if " ".join(words[:2]).lower() not in READ_ONLY_BASH_COMMANDS:
            return False
        return not any(word == "--output" or word.startswith("--output=") for word in words)
    if command not in READ_ONLY_BASH_COMMANDS:
        return False
    return command != "find" or _FIND_WRITE_ARGS.isdisjoint(words)


class AutoMemoryToolRestrictionRail(AgentRail):
    """Restrict tool calls for extract_memories subagent.

    This Rail implements the canUseTool-like mechanism for jiuwenswarm:
    - Checks tool_name and tool_args before each tool execution
    - Sets ctx.extra["_skip_tool"] = True to deny unauthorized tool calls
    - Sets ctx.inputs.tool_result with denial message

    This enables API cache sharing because:
    - The subagent inherits parent's system_prompt and tools
    - Tools are restricted at runtime, not by creating different tool definitions
    - All cache key components (system_prompt, tools, model, messages prefix) match

    Usage:
        restriction_rail = AutoMemoryToolRestrictionRail(memory_dir="/path/to/memory")
        sub_agent = create_deep_agent(
            ...,
            rails=[restriction_rail],
        )

    Attributes:
        memory_dir: Path to the memory directory (allowed write location)
        priority: Rail execution priority (higher runs first)
    """

    priority: int = 100  # High priority to run before other rails

    def __init__(self, memory_dir: str):
        """Initialize the tool restriction rail.

        Args:
            memory_dir: Path to the memory directory where Write/Edit are allowed.
        """
        self.memory_dir = Path(memory_dir).resolve()
        logger.debug(
            "[auto_memory] AutoMemoryToolRestrictionRail initialized with memory_dir: %s",
            self.memory_dir,
        )

    async def before_tool_call(self, ctx: AgentCallbackContext) -> None:
        """Check tool permission before execution.

        This is the core canUseTool-like check:
        1. Get tool_name and tool_args from ctx.inputs
        2. Check against allowed patterns
        3. Set ctx.extra["_skip_tool"] = True if denied

        Args:
            ctx: AgentCallbackContext containing tool call information.
        """
        # Get tool call info from ctx.inputs
        tool_inputs = ctx.inputs
        if not isinstance(tool_inputs, dict):
            # Should be ToolCallInputs, but access as dict-like
            tool_name = getattr(tool_inputs, "tool_name", "")
            tool_args = getattr(tool_inputs, "tool_args", {}) or {}
        else:
            tool_name = tool_inputs.get("tool_name", "")
            tool_args = tool_inputs.get("tool_args", {}) or {}

        # Ensure tool_args is a dict (sometimes it's a JSON string from LLM output)
        if not isinstance(tool_args, dict):
            if isinstance(tool_args, str):
                # Try to parse JSON string to dict
                try:
                    import json
                    tool_args = json.loads(tool_args)
                    logger.debug(
                        "[auto_memory] Successfully parsed tool_args JSON string for tool '%s'",
                        tool_name,
                    )
                except json.JSONDecodeError as e:
                    logger.warning(
                        f"[auto_memory] Failed to parse tool_args JSON string for tool '{tool_name}': {e}"
                    )
                    tool_args = {}
            else:
                logger.warning(
                    f"[auto_memory] tool_args is not dict or string (type={type(tool_args).__name__}), "
                    f"converting to empty dict for tool '{tool_name}'"
                )
                tool_args = {}

        # Log the check
        logger.debug(
            "[auto_memory] AutoMemoryToolRestrictionRail checking tool: %s",
            tool_name,
        )

        # 1. Check unrestricted read tools
        if self._is_unrestricted_read_tool(tool_name):
            logger.debug("[auto_memory] Tool '%s' allowed (unrestricted read)", tool_name)
            return  # Allow execution

        # 2. Check Bash read-only commands
        if tool_name in ("bash", "execute_bash", "run_command", "shell"):
            if self._is_read_only_bash_command(tool_args):
                logger.debug(
                    "[auto_memory] Bash command allowed (read-only): %s",
                    tool_args.get("command", "")[:50],
                )
                return  # Allow read-only bash

        # 3. Check Write/Edit in memory_dir
        if tool_name in ("write_file", "write", "edit_file", "edit"):
            if self._is_in_memory_dir(tool_args):
                logger.debug(
                    "[auto_memory] Write/Edit allowed in memory_dir: %s",
                    tool_args.get("file_path", ""),
                )
                return  # Allow write in memory_dir

        # 4. Check coding_memory_write/edit (allowed for vector indexing)
        if tool_name in ("coding_memory_write", "coding_memory_edit"):
            if self._is_in_memory_dir(tool_args):
                logger.debug(
                    "[auto_memory] coding_memory tool allowed: %s",
                    tool_name,
                )
                return  # Allow coding memory tools in memory_dir

        # 4.5. Check coding_memory_read (always allowed - read-only operation)
        if tool_name in ("coding_memory_read", "ltm_search", "ltm_search_summary"):
            logger.debug(
                "[auto_memory] Read-only memory tool allowed: %s",
                tool_name,
            )
            return  # Allow read-only memory tools

        # 5. Deny all other tools
        logger.warning(
            f"[auto_memory] Tool '{tool_name}' DENIED by AutoMemoryToolRestrictionRail"
        )
        self._deny_tool(ctx, tool_name, tool_args)

    def _is_unrestricted_read_tool(self, tool_name: str) -> bool:
        """Check if tool is an unrestricted read tool.

        Args:
            tool_name: Name of the tool.

        Returns:
            True if the tool is allowed without restrictions.
        """
        return tool_name.lower() in (t.lower() for t in UNRESTRICTED_READ_TOOLS)

    def _is_read_only_bash_command(self, tool_args: Dict[str, Any]) -> bool:
        """Check if bash command is read-only.

        Args:
            tool_args: Tool arguments containing 'command' field.

        Returns:
            True if the command is read-only (no file modification).
        """
        # Every segment of the chain must be read-only on its own. Matching the
        # allowlist as a substring of the whole command let ``rm -rf x && ls``
        # through on the strength of the trailing ``ls``, and a bare ``--read``
        # anywhere allowed anything at all.
        command = str(tool_args.get("command") or "").strip()
        segments = [
            segment for segment in _SHELL_SEGMENT_SEPARATOR_RE.split(command) if segment.strip()
        ]
        return bool(segments) and all(_is_read_only_shell_segment(segment) for segment in segments)

    def _is_in_memory_dir(self, tool_args: Dict[str, Any]) -> bool:
        """Check if the target path is within memory_dir.

        Args:
            tool_args: Tool arguments containing 'file_path' or 'path' field.

        Returns:
            True if the path is within memory_dir.
        """
        # Get the file path from tool_args
        path = tool_args.get("file_path", "")
        if not path:
            # Also check 'path' field (coding_memory_write uses this)
            path = tool_args.get("path", "")

        if not path:
            return False

        try:
            # Handle relative paths (coding_memory_write uses relative paths)
            if Path(path).is_absolute():
                target_path = Path(path).resolve()
            else:
                # Relative path: resolve relative to memory_dir
                target_path = (self.memory_dir / path).resolve()

            # Check if target is within memory_dir
            # Use relative_to() for more robust check
            try:
                target_path.relative_to(self.memory_dir)
                logger.debug(
                    "[auto_memory] Path '%s' resolved to '%s' is within memory_dir",
                    path, target_path,
                )
                return True
            except ValueError:
                # relative_to() raises ValueError if not a subpath
                logger.debug(
                    "[auto_memory] Path '%s' resolved to '%s' is NOT within memory_dir",
                    path, target_path,
                )
                return False
        except Exception as e:
            logger.warning(
                f"[auto_memory] Failed to resolve path '{path}': {e}"
            )
            return False

    def _deny_tool(
        self,
        ctx: AgentCallbackContext,
        tool_name: str,
        tool_args: Dict[str, Any],
    ) -> None:
        """Deny the tool execution by setting _skip_tool.

        This is the key mechanism for runtime tool restriction:
        - Set ctx.extra["_skip_tool"] = True to skip execution
        - Set ctx.inputs.tool_result with denial message
        - Set ctx.inputs.tool_msg with tool denial message

        Args:
            ctx: AgentCallbackContext to modify.
            tool_name: Name of the denied tool.
            tool_args: Arguments of the denied tool.
        """
        # Set the skip flag
        ctx.extra["_skip_tool"] = True

        # Build the denial message
        denial_msg = (
            f"Tool '{tool_name}' is not allowed in extract_memories subagent.\n"
            f"Allowed tools: Read/Grep/Glob (unrestricted), Bash (read-only), "
            f"Write/Edit (only in memory_dir: {self.memory_dir}).\n"
            f"Your call with args {tool_args} was denied."
        )

        # Set the tool_result (for the return value)
        # Handle both ToolCallInputs dataclass and dict inputs
        if hasattr(ctx.inputs, "tool_result"):
            ctx.inputs.tool_result = {
                "success": False,
                "error": denial_msg,
            }
        elif isinstance(ctx.inputs, dict):
            ctx.inputs["tool_result"] = {
                "success": False,
                "error": denial_msg,
            }

        # Set the tool_msg (for the LLM to see)
        # Get tool_call_id from ctx.inputs.tool_call
        tool_call_id = ""
        if hasattr(ctx.inputs, "tool_call"):
            tool_call_obj = ctx.inputs.tool_call
            if hasattr(tool_call_obj, "id"):
                tool_call_id = tool_call_obj.id or ""
            elif isinstance(tool_call_obj, dict):
                tool_call_id = tool_call_obj.get("id", "")

        if hasattr(ctx.inputs, "tool_msg"):
            # Import ToolMessage if available
            try:
                from openjiuwen.core.foundation.llm.schema.message import ToolMessage
                ctx.inputs.tool_msg = ToolMessage(
                    content=denial_msg,
                    tool_call_id=tool_call_id,
                )
            except ImportError:
                # Fallback: use dict-like structure
                ctx.inputs.tool_msg = {
                    "role": "tool",
                    "content": denial_msg,
                    "tool_call_id": tool_call_id,
                }
        elif isinstance(ctx.inputs, dict):
            ctx.inputs["tool_msg"] = {
                "role": "tool",
                "content": denial_msg,
                "tool_call_id": tool_call_id,
            }



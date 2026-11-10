# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""AutoMemoryToolRestrictionRail - restrict tool calls for extract_memories subagent.

Similar to Claude Code's canUseTool mechanism:
- Allows Read/Grep/Glob tools (unrestricted)
- Allows Bash only when every shell segment is a whitelisted read-only
  command (see ``READ_ONLY_BASH_COMMANDS``); a command whose options can write
  or execute is either left out of that whitelist (``file``) or filtered by
  option prefix (``git``, ``find``). Prefix filtering only keeps the explicit
  flags out - it does not make those commands exec-free, because repository
  configuration can still run programs (see the residual list in
  ``_is_read_only_bash_command``). Anything else is denied
- Allows Write/Edit only in memory_dir
- Denies other tools by setting ctx.extra["_skip_tool"] = True

This enables cache sharing: the subagent inherits parent's tools,
but runtime restriction prevents unauthorized tool use.
"""

from __future__ import annotations

import logging
import re
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

# Bash commands whose ordinary invocations only read (or print). A command
# whose options can write or execute is handled separately: either it is not
# whitelisted at all (``file``), or ``_is_read_only_segment`` filters its
# options (``git``, ``find``). Filtering options does not close the command's
# config-driven execution paths - the list of those still open lives in
# ``_is_read_only_bash_command``.
#
# ``file`` is absent because of its option space, not its command name:
# ``file -C``/``--compile`` compiles a magic database and writes
# ``<source>.mgc``. A prefix blacklist cannot cover that - short options
# cluster (``file -bC -m src``) and long options abbreviate
# (``file --compil -m src``), and both still write the compiled file with
# file 5.41. The extraction sub-agent keeps ``read_file``/``grep``/``glob``/
# ``list_files``/``search``, so it loses nothing it needs here.
READ_ONLY_BASH_COMMANDS = (
    "ls",
    "cat",
    "head",
    "tail",
    "grep",
    "find",
    "git",
    "pwd",
    "echo",
    "wc",
    "stat",
)

# Subcommands allowed right after the ``git`` command name.
READ_ONLY_GIT_SUBCOMMANDS = ("status", "log", "diff", "show")

# Shell operators that separate commands. Only an unquoted, unescaped run of
# control characters acts as an operator, and every run other than these
# (``|&``, ``&>>``, ``>&``, ``<>``, ``>|``, ``(`` ...) is rejected, so redirects
# and subshells cannot ride along with a read command.
_COMMAND_SEPARATORS = frozenset({";", "&&", "||", "|", "&"})
_CONTROL_CHARS = frozenset(";&|<>()")
_TOKEN_WHITESPACE = frozenset({" ", "\t"})

# Options that turn an otherwise read-only command into a writer or an executor.
# A long option may be abbreviated to any unambiguous prefix, so a token that is
# a *prefix* of one of these names reaches the option as well - see
# ``_is_forbidden_option``.
_FORBIDDEN_OPTION_PREFIXES = ("--output", "--ext-diff")
# Abbreviations are deliberately not expanded for these: ``--text`` is a real,
# harmless diff option and a prefix of ``--textconv``, so matching backwards
# would deny it. ``--textconv`` itself is only a defensive entry next to
# ``--ext-diff``: git runs the textconv filter configured for the repository by
# default in ``diff``/``log``/``show``, and only an explicit ``--no-textconv``
# turns that off, which this gate cannot inject - so rejecting the flag does not
# make the command exec-free.
_FORBIDDEN_OPTION_NAMES_ONLY = ("--textconv",)
# Harmless options that sit in the prefix shadow of ``--output`` and would be
# caught by the abbreviation match: ``--output-indicator-new`` and its siblings
# only change the marker characters of a patch - they write and execute nothing.
_ALLOWED_OPTION_PREFIXES = ("--output-indicator",)
_FIND_FORBIDDEN_OPTION_PREFIXES = ("-delete", "-exec", "-ok", "-fprint", "-fls")

# Leading ``NAME=value`` prefix: ``PATH=/tmp ls`` runs a different binary.
_ENV_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def _split_command_segments(command: str) -> list[list[str]] | None:
    """Split a shell command into per-command token segments.

    Quoting and escaping are read the way a POSIX shell reads them, and they
    only decide whether a character is literal - they never end a token: the
    shell hands ``--out''put=x`` to the program as one argument ``--output=x``,
    so the scanner has to see it as one token too. Only unquoted whitespace
    separates tokens, and only unquoted control characters act as operators.

    Args:
        command: Raw bash command string (already stripped, single line).

    Returns:
        One token list per command, or None when the command cannot be
        tokenized, uses a shell operator that is not a plain separator, does
        command substitution, or contains an unquoted pattern character (brace
        expansion ``--{output,}=/tmp/x`` and pathname expansion ``git diff *``
        both splice a forbidden option name together at the shell level, and
        the scanner cannot see the result).
    """
    segments: list[list[str]] = []
    current: list[str] = []
    token: list[str] = []
    started = False
    index = 0
    length = len(command)

    def flush_token() -> None:
        nonlocal token, started
        if started:
            current.append("".join(token))
            token = []
            started = False

    while index < length:
        char = command[index]
        if char == "'":
            started = True
            end = command.find("'", index + 1)
            if end < 0:
                return None
            token.extend(command[index + 1:end])
            index = end + 1
            continue
        if char == '"':
            started = True
            index += 1
            while True:
                if index >= length:
                    return None
                inner = command[index]
                if inner == '"':
                    index += 1
                    break
                if inner == "\\" and index + 1 < length and command[index + 1] in '"\\`$':
                    token.append(command[index + 1])
                    index += 2
                    continue
                if inner in "$`":
                    return None
                token.append(inner)
                index += 1
            continue
        if char == "\\":
            if index + 1 >= length:
                return None
            token.append(command[index + 1])
            started = True
            index += 2
            continue
        if char in "$`":
            return None
        # Brace and pathname expansion both splice words together at the shell
        # level: ``--{output,}=x`` becomes ``--output=x``, and ``git diff *`` in
        # a directory holding ``--output=zz`` passes that name to git as an
        # option. Neither is visible in the token the scanner sees.
        if char in "{}*?[":
            return None
        if char in _CONTROL_CHARS:
            run_end = index
            while run_end < length and command[run_end] in _CONTROL_CHARS:
                run_end += 1
            if command[index:run_end] not in _COMMAND_SEPARATORS:
                return None
            flush_token()
            if current:
                segments.append(current)
                current = []
            index = run_end
            continue
        if char in _TOKEN_WHITESPACE:
            flush_token()
            index += 1
            continue
        token.append(char)
        started = True
        index += 1

    flush_token()
    if current:
        segments.append(current)
    return segments or None


def _segment_command_name(tokens: list[str]) -> str:
    """Return the lower-cased executable name of a segment.

    ``_is_read_only_segment`` rejects a path-qualified head before calling this,
    and the scanner has already stripped quoting, so the head is the bare
    command name; only the Windows ``.exe`` suffix is normalized away.
    """
    name = tokens[0].lower()
    return name[:-4] if name.endswith(".exe") else name


def _is_forbidden_option(token: str) -> bool:
    """Check whether a token names a writer or executor option.

    A long option can be abbreviated to any unambiguous prefix, so a token that
    is itself a prefix of a forbidden name reaches that option too. An attached
    value (``--outpu=/tmp/x``) is cut off before the comparison. The ``--``
    option terminator is too short to abbreviate and is never matched.

    Args:
        token: One argument token of a command segment.

    Returns:
        True when the token selects a writer or executor option.
    """
    name = token.split("=", 1)[0]
    if len(name) <= 2:
        return False
    if name.startswith(_ALLOWED_OPTION_PREFIXES):
        return False
    if name.startswith(_FORBIDDEN_OPTION_NAMES_ONLY):
        return True
    return any(
        name.startswith(prefix) or prefix.startswith(name)
        for prefix in _FORBIDDEN_OPTION_PREFIXES
    )


def _is_read_only_segment(tokens: list[str]) -> bool:
    """Check whether a single shell segment only reads.

    Args:
        tokens: Tokens of one command segment.

    Returns:
        True when the segment is a whitelisted read-only command.
    """
    if not tokens:
        return False
    head = tokens[0]
    if _ENV_ASSIGNMENT_RE.match(head):
        return False
    if "/" in head or "\\" in head:
        return False
    if any(_is_forbidden_option(token) for token in tokens[1:]):
        return False

    name = _segment_command_name(tokens)
    if name == "git":
        return len(tokens) > 1 and tokens[1].lower() in READ_ONLY_GIT_SUBCOMMANDS
    if name == "find":
        if any(token.startswith(_FIND_FORBIDDEN_OPTION_PREFIXES) for token in tokens[1:]):
            return False
    return name in READ_ONLY_BASH_COMMANDS


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
        """Check whether a bash command only reads.

        The command is split on shell separators and *every* segment has to be
        a whitelisted read-only command invoked by its bare name. Anything the
        rail cannot prove read-only is denied: multi-line commands, redirects
        and other control operators, command substitution (``$``/backtick),
        ``NAME=value`` prefixes, path-qualified command names, ``git`` global
        options and ``find`` write/exec actions. ``file`` is not whitelisted at
        all: ``-C``/``--compile`` writes a compiled magic database, and its
        options can be clustered or abbreviated, so no prefix list covers them.

        Splitting is quoting-aware: the quotes of ``'...'``/``"..."`` and
        backslash escapes only make characters literal (an operator, ``$`` or
        backtick inside single quotes is plain text), and they never end a
        token - ``--out''put=x`` is one argument ``--output=x`` to the shell.
        Only unquoted whitespace separates tokens. Unquoted pattern characters
        (``{``/``}``/``*``/``?``/``[``) are denied outright: brace and pathname
        expansion both splice words together at the shell level, so
        ``git diff --{output,}=/tmp/x`` reaches ``--output`` and ``git diff *``
        in a directory holding a file named ``--output=zz`` passes that name as
        an option, while the gate only sees a token matching no option name.

        Options are matched against the forbidden names in both directions,
        because a long option may be abbreviated to any unambiguous prefix.
        That guard does not depend on which parser a ``git`` sub-command uses:
        ``diff``/``log``/``show`` keep their own option tables and do not
        abbreviate (measured on git 2.50.1), while ``status`` (parse-options)
        does accept ``--porc`` - but its option space has no writer or executor.
        The ``--output-indicator-*`` family is harmless (it only changes patch
        marker characters) and is allowed before that comparison runs.

        Some execution paths of the whitelisted ``git`` sub-commands come from
        repository configuration rather than from the command line, so option
        filtering cannot close them and they stay open:

        - ``diff.external`` and ``diff.<driver>.command`` (a driver selected by
          ``.gitattributes``): ``git diff`` runs them with no option of its own.
          ``log -p``/``show`` do not - they only do it under ``--ext-diff``,
          which is rejected above (git 2.50.1, measured);
        - the textconv filter, on by default for ``diff``/``log``/``show`` and
          only disabled by an explicit ``--no-textconv`` that this gate cannot
          inject;
        - ``core.fsmonitor``, run by ``status`` and by ``diff`` once the
          worktree is dirty.

        Args:
            tool_args: Tool arguments containing 'command' field.

        Returns:
            True if the whole command is read-only (no file modification).
        """
        command = tool_args.get("command", "")
        if not isinstance(command, str):
            return False
        command = command.strip()
        if not command or "\n" in command or "\r" in command:
            return False

        segments = _split_command_segments(command)
        if not segments:
            return False
        return all(_is_read_only_segment(segment) for segment in segments)

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



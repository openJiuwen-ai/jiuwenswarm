# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Execution-time guard for co-scribe's own files against generic file tools.

The mandate machinery is code standing at the cloud-document boundary; these
files are the machinery itself -- the receipts ledger, the watch registry and
its audit journal, the dedup state, the credential keys, and the working-style
file. A generic file tool can reach them like any path on the machine, and the
model has done so (observed live: the working-style file edited with the
generic editor, no ledger entry, a receipt number invented in the reply; and
the state file's earlier relocation out of the agent workspace exists for the
same reason). Enforcement must therefore stand at the host's tool boundary,
which is this rail: writes to these files through generic tools are refused
with the road back -- the working-style file has its own fenced tool, and the
ledgers are written only by the machinery they audit. Reads stay allowed; the
files are the owner's to inspect.

The same argument reaches one boundary further, and the gap was live. The
machinery's files were guarded while **the cloud document itself** was not:
reached through the toolkit a write is asked about, bounded by the range rail,
read back and written to the ledger, but the platform's own CLI is an ordinary
program on this machine, so a shell tool reaches the identical API with the
identical credentials and none of that. Observed 2026-09-10, unprompted: a
toolkit read failed for want of a scope, the model announced "I'll see whether
the local lark-cli can create a sheet", and ran ``lark-cli sheets +sheet-create``
against the owner's spreadsheet -- while the shell tool sat on a session-wide
allow, so nothing asked and the ledger recorded nothing. A guard that stops at
the ledger while the resource it audits stays reachable is not a boundary.

Shell invocations of a cloud-document CLI are therefore refused unless they are
plainly reads. Fail-closed, the same direction as everywhere else here: an
unrecognised verb refuses rather than passes, because the cost of a wrong
refusal is a sentence naming the tool to use instead, and the cost of a wrong
pass is a change to someone's document that no receipt records.
"""

from __future__ import annotations

import functools
import os
import re
import shlex
from pathlib import PurePath
from typing import Any

from openjiuwen.core.foundation.llm import ToolMessage
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.harness.rails.base import DeepAgentRail

from jiuwenswarm.common.utils import logger

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
        "delete_file",
        "remove_file",
    }
)

# The tools a shell command reaches the machine through. The two the host actually
# registers -- ``mcp_exec_command`` (the cross-platform command tool) and
# ``create_terminal`` (the ACP terminal) -- are named here, and the host's own
# shell-capability classification is unioned in at first use so a tool the host
# adds later is guarded the day it appears rather than the day someone notices.
_SHELL_TOOLS = frozenset(
    {
        "bash",
        "cmd",
        "powershell",
        "shell",
        "run_command",
        "execute_cmd",
        "execute_cmd_stream",
        "execute_cmd_background",
        "terminal",
        "mcp_exec_command",
        "create_terminal",
    }
)


@functools.lru_cache(maxsize=1)
def _shell_tools() -> frozenset[str]:
    """This rail's list, unioned with the host's permission-layer classification.

    Imported lazily: the permission package is a sibling and the rail must not make
    the harness's import order depend on it. Failing to import keeps the local list,
    which already names every tool the host registers today.
    """
    names = set(_SHELL_TOOLS)
    try:
        from jiuwenswarm.agents.harness.common.rails.permissions.tool_capabilities import (
            _SHELL_TOOLS as host_shell_tools,
        )

        names.update(str(n).lower() for n in host_shell_tools)
    except Exception:  # noqa: BLE001 - the local list stands on its own
        pass
    return frozenset(names)

# The cloud platforms' own command-line clients. Only names, because that is what
# a command line carries; whether one is installed is not this rail's business.
_PLATFORM_CLIS = ("lark-cli", "larkcli", "feishu-cli")

# What a read looks like on those clients, **by position in one argv**. An
# allow-list, not a deny-list: a deny-list of write verbs has to enumerate a surface
# the vendor keeps growing, and every verb it has not heard of yet passes. It used to
# be a substring list over the whole command line, and that admitted a write chained
# behind a read ("+cells-get ... && ... +cells-set ...") and a write whose argument
# happened to contain a marker ("+sheet-create --title +get"). Tokens are now parsed
# and only the verb position is judged.
#
# Local verbs: help, version, the CLI's own self-update, session and config
# management. None of them reaches a document.
_CLI_LOCAL_FIRST = frozenset({
    "--help", "-h", "help", "--version", "-v", "version", "update", "whoami",
    "schema", "config", "auth",
})
# The high-level (``+``-prefixed) read verbs the toolkit and the campaigns use.
_CLI_READ_VERBS = frozenset({
    "+get", "+list", "+fetch", "+search", "+download", "+export", "+cells-get",
    "+list-comments", "+member-list", "+raw-content", "+permission-get-setting",
})
# Raw API resources (``drive metas batch_query``, ``drive permission.members auth``)
# take an action word after the resource; these actions read.
_CLI_READ_ACTIONS = frozenset({"get", "list", "batch_query", "batch_get", "search", "query", "auth"})
# A shell that chains, pipes, redirects or substitutes around a platform CLI is
# refused whole: one argv is what can be judged, and a second command behind an
# operator is exactly where the write hid.
_SHELL_OPERATORS = re.compile(r"[;&|<>`$\n]|\(\)")
# Commands that only mention the binary (locate it, print it) without running it.
_INSPECT_ONLY = frozenset({"which", "type", "ls", "stat", "file", "echo", "printf"})
# Words that run the command after them. ``command`` is one: only ``command -v`` /
# ``command -V`` inspects, ``command <program> ...`` executes the program. These are
# peeled off before the verb is judged; a wrapper followed by an option it would
# consume is left in place, and the caller then refuses because it cannot tell what
# runs.
_EXEC_WRAPPERS = frozenset({
    "command", "exec", "env", "nohup", "time", "nice", "sudo", "doas", "stdbuf", "timeout",
})
_ENV_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def _strip_execution_wrappers(argv: list[str]) -> list[str] | None:
    """The argv the shell would actually run, or None for a pure inspection."""
    out = list(argv)
    while out:
        word = os.path.basename(out[0]).casefold()
        if _ENV_ASSIGNMENT.match(out[0]):
            out.pop(0)
            continue
        if word == "command" and len(out) >= 2 and out[1] in ("-v", "-V"):
            return None
        if word == "timeout":
            out.pop(0)
            if out and out[0].startswith("-"):
                return out                   # options before the duration: not judged
            if out:
                out.pop(0)                   # the duration
            continue
        if word in _EXEC_WRAPPERS:
            out.pop(0)
            if out and out[0].startswith("-"):
                return out                   # ``env -i``, ``nice -n``: not judged
            continue
        break
    return out

_CLI_DENIAL = (
    "[CLOUDDOC_CLI_GUARDED] 不能用通用工具直接调平台命令行改动云文档。"
    "这条路绕过了写入审批、范围校验、写后回读和**回执账本**——"
    "改动会落地却在账本上查不到，等于这次修改没有发生过。"
    "请改用 co-scribe 的工具：clouddoc_write_region（按地址写区域）、"
    "clouddoc_batch_edit（按文本改写）、clouddoc_apply_for_comment（批注内直改）。"
    "平台不支持或权限不足时，如实说明并停下，不要绕路。读取命令不受限制。"
)


# The guarded family: the exact files the mechanism persists beside config.yaml
# (the state relocation settled their names), whatever extension or atomic-write
# companion (``.tmp``, ``.lock``) they carry, plus the key directory. Exact stems
# rather than a prefix: a person's own ``clouddoc-notes.md`` is not mechanism
# state, and a guard that refused it would fire on every turn that touched it.
_GUARDED_STEMS = frozenset({
    "clouddoc-workmode",        # the work-conventions file, edited via its own tool
    "clouddoc-receipts",        # the receipt ledger
    "clouddoc-watches",         # the watch registry
    "clouddoc-watches-audit",   # the registry's audit journal
    "clouddoc-state",           # the comment cursor store
})
_GUARDED_DIR = "clouddoc-keys"

_DENIAL_MESSAGE = (
    "[CLOUDDOC_FILE_GUARDED] 这是 co-scribe 的机制文件，通用文件工具不可写入。"
    "工作方式文件请用 clouddoc_workmode_edit 修改；回执、授权、审计与状态文件"
    "只由机制本身写入，人工修改会破坏可回退性。读取不受限制。"
)


def _parse_tool_args(raw_args: Any) -> dict[str, Any]:
    import json

    if isinstance(raw_args, dict):
        return raw_args
    if isinstance(raw_args, str):
        try:
            parsed = json.loads(raw_args)
        except (json.JSONDecodeError, TypeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _is_guarded_path(raw_path: Any) -> bool:
    """Whether the path names one of co-scribe's own files.

    Judged by name rather than by resolving against the live config dir: the rail
    must not import the clouddoc package (it guards the machinery, it is not part
    of it). The names are the exact stems the machinery persists, so a user file
    that happens to start with ``clouddoc-`` is not caught.
    """
    if not isinstance(raw_path, (str, PurePath)):
        return False
    normalized = str(raw_path).strip().strip("\"'").replace("\\", "/")
    if not normalized:
        return False
    parts = [p for p in normalized.split("/") if p not in ("", ".")]
    if not parts:
        return False
    name = parts[-1].casefold()
    if name.split(".", 1)[0] in _GUARDED_STEMS or name == _GUARDED_DIR:
        return True
    return _GUARDED_DIR in {p.casefold() for p in parts[:-1]}


def _is_guarded_cli_command(command: Any) -> bool:
    """Whether a shell command drives a cloud-document CLI at anything but a read.

    Judged on the command text, which is all a shell tool carries. Two ways to be
    wrong, and they are not symmetric: refusing a read costs a sentence, passing a
    write costs a change to someone's document with no receipt. So the read side is
    an allow-list and everything else refuses -- including a verb this build has
    never heard of, which is exactly the case a deny-list would have waved through.

    The judgment is on one parsed argv: the binary must be the command word, the
    command must contain no shell operator (chaining, pipes, redirection,
    substitution), and the verb -- by position, never by substring -- must be a read.
    """
    if not isinstance(command, str) or not command.strip():
        return False
    lowered = command.casefold()
    if not any(cli in lowered for cli in _PLATFORM_CLIS):
        return False
    if _SHELL_OPERATORS.search(command):
        return True
    try:
        argv = shlex.split(command, posix=True)
    except ValueError:
        return True
    if not argv:
        return True
    argv = _strip_execution_wrappers(argv)
    if argv is None:
        return False                         # ``command -v lark-cli``: an inspection
    if not argv:
        return True
    head = os.path.basename(argv[0]).casefold()
    if head in _INSPECT_ONLY:
        return False
    if head not in _PLATFORM_CLIS:
        # The binary is named but not run here as the command word (an interpreter,
        # xargs, a script, a wrapper's own option): nothing can be judged, so
        # nothing is admitted.
        return True
    rest = argv[1:]
    if not rest:
        return False                         # the bare binary prints its help
    if rest[0].casefold() in _CLI_LOCAL_FIRST:
        return False
    if rest[0].startswith("-"):
        return True                          # a global flag before the domain: not a shape we admit
    tail = rest[1:]
    if not tail:
        return False                         # ``lark-cli sheets`` prints the domain's help
    verb = tail[0].casefold()
    if verb in ("--help", "-h") and len(tail) == 1:
        return False
    if len(tail) == 2 and tail[1] in ("--help", "-h"):
        return False                         # ``<domain> <verb> --help``: help, nothing runs
    if verb in _CLI_READ_VERBS:
        return False
    if verb.startswith("+"):
        return True
    # A raw resource: ``drive metas batch_query``. The action is the next token.
    if len(tail) >= 2 and tail[1].casefold() in _CLI_READ_ACTIONS:
        return False
    return True


class CloudDocFileGuardRail(DeepAgentRail):
    """Refuse generic-tool writes to co-scribe's files, and to the documents themselves.

    Two boundaries, one reason: a change that the machinery did not make is a change
    the ledger cannot account for.
    """

    priority: int = 100

    async def before_tool_call(self, ctx: AgentCallbackContext) -> None:
        inputs = ctx.inputs
        if isinstance(inputs, dict):
            tool_name = str(inputs.get("tool_name", "") or "").lower()
            raw_args = inputs.get("tool_args", {})
        else:
            tool_name = str(getattr(inputs, "tool_name", "") or "").lower()
            raw_args = getattr(inputs, "tool_args", {})
        if tool_name in _shell_tools():
            args = _parse_tool_args(raw_args)
            cmd = args.get("command") or args.get("cmd") or args.get("script")
            if _is_guarded_cli_command(cmd):
                logger.warning(
                    "[CloudDocFileGuardRail] blocked a platform CLI write through %s",
                    tool_name,
                )
                self._reject_tool(ctx, _CLI_DENIAL)
            return
        if tool_name not in _FILE_WRITE_TOOLS:
            return
        tool_args = _parse_tool_args(raw_args)
        if not any(
            _is_guarded_path(tool_args.get(key))
            for key in ("file_path", "path", "filename", "target_path")
        ):
            return
        logger.warning(
            "[CloudDocFileGuardRail] blocked generic write to a co-scribe file: tool=%s",
            tool_name,
        )
        self._reject_tool(ctx, _DENIAL_MESSAGE)

    @staticmethod
    def _reject_tool(ctx: AgentCallbackContext, denial: str) -> None:
        # Same reject shape as MemoryForbiddenRail: skip the tool, hand the model
        # a ToolMessage result naming the road back.
        inputs = ctx.inputs
        tool_call = (
            inputs.get("tool_call")
            if isinstance(inputs, dict)
            else getattr(inputs, "tool_call", None)
        )
        tool_call_id = getattr(tool_call, "id", "") if tool_call else ""
        ctx.extra["_skip_tool"] = True
        message = ToolMessage(content=denial, tool_call_id=tool_call_id)
        if isinstance(inputs, dict):
            inputs["tool_result"] = denial
            inputs["tool_msg"] = message
        else:
            inputs.tool_result = denial
            inputs.tool_msg = message

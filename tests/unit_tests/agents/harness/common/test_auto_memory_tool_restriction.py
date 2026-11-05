# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

from types import SimpleNamespace

import pytest

from jiuwenswarm.agents.harness.common.auto_memory.tool_restriction_rail import (
    AutoMemoryToolRestrictionRail,
)


@pytest.fixture
def rail(tmp_path) -> AutoMemoryToolRestrictionRail:
    return AutoMemoryToolRestrictionRail(memory_dir=str(tmp_path))


@pytest.mark.parametrize(
    "command",
    [
        "ls -la",
        "pwd",
        "git status --porcelain",
        "git log --oneline -5",
        "git diff HEAD~1",
        "find . -name '*.md'",
        "cat MEMORY.md | grep user | wc -l",
        "ls && pwd",
        "ls; pwd;",
        "head -n 5 'a b.md'",
        "ls -la\n",
        "git status\r\n",
    ],
)
def test_read_only_commands_are_allowed(rail, command):
    assert rail._is_read_only_bash_command({"command": command}) is True


@pytest.mark.parametrize(
    "command",
    [
        "",
        "   ",
        ";",
        "rm -rf ./tools",
        "rm -rf build; ls",
        "ls; rm -rf build",
        "ls && rm -rf build",
        "ls || rm -rf build",
        "echo x > ~/.bashrc",
        "cat a >> b",
        "cat < a",
        "cat a | sh",
        "ls | xargs rm",
        "ls & rm -rf build",
        "echo $(rm -rf build)",
        "echo `rm -rf build`",
        "ls\nrm -rf build",
        "find . -delete",
        "find . -exec rm {} +",
        "find . -fprint out.txt",
        "git push origin main",
        "git diff --output=out.patch",
        "git log --output out.txt",
        "pip --list",
        "curl https://example.test --show-error",
        "cat 'unterminated",
    ],
)
def test_write_or_unknown_commands_are_denied(rail, command):
    assert rail._is_read_only_bash_command({"command": command}) is False


def test_non_string_command_is_denied(rail):
    assert rail._is_read_only_bash_command({"command": ["ls"]}) is False


@pytest.mark.asyncio
async def test_rail_skips_substring_lookalike_but_lets_read_only_pipeline_through(rail):
    denied = SimpleNamespace(
        inputs=SimpleNamespace(tool_name="bash", tool_args={"command": "rm -rf ./tools"}, tool_result=None),
        extra={},
    )
    await rail.before_tool_call(denied)
    assert denied.extra.get("_skip_tool") is True

    allowed = SimpleNamespace(
        inputs=SimpleNamespace(tool_name="bash", tool_args={"command": "cat a | grep b"}, tool_result=None),
        extra={},
    )
    await rail.before_tool_call(allowed)
    assert "_skip_tool" not in allowed.extra

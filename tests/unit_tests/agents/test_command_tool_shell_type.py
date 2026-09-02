# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import json

import pytest

from jiuwenswarm.agents.harness.common.prompt.shell_environment import (
    build_shell_environment_prompt,
)
from jiuwenswarm.agents.harness.common.tools.command_tools import mcp_exec_command
from jiuwenswarm.agents.harness.code.rails.code_agent_mode_rail import _NON_GIT_WRITE_RE
from jiuwenswarm.agents.harness.design.prompt.design_plan_prompts import DESIGN_PLAN_ALLOWED_TOOLS
from jiuwenswarm.agents.harness.work.prompt.work_plan_prompts import WORK_PLAN_ALLOWED_TOOLS


def test_mcp_schema_requires_explicit_shell_type() -> None:
    params = mcp_exec_command.card.input_params
    assert "shell_type" in params["required"]
    assert "auto" not in params["properties"]["shell_type"].get("description", "")


@pytest.mark.asyncio
async def test_mcp_rejects_auto_before_execution(tmp_path) -> None:
    result = await mcp_exec_command._func(
        command="echo should-not-run",
        shell_type="auto",
        workdir=str(tmp_path),
    )
    payload = json.loads(result)
    assert payload["error"] == "shell_type_required"
    assert payload["example"]["shell_type"] == "bash"


@pytest.mark.asyncio
async def test_mcp_accepts_explicit_shell_type() -> None:
    result = await mcp_exec_command._func(
        command="echo explicit-shell",
        shell_type="cmd",
        workdir=".",
    )
    payload = json.loads(result)
    assert payload["shell_type"] == "cmd"
    assert payload["resolved_shell"] == "cmd"
    assert payload["exit_code"] == 0


@pytest.mark.asyncio
async def test_mcp_rejects_invalid_shell_type() -> None:
    result = await mcp_exec_command._func(
        command="echo should-not-run",
        shell_type="zsh",
        workdir=".",
    )
    payload = json.loads(result)
    assert payload["error"] == "invalid_shell_type"


def test_shell_prompt_names_tool_routing_and_forbids_auto() -> None:
    prompt = build_shell_environment_prompt("cn", "win32")
    assert "mcp_exec_command" in prompt
    assert "shell_type" in prompt
    assert "禁止使用 `auto`" in prompt


def test_shell_prompt_keeps_bash_as_primary_for_posix() -> None:
    prompt = build_shell_environment_prompt("cn", "win32")
    assert "普通 POSIX 命令" in prompt
    assert "Bash 脚本" in prompt
    assert "使用 bash" in prompt


def test_plan_tool_whitelists_include_powershell() -> None:
    assert "powershell" in WORK_PLAN_ALLOWED_TOOLS
    assert "powershell" in DESIGN_PLAN_ALLOWED_TOOLS


def test_plan_write_guard_matches_powershell_file_operations() -> None:
    assert _NON_GIT_WRITE_RE.search('Move-Item "a" "b"')


def test_plan_write_guard_is_case_insensitive() -> None:
    # PowerShell cmdlets are case-insensitive; a lowercase cmdlet must not
    # slip past the plan-mode write guard.
    assert _NON_GIT_WRITE_RE.search('move-item "a" "b"')
    assert _NON_GIT_WRITE_RE.search("new-item -Path build")


# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Phase 1 unit tests for ``command_intent`` (L1 shlex extraction).

L3-Cmd LLM 解析依赖外部模型，这里只覆盖 L1 与 ``CommandIntent`` 数据契约。
"""

from pathlib import Path

import pytest

from jiuwenclaw.agentserver.permissions.command_intent import (
    CommandIntent,
    extract_l1_intents,
    is_command_intent_enabled,
    is_l3_cmd_enabled,
)


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "repo"
    ws.mkdir()
    return ws


def _actions_for(intents, action: str) -> list[str]:
    out: list[str] = []
    for it in intents:
        if it.action == action:
            out.extend(it.paths)
    return out


def test_l1_skips_non_shell_tools(workspace: Path):
    intents = extract_l1_intents(
        "read_file",
        {"file_path": str(workspace / "a.txt")},
        workspace,
    )
    assert intents == []


def test_l1_extracts_redirect_write(workspace: Path):
    intents = extract_l1_intents(
        "mcp_exec_command",
        {"command": f'echo hi > {workspace}/out.txt', "workdir": str(workspace)},
        workspace,
    )
    write_paths = _actions_for(intents, "write")
    assert any(p.endswith("out.txt") for p in write_paths), intents


def test_l1_extracts_python_script_exec(workspace: Path):
    script = workspace / "x.py"
    script.write_text("print(1)\n", encoding="utf-8")
    intents = extract_l1_intents(
        "mcp_exec_command",
        {"command": f'python "{script}"', "workdir": str(workspace)},
        workspace,
    )
    exec_paths = _actions_for(intents, "exec")
    assert any(p.endswith("x.py") for p in exec_paths), intents


def test_l1_does_not_treat_system_command_as_exec(workspace: Path):
    """cp/mv/rm 等系统命令不得拆为 exec；权限由 tiered_policy 子线 A 裁决。"""
    src = workspace / "a.txt"
    dst = workspace / "b.txt"
    src.write_text("hello", encoding="utf-8")
    intents = extract_l1_intents(
        "mcp_exec_command",
        {"command": f'cp "{src}" "{dst}"', "workdir": str(workspace)},
        workspace,
    )
    exec_paths = _actions_for(intents, "exec")
    assert exec_paths == [], (
        "系统命令 cp 不应产生 exec 意图；其权限走子线 A"
    )


def test_command_intent_is_frozen_dataclass():
    intent = CommandIntent(
        summary="读取 a",
        action="read",
        paths=("/tmp/a",),
        executable=None,
        source="shlex",
    )
    with pytest.raises(Exception):
        intent.action = "write"


def test_is_command_intent_enabled_defaults_to_true():
    assert is_command_intent_enabled(None) is True
    assert is_command_intent_enabled({}) is True
    assert is_command_intent_enabled({"command_intent": {"enabled": False}}) is False
    assert is_l3_cmd_enabled({"command_intent": {"enabled": False}}) is False
    assert is_l3_cmd_enabled({"command_intent": {"enabled": "off"}}) is False

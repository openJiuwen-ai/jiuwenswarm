# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""SkillTurbo 统一审批卡工具清单回归测试。

审批卡清单（SKILL_TURBO_APPROVAL_TOOLS）仅用于展示、无行为耦合，
但必须与 turbo 节点实际可用的工具类别保持同步（含网络工具组）。
"""

from __future__ import annotations

from jiuwenswarm.server.runtime.skill_turbo.rails.permission_rail import (
    SKILL_TURBO_APPROVAL_TOOLS,
    SkillTurboPermissionRail,
)


def test_approval_tools_cover_network_group() -> None:
    """网络工具组必须在审批卡清单中。"""
    names = {name for name, _ in SKILL_TURBO_APPROVAL_TOOLS}
    assert "web_search" in names
    assert "fetch_webpage" in names


def test_approval_tools_keep_core_tool_group() -> None:
    """既有核心工具不得回归丢失。"""
    names = {name for name, _ in SKILL_TURBO_APPROVAL_TOOLS}
    for required in (
        "bash",
        "read_file",
        "write_file",
        "list_dir",
        "glob",
        "generate_image",
        "send_file_to_user",
    ):
        assert required in names


def test_skill_turbo_message_renders_network_tools() -> None:
    """审批卡消息必须渲染出网络工具（用户知情）。"""
    rail = object.__new__(SkillTurboPermissionRail)  # 消息构建不依赖实例状态
    message = rail._build_skill_turbo_message()
    assert "web_search" in message
    assert "fetch_webpage" in message

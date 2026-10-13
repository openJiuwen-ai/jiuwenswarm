# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""SkillTurbo 统一审批卡工具清单回归测试。

审批卡清单（SKILL_TURBO_APPROVAL_TOOLS）仅用于展示、无行为耦合，
但必须与 turbo 节点实际可用的工具类别保持同步（含网络工具组）。
"""

from __future__ import annotations

from types import SimpleNamespace

from openjiuwen.core.session.interaction.interactive_input import InteractiveInput
from openjiuwen.core.single_agent.interrupt.state import RESUME_USER_INPUT_KEY

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


def _resume_ctx(extra_value) -> SimpleNamespace:
    return SimpleNamespace(
        inputs=SimpleNamespace(tool_name="skill_acceleration_exec"),
        extra={RESUME_USER_INPUT_KEY: extra_value},
    )


def test_is_ask_user_resume_detects_protocol_dict() -> None:
    """P2 协议化 ask_user 作答（{"status","answers"}）必须判为 ask_user resume。

    回归 R3：权限审批 + 内层 ask_user 混合续跑时，handle_resume 重跑
    skill_acceleration_exec 的 before_tool_call 须短路不重弹权限卡。
    """
    interactive = InteractiveInput()
    interactive.update(
        "call-1",
        {"status": "answered", "answers": [{"question": "页数", "selected_options": ["10"]}]},
    )
    assert SkillTurboPermissionRail._is_ask_user_resume(_resume_ctx(interactive)) is True


def test_is_ask_user_resume_detects_raw_answers_list() -> None:
    """裸 answers list 形态（历史兼容）同样判为 ask_user resume。"""
    raw = [{"question": "页数", "selected_options": ["10"]}]
    assert SkillTurboPermissionRail._is_ask_user_resume(_resume_ctx(raw)) is True


def test_is_ask_user_resume_rejects_permission_confirm_payload() -> None:
    """权限审批 ConfirmPayload 不得误判为 ask_user resume（走父类正常消费）。"""
    interactive = InteractiveInput()
    interactive.update(
        "call-1", {"approved": True, "auto_confirm": False, "feedback": ""}
    )
    assert SkillTurboPermissionRail._is_ask_user_resume(_resume_ctx(interactive)) is False


def test_is_ask_user_resume_ignores_non_skill_turbo_tool() -> None:
    """非 skill_acceleration_exec 工具一律不短路。"""
    interactive = InteractiveInput()
    interactive.update(
        "call-1",
        {"status": "answered", "answers": [{"question": "页数", "selected_options": ["10"]}]},
    )
    ctx = SimpleNamespace(
        inputs=SimpleNamespace(tool_name="other_tool"),
        extra={RESUME_USER_INPUT_KEY: interactive},
    )
    assert SkillTurboPermissionRail._is_ask_user_resume(ctx) is False

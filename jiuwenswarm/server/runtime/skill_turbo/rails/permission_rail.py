# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""SkillTurbo 权限审批消息定制。

当 skill_acceleration_exec 被调用时，审批消息需展示内部可能用到的全部工具清单，
让用户一次性授权。审批通过后内部工具调用直接放行，不再逐个审批。
"""

from __future__ import annotations

from typing import Any, Optional

from jiuwenswarm.agents.harness.common.rails.permissions.skill_authorization_permission_rail import (
    SkillAuthorizationPermissionRail,
)

# skill_turbo 外层统一审批：通用兜底描述 + 工具清单
SKILL_TURBO_APPROVAL_DESCRIPTION = (
    "即将调用 skill加速 执行技能任务，需要一次性授权下列工具。"
    "确认后执行过程中不再逐个询问。"
)

SKILL_TURBO_APPROVAL_TOOLS: list[tuple[str, str]] = [
    ("bash", "执行 shell 命令（依赖安装、PPT 导出等）"),
    ("read_file", "读取文件内容"),
    ("write_file", "写入文件"),
    ("list_dir", "列出目录"),
    ("glob", "搜索文件"),
    ("image_ocr", "图片文字识别"),
    ("visual_question_answering", "图片视觉理解"),
    ("generate_image", "生成图片"),
    ("send_file_to_user", "发送最终产物"),
]


class SkillTurboPermissionRail(SkillAuthorizationPermissionRail):
    """SkillAuthorizationPermissionRail 子类，定制 skill_acceleration_exec 审批消息。

    仅覆盖 _build_message：当 tool_name == "skill_acceleration_exec" 时，
    展示统一审批消息（含内部工具清单），其余工具走父类默认逻辑。
    基类同时携带 Skill 动态授权门禁短路（gate-handled 命中即跳过）。
    """

    def _build_message(
        self,
        tool_call: Optional[Any],
        result: Any,
    ) -> str:
        tool_name = tool_call.name if tool_call else ""
        if tool_name == "skill_acceleration_exec":
            return self._build_skill_turbo_message()
        return super()._build_message(tool_call, result)

    def _build_skill_turbo_message(self) -> str:
        """skill_turbo 外层统一审批消息（工具清单）；勿再调用已移除的 risk API。"""
        tool_lines = "\n".join(
            f"- `{name}` — {desc}" for name, desc in SKILL_TURBO_APPROVAL_TOOLS
        )
        # 文案需含「需要授权」，供 Relay 权限桥识别为审批卡。
        return (
            f"**即将调用 `skill加速`，需要授权后整体放行：**\n\n"
            f"{SKILL_TURBO_APPROVAL_DESCRIPTION}\n\n"
            f"**可能用到的工具：**\n\n{tool_lines}\n\n"
        )


__all__ = [
    "SKILL_TURBO_APPROVAL_DESCRIPTION",
    "SKILL_TURBO_APPROVAL_TOOLS",
    "SkillTurboPermissionRail",
]

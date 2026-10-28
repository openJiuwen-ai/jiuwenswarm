# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""SkillTurbo 权限审批消息定制。

当 skill_acceleration_exec 被调用时，审批消息需展示内部可能用到的全部工具清单，
让用户一次性授权。审批通过后内部工具调用直接放行，不再逐个审批。
"""

from __future__ import annotations

from typing import Any, Optional

from openjiuwen.core.single_agent.interrupt.state import RESUME_USER_INPUT_KEY
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext

from jiuwenswarm.agents.harness.common.rails.permissions.skill_authorization_permission_rail import (
    SkillAuthorizationPermissionRail,
)

# skill_turbo 外层统一审批：通用兜底描述 + 工具清单
SKILL_TURBO_APPROVAL_DESCRIPTION = (
    "即将调用 skill加速 执行技能任务，需要一次性授权下列工具。"
    "确认后执行过程中不再逐个询问。"
)

# 维护约定：本清单必须覆盖 turbo 节点实际可用的工具类别（含网络组）。
# 该清单仅用于审批卡展示，无行为耦合；新增 turbo 工具类别时需同步此表。
SKILL_TURBO_APPROVAL_TOOLS: list[tuple[str, str]] = [
    ("bash", "执行 shell 命令（依赖安装、PPT 导出等）"),
    ("read_file", "读取文件内容"),
    ("write_file", "写入文件"),
    ("list_dir", "列出目录"),
    ("glob", "搜索文件"),
    ("web_search", "联网搜索（快速调研、深度研究）"),
    ("fetch_webpage", "抓取网页内容（研究引用来源）"),
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

    async def before_tool_call(self, ctx: AgentCallbackContext) -> None:
        # ask_user resume（结构化 answers）：权限早在首次 skill_acceleration_exec
        # 调用时已通过，handle_resume 重跑本工具时短路不重弹审批卡（否则 R3 重弹）。
        # ConfirmPayload（权限审批 resume）走父类正常消费（approve/reject/
        # persist_allow 语义不受影响）。
        if self._is_ask_user_resume(ctx):
            return
        await super().before_tool_call(ctx)

    @staticmethod
    def _is_ask_user_resume(ctx: AgentCallbackContext) -> bool:
        inputs = getattr(ctx, "inputs", None)
        if getattr(inputs, "tool_name", "") != "skill_acceleration_exec":
            return False
        extra = getattr(ctx, "extra", None)
        if not isinstance(extra, dict):
            return False
        raw = extra.get(RESUME_USER_INPUT_KEY)
        if raw is None:
            return False
        # 解包 InteractiveInput.user_inputs（外层 HITL 当前仅一个 interrupt，取首个
        # value）或原样透传（dict/list 形态），判别口径与
        # _skill_turbo_answers_to_confirm_payload 一致。
        user_inputs = getattr(raw, "user_inputs", None)
        payload = (
            next(iter(user_inputs.values()))
            if isinstance(user_inputs, dict) and user_inputs
            else raw
        )
        return SkillTurboPermissionRail._is_structured_ask_user_payload(payload)

    @staticmethod
    def _is_structured_ask_user_payload(payload: Any) -> bool:
        """结构化 ask_user 作答载荷判别（排除权限审批 ConfirmPayload）。

        两种合法形态：
        - P2 协议化 dict：``{"status": "answered"/"skipped", "answers": [...]}``
          （_build_interactive_input_from_answers 对 source=ask_user_interrupt 的产出）。
        - 裸 answers list：每项含 question / selected_options。

        权限审批 ConfirmPayload 形如 ``{"approved", "auto_confirm", ...}``（或带
        ``action``），不含 ``status``/``answers`` 键，命中不了本判别，走父类正常消费。
        """
        if isinstance(payload, dict):
            return isinstance(payload.get("answers"), list) and "status" in payload
        if isinstance(payload, list) and payload:
            return all(
                isinstance(a, dict) and ("question" in a or "selected_options" in a)
                for a in payload
            )
        return False

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

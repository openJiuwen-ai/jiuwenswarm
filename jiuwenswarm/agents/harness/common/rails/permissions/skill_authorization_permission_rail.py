# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Skill 动态授权与 PermissionInterruptRail 的协调子类。

``SkillAuthorizationRail``（priority=95）对本张 ``skill_tool``/``skill_complete``
调用完成门禁裁决后，会在 ``ctx.extra`` 写入 ``SKILL_AUTHORIZATION_GATE_HANDLED_KEY``
标记；本 rail（priority=90）命中标记即跳过，避免同一次调用重复弹权限审批卡。
对应 0708 对 permission_rail.py 的原地修改，这里下沉为 jiuwenswarm 侧子类，
不改动 agent-core。
"""

from __future__ import annotations

import logging

from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.harness.rails.security.tool_security_rail import PermissionInterruptRail

logger = logging.getLogger(__name__)

#: 由 SkillAuthorizationRail 专属门禁裁决的工具。
_SKILL_GATE_TOOL_NAMES = ("skill_tool", "skill_complete")


class SkillAuthorizationPermissionRail(PermissionInterruptRail):
    """PermissionInterruptRail 子类：Skill 门禁已裁决的调用跳过权限 Rail。"""

    async def before_tool_call(self, ctx: AgentCallbackContext) -> None:
        if self._skill_authorization_gate_handled(ctx):
            return
        await super().before_tool_call(ctx)

    def _skill_authorization_gate_handled(self, ctx: AgentCallbackContext) -> bool:
        """本次 skill_tool/skill_complete 是否已被动态授权门禁接管。"""
        inputs = getattr(ctx, "inputs", None)
        tool_name = getattr(inputs, "tool_name", "")
        if tool_name not in _SKILL_GATE_TOOL_NAMES:
            return False

        # 数字分身场景保留既有专用裁决，动态授权不接管（与 SkillAuthorizationRail
        # 的 _preserve_legacy_scene 对齐）；读取失败保守起见不跳过，由原有裁决处理。
        try:
            from jiuwenswarm.agents.harness.common.rails.permissions.owner_scopes import (
                TOOL_PERMISSION_CONTEXT,
            )

            permission_context = TOOL_PERMISSION_CONTEXT.get()
            if (
                permission_context is not None
                and getattr(permission_context, "scene", None) == "group_digital_avatar"
            ):
                return False
        except Exception:  # noqa: BLE001
            return False

        from openjiuwen.harness.rails.skills.skill_lifecycle_events import (
            SKILL_AUTHORIZATION_GATE_HANDLED_KEY,
        )

        extra = getattr(ctx, "extra", None)
        if not isinstance(extra, dict):
            return False
        marker = extra.get(SKILL_AUTHORIZATION_GATE_HANDLED_KEY)
        tool_call = getattr(inputs, "tool_call", None)
        tool_call_id = self._resolve_tool_call_id(tool_call)
        # 标记值须与当前 tool_call_id 精确匹配（Agent 循环复用同一 ctx.extra，
        # 残留标记不得误伤后续工具）；True 为无 id 时的兜底。
        handled_this_call = marker is True or (
            isinstance(marker, str) and bool(marker) and marker == tool_call_id
        )
        if handled_this_call:
            logger.info(
                "[PermissionEngine] permission.rail.skip "
                "reason=skill_authorization_gate tool=%s",
                tool_name,
            )
        return handled_this_call


__all__ = [
    "SkillAuthorizationPermissionRail",
]

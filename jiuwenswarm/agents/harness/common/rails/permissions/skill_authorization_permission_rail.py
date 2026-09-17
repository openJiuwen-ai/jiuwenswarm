# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Skill 动态授权与 PermissionInterruptRail 的协调子类。

``SkillAuthorizationRail``（priority=95）对本张 ``skill_tool``/``skill_complete``
调用完成门禁裁决后，会在 ``ctx.extra`` 写入 ``SKILL_AUTHORIZATION_GATE_HANDLED_KEY``
标记；本 rail（priority=90）命中标记即跳过，避免同一次调用重复弹权限审批卡。
对应 0708 对 permission_rail.py 的原地修改，这里下沉为 jiuwenswarm 侧子类，
不改动 agent-core。

另：会话里若残留 bash 等权限 HITL，下一条普通 ``chat.send`` 会被 ReAct 当成
resume 答案。openjiuwen 解析失败会再弹 ``匹配规则: N/A`` 的卡，并跳过
``auto_confirm``。此处仅把「无法解析的纯文本」回退成首次检查。
"""

from __future__ import annotations

import copy
import logging
from typing import Any, Optional

from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.harness.rails.security.tool_security_rail import PermissionInterruptRail

logger = logging.getLogger(__name__)

#: 由 SkillAuthorizationRail 专属门禁裁决的工具。
_SKILL_GATE_TOOL_NAMES = ("skill_tool", "skill_complete")


def is_unparseable_permission_resume_text(user_input: Any) -> bool:
    """Whether leftover HITL resume fed a non-confirm chat string into the rail."""
    if not isinstance(user_input, str):
        return False
    return PermissionInterruptRail.parse_confirm_payload(user_input) is None


def _compose_active_skill_permissions(
    base_config: dict[str, Any],
    session_id: str,
    agent_scope_id: str,
    grant_store: Any,
) -> dict[str, Any]:
    """Compose the active Skill overlay into one permission snapshot."""
    from openjiuwen.harness.security.skill_authorization import compose_skill_permissions

    active = grant_store.get_active(session_id, agent_scope_id)
    if active is None or not active.overlay_snapshot:
        return copy.deepcopy(base_config)
    return compose_skill_permissions(base_config, active.overlay_snapshot)


class SkillAuthorizationPermissionRail(PermissionInterruptRail):
    """PermissionInterruptRail 子类：Skill 门禁已裁决的调用跳过权限 Rail。"""

    async def before_tool_call(self, ctx: AgentCallbackContext) -> None:
        if self._skill_authorization_gate_handled(ctx):
            return
        await super().before_tool_call(ctx)

    async def resolve_interrupt(
        self,
        ctx: AgentCallbackContext,
        tool_call: Optional[Any],
        user_input: Optional[Any],
        auto_confirm_config: Optional[dict] = None,
    ):
        if is_unparseable_permission_resume_text(user_input):
            logger.info(
                "[PermissionEngine] permission.rail.invalid_payload_fallback "
                "tool=%s user_input_type=%s reason=treat_as_first_check",
                getattr(tool_call, "name", "") if tool_call is not None else "",
                type(user_input).__name__,
            )
            user_input = None
        return await super().resolve_interrupt(
            ctx, tool_call, user_input, auto_confirm_config
        )

    def _refresh_permissions_for_tool_call(self, ctx: AgentCallbackContext) -> None:
        """Apply the active Skill overlay without mutating the static baseline."""
        from openjiuwen.harness.security.skill_authorization import (
            get_skill_authorization_context,
            get_skill_grant_store,
        )

        authorization = get_skill_authorization_context()
        if (
            authorization is None
            or not authorization.session_id
            or not authorization.agent_scope_id
        ):
            super()._refresh_permissions_for_tool_call(ctx)
            return
        grant_store = get_skill_grant_store()
        active = grant_store.get_active(
            authorization.session_id,
            authorization.agent_scope_id,
        )
        if active is None:
            super()._refresh_permissions_for_tool_call(ctx)
            return
        base_config = super()._get_permissions_snapshot(ctx)
        if base_config is None:
            base_config = self._static_config
        effective_config = _compose_active_skill_permissions(
            base_config,
            authorization.session_id,
            authorization.agent_scope_id,
            grant_store,
        )
        self._engine.update_config(effective_config)

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
    "is_unparseable_permission_resume_text",
]

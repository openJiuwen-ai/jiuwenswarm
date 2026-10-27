# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Skill 动态授权与 PermissionInterruptRail 的协调子类。

``SkillAuthorizationRail``（priority=95）对本张 ``skill_tool``/``skill_complete``
调用完成门禁裁决后，会在 ``ctx.extra`` 写入 ``SKILL_AUTHORIZATION_GATE_HANDLED_KEY``
标记；本 rail（priority=90）命中标记即跳过，避免同一次调用重复弹权限审批卡。

有 ACTIVE Skill grant 时，用合成配置做一次性 ``check_permission(..., config=)``，
不登记新 key、不改父 Agent 已发布的基线配置。
"""

from __future__ import annotations

import copy
import logging
from typing import Any, Callable, Optional

from agent_shield.tool_permission import get_permission_manager
from openjiuwen.core.runner.callback.errors import AbortError
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.harness.rails.security.tool_security_rail import PermissionInterruptRail
from openjiuwen.harness.security.models import PermissionResult

from jiuwenswarm.common.audit_emit import emit_audit_evt

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
    active: Any,
) -> dict[str, Any]:
    """Compose the active Skill overlay into one permission snapshot."""
    from openjiuwen.harness.security.skill_authorization import (
        compose_skill_permissions,
    )

    if active is None or not active.overlay_snapshot:
        return copy.deepcopy(base_config)
    return compose_skill_permissions(base_config, active.overlay_snapshot)


def _tool_name_from_ctx(ctx: AgentCallbackContext) -> str:
    inputs = getattr(ctx, "inputs", None)
    name = str(getattr(inputs, "tool_name", "") or "").strip()
    if name:
        return name
    tool_call = getattr(inputs, "tool_call", None)
    return str(getattr(tool_call, "name", "") or "").strip()


def _permission_choice_label(payload: Any) -> str:
    """Map a parsed confirm payload to the button the user actually pressed."""
    if not getattr(payload, "approved", False):
        return "不允许"
    if getattr(payload, "auto_confirm", False) and getattr(payload, "persist_allow", False):
        return "始终允许"
    if getattr(payload, "auto_confirm", False):
        return "会话内记住"
    return "本次允许"


class SkillAuthorizationPermissionRail(PermissionInterruptRail):
    """PermissionInterruptRail 子类：Skill 门禁已裁决的调用跳过；ACTIVE grant 用临时 config。"""

    def set_skill_scope_resolver(
        self,
        resolver: Callable[[AgentCallbackContext], tuple[str, str] | None],
    ) -> None:
        """由宿主绑定同一 Agent 的技能授权作用域解析器。"""
        self._skill_scope_resolver = resolver

    async def before_tool_call(self, ctx: AgentCallbackContext) -> None:
        if self._skill_authorization_gate_handled(ctx):
            return
        try:
            await super().before_tool_call(ctx)
        except AbortError:
            tool_name = _tool_name_from_ctx(ctx)
            emit_audit_evt(
                SUBMDL="agent",
                PROC="skill_authorize",
                MSG=tool_name or "skill_authorize_check",
                EVT="skill_authorize_check",
            )
            raise

    async def resolve_interrupt(
        self,
        ctx: AgentCallbackContext,
        tool_call: Optional[Any],
        user_input: Optional[Any],
        auto_confirm_config: Optional[dict] = None,
    ):
        choice_input = user_input
        if is_unparseable_permission_resume_text(user_input):
            logger.info(
                "[PermissionEngine] permission.rail.invalid_payload_fallback "
                "tool=%s user_input_type=%s reason=treat_as_first_check",
                getattr(tool_call, "name", "") if tool_call is not None else "",
                type(user_input).__name__,
            )
            user_input = None
            choice_input = None
        decision = await super().resolve_interrupt(
            ctx, tool_call, user_input, auto_confirm_config
        )
        payload = (
            self.parse_confirm_payload(choice_input)
            if choice_input is not None
            else None
        )
        if payload is not None:
            tool_name = str(getattr(tool_call, "name", "") or "").strip()
            choice = _permission_choice_label(payload)
            emit_audit_evt(
                SUBMDL="agent",
                PROC="skill_authorize",
                MSG=f"{tool_name}: {choice}" if tool_name else choice,
                EVT="skill_authorize_decision",
                choice=choice,
            )
        return decision

    async def _check_tool_permission(
        self,
        tool_name: str,
        tool_args: dict[str, Any],
        ctx: AgentCallbackContext,
    ) -> PermissionResult:
        """Baseline key, or one-off ``config=`` when a Skill grant is ACTIVE."""
        composed = self._compose_active_skill_config(ctx)
        if composed is None:
            return await super()._check_tool_permission(tool_name, tool_args, ctx)
        return await get_permission_manager().check_permission(
            tool_name,
            tool_args,
            config=composed,
        )

    def _compose_active_skill_config(
        self,
        ctx: AgentCallbackContext,
    ) -> dict[str, Any] | None:
        from openjiuwen.harness.security.skill_authorization import (
            get_skill_authorization_context,
            get_skill_grant_store,
            is_skill_authorization_enabled,
        )

        try:
            base_config = dict(get_permission_manager().get_config(self._key))
        except KeyError:
            logger.warning(
                "[PermissionEngine] skill_overlay.skip reason=unregistered_key key=%s",
                self._key,
            )
            return None
        if not is_skill_authorization_enabled(base_config):
            return None
        authorization = get_skill_authorization_context()
        scope = None
        if (
            authorization is not None
            and authorization.session_id
            and authorization.agent_scope_id
        ):
            scope = authorization.session_id, authorization.agent_scope_id
        else:
            resolver = getattr(self, "_skill_scope_resolver", None)
            if resolver is not None:
                scope = resolver(ctx)
        if scope is None:
            return None
        session_id, agent_scope_id = scope
        active = get_skill_grant_store().get_active(session_id, agent_scope_id)
        if active is None:
            return None
        effective = _compose_active_skill_permissions(base_config, active)
        logger.info(
            "[PermissionEngine] skill_overlay.one_off session=%s scope=%s skill=%s tool=%s",
            session_id,
            agent_scope_id,
            active.skill_name,
            _tool_name_from_ctx(ctx),
        )
        return effective

    def _skill_authorization_gate_handled(self, ctx: AgentCallbackContext) -> bool:
        """本次 skill_tool/skill_complete 是否已被动态授权门禁接管。"""
        inputs = getattr(ctx, "inputs", None)
        tool_name = getattr(inputs, "tool_name", "")
        if tool_name not in _SKILL_GATE_TOOL_NAMES:
            return False

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
        handled_this_call = marker is True or (
            isinstance(marker, str) and bool(marker) and marker == tool_call_id
        )
        if handled_this_call:
            logger.info(
                "[PermissionEngine] permission.rail.skip "
                "tool=%s reason=skill_authorization_gate_handled",
                tool_name,
            )
        return handled_this_call

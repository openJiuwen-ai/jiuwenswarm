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
from contextvars import ContextVar
from typing import Any, Optional

from openjiuwen.core.single_agent.interrupt.state import INTERRUPT_AUTO_CONFIRM_KEY
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.harness.rails.security.tool_security_rail import PermissionInterruptRail

logger = logging.getLogger(__name__)

#: 由 SkillAuthorizationRail 专属门禁裁决的工具。
_SKILL_GATE_TOOL_NAMES = ("skill_tool", "skill_complete")

#: 「本次允许」轮内复用：allow_once 授权写入会话 auto_confirm 表时同步记录
#: 的 key 集合（session state key）。新一轮用户消息开始时仅清除这些 key，
#: 用户显式选择「会话内记住/永久记住」的授权不受影响。
ROUND_AUTO_CONFIRM_KEYS = "__round_auto_confirm_keys__"

#: 在 ``_should_store_auto_confirm``（持有 auto_confirm 标志）与
#: ``_store_auto_confirm``（签名不含该标志）之间传递写入范围。
_PENDING_STORE_SCOPE: ContextVar[Optional[str]] = ContextVar(
    "permission_auto_confirm_store_scope", default=None
)


def _store_round_scoped_auto_confirm(ctx: AgentCallbackContext, auto_confirm_key: str) -> None:
    """allow_once 授权写入 auto_confirm 表并打轮级标记。

    写入现有 ``__interrupt_auto_confirm__`` 使既有的首轮命中路径
    （``_is_auto_confirmed``）直接放行；同时把 key 记入
    ``__round_auto_confirm_keys__``，供新一轮用户消息开始时精确回收。
    """
    session = ctx.session
    config = session.get_state(INTERRUPT_AUTO_CONFIRM_KEY) or {}
    if not isinstance(config, dict):
        config = {}
    config[auto_confirm_key] = True
    round_keys = session.get_state(ROUND_AUTO_CONFIRM_KEYS) or []
    if not isinstance(round_keys, list):
        round_keys = []
    if auto_confirm_key not in round_keys:
        round_keys = [*round_keys, auto_confirm_key]
    session.update_state({
        INTERRUPT_AUTO_CONFIRM_KEY: config,
        ROUND_AUTO_CONFIRM_KEYS: round_keys,
    })
    logger.info(
        "[PermissionEngine] permission.auto_confirm.store_round_scoped key=%s",
        auto_confirm_key,
    )


def clear_round_scoped_auto_confirm(session: Any) -> None:
    """新一轮用户消息开始时回收「本次允许」的轮级授权。

    只删除 ``__round_auto_confirm_keys__`` 记录的 key；「会话内记住」
    （``auto_confirm=True``）写入的条目不在该集合中，保持有效。
    """
    if session is None:
        return
    try:
        round_keys = session.get_state(ROUND_AUTO_CONFIRM_KEYS)
    except Exception:  # noqa: BLE001
        return
    if not isinstance(round_keys, list) or not round_keys:
        return
    config = session.get_state(INTERRUPT_AUTO_CONFIRM_KEY) or {}
    if isinstance(config, dict):
        for key in round_keys:
            config.pop(key, None)
    session.update_state({
        INTERRUPT_AUTO_CONFIRM_KEY: config,
        ROUND_AUTO_CONFIRM_KEYS: [],
    })
    logger.info(
        "[PermissionEngine] permission.auto_confirm.round_scope_cleared keys=%s",
        round_keys,
    )


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
    """PermissionInterruptRail 子类：Skill 门禁已裁决的调用跳过权限 Rail。

    另：覆盖 auto_confirm 写入判定——「本次允许」（allow_once）也写入会话
    auto_confirm 表但打轮级标记（``ROUND_AUTO_CONFIRM_KEYS``），使同一轮
    任务内同 key 的后续工具调用免于重复弹卡；新一轮用户消息开始时由
    ``clear_round_scoped_auto_confirm`` 回收，不扩大到跨轮/跨会话。
    """

    @staticmethod
    def _should_store_auto_confirm(
        *,
        approved: bool,
        auto_confirm: bool,
        session: Any,
        auto_confirm_key: str,
        persisted: bool,
    ) -> bool:
        base_ok = bool(approved and session is not None and auto_confirm_key and not persisted)
        if base_ok and not auto_confirm:
            # allow_once：本轮内复用（写轮级标记，新用户消息时回收）。
            _PENDING_STORE_SCOPE.set("round")
            return True
        _PENDING_STORE_SCOPE.set("session" if (base_ok and auto_confirm) else None)
        return bool(base_ok and auto_confirm)

    @staticmethod
    def _store_auto_confirm(ctx: AgentCallbackContext, auto_confirm_key: str) -> None:
        if _PENDING_STORE_SCOPE.get() == "round":
            _store_round_scoped_auto_confirm(ctx, auto_confirm_key)
            return
        PermissionInterruptRail._store_auto_confirm(ctx, auto_confirm_key)

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
    "ROUND_AUTO_CONFIRM_KEYS",
    "SkillAuthorizationPermissionRail",
    "clear_round_scoped_auto_confirm",
    "is_unparseable_permission_resume_text",
]

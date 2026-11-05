# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Skill 动态授权与 PermissionInterruptRail 的协调子类。

``SkillAuthorizationRail``（priority=95）对本张 ``skill_tool``/``skill_complete``
调用完成门禁裁决后，会在 ``ctx.extra`` 写入 ``SKILL_AUTHORIZATION_GATE_HANDLED_KEY``
标记；本 rail（priority=90）命中标记即跳过，避免同一次调用重复弹权限审批卡。

用户级配置登记在 Manager 的 routing/instance key 上。ACTIVE Skill 在未发布
会话快照时用临时 ``config=`` 合成判定，不再要求会话级 Manager key。
"""

from __future__ import annotations

import copy
import logging
from contextlib import contextmanager
from typing import Any, Callable, Optional

from openjiuwen.core.runner.callback.errors import AbortError
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.harness.rails.security.tool_security_rail import PermissionInterruptRail
from openjiuwen.harness.security.models import PermissionLevel, PermissionResult
from openjiuwen.harness.security.permission_checks import fail_closed_permission_callback

from jiuwenswarm.common.audit_emit import emit_audit_evt

logger = logging.getLogger(__name__)

#: 由 SkillAuthorizationRail 专属门禁裁决的工具。
_SKILL_GATE_TOOL_NAMES = ("skill_tool", "skill_complete")


def is_unparseable_permission_resume_text(user_input: Any) -> bool:
    """Whether leftover HITL resume fed a non-confirm chat string into the rail."""
    if not isinstance(user_input, str):
        return False
    return PermissionInterruptRail.parse_confirm_payload(user_input) is None



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
    """Skill 门禁已裁决的调用跳过；其余调用用用户级 key，Skill 临时 config=。"""

    def __init__(self, config=None, tool_names=None, llm=None, model_name=None, host=None, permission_key=None):
        from uuid import uuid4
        from openjiuwen.harness.security.permission_key import DEFAULT_KEY

        if permission_key is not None and (not isinstance(permission_key, str) or not permission_key.strip()):
            raise ValueError("permission_key must be a non-empty string")
        self.routing_key = DEFAULT_KEY if permission_key is None else permission_key
        # Business identity is a routing prefix, not registration ownership.
        # Old/warm/replacement instances may overlap even in the same tenant.
        instance_key = f"{self.routing_key}:agent:{uuid4().hex}"
        super().__init__(config=config, tool_names=tool_names, llm=llm, model_name=model_name,
                         host=host, permission_key=instance_key)
        from .published_scopes import PublishedPermissionScopes

        try:
            self._published_scopes = PublishedPermissionScopes(self._key, base_config=self._static_config)
        except BaseException:
            super()._release_registration()
            raise

    def update_session_config(self, session_id, config):
        """会话级配置不再发布到 Manager；仅保留用户级 key。"""
        return

    def acquire_session_config(self, session_id, config):
        """会话租约已停用；返回空释放回调以兼容旧调用方。"""
        return lambda: None

    def retain_permission_session(self, session_id):
        """会话租约已停用；子 Agent 走用户级 key。"""
        return lambda: None

    def update_config(self, config, tool_names=None):
        from .published_scopes import publish_with_key_budget

        publish_base = super().update_config

        def publish():
            publisher = getattr(self, "_published_scopes", None)
            if publisher is not None:
                publisher.ensure_open()
                publisher.base_config = copy.deepcopy(self._static_config)
            publish_base(config, tool_names)

        publish_with_key_budget(publish)

    @contextmanager
    def permission_persist_context(self, ctx):
        """Bind trusted callback identity only during the configuration write."""
        from .config_loader import setup_permissions_session_scope, reset_permissions_session_scope

        scope = self._permission_scope(ctx)
        token = setup_permissions_session_scope(scope[0] if scope else None)
        try:
            yield
        finally:
            reset_permissions_session_scope(token)

    def _release_registration(self):
        publisher = getattr(self, "_published_scopes", None)
        if publisher is not None:
            publisher.close()
        super()._release_registration()

    def _permission_scope(self, ctx):
        from openjiuwen.harness.rails.skills.skill_authorization_rail import _resolve_session_id
        from openjiuwen.harness.security.skill_authorization import get_skill_authorization_context
        from .config_loader import get_permissions_session_id

        authorization = get_skill_authorization_context()
        if authorization is not None and authorization.session_id and authorization.agent_scope_id:
            return authorization.session_id, authorization.agent_scope_id
        resolver = getattr(self, "_skill_scope_resolver", None)
        scope = resolver(ctx) if resolver is not None else None
        if scope is not None:
            return scope
        sid = get_permissions_session_id() or _resolve_session_id(ctx)
        return (sid, "") if sid else None

    def permission_key_for(self, ctx):
        scope = self._permission_scope(ctx)
        if scope is None:
            return self._key
        if not self._published_scopes.has_session(scope[0]):
            # 无会话发布：走用户级 key；ACTIVE Skill 由
            # ``_compose_unpublished_skill_config`` + ``config=`` 收紧。
            return self._key
        return self._published_scopes.key_for(*scope)

    def permission_baseline_key_for(self, ctx):
        """Skill 加载门禁读取用户/已发布基线，不能用当前 Skill overlay 自授权。"""
        scope = self._permission_scope(ctx)
        if scope is None or not self._published_scopes.has_session(scope[0]):
            return self._key
        return self._published_scopes.baseline_key_for(scope[0])

    def _compose_unpublished_skill_config(self, ctx) -> dict[str, Any] | None:
        """无会话 Manager key 时，把 ACTIVE Skill overlay 合成临时 config。"""
        from openjiuwen.harness.security.skill_authorization import (
            compose_skill_permissions,
        )

        scope = self._permission_scope(ctx)
        if scope is None or not scope[1]:
            return None
        if self._published_scopes.has_session(scope[0]):
            return None
        if not self._published_scopes.has_active_grant(*scope):
            return None
        active = self._published_scopes.get_active(*scope)
        if active is None or not active.overlay_snapshot:
            return None
        return compose_skill_permissions(
            copy.deepcopy(self._static_config), active.overlay_snapshot
        )

    async def _check_tool_permission(
        self,
        tool_name: str,
        tool_args: dict[str, Any],
        ctx: AgentCallbackContext,
    ) -> PermissionResult:
        skill_cfg = self._compose_unpublished_skill_config(ctx)
        if skill_cfg is None:
            return await super()._check_tool_permission(tool_name, tool_args, ctx)
        from agent_shield.tool_permission import get_permission_manager
        from openjiuwen.harness.security.permission_checks import (
            _FETCH_TOOLS,
            merge_permission_results,
        )

        manager = get_permission_manager()
        if tool_name in _FETCH_TOOLS:
            urls = tool_args.get("url")
            if isinstance(urls, list):
                if not urls or any(
                    not isinstance(url, str) or not url.strip() for url in urls
                ):
                    return PermissionResult(
                        PermissionLevel.DENY,
                        "host:invalid_urls",
                        "Invalid fetch URL list",
                    )
                results = [
                    await manager.check_permission(
                        tool_name, {**tool_args, "url": url}, config=skill_cfg
                    )
                    for url in urls
                ]
                return merge_permission_results(results)
        return await manager.check_permission(tool_name, tool_args, config=skill_cfg)

    def projection_config_for(self, ctx):
        skill_cfg = self._compose_unpublished_skill_config(ctx)
        if skill_cfg is not None:
            return skill_cfg
        scope = self._permission_scope(ctx)
        if scope is None or not self._published_scopes.has_session(scope[0]):
            return self._static_config
        return self._published_scopes.projection_config(scope[0], self.permission_key_for(ctx))

    def outbound_checks_enabled(self, ctx):
        scope = self._permission_scope(ctx)
        if scope is None or not self._published_scopes.has_session(scope[0]):
            return super().outbound_checks_enabled(ctx)
        return bool(self.projection_config_for(ctx).get("net_guard", {}).get("enabled", False))

    def set_skill_scope_resolver(
        self,
        resolver: Callable[[AgentCallbackContext], tuple[str, str] | None],
    ) -> None:
        """由宿主绑定同一 Agent 的技能授权作用域解析器。"""
        self._skill_scope_resolver = resolver

    @fail_closed_permission_callback
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

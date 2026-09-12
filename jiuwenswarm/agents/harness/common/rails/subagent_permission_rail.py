# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""子 Agent Skill 执行期的权限裁决 rail：ASK 走 Future 委托，不抛 checkpoint。

dev-stable 适配说明（相对 0708
``jiuwenclaw...deep_agent/rails/subagent_permission_rail.py``）：

- 基类为 agent-core ``openjiuwen.harness.rails.interrupt.confirm_rail.ConfirmInterruptRail``；
- agent-core ``PermissionEngine.check_permission`` 无 session/overlay 钩子，
  ACTIVE Grant 的 overlay 由本 rail 手工合成（``compose_skill_permissions`` +
  ``get_effective_permissions_config`` + 会话 overlay）后以临时引擎裁决；
- agent-core ``PermissionResult`` 无 ``risk`` 字段：审批卡不再展示风险等级；
- 0708 的 ``EXCLUDED_TOOLS_SPAWN`` 注册表在 dev-stable 不存在（子 Agent 工具
  剔除由 agent-core TaskTool/SubAgentConfig 装配层负责），此处保留同名兜底集合
  作为防御性深防御，命中即无条件拒绝。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.harness.rails.interrupt.confirm_rail import ConfirmInterruptRail
from openjiuwen.harness.rails.security.tool_security_rail import TOOL_NAME_ALIASES
from openjiuwen.harness.rails.skills.skill_authorization_rail import (
    _resolve_session_id,
)
from openjiuwen.harness.rails.skills.skill_lifecycle_events import (
    extract_skill_lifecycle_event,
    is_root_skill_load,
    is_skill_authorization_gate_call,
    is_skill_complete,
    parse_tool_call_arguments,
)
from openjiuwen.harness.security.core import PermissionEngine
from openjiuwen.harness.security.models import PermissionLevel
from openjiuwen.harness.security.skill_authorization import (
    compose_skill_permissions,
    is_skill_authorization_enabled,
)
from openjiuwen.harness.security.skill_authorization.grant_store import (
    SkillGrantStore,
    get_skill_grant_store,
)
from openjiuwen.harness.security.skill_authorization.subagent_approval_registry import (
    ApprovalExpirySender,
    ApprovalSender,
    SubagentApprovalCancelled,
    SubagentApprovalCapacityError,
    SubagentApprovalKind,
    SubagentApprovalRegistry,
    SubagentApprovalRequest,
    SubagentApprovalTimeout,
    get_subagent_approval_registry,
)

from jiuwenswarm.agents.harness.common.rails.permissions.config_loader import (
    get_effective_permissions_config,
    merge_session_permissions_overlay,
)
from jiuwenswarm.agents.harness.common.rails.permissions.workspace_untrusted_policy import (
    WorkspaceUntrustedPolicyEngine,
)
from jiuwenswarm.agents.harness.common.rails.permissions.owner_scopes import (
    TOOL_PERMISSION_CONTEXT,
    _resolve_owner_scope_level,
)
from jiuwenswarm.agents.harness.common.rails.subagent_skill_authorization_rail import (
    _resolve_subagent_scope,
    build_context_approval_sender,
    build_context_expiry_sender,
    emit_subagent_approval,
    _parent_stream_session,
)

logger = logging.getLogger(__name__)


_SEVERITY = {PermissionLevel.ALLOW: 0, PermissionLevel.ASK: 1, PermissionLevel.DENY: 2}

#: 同一调用被拒绝/审批超时后的统一提示；before 拒绝与 after 强制收口共用。
_REJECTED_RETRY_TOOL_RESULT = (
    "[PERMISSION_DENIED] 相同的子 Agent 工具调用已被拒绝或审批超时"
)

#: 主 Agent 专属工具兜底集合（0708 ``EXCLUDED_TOOLS_SPAWN`` 平移）。
#: dev-stable 装配层已在子 Agent 工具列表中剔除这些工具；此处仅作异常路径
#: （继承异常、直接注入）下的防御性深防御，命中即无条件拒绝。
EXCLUDED_TOOLS_SPAWN = frozenset({
    "spawn_subagent",
    "send_file_to_user",
    # 主 Agent 级调度与消息（子 Agent 不应触发）
    "office_claw_dispatch_agent_task",
    "office_claw_post_message",
    "office_claw_get_pending_mentions",
    "office_claw_ack_mentions",
    "office_claw_get_thread_context",
    "office_claw_list_threads",
    "office_claw_cross_post_message",
    "office_claw_register_pr_tracking",
    "office_claw_multi_mention",
    # 主 Agent 级计划任务
    "office_claw_list_scheduled_tasks",
    "office_claw_list_schedule_templates",
    "office_claw_preview_scheduled_task",
    "office_claw_register_scheduled_task",
    "office_claw_set_scheduled_task_enabled",
    "office_claw_remove_scheduled_task",
    "office_claw_update_scheduled_task",
    # 主 Agent 级记忆与反思
    "office_claw_retain_memory_callback",
    "office_claw_search_evidence",
    "office_claw_reflect",
    # 主 Agent 级会话链追踪
    "office_claw_list_session_chain",
    "office_claw_read_session_events",
    "office_claw_read_session_digest",
    "office_claw_read_invocation_detail",
    # 主 Agent 级技能管理
    "office_claw_list_skills",
})

_APPROVAL_PREVIEW_MAX_CHARS = 2000


def _format_tool_args_preview(tool_name: str, tool_args: Any) -> str:
    """Render the concrete approval target without allowing an unbounded card."""
    args = tool_args if isinstance(tool_args, dict) else {}
    preview = ""
    if tool_name in {"bash", "mcp_exec_command", "create_terminal"}:
        for key in ("command", "cmd", "script", "input"):
            value = args.get(key)
            if isinstance(value, str) and value.strip():
                preview = value.strip()
                break
    if not preview:
        try:
            preview = json.dumps(args, ensure_ascii=False, sort_keys=True, default=str)
        except (TypeError, ValueError):
            preview = str(args)
    if len(preview) > _APPROVAL_PREVIEW_MAX_CHARS:
        preview = preview[:_APPROVAL_PREVIEW_MAX_CHARS] + "…"
    return "\n".join(f"    {line}" for line in (preview or "{}").splitlines())


def build_tool_permission_approval_sender() -> ApprovalSender:
    """默认审批 sender：经父会话 ``write_stream`` 下发子 Agent 工具审批卡。

    dev-stable 的 ``PermissionResult`` 无 ``risk`` 字段，审批卡不展示风险等级。
    父 session 缺失时抛错，由 rail 按审批通道异常 fail-closed。
    """

    async def send(request: SubagentApprovalRequest) -> None:
        session = _parent_stream_session()
        if session is None:
            raise RuntimeError("subagent approval session unavailable")
        payload = request.payload if isinstance(request.payload, dict) else {}
        tool_name = str(payload.get("tool_name") or "").strip()
        tool_args = payload.get("tool_args")
        skill_name = str(payload.get("skill_name") or "").strip()
        reason = str(payload.get("reason") or "").strip()
        question_parts = [
            f"**工具 `{tool_name}` 需要授权才能执行**\n\n",
            f"> 发起方：子 Agent `{request.agent_scope_id}`"
            f"（当前 Skill：`{skill_name}`）",
            "\n\n**关键参数或命令：**\n\n",
            _format_tool_args_preview(tool_name, tool_args),
        ]
        if reason:
            question_parts.append(f"\n\n**权限原因：** {reason}")
        await emit_subagent_approval(
            session,
            request,
            header=f"权限审批: {tool_name}",
            question="".join(question_parts),
            options=[
                {"label": "本次允许", "description": "仅本次授权执行"},
                {"label": "拒绝", "description": "拒绝本次工具调用"},
            ],
        )

    return send


def _tighten_permission_level(
    base_level: PermissionLevel,
    owner_level: str | None,
) -> PermissionLevel:
    """Apply owner scope as a restriction after the Skill-aware engine result."""
    normalized = owner_level.strip().lower() if isinstance(owner_level, str) else ""
    try:
        owner = PermissionLevel(normalized)
    except ValueError:
        return base_level
    return owner if _SEVERITY.get(owner, 0) > _SEVERITY.get(base_level, 0) else base_level


class SubagentPermissionRail(ConfirmInterruptRail):
    """Apply normal tool permission semantics during a subagent Skill window.

    ``engine`` 由调用方构造注入，仅作裁决模板（复用其 workspace_root /
    trusted_dirs）；实际裁决使用手工合成 overlay 后的临时引擎，agent-core
    引擎本身不感知 Skill Grant。
    """

    priority: int = 90

    def __init__(
        self,
        *,
        agent_scope_id: str,
        engine: PermissionEngine | Any | None = None,
        grant_store: SkillGrantStore | None = None,
        approval_registry: SubagentApprovalRegistry | None = None,
        approval_sender: ApprovalSender | None = None,
        approval_expiry_sender: ApprovalExpirySender | None = None,
        approval_timeout: float = 120.0,
        config_provider: Callable[[], dict[str, Any]] | None = None,
        session_id: str | None = None,
        workspace_root: Any = None,
    ) -> None:
        super().__init__(tool_names=[])
        self._agent_scope_id = (agent_scope_id or "").strip()
        self._engine = engine
        self._grant_store = grant_store or get_skill_grant_store()
        self._approval_registry = approval_registry or get_subagent_approval_registry()
        self._approval_sender = (
            approval_sender
            if approval_sender is not None
            else build_tool_permission_approval_sender()
        )
        self._approval_expiry_sender = (
            approval_expiry_sender
            if approval_expiry_sender is not None
            else build_context_expiry_sender()
        )
        self._approval_timeout = approval_timeout
        self._config_provider = config_provider or get_effective_permissions_config
        self._approval_session_id = (session_id or "").strip()
        if workspace_root is None and engine is not None:
            workspace_root = getattr(engine, "_workspace_root", None)
        self._workspace_root = Path(workspace_root) if workspace_root else None
        self._rejected_call_fingerprints: set[str] = set()

    def _resolve_scope(self, ctx: AgentCallbackContext) -> tuple[str, str] | None:
        return _resolve_subagent_scope(
            ctx,
            agent_scope_id=self._agent_scope_id,
            approval_session_id=self._approval_session_id,
            session_resolver=_resolve_session_id,
        )

    def _enabled(self) -> bool:
        try:
            return is_skill_authorization_enabled(self._config_provider())
        except Exception:  # noqa: BLE001
            logger.warning(
                "[skill_authorization] subagent.permission.flag_read_failed",
                exc_info=True,
            )
            return False

    def _adjudicate_config(self, session_id: str, agent_scope_id: str) -> dict[str, Any]:
        """手工合成裁决配置：生效配置（全局 + 会话 overlay）+ 当前 ACTIVE Grant overlay。"""
        try:
            base = self._config_provider()
        except Exception:  # noqa: BLE001 — 配置读取失败按空配置（全 ASK）处理
            logger.warning(
                "[skill_authorization] subagent.permission.config_read_failed",
                exc_info=True,
            )
            base = {}
        base = base if isinstance(base, dict) else {}
        try:
            base = merge_session_permissions_overlay(base, session_id=session_id)
        except Exception:  # noqa: BLE001
            logger.warning(
                "[skill_authorization] subagent.permission.session_overlay_failed",
                exc_info=True,
            )
        grant = self._grant_store.get_active(session_id, agent_scope_id)
        overlay = grant.overlay_snapshot if grant is not None else None
        if overlay:
            return compose_skill_permissions(base, overlay)
        return base

    @staticmethod
    def _approved(answer: Any) -> bool:
        if isinstance(answer, list):
            return any(SubagentPermissionRail._approved(item) for item in answer)
        if not isinstance(answer, dict):
            return False
        selected = answer.get("selected_options")
        return isinstance(selected, list) and any(
            str(item).strip() in {"本次允许", "允许"}
            for item in selected
        )

    @staticmethod
    def _explicitly_rejected(answer: Any) -> bool:
        if isinstance(answer, list):
            return any(SubagentPermissionRail._explicitly_rejected(item) for item in answer)
        if not isinstance(answer, dict):
            return False
        selected = answer.get("selected_options")
        return isinstance(selected, list) and any(
            str(item).strip() == "拒绝" for item in selected
        )

    @staticmethod
    def _call_fingerprint(
        *,
        skill_name: str,
        tool_name: str,
        tool_args: dict[str, Any],
    ) -> str:
        """Hash a stable call identity without retaining sensitive raw arguments."""
        canonical = json.dumps(
            {
                "skill_name": skill_name,
                "tool_name": TOOL_NAME_ALIASES.get(tool_name, tool_name),
                "tool_args": tool_args,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    async def before_tool_call(self, ctx: AgentCallbackContext) -> None:
        # 执行期兜底：EXCLUDED_TOOLS_SPAWN 中的工具在装配时已从子 Agent 工具列表
        # 剔除；若经其他路径混入子 Agent（如继承异常、直接注入），在此无条件拒绝。
        # 与工具列表剔除保持一致，且不受动态授权开关影响。
        excluded_tool_name = str(getattr(ctx.inputs, "tool_name", "") or "")
        if excluded_tool_name in EXCLUDED_TOOLS_SPAWN:
            tool_call = ctx.inputs.tool_call
            logger.warning(
                "[skill_authorization] subagent.permission.excluded_tool tool=%s scope=%s",
                excluded_tool_name,
                self._agent_scope_id,
            )
            decision = self.reject(
                tool_result=(
                    f"[PERMISSION_DENIED] {excluded_tool_name} 为主 Agent 专属工具，"
                    "子 Agent 不允许执行"
                ),
            )
            ctx.extra["_interrupt_decision"] = decision
            self._apply_decision(ctx, tool_call, excluded_tool_name, decision)
            return
        if not self._enabled():
            return
        permission_context = TOOL_PERMISSION_CONTEXT.get()
        if (
            permission_context is not None
            and permission_context.scene == "group_digital_avatar"
        ):
            # 数字分身使用既有专用裁决，动态授权不得接管。
            return
        scope = self._resolve_scope(ctx)
        if scope is None:
            return
        session_id, agent_scope_id = scope
        active_skill_name = self._grant_store.get_active_skill_execution(
            session_id,
            agent_scope_id,
        )
        if active_skill_name is None:
            return

        tool_call = ctx.inputs.tool_call
        tool_name = ctx.inputs.tool_name
        tool_args = parse_tool_call_arguments(tool_call)
        if is_skill_authorization_gate_call(
            tool_name,
            tool_args,
            self._config_provider(),
        ):
            return

        # dev-stable：agent-core 引擎无 Skill overlay 钩子，手工合成后以临时引擎裁决。
        effective_config = self._adjudicate_config(session_id, agent_scope_id)
        adjudication_engine = WorkspaceUntrustedPolicyEngine(
            config=effective_config,
            workspace_root=self._workspace_root,
        )
        result = await adjudication_engine.check_permission(
            TOOL_NAME_ALIASES.get(tool_name, tool_name),
            tool_args,
        )

        # 与主 Agent 一致：owner_scopes 只能在引擎（含 Skill overlay）之后收紧。
        try:
            perm_ctx = TOOL_PERMISSION_CONTEXT.get()
            if perm_ctx is not None and perm_ctx.principal_user_id:
                config = self._config_provider()
                owner_scopes = config.get("owner_scopes", {}) if isinstance(config, dict) else {}
                cid = perm_ctx.channel_id.strip()
                uid = perm_ctx.principal_user_id.strip()
                channel_scopes = owner_scopes.get(cid) or {} if isinstance(owner_scopes, dict) else {}
                scope_cfg = channel_scopes.get(uid) if isinstance(channel_scopes, dict) else None
                owner_level = _resolve_owner_scope_level(
                    scope_cfg,
                    TOOL_NAME_ALIASES.get(tool_name, tool_name),
                    tool_args,
                )
                tightened = _tighten_permission_level(result.permission, owner_level)
                if tightened != result.permission:
                    result.permission = tightened
                    result.matched_rule = (
                        f"{result.matched_rule or 'permission_engine'}|owner_scopes:{owner_level}"
                    )
                    result.reason = f"owner_scopes 将权限收紧为 {tightened.value}"
        except Exception:  # noqa: BLE001 — owner 上下文存在时异常必须拒绝
            logger.warning(
                "[skill_authorization] subagent.permission.owner_scope_failed",
                exc_info=True,
            )
            result.permission = PermissionLevel.DENY
            result.matched_rule = "owner_scopes:evaluation_error"
            result.reason = "owner_scopes 权限评估失败"
        if result.permission == PermissionLevel.ALLOW:
            decision = self.approve()
        elif result.permission == PermissionLevel.DENY:
            decision = self.reject(
                tool_result=f"[PERMISSION_DENIED] {result.reason or 'Operation not allowed'}",
            )
        else:
            call_fingerprint = self._call_fingerprint(
                skill_name=active_skill_name,
                tool_name=tool_name,
                tool_args=tool_args,
            )
            if call_fingerprint in self._rejected_call_fingerprints:
                decision = self.reject(tool_result=_REJECTED_RETRY_TOOL_RESULT)
                # 不能在 before hook 直接 request_force_finish：agent-core 会跳过
                # railed 方法体并返回 None，破坏工具结果二元组。after hook 再终止。
                ctx.extra["_subagent_force_finish"] = True
            else:
                decision = await self._resolve_ask(
                    session_id=session_id,
                    agent_scope_id=agent_scope_id,
                    tool_call=tool_call,
                    tool_name=tool_name,
                    tool_args=tool_args,
                    reason=result.reason,
                    skill_name=active_skill_name,
                    call_fingerprint=call_fingerprint,
                )
        ctx.extra["_interrupt_decision"] = decision
        self._apply_decision(ctx, tool_call, tool_name, decision)

    async def _resolve_ask(
        self,
        *,
        session_id: str,
        agent_scope_id: str,
        tool_call: Any,
        tool_name: str,
        tool_args: dict[str, Any],
        reason: str | None,
        skill_name: str,
        call_fingerprint: str,
    ):
        from openjiuwen.harness.security.skill_authorization import (
            get_skill_authorization_generation,
        )

        authorization_generation = get_skill_authorization_generation()
        if self._approval_sender is None:
            return self.reject(
                tool_result="[PERMISSION_DENIED] 子 Agent 权限审批通道不可用",
            )
        tool_call_id = str(getattr(tool_call, "id", "") or "")
        try:
            answer = await self._approval_registry.request(
                kind=SubagentApprovalKind.TOOL_PERMISSION,
                session_id=self._approval_session_id or session_id,
                agent_scope_id=agent_scope_id,
                tool_call_id=tool_call_id,
                payload={
                    "tool_name": tool_name,
                    "tool_args": tool_args,
                    "skill_name": skill_name,
                    "reason": reason or "Operation requires approval",
                },
                sender=self._approval_sender,
                expiry_sender=self._approval_expiry_sender,
                timeout=self._approval_timeout,
            )
        except SubagentApprovalTimeout:
            self._rejected_call_fingerprints.add(call_fingerprint)
            logger.warning(
                "[skill_authorization] subagent.permission.ask_timeout session=%s scope=%s tool=%s",
                session_id,
                agent_scope_id,
                tool_name,
            )
            return self.reject(
                tool_result="[PERMISSION_DENIED] 子 Agent 权限审批超时",
            )
        except asyncio.TimeoutError:
            logger.warning(
                "[skill_authorization] subagent.permission.ask_sender_timeout "
                "session=%s scope=%s tool=%s",
                session_id,
                agent_scope_id,
                tool_name,
            )
            return self.reject(
                tool_result="[PERMISSION_DENIED] 子 Agent 权限审批通道超时",
            )
        except (SubagentApprovalCancelled, SubagentApprovalCapacityError):
            return self.reject(
                tool_result="[PERMISSION_DENIED] 子 Agent 权限审批已取消或请求过多",
            )
        except Exception:  # noqa: BLE001 — 审批通道异常 fail-closed：仅拒绝当前调用
            logger.warning(
                "[skill_authorization] subagent.permission.ask_channel_error session=%s scope=%s tool=%s",
                session_id,
                agent_scope_id,
                tool_name,
                exc_info=True,
            )
            return self.reject(
                tool_result="[PERMISSION_DENIED] 子 Agent 权限审批通道异常",
            )
        if not self._enabled():
            return self.reject(
                tool_result="[PERMISSION_DENIED] Skill 动态授权功能已关闭",
            )
        if authorization_generation != get_skill_authorization_generation():
            return self.reject(
                tool_result="[PERMISSION_DENIED] Skill 动态授权配置已变更",
            )
        if self._grant_store.get_active_skill_execution(
            session_id,
            agent_scope_id,
        ) != skill_name:
            return self.reject(
                tool_result="[PERMISSION_DENIED] Skill 执行作用域已结束或发生变化",
            )
        if self._approved(answer):
            return self.approve()
        if self._explicitly_rejected(answer):
            self._rejected_call_fingerprints.add(call_fingerprint)
        return self.reject(tool_result="[PERMISSION_DENIED] 用户未批准子 Agent 工具调用")

    async def after_tool_call(self, ctx: AgentCallbackContext) -> None:
        event = extract_skill_lifecycle_event(ctx)
        if is_root_skill_load(event) or is_skill_complete(event):
            self._rejected_call_fingerprints.clear()
        if not ctx.extra.pop("_subagent_force_finish", False):
            return
        ctx.request_force_finish({
            "output": _REJECTED_RETRY_TOOL_RESULT,
            "result_type": "answer",
        })


__all__ = [
    "EXCLUDED_TOOLS_SPAWN",
    "SubagentPermissionRail",
    "build_tool_permission_approval_sender",
]

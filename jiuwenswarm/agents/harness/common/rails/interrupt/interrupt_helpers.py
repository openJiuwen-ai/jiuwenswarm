# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Interrupt helpers for DeepAgent.

Provides utilities for converting interrupt payloads to frontend format
and building permission rails.
"""
from __future__ import annotations
from jiuwenswarm.edition import is_enterprise

import copy
import json
import re
from typing import Any

from jiuwenswarm.agents.harness.code.rails.code_plan_approval_interrupt_rail import (
    build_plan_approval_options_from_message,
    extract_plan_approval_content,
    is_plan_approval_message,
    strip_inline_plan_approval_choices,
)
from jiuwenswarm.common.utils import logger

SKILL_EVOLUTION_APPROVAL_SCHEMA = "openjiuwen.skill_evolution_approval.v1"
EVOLUTION_INTERRUPT_SOURCE = "evolution_interrupt"
LEGACY_SKILL_EVOLUTION_APPROVAL_SOURCE = "skill_evolution_approval"
INTERRUPT_RESUME_SOURCES = frozenset({
    "permission_interrupt",
    "confirm_interrupt",
    "ask_user_interrupt",
    EVOLUTION_INTERRUPT_SOURCE,
})
EVOLUTION_INTERRUPT_METADATA_SOURCES = frozenset({
    EVOLUTION_INTERRUPT_SOURCE,
    LEGACY_SKILL_EVOLUTION_APPROVAL_SOURCE,
})
SKILL_EVOLUTION_APPROVAL_TOOL_KINDS = {
    "evolve_skill_experiences": "evolve",
    "simplify_skill_experiences": "simplify",
}


def is_interrupt_resume_source(source: Any) -> bool:
    """True for permission / confirm / ask_user HITL resume sources.

    Used to keep the active skill (and thus skill_envs injection) across
    security-guard HITL. Intentionally excludes skill-evolution sources.
    """
    text = str(source or "").strip()
    return text in {
        "permission_interrupt",
        "confirm_interrupt",
        "ask_user_interrupt",
    }


def has_interrupt_resume_payload(params: Any) -> bool:
    if not isinstance(params, dict):
        return False
    if not str(params.get("request_id") or "").strip():
        return False
    answers = params.get("answers")
    return isinstance(answers, list) and bool(answers)


def _has_active_skill_overlay() -> bool:
    """当前 Skill 授权上下文是否存在 ACTIVE Grant（owner_scopes allow 回落判定）。

    判定失败按存在处理（返回 True，强制回落 engine），不放宽权限。
    """
    try:
        from jiuwenswarm.agents.harness.common.rails.permissions.config_loader import (
            get_effective_permissions_config,
        )
        from openjiuwen.harness.security.skill_authorization import (
            get_skill_authorization_context,
            get_skill_grant_store,
            is_skill_authorization_enabled,
        )

        if not is_skill_authorization_enabled(get_effective_permissions_config()):
            return False
        authz = get_skill_authorization_context()
        if authz is None or not authz.session_id or not authz.agent_scope_id:
            return False
        return (
            get_skill_grant_store().get_active(authz.session_id, authz.agent_scope_id)
            is not None
        )
    except Exception:  # noqa: BLE001 — 不确定时不允许绕过引擎
        logger.warning(
            "[InterruptHelpers] active Skill lookup failed; owner allow falls through to engine",
            exc_info=True,
        )
        return True


def is_interrupt_resume_payload(params: Any) -> bool:
    if not has_interrupt_resume_payload(params):
        return False
    source = str(params.get("source") or "").strip()
    if source in INTERRUPT_RESUME_SOURCES:
        return True
    if source != LEGACY_SKILL_EVOLUTION_APPROVAL_SOURCE:
        return False
    evolution_meta = params.get("evolution_meta")
    return (
        isinstance(evolution_meta, dict)
        and evolution_meta.get("approval_transport") == "interrupt"
    )


_SUBAGENT_PERMISSION_APPROVAL_TIMEOUT = 120.0

# Hosted cards need chat.ask_user_question rendering (web / OfficeClaw / TUI).
# ACP keeps native session/request_permission JSON-RPC — do not hijack it.
_HOSTED_PERMISSION_CHANNELS = frozenset({"web", "officeclaw", "tui"})

_HOSTED_ALLOW_ONCE_LABELS = frozenset({
    "本次允许",
    "允许",
    "Allow once",
    "allow_once",
    "allow-once",
})
_HOSTED_SESSION_REMEMBER_LABELS = frozenset({
    "会话内记住",
    "Session remember",
    "allow_always_session",
})
_HOSTED_REJECT_LABELS = frozenset({
    "拒绝",
    "Reject",
    "reject",
    "reject-once",
    "reject_once",
})


def _unwrap_session(session: Any) -> Any:
    return getattr(session, "_parent", session) if session is not None else None


def _session_id_of(session: Any) -> str | None:
    if session is None:
        return None
    for attr_name in ("get_session_id", "session_id"):
        attr = getattr(session, attr_name, None)
        try:
            value = attr() if callable(attr) else attr
        except Exception:
            value = None
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def resolve_subagent_permission_parent_session(ctx: Any) -> Any | None:
    """Return parent session when ``ctx`` is a nested agent under an outer tool.

    Real trigger surface (not ``task_tool`` inheriting PermissionInterruptRail):
    a child agent that still mounts its own permission rail, with
    ``ctx.session`` id different from the parent session ContextVar bound by
    ``StreamEventRail`` for the outer tool call (OfficeClaw worker / nested
    agent). Same-session bindings must not take the hosted path.

    ``PermissionInterruptRail`` on the *same* agent as the outer tool still
    sees no foreign parent, so main-agent ASK stays on interrupt / ACP.
    """
    try:
        from jiuwenswarm.agents.harness.common.tools.subagent_executor.context_vars import (
            get_subagent_parent_session,
        )

        parent = get_subagent_parent_session()
    except Exception:  # noqa: BLE001
        logger.warning(
            "[InterruptHelpers] subagent parent session read failed",
            exc_info=True,
        )
        return None

    parent = _unwrap_session(parent)
    current = _unwrap_session(getattr(ctx, "session", None))
    if parent is None or current is None or parent is current:
        return None
    parent_sid = _session_id_of(parent)
    current_sid = _session_id_of(current)
    if parent_sid and current_sid and parent_sid == current_sid:
        return None
    if not callable(getattr(parent, "write_stream", None)):
        return None
    return parent


def _selected_option_labels(answer: Any) -> list[str]:
    labels: list[str] = []
    if isinstance(answer, list):
        for item in answer:
            labels.extend(_selected_option_labels(item))
        return labels
    if not isinstance(answer, dict):
        return labels
    selected = answer.get("selected_options")
    if isinstance(selected, list):
        for item in selected:
            text = str(item).strip()
            if text:
                labels.append(text)
    return labels


def parse_hosted_permission_answer(answer: Any) -> Any:
    """Map card answers to :class:`PermissionConfirmResponse` (or None if empty).

    Enterprise never persists permanent-remember to local config.yaml (same
    constraint as :func:`_default_interrupt_options`); those labels degrade to
    session auto_confirm only.
    """
    from openjiuwen.harness.security.models import PermissionConfirmResponse

    labels = _selected_option_labels(answer)
    if not labels:
        return None
    if any(label in _HOSTED_REJECT_LABELS for label in labels):
        return PermissionConfirmResponse(
            approved=False,
            auto_confirm=False,
            persist_allow=False,
            feedback="[PERMISSION_REJECTED] User rejected the request.",
        )
    if any(label in _PERMANENT_REMEMBER_LABELS for label in labels):
        # 企业版：永久标签降级为会话内记住，不写本地 config。
        persist = not is_enterprise()
        return PermissionConfirmResponse(
            approved=True,
            auto_confirm=True,
            persist_allow=persist,
            feedback="",
        )
    if any(label in _HOSTED_SESSION_REMEMBER_LABELS for label in labels):
        return PermissionConfirmResponse(
            approved=True,
            auto_confirm=True,
            persist_allow=False,
            feedback="",
        )
    if any(label in _HOSTED_ALLOW_ONCE_LABELS for label in labels):
        return PermissionConfirmResponse(
            approved=True,
            auto_confirm=False,
            persist_allow=False,
            feedback="",
        )
    return None


def _format_hosted_permission_question(
    *,
    tool_name: str,
    tool_args: Any,
    reason: str,
    agent_scope_id: str,
) -> str:
    from jiuwenswarm.agents.harness.common.rails.subagent_permission_rail import (
        _format_tool_args_preview,
    )

    parts = [
        f"**工具 `{tool_name}` 需要授权才能执行**\n\n",
        f"> 发起方：子 Agent `{agent_scope_id}`",
        "\n\n**关键参数或命令：**\n\n",
        _format_tool_args_preview(tool_name, tool_args),
    ]
    if reason:
        parts.append(f"\n\n**权限原因：** {reason}")
    return "".join(parts)


async def request_subagent_hosted_permission_confirmation(
    req: Any,
    *,
    parent_session: Any,
    timeout: float = _SUBAGENT_PERMISSION_APPROVAL_TIMEOUT,
) -> Any:
    """Await parent-session card approval; never returns ``"interrupt"``."""
    from openjiuwen.harness.security.models import PermissionConfirmResponse
    from openjiuwen.harness.security.skill_authorization import (
        SubagentApprovalKind,
        get_subagent_approval_registry,
    )
    from openjiuwen.harness.security.skill_authorization.subagent_approval_registry import (
        SubagentApprovalCancelled,
        SubagentApprovalCapacityError,
        SubagentApprovalTimeout,
    )

    from jiuwenswarm.agents.harness.common.rails.subagent_skill_authorization_rail import (
        build_context_expiry_sender,
        emit_subagent_approval,
    )
    from jiuwenswarm.openjiuwen_streaming_tool_patch import (
        streaming_tool_wait_timeout_paused,
    )

    tool_call = req.tool_call
    tool_name = str(getattr(tool_call, "name", "") or "").strip()
    tool_call_id = str(
        getattr(tool_call, "id", "") or f"permission_{tool_name or 'tool'}"
    ).strip()
    agent_scope_id = _session_id_of(getattr(req.ctx, "session", None))
    parent_session_id = _session_id_of(parent_session)
    if not agent_scope_id or not parent_session_id or not tool_call_id:
        logger.warning(
            "[InterruptHelpers] subagent hosted permission missing ids "
            "parent=%s scope=%s tool_call=%s",
            parent_session_id,
            agent_scope_id,
            tool_call_id,
        )
        return None

    raw_args = getattr(tool_call, "arguments", None) if tool_call is not None else None
    if isinstance(raw_args, str):
        try:
            tool_args = json.loads(raw_args) if raw_args.strip() else {}
        except (TypeError, ValueError):
            tool_args = {"_raw": raw_args}
    elif isinstance(raw_args, dict):
        tool_args = raw_args
    else:
        tool_args = {}

    reason = str(getattr(getattr(req, "result", None), "reason", None) or "").strip()
    question = _format_hosted_permission_question(
        tool_name=tool_name,
        tool_args=tool_args,
        reason=reason or "Operation requires approval",
        agent_scope_id=agent_scope_id,
    )
    # Session remember is stored on the child rail session; copy must not
    # imply parent-session scope.
    options = []
    for opt in _default_interrupt_options():
        item = dict(opt)
        if item.get("label") in _HOSTED_SESSION_REMEMBER_LABELS:
            item["description"] = "该子 Agent 委托内自动放行同类操作"
        options.append(item)

    async def _send(request: Any) -> None:
        await emit_subagent_approval(
            parent_session,
            request,
            header=f"权限审批: {tool_name}",
            question=question,
            options=options,
        )

    try:
        async with streaming_tool_wait_timeout_paused():
            answer = await get_subagent_approval_registry().request(
                kind=SubagentApprovalKind.TOOL_PERMISSION,
                session_id=parent_session_id,
                agent_scope_id=agent_scope_id,
                tool_call_id=tool_call_id,
                payload={
                    "tool_name": tool_name,
                    "tool_args": tool_args,
                    "reason": reason or "Operation requires approval",
                    "auto_confirm_key": getattr(req, "auto_confirm_key", "") or "",
                },
                sender=_send,
                expiry_sender=build_context_expiry_sender(),
                timeout=timeout,
            )
    except SubagentApprovalTimeout:
        logger.warning(
            "[InterruptHelpers] subagent hosted permission timeout "
            "session=%s scope=%s tool=%s",
            parent_session_id,
            agent_scope_id,
            tool_name,
        )
        return PermissionConfirmResponse(
            approved=False,
            auto_confirm=False,
            feedback="[PERMISSION_DENIED] 子 Agent 权限审批超时",
        )
    except (SubagentApprovalCancelled, SubagentApprovalCapacityError) as exc:
        logger.warning(
            "[InterruptHelpers] subagent hosted permission cancelled/capacity "
            "session=%s scope=%s tool=%s error=%s",
            parent_session_id,
            agent_scope_id,
            tool_name,
            exc,
        )
        return PermissionConfirmResponse(
            approved=False,
            auto_confirm=False,
            feedback="[PERMISSION_DENIED] 子 Agent 权限审批已取消或请求过多",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[InterruptHelpers] subagent hosted permission failed: %s",
            exc,
            exc_info=True,
        )
        return None

    parsed = parse_hosted_permission_answer(answer)
    if parsed is not None:
        return parsed
    return PermissionConfirmResponse(
        approved=False,
        auto_confirm=False,
        feedback="[PERMISSION_REJECTED] User rejected the request.",
    )


def build_permission_rail(
    config: dict[str, Any],
    llm: Any = None,
    model_name: str | None = None,
    permission_config: dict[str, Any] | None = None,
) -> Any | None:
    """Build openjiuwen PermissionInterruptRail for tool permission checks.

    Args:
        config: Agent config dict containing permissions section
        llm: LLM instance for risk assessment
        model_name: Model name for risk assessment
        permission_config: Optional Agent-level permissions body (enterprise template).
            When omitted, falls back to effective/global permissions config.

    Returns:
        PermissionInterruptRail instance or None if disabled
    """
    from openjiuwen.harness.rails.security.tool_security_rail import PermissionInterruptRail
    from openjiuwen.harness.security.host import (
        PermissionConfirmationRequest,
        PermissionSceneHookInput,
        ToolPermissionHost,
    )
    from openjiuwen.harness.security.models import PermissionConfirmResponse

    from jiuwenswarm.agents.harness.common.rails.permissions.tool_permission_context import (
        TOOL_PERMISSION_CHANNEL_ID,
    )
    from jiuwenswarm.common.e2a.acp.acp_tool_updates import build_acp_tool_descriptor
    from jiuwenswarm.common.utils import get_config_file, get_workspace_dir
    from jiuwenswarm.agents.harness.common.rails.permissions.config_loader import (
        get_base_permissions_config,
        get_effective_permissions_config,
        merge_session_permissions_overlay,
    )

    if isinstance(permission_config, dict):
        # Agent 模板 body：企业版仍叠加当前会话 overlay（若有）。
        permission_config = (
            merge_session_permissions_overlay(permission_config)
            if is_enterprise()
            else copy.deepcopy(permission_config)
        )
        config_source = "agent_template"
    else:
        permission_config = get_effective_permissions_config()
        config_source = "effective"
    logger.info(
        "[InterruptHelpers] build_permission_rail called: enabled=%s source=%s",
        permission_config.get("enabled", False),
        config_source,
    )

    if not permission_config.get("enabled", False):
        logger.info("[InterruptHelpers] Permission system is disabled, returning None")
        return None

    def _collect_optional_tool_tags(cfg: dict[str, Any]) -> list[str]:
        # openjiuwen PermissionInterruptRail 会拦截所有工具；
        # 这里的 tool_names 仅作为标签展示/日志辅助（尽量覆盖 tools + rules 声明）。
        names: set[str] = set()
        tools_cfg = cfg.get("tools") or {}
        if isinstance(tools_cfg, dict):
            for k in tools_cfg.keys():
                label = str(k).strip()
                if label:
                    names.add(label)
        rules = cfg.get("rules") or []
        if isinstance(rules, list):
            for entry in rules:
                if not isinstance(entry, dict):
                    continue
                raw_tools = entry.get("tools")
                if raw_tools is None:
                    continue
                if isinstance(raw_tools, str):
                    raw_tools = [raw_tools]
                if isinstance(raw_tools, list):
                    for item in raw_tools:
                        if isinstance(item, str) and item.strip():
                            names.add(item.strip())
        return sorted(names)

    tool_names = _collect_optional_tool_tags(permission_config)
    logger.info(
        "[InterruptHelpers] tools_config keys: %s, rail tool_names (with rules): %s",
        list((permission_config.get("tools") or {}).keys()),
        tool_names,
    )
    logger.info(
        "[InterruptHelpers] Building PermissionInterruptRail with tool_names=%s llm=%s model_name=%s",
        tool_names, llm is not None, model_name,
    )
    try:
        def _persist_allow_rule(permissions: dict[str, Any]) -> bool:
            """Persist merged `permissions` config back to config.yaml.

            openjiuwen PermissionInterruptRail calls this when user selects "always allow".

            Instead of replacing the entire ``permissions`` section with the
            in-memory snapshot (which may contain stale entries that were
            already deleted from config.yaml), we first re-read the current
            on-disk permissions, then merge only the *approval_overrides*、
            *file_guard*（及过渡期 *external_directory*）deltas from
            ``permissions`` into it.
            This prevents re-creating tool-level entries (e.g. ``bash: ask``)
            that the user has already removed via the webui.
            """
            try:
                from jiuwenswarm.agents.harness.common.rails.permissions.config_loader import (
                    get_base_permissions_config,
                    persist_permissions_mutate,
                )
                from jiuwenswarm.common.config import _load_yaml_round_trip

                yaml_path = get_config_file()
                data = _load_yaml_round_trip(yaml_path)
                if not isinstance(data, dict):
                    data = {}

                on_disk_perms = data.get("permissions")
                if not isinstance(on_disk_perms, dict):
                    on_disk_perms = get_base_permissions_config() if is_enterprise() else {}

                merged = dict(on_disk_perms)
                overrides_new = permissions.get("approval_overrides")
                if overrides_new is not None:
                    merged["approval_overrides"] = overrides_new
                fg_new = permissions.get("file_guard")
                if fg_new is not None:
                    merged["file_guard"] = fg_new
                ext_dir_new = permissions.get("external_directory")
                if ext_dir_new is not None:
                    merged["external_directory"] = ext_dir_new

                def mutate(perms: dict[str, Any]) -> None:
                    perms.clear()
                    perms.update(copy.deepcopy(merged))

                persist_permissions_mutate(mutate)
                return True
            except Exception as exc:
                logger.warning("[InterruptHelpers] persist_allow_rule failed: %s", exc)
                return False

        def _resolve_session_id(ctx: Any) -> str | None:
            session = getattr(ctx, "session", None)
            if session is None:
                return None
            for attr_name in ("get_session_id", "session_id"):
                attr = getattr(session, attr_name, None)
                try:
                    value = attr() if callable(attr) else attr
                except Exception:
                    value = None
                if isinstance(value, str) and value.strip():
                    return value.strip()
            return None

        async def _request_permission_confirmation(
            req: PermissionConfirmationRequest,
        ) -> PermissionConfirmResponse | str | None:
            channel = (TOOL_PERMISSION_CHANNEL_ID.get() or "web").strip() or "web"
            parent_session = resolve_subagent_permission_parent_session(req.ctx)
            # Hosted parent-card path: nested agent with own rail + different
            # session id (e.g. OfficeClaw worker). Only channels that render
            # chat.ask_user_question — never hijack ACP JSON-RPC.
            if (
                parent_session is not None
                and channel in _HOSTED_PERMISSION_CHANNELS
            ):
                return await request_subagent_hosted_permission_confirmation(
                    req,
                    parent_session=parent_session,
                )

            if channel != "acp":
                return "interrupt"

            session_id = _resolve_session_id(req.ctx)
            if not session_id:
                return None

            from jiuwenswarm.agents.harness.common.tools.acp_output_tools import get_acp_output_manager

            tool_call = req.tool_call
            tool_name = getattr(tool_call, "name", "") if tool_call is not None else ""
            tool_args_raw = getattr(tool_call, "arguments", None) if tool_call is not None else None
            tool_call_id = str(getattr(tool_call, "id", "") or f"permission_{tool_name or 'tool'}").strip()
            descriptor = build_acp_tool_descriptor(
                tool_name,
                tool_args_raw,
                tool_call_id=tool_call_id,
                status="pending",
                kind="other",
            )
            title = str(descriptor.get("title") or f"Approve `{tool_name}`")
            if getattr(req.result, "reason", None):
                title = f"{title}: {req.result.reason}"

            request_params: dict[str, Any] = {
                "toolCall": {
                    **descriptor,
                    "title": title,
                },
                "options": [
                    {"optionId": "allow-once", "name": "Allow once", "kind": "allow_once"},
                    {"optionId": "allow-always", "name": "Always allow", "kind": "allow_always"},
                    {"optionId": "reject-once", "name": "Reject", "kind": "reject_once"},
                ],
            }

            try:
                # ACP permission HITL must not consume StreamingToolExecutor.wait_all budget.
                from jiuwenswarm.openjiuwen_streaming_tool_patch import (
                    streaming_tool_wait_timeout_paused,
                )

                async with streaming_tool_wait_timeout_paused():
                    response = await get_acp_output_manager().send_jsonrpc_request(
                        "session/request_permission",
                        request_params,
                        session_id=session_id,
                    )
            except Exception as exc:
                logger.warning("[InterruptHelpers] ACP permission request failed: %s", exc)
                return None

            if not isinstance(response, dict):
                return None
            if isinstance(response.get("error"), dict):
                message = str(response["error"].get("message") or "Permission request failed")
                return PermissionConfirmResponse(
                    approved=False,
                    auto_confirm=False,
                    feedback=f"[PERMISSION_DENIED] {message}",
                )

            result_payload = response.get("result") if isinstance(response.get("result"), dict) else {}
            outcome = result_payload.get("outcome") if isinstance(result_payload.get("outcome"), dict) else {}
            outcome_kind = str(outcome.get("outcome") or "").strip().lower()
            option_id = str(outcome.get("optionId") or "").strip().lower()

            if outcome_kind == "selected":
                if option_id == "allow-once":
                    return PermissionConfirmResponse(approved=True, auto_confirm=False, feedback="")
                if option_id == "allow-always":
                    return PermissionConfirmResponse(approved=True, auto_confirm=True, feedback="")
                return PermissionConfirmResponse(
                    approved=False,
                    auto_confirm=False,
                    feedback="[PERMISSION_REJECTED] User rejected the request.",
                )

            if outcome_kind == "cancelled":
                return PermissionConfirmResponse(
                    approved=False,
                    auto_confirm=False,
                    feedback="[PERMISSION_REJECTED] Permission request was cancelled.",
                )
            return None

        async def _permission_scene_hook(
            inp: PermissionSceneHookInput,
        ) -> tuple[str, ...] | None:
            from jiuwenswarm.agents.harness.common.rails.permissions.owner_scopes import (
                TOOL_PERMISSION_CONTEXT,
                check_avatar_permission,
                _resolve_owner_scope_level,
            )

            perm_ctx = TOOL_PERMISSION_CONTEXT.get()

            # issue #1976: ask_user has its own dedicated interrupt rail. The
            # permission rail intercepts *every* tool, so on resume it grabs the
            # ask_user answer (keyed by tool_call_id) as its own user_input,
            # fails to parse it as a ConfirmPayload, and re-raises a permission
            # interrupt that swallows the answer — the option card then re-pops
            # forever. Bypass the permission rail for ask_user so the ask_user
            # rail's answer reaches the model. The digital-avatar scene below
            # intentionally blocks interactive tools, so exclude it here.
            if inp.normalized_tool_name == "ask_user" and (
                perm_ctx is None
                or getattr(perm_ctx, "scene", None) != "group_digital_avatar"
            ):
                return ("approve",)

            # deepresearch_execute owns a sequence of native AskUser
            # interruptions under one outer tool call.  Once its initial tool
            # permission has been decided, structured card answers must reach
            # DeepResearchExecutionRail instead of being parsed again as a
            # ConfirmPayload by the permission rail.  Keep first-pass and
            # explicit permission responses on the normal permission path.
            workflow_input = inp.user_input
            is_deepresearch_workflow = (
                inp.normalized_tool_name == "deepresearch_execute"
                and isinstance(workflow_input, dict)
            )
            if is_deepresearch_workflow:
                status = workflow_input.get("status")
                if status in {
                    "answered",
                    "skipped",
                    "cancelled",
                    "error",
                } and isinstance(workflow_input.get("answers"), list):
                    return ("approve",)

            if perm_ctx is None:
                return None

            if getattr(perm_ctx, "scene", None) == "group_digital_avatar":
                if inp.user_input is not None:
                    return ("reject", "[PERMISSION_DENIED] 数字分身场景不支持交互审批")
                level = await check_avatar_permission(
                    inp.normalized_tool_name,
                    inp.tool_args,
                    channel_id=str(getattr(perm_ctx, "channel_id", "") or ""),
                    session_id=None,
                )
                if level == "allow":
                    return ("approve",)
                return ("reject", "[PERMISSION_DENIED] 该工具未被授权在数字分身场景下使用")

            principal_user_id = str(getattr(perm_ctx, "principal_user_id", "") or "").strip()
            channel_id = str(getattr(perm_ctx, "channel_id", "") or "").strip()
            if not principal_user_id or not channel_id:
                return None

            perm_cfg = get_config()
            perm_all = perm_cfg.get("permissions") if isinstance(perm_cfg, dict) else {}
            owner_scopes = perm_all.get("owner_scopes") if isinstance(perm_all, dict) else None
            if not isinstance(owner_scopes, dict) or not owner_scopes:
                return None

            scope_cfg = (owner_scopes.get(channel_id) or {}).get(principal_user_id)
            owner_level = _resolve_owner_scope_level(
                scope_cfg, inp.normalized_tool_name, inp.tool_args
            )
            if owner_level is None:
                return None
            if owner_level == "allow":
                # 有 ACTIVE Skill overlay 时强制走 engine，避免 owner allow 绕过 overlay 收紧。
                if _has_active_skill_overlay():
                    return None
                return ("approve",)
            return ("reject", f"[PERMISSION_DENIED] 该工具未被授权 (owner_scopes: {owner_level})")

        def _get_permissions_snapshot():
            # 企业版 Agent 模板 rail：工具校验跑在 DeepAgent supervisor Task
            # （start_interaction 时 create_task），不会继承请求 Task 上的
            # PERMISSIONS_AGENT_BASE；若这里再走 get_effective_permissions_config()
            # 会回落 yaml（常为 enabled:false）并覆盖模板配置。
            # 返回 None → 使用 _static_config（请求开头 _update_permission_rail
            # 与 persist 的 update_config 会刷新它）。
            if is_enterprise() and config_source == "agent_template":
                return None
            return get_effective_permissions_config()

        host = ToolPermissionHost(
            get_permissions_snapshot=_get_permissions_snapshot,
            persist_allow_rule=_persist_allow_rule,
            resolve_workspace_dir=get_workspace_dir,
            permission_yaml_path=get_config_file(),
            request_permission_confirmation=_request_permission_confirmation,
            permission_scene_hook=_permission_scene_hook,
        )

        workspace_root = None
        try:
            workspace_root = get_workspace_dir()
        except Exception:
            logger.warning(
                "[InterruptHelpers] workspace_dir resolve failed for permission engine; "
                "continue with workspace_root=None (same as PermissionInterruptRail default)",
                exc_info=True,
            )
        from jiuwenswarm.agents.harness.common.rails.permissions.workspace_untrusted_policy import (
            WorkspaceUntrustedPolicyEngine,
        )

        # 不在此写死 trusted_dirs：与 PermissionInterruptRail 默认引擎一致，
        # 请求级白名单由 adapter 的 set_trusted_dirs → engine.update_trusted_dirs 注入。
        permission_engine = WorkspaceUntrustedPolicyEngine(
            config=permission_config,
            llm=llm,
            model_name=model_name,
            workspace_root=workspace_root,
        )

        # Skill 动态授权协调：默认权限 Rail 叠加 gate-handled 短路
        # （skill_tool/skill_complete 由 SkillAuthorizationRail 专属裁决，避免重复弹卡）。
        rail_cls = PermissionInterruptRail
        try:
            from jiuwenswarm.agents.harness.common.rails.permissions.skill_authorization_permission_rail import (
                SkillAuthorizationPermissionRail,
            )

            rail_cls = SkillAuthorizationPermissionRail
        except Exception:
            logger.debug(
                "[InterruptHelpers] SkillAuthorizationPermissionRail switch failed, "
                "fallback to PermissionInterruptRail",
                exc_info=True,
            )

        # skill_turbo 启用时使用子类，定制 skill_acceleration_exec 审批消息
        # （SkillTurboPermissionRail 继承 SkillAuthorizationPermissionRail，两种定制叠加）
        try:
            from jiuwenswarm.common.config import get_config as _get_cfg
            _cfg = _get_cfg()
            _react = _cfg.get("react", {}) if isinstance(_cfg, dict) else {}
            _st = _react.get("skill_turbo", {}) if isinstance(_react, dict) else {}
            if _st.get("enabled", False):
                from jiuwenswarm.server.runtime.skill_turbo.rails.permission_rail import (
                    SkillTurboPermissionRail,
                )
                rail_cls = SkillTurboPermissionRail
                logger.info("[InterruptHelpers] Using SkillTurboPermissionRail")
        except Exception:
            logger.debug(
                "[InterruptHelpers] SkillTurboPermissionRail switch failed, "
                "fallback to PermissionInterruptRail",
                exc_info=True,
            )

        permission_rail = rail_cls(
            config=permission_config,
            engine=permission_engine,
            tool_names=tool_names,
            llm=llm,
            model_name=model_name,
            host=host,
        )
        logger.info(
            "[InterruptHelpers] PermissionInterruptRail created successfully with tool_names=%s",
            tool_names
        )
    except Exception as exc:
        logger.warning("[InterruptHelpers] PermissionInterruptRail create failed: %s", exc)
        permission_rail = None
    return permission_rail



def _read_value_field(value_obj: Any, field_name: str, default: Any = "") -> Any:
    if hasattr(value_obj, field_name):
        return getattr(value_obj, field_name, default)
    if isinstance(value_obj, dict):
        return value_obj.get(field_name, default)
    return default


def _normalize_tool_args(raw: Any) -> dict | None:
    if raw is None:
        return None
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def _is_ask_user_interrupt_value(value_obj: Any) -> bool:
    tool_name = str(_read_value_field(value_obj, "tool_name", "") or "").strip()
    if tool_name == "ask_user":
        return True
    if hasattr(value_obj, "payload_schema") and hasattr(value_obj, "questions"):
        return True
    if isinstance(value_obj, dict) and "payload_schema" in value_obj and "questions" in value_obj:
        return True
    return False


def _build_plain_ask_user_question(value_obj: Any) -> dict | None:
    """Build a free-text ask_user question when no structured options are present."""
    if not _is_ask_user_interrupt_value(value_obj):
        return None
    if _extract_questions_from_value(value_obj) is not None:
        return None

    query = ""
    tool_args = _normalize_tool_args(_read_value_field(value_obj, "tool_args", None))
    if isinstance(tool_args, dict):
        query = str(tool_args.get("query") or "").strip()
    if not query:
        query = str(_read_value_field(value_obj, "message", "") or "").strip()
    if not query:
        query = str(_read_value_field(value_obj, "question", "") or "").strip()
    if not query:
        return None

    return {
        "question": query,
        "header": "Question",
        "options": [],
        "multi_select": False,
    }


_PERMISSION_INTERRUPT_MARKERS = (
    "需要授权才能执行",
    "requires permission",
    "Permission denied",
    "安全风险评估",
)
# exit_plan_mode uses PlanApprovalInterruptRail (extends ConfirmInterruptRail)
_CONFIRM_INTERRUPT_TOOLS = frozenset({"switch_mode", "exit_plan_mode"})  


def _read_interrupt_fields(value_obj: Any) -> tuple[str, str, dict | None]:
    """Return ``(tool_name, message, tool_args)`` from an interrupt value object."""
    tool_name = ""
    message = ""
    tool_args: dict | None = None

    if hasattr(value_obj, "tool_name"):
        tool_name = str(getattr(value_obj, "tool_name", "") or "").strip()
    if hasattr(value_obj, "message"):
        message = str(getattr(value_obj, "message", "") or "").strip()
    if not message and hasattr(value_obj, "question"):
        message = str(getattr(value_obj, "question", "") or "").strip()
    tool_args = _normalize_tool_args(getattr(value_obj, "tool_args", None))

    if isinstance(value_obj, dict):
        tool_name = tool_name or str(value_obj.get("tool_name", "") or "").strip()
        message = message or str(
            value_obj.get("message", "") or value_obj.get("question", "") or ""
        ).strip()
        if tool_args is None:
            tool_args = _normalize_tool_args(value_obj.get("tool_args"))

    return tool_name, message, tool_args


def _is_permission_interrupt_message(message: str, tool_name: str) -> bool:
    """Heuristic: PermissionInterruptRail copy vs ConfirmInterruptRail copy."""
    normalized = message.strip()
    if any(marker in normalized for marker in _PERMISSION_INTERRUPT_MARKERS):
        return True
    if normalized.startswith("**工具 `") or normalized.startswith("**Tool `"):
        return True
    if tool_name and tool_name not in _CONFIRM_INTERRUPT_TOOLS:
        return True
    if normalized in {"", "Please approve or reject?"}:
        return tool_name not in _CONFIRM_INTERRUPT_TOOLS
    return False


def _parse_plan_metadata_from_message(message: str) -> tuple[str, str]:
    plan_path = ""
    plan_slug = ""
    path_match = re.search(r"\*\*Plan file:\*\* `([^`]+)`", message)
    if path_match:
        plan_path = path_match.group(1).strip()
    slug_match = re.search(r"\*\*Plan id:\*\* `([^`]+)`", message)
    if slug_match:
        plan_slug = slug_match.group(1).strip()
    return plan_path, plan_slug


def _resolve_interrupt_source(tool_name: str, message: str) -> str:
    if _is_permission_interrupt_message(message, tool_name):
        return "permission_interrupt"
    return "confirm_interrupt"


def convert_interactions_to_ask_user_question(state_outputs: list) -> dict | None:
    """Convert __interaction__ list to frontend chat.ask_user_question format.

    AskUserRail 中断: value 有 questions 字段，或 ask_user 的 plain query
        → source="ask_user_interrupt"
    PermissionRail 中断: value 无 questions 字段 → source="permission_interrupt"
    ConfirmInterruptRail 中断: 控制类工具确认 → source="confirm_interrupt"

    state_outputs 中的元素可能是:
    - InteractionOutput 对象 (有 id, value 属性, value 是 ToolCallInterruptRequest)
    - dict (有 id, value 键)
    """
    if not state_outputs:
        return None

    interactions = list(_iter_interactions(state_outputs))
    if not interactions:
        return None

    # A controller output can contain both a permission interrupt shell and the
    # real ask_user interrupt. Prefer the structured ask_user payload; otherwise
    # the frontend may receive an empty permission prompt and have no request_id
    # to resume the waiting tool call.
    for interaction in interactions:
        request_id, value_obj = _extract_interaction_parts(interaction)
        if not request_id:
            continue

        questions_raw = _extract_questions_from_value(value_obj)
        if questions_raw is None:
            continue

        questions = _build_multi_questions(questions_raw)
        return {
            "event_type": "chat.ask_user_question",
            "request_id": request_id,
            "questions": questions,
            "source": "ask_user_interrupt",
        }

    for interaction in interactions:
        request_id, value_obj = _extract_interaction_parts(interaction)
        if not request_id:
            continue

        plain_question = _build_plain_ask_user_question(value_obj)
        if plain_question:
            return {
                "event_type": "chat.ask_user_question",
                "request_id": request_id,
                "questions": [plain_question],
                "source": "ask_user_interrupt",
            }

    for interaction in interactions:
        request_id, value_obj = _extract_interaction_parts(interaction)
        if not request_id:
            continue

        question_data = extract_question_from_interaction(interaction)
        if not question_data:
            continue

        tool_name, message, _tool_args = _read_interrupt_fields(value_obj)
        source = _resolve_interrupt_source(tool_name, message)

        payload = {
            "event_type": "chat.ask_user_question",
            "request_id": request_id,
            "questions": [question_data],
            "source": source,
        }
        # Skill 动态授权审批卡：随事件顶层透传给前端结构化渲染（契约键冻结）。
        payload_schema = _read_value_field(value_obj, "payload_schema", None)
        if isinstance(payload_schema, dict):
            from openjiuwen.harness.security.skill_authorization import (
                SKILL_APPROVAL_CARD_EXTENSION_KEY,
            )

            card = payload_schema.get(SKILL_APPROVAL_CARD_EXTENSION_KEY)
            if isinstance(card, dict):
                payload[SKILL_APPROVAL_CARD_EXTENSION_KEY] = card
        if (
            source == "confirm_interrupt"
            and tool_name == "exit_plan_mode"
            and is_plan_approval_message(message)
        ):
            plan_content, plan_language = extract_plan_approval_content(message)
            payload["plan_content"] = plan_content
            payload["plan_language"] = "en" if plan_language == "en" else "cn"
            payload["plan_approval_kind"] = "plan_approval"
        plan_path = str(question_data.get("plan_path") or "").strip()
        plan_slug = str(question_data.get("plan_slug") or "").strip()
        if plan_path:
            payload["plan_path"] = plan_path
        if plan_slug:
            payload["plan_slug"] = plan_slug
        structured_approval = _classify_structured_approval(value_obj, question_data)
        if structured_approval:
            payload.update(structured_approval)
        return payload

    return None


def _iter_interactions(state_outputs: list) -> Any:
    """Yield interaction objects, flattening nested interaction lists."""
    for interaction in state_outputs:
        if isinstance(interaction, (list, tuple)):
            yield from _iter_interactions(list(interaction))
        else:
            yield interaction


def _extract_interaction_parts(interaction: Any) -> tuple[str, Any]:
    """Return ``(request_id, value)`` for dict or InteractionOutput-like objects."""
    if hasattr(interaction, "id"):
        request_id = getattr(interaction, "id", "")
        value_obj = interaction.value
    elif isinstance(interaction, dict):
        request_id = interaction.get("id", "")
        value_obj = interaction.get("value", {})
    else:
        return "", None

    return str(request_id or "").strip(), value_obj


def _extract_questions_from_value(value_obj: Any) -> list | None:
    """从 value 对象中提取 questions 列表.

    AskUserRail 的 value (ToolCallInterruptRequest) 有 questions 属性.
    如果 questions 存在且非空, 返回列表; 否则返回 None 表示不是 AskUserRail 中断.

    Additional source: StructuredAskUserRail puts `questions` in the tool call
    arguments, which are preserved in ToolCallInterruptRequest.tool_args.
    """
    # 1. Direct questions attribute on value_obj
    if hasattr(value_obj, 'questions'):
        qs = value_obj.questions
        if qs and len(qs) > 0:
            return qs
    elif isinstance(value_obj, dict):
        qs = value_obj.get("questions", [])
        if qs and len(qs) > 0:
            return qs

    # 2. questions embedded in tool_args (StructuredAskUserRail path)
    # ToolCallInterruptRequest.tool_args preserves the original tool call
    # arguments, including the `questions` parameter.
    tool_args = getattr(value_obj, 'tool_args', None)
    if tool_args is not None:
        if isinstance(tool_args, str):
            try:
                tool_args = json.loads(tool_args)
            except (ValueError, TypeError):
                pass
        if isinstance(tool_args, dict):
            qs = tool_args.get("questions", [])
            if qs and len(qs) > 0:
                return qs

    return None


def _build_multi_questions(questions_data: list) -> list:
    """Build frontend PendingQuestionItem list from questions data.

    有选项的问题: 保留原始选项 + 追加 __other__ (自定义输入)
    无选项的问题: 不追加 __other__, 前端应直接进入自由输入模式
    """
    questions = []
    for q in questions_data:
        raw_options = q.get("options", [])
        # Non-array options (e.g. "a,b") must not be iterated as characters (#2331).
        if not isinstance(raw_options, list):
            raw_options = []
        if raw_options:
            options = [_normalize_question_option(opt) for opt in raw_options if isinstance(opt, dict)]
            options.append({"label": "Other", "description": "Custom input"})
        else:
            options = []
        question_payload = {
            "question": q["question"],
            "header": q.get("header") or "Question",
            "options": options,
            "multi_select": q.get("multi_select", False),
        }
        preview = _normalize_question_preview(q.get("preview"))
        if preview is not None:
            question_payload["preview"] = preview
        questions.append(question_payload)
    return questions


def _extract_ui_options(value_obj: Any) -> list[dict[str, Any]]:
    options = getattr(value_obj, "ui_options", None) if hasattr(value_obj, "ui_options") else None
    if options is None and isinstance(value_obj, dict):
        options = value_obj.get("ui_options")
    return [item for item in options or [] if isinstance(item, dict)]


def _extract_tool_name(value_obj: Any) -> str:
    if hasattr(value_obj, "tool_name"):
        return str(getattr(value_obj, "tool_name", "") or "")
    if isinstance(value_obj, dict):
        return str(value_obj.get("tool_name") or "")
    return ""


def _extract_interrupt_metadata(value_obj: Any) -> dict[str, Any]:
    metadata = getattr(value_obj, "metadata", None)
    if metadata is None and isinstance(value_obj, dict):
        metadata = value_obj.get("metadata")
    return dict(metadata) if isinstance(metadata, dict) else {}


def _normalize_question_option(option: dict[str, Any]) -> dict[str, Any]:
    normalized = {
        "label": str(option.get("label") or option.get("value") or "").strip(),
        "description": str(option.get("description") or "").strip(),
    }
    value = option.get("value")
    if isinstance(value, str) and value:
        normalized["value"] = value
    preview = option.get("preview")
    if isinstance(preview, str) and preview.strip():
        normalized["preview"] = preview
    return normalized


def _normalize_question_preview(preview: Any) -> dict[str, Any] | None:
    if not isinstance(preview, dict):
        return None
    text = preview.get("text")
    if not isinstance(text, str) or not text.strip():
        return None

    normalized: dict[str, Any] = {"text": text}
    for field in ("title", "outline_ref"):
        value = preview.get(field)
        if isinstance(value, str):
            normalized[field] = value
    if preview.get("format") == "markdown":
        normalized["format"] = "markdown"
    editable = preview.get("editable")
    if isinstance(editable, bool):
        normalized["editable"] = editable
    meta = preview.get("meta")
    if isinstance(meta, dict):
        normalized["meta"] = dict(meta)
    return normalized


_PERMANENT_REMEMBER_LABELS = frozenset({
    "永久记住",
    "总是允许",  # OfficeClaw PermissionBridge global scope 回传标签
    "Always Allow",
    "always_allow",
    "allow_always",
})

_PERMANENT_REMEMBER_HINT_RE = re.compile(
    r"[；;]?\s*选择「永久记住」[^。\n]*[。.]?",
)


def _default_interrupt_options() -> list[dict[str, str]]:
    """权限审批默认选项。

    企业版不提供「永久记住」：该能力依赖写回本地 config.yaml / session overlay，
    与 Agent permissions 模板权威源不一致，且当前 supervisor Task 下 session_id
    常丢失导致落盘失败。长期策略请改 Manager permissions 模板。
    """
    options = [
        {"label": "本次允许", "description": "仅本次授权执行"},
        {"label": "会话内记住", "description": "本次会话内自动放行同类操作"},
        {"label": "拒绝", "description": "拒绝执行此工具"},
    ]
    if not is_enterprise():
        options.insert(
            2,
            {"label": "永久记住", "description": "写回磁盘，所有会话均自动放行"},
        )
    return options


def _is_permanent_remember_option(option: dict[str, Any]) -> bool:
    label = str(option.get("label") or "").strip()
    value = str(option.get("value") or "").strip()
    return label in _PERMANENT_REMEMBER_LABELS or value in _PERMANENT_REMEMBER_LABELS


def _strip_permanent_remember_hint(message: str) -> str:
    cleaned = _PERMANENT_REMEMBER_HINT_RE.sub("", message or "")
    return cleaned.strip()


def _plan_approval_interrupt_options(
    source: str,
    tool_name: str,
    message: str,
) -> list[dict[str, str]] | None:
    if not (
        source == "confirm_interrupt"
        and tool_name == "exit_plan_mode"
        and is_plan_approval_message(message)
    ):
        return None
    return build_plan_approval_options_from_message(message)


def _question_options_from_ui_options(
    value_obj: Any,
    source: str,
    tool_name: str,
    message: str,
) -> list[dict[str, Any]]:
    options = []
    for option in _extract_ui_options(value_obj):
        normalized = _normalize_question_option(option)
        if normalized["label"]:
            options.append(normalized)
    if options:
        # 仅过滤权限审批弹窗；技能演进等仍可保留「总是允许」。
        if is_enterprise() and source == "permission_interrupt":
            options = [
                option for option in options
                if not _is_permanent_remember_option(option)
            ]
        return options
    return _plan_approval_interrupt_options(source, tool_name, message) or _default_interrupt_options()


def _classify_structured_approval(
    value_obj: Any,
    question_data: dict[str, Any],
) -> dict[str, Any] | None:
    del question_data
    metadata = _extract_interrupt_metadata(value_obj)
    source = str(metadata.get("source") or "").strip()
    interrupt_kind = str(metadata.get("interrupt_kind") or "").strip()
    tool_name = _extract_tool_name(value_obj)

    is_evolution_interrupt = (
        source in EVOLUTION_INTERRUPT_METADATA_SOURCES
        or interrupt_kind == LEGACY_SKILL_EVOLUTION_APPROVAL_SOURCE
    )
    if not is_evolution_interrupt and tool_name not in SKILL_EVOLUTION_APPROVAL_TOOL_KINDS:
        return None
    approval_kind = str(metadata.get("approval_kind") or "").strip()
    if approval_kind not in {"evolve", "simplify"}:
        approval_kind = SKILL_EVOLUTION_APPROVAL_TOOL_KINDS.get(tool_name, "evolve")

    payload: dict[str, Any] = {
        "source": EVOLUTION_INTERRUPT_SOURCE,
        "approval_kind": approval_kind,
    }
    evolution_context = str(metadata.get("evolution_context") or "").strip()
    if evolution_context in {"agent", "team"}:
        payload["evolution_context"] = evolution_context
    return payload


def extract_question_from_interaction(payload: Any) -> dict | None:
    """Extract question info from a single interaction payload.

    Args:
        payload: InteractionOutput instance or dict

    Returns:
        Question format dict for frontend
    """
    if payload is None:
        return None

    if hasattr(payload, "value"):
        value_obj = payload.value
    elif isinstance(payload, dict):
        value_obj = payload.get("value", payload)
    else:
        return None

    tool_name, message, tool_args = _read_interrupt_fields(value_obj)
    source = _resolve_interrupt_source(tool_name, message)

    generic_confirm_message = message.strip() in {"", "Please approve or reject?"}
    needs_message = not message or (source == "confirm_interrupt" and generic_confirm_message)
    if tool_name and needs_message:
        if source == "confirm_interrupt":
            from jiuwenswarm.agents.harness.code.rails.code_confirm_interrupt_rail import (
                build_confirm_interrupt_message,
            )

            message = build_confirm_interrupt_message(tool_name, tool_args or {})
        elif not message:
            message = f"工具 `{tool_name}` 需要授权才能执行"

    plan_approval_options = _plan_approval_interrupt_options(source, tool_name, message)
    if plan_approval_options:
        header = "Exit Plan and Execute"
        question = strip_inline_plan_approval_choices(message)
    elif source == "confirm_interrupt":
        header = f"操作确认: {tool_name}" if tool_name else "操作确认"
        question = message
    else:
        header = f"权限审批: {tool_name}" if tool_name else "权限审批"
        question = message
        if is_enterprise() and source == "permission_interrupt":
            question = _strip_permanent_remember_hint(question)

    return {
        "question": question,
        "header": header,
        "options": _question_options_from_ui_options(value_obj, source, tool_name, message),
        "multi_select": False,
    }

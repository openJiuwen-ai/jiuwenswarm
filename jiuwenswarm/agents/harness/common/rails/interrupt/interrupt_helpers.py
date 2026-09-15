# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Interrupt helpers for DeepAgent.

Provides utilities for converting interrupt payloads to frontend format
and building permission rails.
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any, Mapping

from collections.abc import Callable
from copy import deepcopy

from jiuwenswarm.agents.harness.code.prompt.plan_approval import (
    build_plan_approval_actions,
)
from jiuwenswarm.agents.harness.code.rails.code_plan_approval_interrupt_rail import (
    build_plan_approval_options_from_message,
    extract_plan_approval_content,
    is_plan_approval_message,
    strip_inline_plan_approval_choices,
)
from jiuwenswarm.agents.harness.common.rails.interrupt.permission_options import (
    ALLOW_ONCE,
    ALWAYS_ALLOW,
    REJECT,
    SESSION_ALLOW,
)
from jiuwenswarm.agents.harness.common.rails.permissions.tool_invocation_key import (
    ToolInvocationKeyV1,
)
from jiuwenswarm.agents.harness.common.rails.permissions.root_permission_queue import (
    RootPermissionCard,
    RootPermissionQueue,
)
from jiuwenswarm.agents.harness.common.rails.permissions.tool_capabilities import (
    install_permission_file_semantics,
)
from jiuwenswarm.agents.harness.common.rails.permissions.reviewer_redaction import (
    redact_secret_values,
    sanitize_permission_ui_payload,
)
from jiuwenswarm.common.utils import logger

SKILL_EVOLUTION_APPROVAL_SCHEMA = "openjiuwen.skill_evolution_approval.v1"
EVOLUTION_INTERRUPT_SOURCE = "evolution_interrupt"
LEGACY_SKILL_EVOLUTION_APPROVAL_SOURCE = "skill_evolution_approval"
INTERRUPT_RESUME_SOURCES = frozenset(
    {
        "permission_interrupt",
        "confirm_interrupt",
        "ask_user_interrupt",
        EVOLUTION_INTERRUPT_SOURCE,
    }
)
EVOLUTION_INTERRUPT_METADATA_SOURCES = frozenset(
    {
        EVOLUTION_INTERRUPT_SOURCE,
        LEGACY_SKILL_EVOLUTION_APPROVAL_SOURCE,
    }
)
SKILL_EVOLUTION_APPROVAL_TOOL_KINDS = {
    "evolve_skill_experiences": "evolve",
    "simplify_skill_experiences": "simplify",
}

_AUTO_REVIEWER_UI_METADATA_KEYS = (
    "action_summary",
    "contract_gate_missing_evidence",
    "decision_source",
    "evidence_summary",
    "fallback_reason",
    "final_reviewer_status",
    "manual_reason_code",
    "manual_reason_summary",
    "remaining_forbidden_actions",
    "reviewer_status",
    "risk_level",
    "user_authorization",
    "user_review_hint",
)
_AUTO_REVIEWER_UI_LIST_METADATA_KEYS = frozenset(
    {"contract_gate_missing_evidence", "remaining_forbidden_actions"}
)
_AUTO_REVIEWER_UI_MAX_LIST_ITEMS = 8
_AUTO_REVIEWER_UI_MAX_TEXT_LENGTH = 512


def resolve_permission_workspace_dir(session_id: str | None = None) -> Path:
    """Default file_guard workspace: session task dir, not the whole agent workspace."""
    from jiuwenswarm.common.utils import get_default_project_session_workspace_dir

    return get_default_project_session_workspace_dir(session_id)


def merge_permission_trusted_dirs(
    trusted_dirs: list[str] | None = None,
    project_dir: str | None = None,
) -> list[str]:
    """Merge request trusted_dirs with frontend project_dir for file_guard."""
    merged: list[str] = []
    seen: set[str] = set()

    def _add(raw: Any) -> None:
        text = str(raw or "").strip()
        if not text:
            return
        try:
            key = str(Path(text).expanduser().resolve(strict=False))
        except (OSError, RuntimeError, ValueError):
            key = text
        folded = key.casefold()
        if folded in seen:
            return
        seen.add(folded)
        merged.append(key)

    if isinstance(trusted_dirs, (list, tuple)):
        for item in trusted_dirs:
            _add(item)
    _add(project_dir)
    return merged


def apply_permission_trusted_dirs(
    rail: Any,
    *,
    trusted_dirs: list[str] | None = None,
    project_dir: str | None = None,
) -> None:
    """Hot-update file_guard trusted prefixes: client dirs plus project_dir."""
    setter = getattr(rail, "set_trusted_dirs", None)
    if not callable(setter):
        return
    setter(merge_permission_trusted_dirs(trusted_dirs, project_dir))


def has_interrupt_resume_payload(params: Any) -> bool:
    if not isinstance(params, dict):
        return False
    if not str(params.get("request_id") or "").strip():
        return False
    answers = params.get("answers")
    return isinstance(answers, list) and bool(answers)


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


def build_permission_rail(
    config: dict[str, Any],
    llm: Any = None,
    model_name: str | None = None,
    session_id: str | None = None,

    *,
    enable_auto_permission: bool = False,
    installed_permissions: dict[str, Any] | None = None,
    workspace_root: Any = None,
    platform_trusted_root: Any = None,
    sys_operation: Any = None,
    permissions_changed_notifier: Callable[[], None] | None = None,
    browser_runtime_security_profile: Any = None,
    trusted_search_urls: Any = None,
) -> Any | None:
    """Build openjiuwen PermissionInterruptRail for tool permission checks.

    Args:
        config: Agent config dict containing permissions section
        llm: LLM instance for risk assessment
        model_name: Model name for risk assessment
        session_id: Host identity for User/Session compose and persist
        installed_permissions: Complete installed snapshot for explicit auto mode

    Returns:
        PermissionInterruptRail instance or None if disabled
    """
    from openjiuwen.harness.security import (
        PermissionConfirmationRequest,
        PermissionConfirmResponse,
        PermissionSceneHookInput,
        ToolPermissionHost,
        build_permission_interrupt_rail,
    )

    from jiuwenswarm.agents.harness.common.rails.permissions.permission_compose import (
        compose_host_effective_permissions,
    )
    from jiuwenswarm.agents.harness.common.rails.permissions.permission_interrupt_rail import (
        JiuwenSwarmPermissionInterruptRail,
    )
    from jiuwenswarm.agents.harness.common.rails.permissions.permissions_layers import (
        load_session_permissions,
        load_user_permissions,
        persist_session_overlay_from_effective,
        persist_user_overlay_from_effective,
    )
    from jiuwenswarm.agents.harness.common.rails.permissions.tool_permission_context import (
        SKILLS_REBUILD_SILENT,
        TOOL_PERMISSION_CHANNEL_ID,
    )
    from jiuwenswarm.agents.harness.common.rails.permissions.auto_config import (
        is_auto_permission_enabled,
        normalize_permissions_for_runtime,
    )
    from jiuwenswarm.common.config import get_config
    from jiuwenswarm.common.e2a.acp.acp_tool_updates import build_acp_tool_descriptor
    from jiuwenswarm.common.utils import get_config_file

    inline_permissions = config.get("permissions", {})
    if not isinstance(inline_permissions, dict):
        inline_permissions = {}
    if installed_permissions is not None:
        if not isinstance(installed_permissions, dict):
            raise TypeError("installed_permissions_must_be_dict")
        if not enable_auto_permission:
            raise ValueError("installed_permissions_requires_auto_permission")
        if not is_auto_permission_enabled(installed_permissions):
            raise ValueError("installed_permissions_requires_enabled_auto_mode")
    if enable_auto_permission and not is_auto_permission_enabled(inline_permissions):
        raise ValueError("auto_permission_activation_requires_enabled_auto_mode")
    logger.info(
        "[InterruptHelpers] build_permission_rail called: enabled=%s session_id=%s",
        inline_permissions.get("enabled", False),
        session_id,
    )

    if not inline_permissions.get("enabled", False):
        logger.info("[InterruptHelpers] Permission system is disabled, returning None")
        return None

    bound_session_id = str(session_id).strip() if session_id else None
    permission_config = (
        normalize_permissions_for_runtime(deepcopy(installed_permissions))
        if installed_permissions is not None
        else compose_host_effective_permissions(
            global_permissions=inline_permissions,
            user_permissions=load_user_permissions(),
            session_permissions=load_session_permissions(bound_session_id),
            session_id=bound_session_id,
        )
    )
    if enable_auto_permission:
        install_permission_file_semantics()

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
        tool_names,
        llm is not None,
        model_name,
    )
    try:
        def _effective_session_id(session_id: str | None = None) -> str | None:
            sid = (session_id or "").strip()
            return sid or bound_session_id

        def _persist_allow_rule(
            permissions: dict[str, Any], session_id: str | None = None
        ) -> bool:
            try:
                return persist_user_overlay_from_effective(
                    permissions, session_id=_effective_session_id(session_id)
                )
            except Exception as exc:
                logger.warning("[InterruptHelpers] persist_allow_rule failed: %s", exc)
                return False

        def _persist_session_allow_rule(
            permissions: dict[str, Any], session_id: str | None = None
        ) -> bool:
            sid = _effective_session_id(session_id)
            if not sid:
                logger.warning("[InterruptHelpers] persist_session_allow_rule skipped: no session_id")
                return False
            try:
                return persist_session_overlay_from_effective(sid, permissions)
            except Exception as exc:
                logger.warning("[InterruptHelpers] persist_session_allow_rule failed: %s", exc)
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
            # skills.rebuild 静默 follow-up 使用临时 session，无法弹 UI 审批；
            # 若返回 "interrupt"，Agent 会停在 bash/write 上却仍被当成重建成功。
            if SKILLS_REBUILD_SILENT.get():
                tool_name = getattr(getattr(req, "tool_call", None), "name", "") or ""
                logger.info(
                    "[InterruptHelpers] auto-approve permission for silent skills.rebuild tool=%s",
                    tool_name,
                )
                return PermissionConfirmResponse(
                    approved=True,
                    auto_confirm=False,
                    feedback="",
                )

            channel = TOOL_PERMISSION_CHANNEL_ID.get() or "web"
            if channel != "acp":
                return "interrupt"

            session_id = _resolve_session_id(req.ctx)
            if not session_id:
                return None

            from jiuwenswarm.agents.harness.common.tools.acp_output_tools import (
                get_acp_output_manager,
            )

            tool_call = req.tool_call
            tool_name = getattr(tool_call, "name", "") if tool_call is not None else ""
            tool_args_raw = (
                getattr(tool_call, "arguments", None) if tool_call is not None else None
            )
            tool_call_id = str(
                getattr(tool_call, "id", "") or f"permission_{tool_name or 'tool'}"
            ).strip()
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
                    {
                        "optionId": "allow-once",
                        "name": "Allow once",
                        "kind": "allow_once",
                    },
                    {
                        "optionId": "allow-always",
                        "name": "Always allow",
                        "kind": "allow_always",
                    },
                    {
                        "optionId": "reject-once",
                        "name": "Reject",
                        "kind": "reject_once",
                    },
                ],
            }

            try:
                response = await get_acp_output_manager().send_jsonrpc_request(
                    "session/request_permission",
                    request_params,
                    session_id=session_id,
                )
            except Exception as exc:
                logger.warning(
                    "[InterruptHelpers] ACP permission request failed: %s", exc
                )
                return None

            if not isinstance(response, dict):
                return None
            if isinstance(response.get("error"), dict):
                message = str(
                    response["error"].get("message") or "Permission request failed"
                )
                return PermissionConfirmResponse(
                    approved=False,
                    auto_confirm=False,
                    feedback=f"[PERMISSION_DENIED] {message}",
                )

            result_payload = (
                response.get("result")
                if isinstance(response.get("result"), dict)
                else {}
            )
            outcome = (
                result_payload.get("outcome")
                if isinstance(result_payload.get("outcome"), dict)
                else {}
            )
            outcome_kind = str(outcome.get("outcome") or "").strip().lower()
            option_id = str(outcome.get("optionId") or "").strip().lower()

            if outcome_kind == "selected":
                if option_id == "allow-once":
                    return PermissionConfirmResponse(
                        approved=True, auto_confirm=False, feedback=""
                    )
                if option_id == "allow-always":
                    return PermissionConfirmResponse(
                        approved=True, auto_confirm=True, feedback=""
                    )
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

        def _is_silent_skills_rebuild_session() -> bool:
            return bool(SKILLS_REBUILD_SILENT.get())

        async def _permission_scene_hook(
            inp: PermissionSceneHookInput,
        ) -> tuple[str, ...] | None:
            from jiuwenswarm.agents.harness.common.rails.permissions.owner_scopes import (
                TOOL_PERMISSION_CONTEXT,
                check_avatar_permission,
                _resolve_owner_scope_level,
            )

            # skills.rebuild 静默 Agent 无法弹权限卡片；在 tiered 判定前直接放行。
            if _is_silent_skills_rebuild_session():
                return ("approve",)

            perm_ctx = TOOL_PERMISSION_CONTEXT.get()

            # ask_user is an interactive control action owned by its dedicated
            # rail, not a Permission decision. Non-Permission continuation
            # routing is handled separately at the Host callback boundary; this
            # branch only preserves the initial-call ownership contract. The
            # digital-avatar scene below intentionally blocks interactive tools.
            if inp.normalized_tool_name == "ask_user" and (
                perm_ctx is None
                or getattr(perm_ctx, "scene", None) != "group_digital_avatar"
            ):
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
                    **({
                        "permission_config": _get_installed_permissions(),
                        "use_installed_permissions": True,
                        "installed_engine": getattr(inp, "engine", None),
                    } if enable_auto_permission else {}),
                )
                if level == "allow":
                    return ("approve",)
                return (
                    "reject",
                    "[PERMISSION_DENIED] 该工具未被授权在数字分身场景下使用",
                )

            principal_user_id = str(
                getattr(perm_ctx, "principal_user_id", "") or ""
            ).strip()
            channel_id = str(getattr(perm_ctx, "channel_id", "") or "").strip()
            if not principal_user_id or not channel_id:
                return None

            perm_all = _permission_scene_config()
            owner_scopes = (
                perm_all.get("owner_scopes") if isinstance(perm_all, dict) else None
            )
            if not isinstance(owner_scopes, dict) or not owner_scopes:
                return None

            scope_cfg = (owner_scopes.get(channel_id) or {}).get(principal_user_id)
            owner_level = _resolve_owner_scope_level(
                scope_cfg, inp.normalized_tool_name, inp.tool_args
            )
            if owner_level is None:
                return None
            if owner_level == "allow":
                return ("approve",)
            return (
                "reject",
                f"[PERMISSION_DENIED] 该工具未被授权 (owner_scopes: {owner_level})",
            )

        installed_permission_rail: Any | None = None

        def _get_installed_permissions(session_id: str | None = None) -> dict[str, Any]:
            """Expose only the policy committed by the session adapter.

            Disk persistence and runtime installation are deliberately separate:
            config writes schedule a lazy reload, while a tool callback must keep
            using the policy epoch installed for its current logical turn.
            """

            # skills.rebuild is an internal, control-silent follow-up with no UI
            # approval surface. Preserve develop's full-access override while
            # keeping ordinary calls pinned to the adapter-installed policy epoch.
            if SKILLS_REBUILD_SILENT.get():
                return {
                    "enabled": True,
                    "defaults": {"*": "allow"},
                    "file_guard": {"enabled": False},
                }
            if not enable_auto_permission:
                sid = _effective_session_id(session_id)
                return compose_host_effective_permissions(
                    global_permissions=inline_permissions,
                    user_permissions=load_user_permissions(),
                    session_permissions=load_session_permissions(sid),
                    session_id=sid,
                )
            rail = installed_permission_rail
            getter = getattr(rail, "installed_permission_config", None)
            if callable(getter):
                installed = getter()
                return installed if isinstance(installed, dict) else {}
            return deepcopy(permission_config)

        def _permission_scene_config() -> dict[str, Any]:
            if enable_auto_permission:
                return _get_installed_permissions()
            current_config = get_config()
            return current_config.get("permissions", {}) if isinstance(current_config, dict) else {}

        def _persist_exact_allow_rule(
            normalized_name: str,
            tool_args: dict[str, Any],
            ask_accesses: tuple[tuple[str, str], ...],
        ) -> bool:
            from jiuwenswarm.agents.harness.common.rails.permissions.permissions_persist import (
                persist_exact_permission_allow_rule,
            )

            persisted = persist_exact_permission_allow_rule(
                normalized_name,
                tool_args,
                ask_accesses,
                session_id=bound_session_id,
                workspace_root=effective_workspace_root,
            )
            if persisted and permissions_changed_notifier is not None:
                try:
                    permissions_changed_notifier()
                except Exception:
                    logger.exception(
                        "[InterruptHelpers] permissions reload notification failed"
                    )
            return persisted

        effective_workspace_root = (
            workspace_root if enable_auto_permission and workspace_root is not None
            else resolve_permission_workspace_dir(bound_session_id)
        )
        host = ToolPermissionHost(
            get_permissions_snapshot=_get_installed_permissions,
            persist_allow_rule=_persist_allow_rule,
            persist_session_allow_rule=_persist_session_allow_rule,
            resolve_workspace_dir=lambda: effective_workspace_root,
            permission_yaml_path=get_config_file(),
            request_permission_confirmation=_request_permission_confirmation,
            permission_scene_hook=_permission_scene_hook,
        )

        if enable_auto_permission:
            permission_rail = JiuwenSwarmPermissionInterruptRail(
                config=permission_config,
                tool_names=tool_names,
                llm=llm,
                model_name=model_name,
                host=host,
                exact_persist_callback=_persist_exact_allow_rule,
            )
        else:
            permission_rail = build_permission_interrupt_rail(
                permissions=permission_config,
                llm=llm,
                model_name=model_name,
                host=host,
            )
        if enable_auto_permission:
            from jiuwenswarm.agents.harness.common.rails.permissions.auto_config import (
                normalize_auto_permission_options,
            )
            from jiuwenswarm.agents.harness.common.rails.permissions.auto_permission_rail import (
                AutoPermissionInterruptRail,
            )
            from jiuwenswarm.agents.harness.common.rails.permissions.auto_reviewer import (
                AutoReviewer,
                IsolatedModelReviewerClient,
                build_isolated_reviewer_model,
            )
            from jiuwenswarm.agents.harness.common.rails.permissions.persistent_audit import (
                PersistentAuditWriter,
                resolve_persistent_audit_root,
            )

            auto_options = normalize_auto_permission_options(
                permission_config.get("auto")
            )
            auto_reviewer = None
            if llm is not None:
                reviewer_model = build_isolated_reviewer_model(llm)
                if reviewer_model is not None:
                    auto_reviewer = AutoReviewer(
                        client=IsolatedModelReviewerClient(
                            model=reviewer_model,
                            display_language_getter=lambda: get_config().get(
                                "preferred_language", "zh"
                            ),
                        ),
                        timeout_ms=auto_options["reviewer_timeout_ms"],
                        min_confidence=auto_options["reviewer_min_confidence"],
                    )
            persistent_audit_writer = None
            if auto_options["persistent_audit_enabled"]:
                persistent_audit_writer = PersistentAuditWriter(
                    data_root=resolve_persistent_audit_root(permission_config)
                )
            permission_rail = AutoPermissionInterruptRail(
                base_rail=permission_rail,
                permission_config=permission_config,
                workspace_root=effective_workspace_root,
                platform_trusted_root=platform_trusted_root,
                sys_operation=sys_operation,
                auto_reviewer=auto_reviewer,
                persistent_audit_writer=persistent_audit_writer,
                exact_permission_persist_callback=_persist_exact_allow_rule,
                browser_runtime_security_profile=browser_runtime_security_profile,
                trusted_search_urls=trusted_search_urls,
            )
            permission_rail.set_trusted_dirs(None)

        installed_permission_rail = permission_rail
        logger.info(
            "[InterruptHelpers] %s created successfully with tool_names=%s",
            type(permission_rail).__name__,
            tool_names,
        )
    except Exception as exc:
        if not enable_auto_permission:
            logger.warning("[InterruptHelpers] PermissionInterruptRail create failed: %s", exc)
            return None
        logger.exception(
            "[InterruptHelpers] PermissionInterruptRail create failed: %s", exc
        )
        raise
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
    # Prefer the explicit tool identity whenever it is available.  Many tools
    # (for example memory_search) have a plain ``query`` argument, so treating
    # every query-only interrupt as ask_user misroutes permission responses.
    if tool_name:
        return tool_name == "ask_user"

    # Legacy ask_user interrupt payloads may not carry tool_name.  Keep the
    # structural fallbacks below only for those identity-less payloads.
    if hasattr(value_obj, "payload_schema") and hasattr(value_obj, "questions"):
        return True
    if (
        isinstance(value_obj, dict)
        and "payload_schema" in value_obj
        and "questions" in value_obj
    ):
        return True
    return False


def _is_explicit_ask_user_shell(value_obj: Any) -> bool:
    return (
        str(_read_value_field(value_obj, "tool_name", "") or "").strip() == "ask_user"
    )


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
    "需要授权后才能使用",
    "检测到受保护的文件路径访问",
    "检测到需确认的网络访问",
    "检测到需确认的命令执行",
    "检测到风险命令结构",
    "操作需要授权",
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
        message = (
            message
            or str(
                value_obj.get("message", "") or value_obj.get("question", "") or ""
            ).strip()
        )
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


def convert_interactions_to_ask_user_question(
    state_outputs: list,
    *,
    root_permission_queue: RootPermissionQueue | None = None,
) -> dict | None:
    """Convert __interaction__ list to frontend chat.ask_user_question format.

    AskUserRail 中断: value 有 questions 字段，或明确 ask_user 的 plain query
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
    locator_rows = [
        (
            interaction,
            *_tool_invocation_locator_from_interaction(
                interaction,
                _extract_interaction_parts(interaction)[1],
                root_permission_queue=root_permission_queue,
            ),
        )
        for interaction in interactions
    ]
    if any(state == "invalid" for _item, state, _record in locator_rows):
        return None
    live_interactions = [
        item for item, state, _record in locator_rows if state == "live"
    ]
    if live_interactions:
        absent_interactions = [
            item for item, state, _record in locator_rows if state == "absent"
        ]
        if any(
            not _is_explicit_ask_user_shell(_extract_interaction_parts(item)[1])
            for item in absent_interactions
        ):
            return None
        interactions = live_interactions
        if len(live_interactions) != 1:
            return None

    # Without a live permission locator, retain the existing ask_user projection.
    # A live host-owned locator takes priority and cannot be reclassified by a
    # query-shaped payload or a parallel ask_user shell.
    if not live_interactions:
        for interaction in interactions:
            request_id, value_obj = _extract_interaction_parts(interaction)
            if not request_id:
                continue

            questions_raw = _extract_questions_from_value(value_obj)
            if questions_raw is None:
                continue

            questions = _build_multi_questions(questions_raw, _extract_ask_user_query(value_obj))
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

    # Only the Host-bound Smart queue owns the single-card contract. Ordinary
    # SDK permission batches keep develop's first-question projection.
    if root_permission_queue is not None and len(interactions) > 1 and any(
        _is_permission_interaction(item) for item in interactions
    ):
        return None

    for interaction in interactions:
        request_id, value_obj = _extract_interaction_parts(interaction)
        if not request_id:
            continue

        question_data = extract_question_from_interaction(
            interaction,
            root_permission_queue=root_permission_queue,
        )
        if not question_data:
            continue

        tool_name, message, _tool_args = _read_interrupt_fields(value_obj)
        has_permission_locator = "card_id" in question_data
        source = (
            "permission_interrupt"
            if has_permission_locator
            else _resolve_interrupt_source(tool_name, message)
        )
        structured_approval = (
            None
            if has_permission_locator
            else _classify_structured_approval(value_obj, question_data)
        )
        unbound_permission = source == "permission_interrupt" and not has_permission_locator
        if (
            root_permission_queue is not None
            and unbound_permission
            and structured_approval is None
        ):
            return None

        payload = {
            "event_type": "chat.ask_user_question",
            "request_id": request_id,
            "questions": [question_data],
            "source": source,
        }
        if (
            source == "confirm_interrupt"
            and tool_name == "exit_plan_mode"
            and is_plan_approval_message(message)
        ):
            plan_content, plan_language = extract_plan_approval_content(message)
            resolved_plan_language = "en" if plan_language == "en" else "cn"
            payload["plan_content"] = plan_content
            payload["plan_language"] = resolved_plan_language
            payload["plan_approval_kind"] = "plan_approval"
            # Web 用的动作说明。TUI 忽略该字段，继续使用 questions[].options 的
            # approve / reject，因此两端行为互不影响。
            payload["plan_actions"] = build_plan_approval_actions(
                resolved_plan_language
            )
        plan_path = str(question_data.get("plan_path") or "").strip()
        plan_slug = str(question_data.get("plan_slug") or "").strip()
        if plan_path:
            payload["plan_path"] = plan_path
        if plan_slug:
            payload["plan_slug"] = plan_slug
        if structured_approval:
            payload.update(structured_approval)
        return payload

    return None


def build_verified_permission_ask_user_question(
    interaction: Any,
    card: RootPermissionCard,
) -> dict[str, Any] | None:
    """Render one queue-verified permission card."""

    request_id, value_obj = _extract_interaction_parts(interaction)
    if request_id != card.key.tool_call_id:
        return None
    question = _format_question_from_interaction(
        interaction,
        value_obj,
        source="permission_interrupt",
        invocation_record=card,
    )
    return {
        "event_type": "chat.ask_user_question",
        "request_id": request_id,
        "questions": [question],
        "source": "permission_interrupt",
    }


def _is_permission_interaction(interaction: Any) -> bool:
    _request_id, value_obj = _extract_interaction_parts(interaction)
    tool_name, message, _tool_args = _read_interrupt_fields(value_obj)
    return _resolve_interrupt_source(tool_name, message) == "permission_interrupt"


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
    if hasattr(value_obj, "questions"):
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
    tool_args = getattr(value_obj, "tool_args", None)
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


def _extract_ask_user_query(value_obj: Any) -> str:
    """The call's top-level ``query``, or ``""`` when there is none.

    Read from the same place ``_extract_questions_from_value`` reads the
    questions: ``ToolCallInterruptRequest.tool_args`` preserves the original
    ``ask_user`` arguments. It is the last source of prompt text for a question
    that arrived without any of its own, and the only one still available once
    the tool call itself has been consumed.
    """
    query = getattr(value_obj, "query", None)
    if isinstance(query, str) and query.strip():
        return query
    if isinstance(value_obj, dict):
        query = value_obj.get("query")
        if isinstance(query, str) and query.strip():
            return query

    tool_args = getattr(value_obj, "tool_args", None)
    if isinstance(tool_args, str):
        try:
            tool_args = json.loads(tool_args)
        except (ValueError, TypeError):
            tool_args = None
    if isinstance(tool_args, dict):
        query = tool_args.get("query")
        if isinstance(query, str) and query.strip():
            return query
    return ""


def _resolve_question_text(question: Mapping[str, Any], fallback_query: str) -> str:
    """The prompt to show for one question.

    Three sources in order: the question's own ``question``, its ``header``,
    then the call's top-level ``query``. A call that satisfied the ask_user rail
    never reaches past the first, because that rail rejects a question carrying
    no text. The fallbacks are for questions that did not come through it -- the
    same class of input the non-array ``options`` guard in
    ``_build_multi_questions`` already accounts for. There a malformed value
    built a question out of single characters; here a missing one raised
    ``KeyError``, losing the whole conversion and every question in the call
    with it.
    """
    for candidate in (
        question.get("question"),
        question.get("header"),
        fallback_query,
    ):
        if isinstance(candidate, str) and candidate.strip():
            return candidate
    return ""


def _build_multi_questions(questions_data: list, fallback_query: str = "") -> list:
    """Build frontend PendingQuestionItem list from questions data.

    有选项的问题: 保留原始选项 + 追加 __other__ (自定义输入)
    无选项的问题: 不追加 __other__, 前端应直接进入自由输入模式

    问题若声明了 ``inputs``，该键原样带出，本函数不解释其内容。

    A question's ``inputs`` declaration is carried through as opaque data. This
    is the one normalisation point every channel's question passes through, so
    it must not learn what any single channel's renderer makes of the
    declaration: it neither reads nor validates nor reshapes it. A channel with
    no renderer for it never looks the key up and behaves exactly as before.
    """
    questions = []
    for q in questions_data:
        raw_options = q.get("options", [])
        # Non-array options (e.g. "a,b") must not be iterated as characters (#2331).
        if not isinstance(raw_options, list):
            raw_options = []
        if raw_options:
            options = [
                _normalize_question_option(opt)
                for opt in raw_options
                if isinstance(opt, dict)
            ]
            options.append({"label": "Other", "description": "Custom input"})
        else:
            options = []
        question_payload = {
            "question": _resolve_question_text(q, fallback_query),
            "header": q.get("header") or "Question",
            "options": options,
            "multi_select": q.get("multi_select", False),
        }
        # A non-empty array only, so a question declaring nothing keeps the
        # payload it had; copied, not referenced, because one question can reach
        # several channels and none may edit what the next one receives.
        declared_inputs = q.get("inputs")
        if isinstance(declared_inputs, list) and declared_inputs:
            question_payload["inputs"] = copy.deepcopy(declared_inputs)
        questions.append(question_payload)
    return questions


def _extract_ui_options(value_obj: Any) -> list[dict[str, Any]]:
    options = (
        getattr(value_obj, "ui_options", None)
        if hasattr(value_obj, "ui_options")
        else None
    )
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


def _extract_interaction_metadata(interaction: Any) -> dict[str, Any]:
    metadata = (
        interaction.get("metadata")
        if isinstance(interaction, dict)
        else getattr(interaction, "metadata", None)
    )
    return dict(metadata) if isinstance(metadata, dict) else {}


def _reviewer_ui_metadata_from_interaction(
    interaction: Any,
    value_obj: Any,
) -> dict[str, Any]:
    metadata = _extract_interaction_metadata(interaction)
    metadata.update(_extract_interrupt_metadata(value_obj))
    for key in _AUTO_REVIEWER_UI_METADATA_KEYS:
        value = _read_value_field(value_obj, key, None)
        if value not in (None, ""):
            metadata[key] = value
    projected: dict[str, Any] = {}
    for key in _AUTO_REVIEWER_UI_METADATA_KEYS:
        value = metadata.get(key)
        if value in (None, ""):
            continue
        if key in _AUTO_REVIEWER_UI_LIST_METADATA_KEYS:
            if not isinstance(value, list | tuple):
                continue
            items = [
                redact_secret_values(item, max_length=_AUTO_REVIEWER_UI_MAX_TEXT_LENGTH)
                for item in value[:_AUTO_REVIEWER_UI_MAX_LIST_ITEMS]
                if isinstance(item, str) and item.strip()
            ]
            if len(value) > _AUTO_REVIEWER_UI_MAX_LIST_ITEMS:
                items.append("[TRUNCATED]")
            if items:
                projected[key] = items
            continue
        if isinstance(value, str) and value.strip():
            projected[key] = redact_secret_values(
                value,
                max_length=_AUTO_REVIEWER_UI_MAX_TEXT_LENGTH,
            )
    return projected


def _tool_invocation_locator_from_interaction(
    interaction: Any,
    value_obj: Any,
    *,
    root_permission_queue: RootPermissionQueue | None,
) -> tuple[str, RootPermissionCard | None]:
    metadata = _extract_interaction_metadata(interaction)
    metadata.update(_extract_interrupt_metadata(value_obj))
    if "tool_invocation_key" not in metadata:
        return "absent", None
    if root_permission_queue is None:
        return "invalid", None
    candidate = metadata.get("tool_invocation_key")
    try:
        key = ToolInvocationKeyV1.from_wire(candidate)
    except (TypeError, ValueError):
        return "invalid", None
    card = root_permission_queue.get(key)
    if card is None or card.state != "pending":
        return "invalid", None
    return "live", card


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


# 权限审批的兜底选项。``value`` 取自 ``permission_options``，与解析回答用的是同一份
# 词表：web / CLI 回传 ``value``，TUI 回传 ``label``，两条路都要能被 ``build_inputs``
# 解出同一个动作。label / description 保持原样，渲染出的文案不变。
def _default_interrupt_options() -> list[dict[str, str]]:
    return [
        {"value": ALLOW_ONCE, "label": "本次允许", "description": "仅本次授权执行"},
        {"value": SESSION_ALLOW, "label": "会话内记住", "description": "本次会话内自动放行同类操作"},
        {"value": ALWAYS_ALLOW, "label": "永久记住", "description": "写回磁盘，所有会话均自动放行"},
        {"value": REJECT, "label": "拒绝", "description": "拒绝执行此工具"},
    ]


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
        return options
    return (
        _plan_approval_interrupt_options(source, tool_name, message)
        or _default_interrupt_options()
    )


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
    if (
        not is_evolution_interrupt
        and tool_name not in SKILL_EVOLUTION_APPROVAL_TOOL_KINDS
    ):
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


def extract_question_from_interaction(
    payload: Any,
    *,
    root_permission_queue: RootPermissionQueue | None = None,
) -> dict | None:
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
    locator_state, invocation_record = _tool_invocation_locator_from_interaction(
        payload,
        value_obj,
        root_permission_queue=root_permission_queue,
    )
    if locator_state == "invalid":
        return None
    source = (
        "permission_interrupt"
        if locator_state == "live"
        else _resolve_interrupt_source(tool_name, message)
    )
    return _format_question_from_interaction(
        payload,
        value_obj,
        source=source,
        invocation_record=invocation_record,
    )


def _format_question_from_interaction(
    payload: Any,
    value_obj: Any,
    *,
    source: str,
    invocation_record: RootPermissionCard | None,
) -> dict[str, Any]:
    """Render one question after queue identity validation."""

    tool_name, message, tool_args = _read_interrupt_fields(value_obj)
    reviewer_metadata = _reviewer_ui_metadata_from_interaction(payload, value_obj)
    generic_confirm_message = message.strip() in {"", "Please approve or reject?"}
    needs_message = not message or (
        source == "confirm_interrupt" and generic_confirm_message
    )
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
        metadata = _extract_interrupt_metadata(value_obj)
        ask_title = str(metadata.get("ask_title") or "").strip()
        header = ask_title or (f"权限审批: {tool_name}" if tool_name else "权限审批")
        question = message

    question_data = {
        "question": question,
        "header": header,
        "options": _question_options_from_ui_options(
            value_obj, source, tool_name, message
        ),
        "multi_select": False,
    }
    if source == "permission_interrupt":
        if invocation_record is not None:
            question_data["card_id"] = invocation_record.key.invocation_id
        if reviewer_metadata:
            question_data["reviewer_metadata"] = reviewer_metadata
        if isinstance(tool_args, dict):
            question_data["tool_payload"] = sanitize_permission_ui_payload(tool_args)
    return question_data

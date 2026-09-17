# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Interrupt helpers for DeepAgent.

Provides utilities for converting interrupt payloads to frontend format
and building permission rails.
"""
from __future__ import annotations

import json
import re
from typing import Any

from jiuwenswarm.agents.harness.code.prompt.plan_approval import (
    build_plan_approval_actions,
)
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
) -> Any | None:
    """Build openjiuwen PermissionInterruptRail for tool permission checks.

    Args:
        config: Agent config dict containing permissions section
        llm: LLM instance for risk assessment
        model_name: Model name for risk assessment

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
    from jiuwenswarm.common.config import get_config
    from jiuwenswarm.common.e2a.acp.acp_tool_updates import build_acp_tool_descriptor
    from jiuwenswarm.common.utils import get_config_file, get_workspace_dir

    permission_config = config.get("permissions", {})
    logger.info(
        "[InterruptHelpers] build_permission_rail called: enabled=%s",
        permission_config.get("enabled", False)
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
                from jiuwenswarm.common.config import _dump_yaml_round_trip, _load_yaml_round_trip

                yaml_path = get_config_file()
                data = _load_yaml_round_trip(yaml_path)
                if not isinstance(data, dict):
                    data = {}

                on_disk_perms = data.get("permissions")
                if not isinstance(on_disk_perms, dict):
                    on_disk_perms = {}

                # Overlay path-related deltas + approval_overrides;
                # keep on-disk tools/defaults/rules to avoid restoring
                # entries the user already deleted via webui.
                merged = dict(on_disk_perms)
                overrides_new = permissions.get("approval_overrides")
                if overrides_new is not None:
                    merged["approval_overrides"] = overrides_new
                # 路径信任写 file_guard.paths（agent-core §5.5.6）；过渡期仍接受旧 external_directory
                fg_new = permissions.get("file_guard")
                if fg_new is not None:
                    merged["file_guard"] = fg_new
                ext_dir_new = permissions.get("external_directory")
                if ext_dir_new is not None:
                    merged["external_directory"] = ext_dir_new

                data["permissions"] = merged
                _dump_yaml_round_trip(yaml_path, data)
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
            channel = TOOL_PERMISSION_CHANNEL_ID.get() or "web"
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
                return ("approve",)
            return ("reject", f"[PERMISSION_DENIED] 该工具未被授权 (owner_scopes: {owner_level})")

        def _get_permissions_snapshot():
            cfg = get_config()
            return cfg.get("permissions") if isinstance(cfg, dict) else {}

        host = ToolPermissionHost(
            get_permissions_snapshot=_get_permissions_snapshot,
            persist_allow_rule=_persist_allow_rule,
            resolve_workspace_dir=get_workspace_dir,
            permission_yaml_path=get_config_file(),
            request_permission_confirmation=_request_permission_confirmation,
            permission_scene_hook=_permission_scene_hook,
        )

        permission_rail = PermissionInterruptRail(
            config=permission_config,
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
    # Prefer the explicit tool identity whenever it is available.  Many tools
    # (for example memory_search) have a plain ``query`` argument, so treating
    # every query-only interrupt as ask_user misroutes permission responses.
    if tool_name:
        return tool_name == "ask_user"

    # Legacy ask_user interrupt payloads may not carry tool_name.  Keep the
    # structural fallbacks below only for those identity-less payloads.
    if hasattr(value_obj, "payload_schema") and hasattr(value_obj, "questions"):
        return True
    if isinstance(value_obj, dict) and "payload_schema" in value_obj and "questions" in value_obj:
        return True
    tool_args = _normalize_tool_args(_read_value_field(value_obj, "tool_args", None))
    if isinstance(tool_args, dict) and str(tool_args.get("query") or "").strip():
        if not tool_args.get("questions"):
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
            payload["plan_actions"] = build_plan_approval_actions(resolved_plan_language)
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


def _default_interrupt_options() -> list[dict[str, str]]:
    return [
        {"label": "本次允许", "description": "仅本次授权执行"},
        {"label": "会话内记住", "description": "本次会话内自动放行同类操作"},
        {"label": "永久记住", "description": "写回磁盘，所有会话均自动放行"},
        {"label": "拒绝", "description": "拒绝执行此工具"},
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

    return {
        "question": question,
        "header": header,
        "options": _question_options_from_ui_options(value_obj, source, tool_name, message),
        "multi_select": False,
    }

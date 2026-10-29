# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""JiuWenSwarm Facade - 统一入口与 SDK 适配层.

此模块提供：
- 统一的 JiuWenSwarm 公开 API
- SDK 工厂路由（通过环境变量选择）
- 公共编排逻辑（session 队列、Skills 路由、heartbeat、流式包装）
"""

from __future__ import annotations

import json
from concurrent.futures import Future
from jiuwenswarm.agents.harness.common.recommendation.prompt_suggestion import USER_INTERACTION_EVENTS

import asyncio
from contextlib import aclosing
from copy import deepcopy
from dataclasses import replace
import inspect
import logging
import re
import time
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Tuple

from jiuwenswarm.dotenv_early import load_dotenv_runtime
from gateway.common.session_message import (
    SESSION_MESSAGE_INTERNAL_KEY,
    SESSION_MESSAGE_ORIGIN,
    with_cross_session_history_metadata,
)

from jiuwenswarm.server.runtime.agent_adapter.agent_adapters import (
    AgentAdapter,
    create_adapter,
    resolve_sdk_choice,
)
from jiuwenswarm.server.runtime.agent_adapter.management_routes import (
    _SKILLDEV_METHODS,
    _SKILL_ROUTES,
    _SESSION_ASSET_ROUTES,
    _SKILL_VISIBILITY_WRITE_HANDLERS as _SKILL_VISIBILITY_WRITE_HANDLERS,
    _SKILL_SYNC_HANDLERS as _SKILL_SYNC_HANDLERS,
    _SKILL_SYNC_WS_REJECTED,
    _PLUGIN_ROUTES,
    _PACKAGE_ROUTES,
    _AGENT_GROUP_PACKAGE_METHODS as _AGENT_GROUP_PACKAGE_METHODS,
)
from jiuwenswarm.agents.harness.common.memory.config import get_memory_mode, is_auto_memory_enabled, is_memory_enabled
from jiuwenswarm.server.runtime.session.history_io import run_history_io as _run_history_io
from jiuwenswarm.server.runtime.session.session_history import (
    append_compact_history_records,
    append_history_record,
    collapse_file_content_blocks,
)
from jiuwenswarm.server.runtime.agent_adapter.user_turn import UserTurn
from jiuwenswarm.server.runtime.agent_adapter.statusline_setup_agent import (
    STATUSLINE_SETUP_SYSTEM_PROMPT,
    build_statusline_setup_dispatch,
)
from jiuwenswarm.server.runtime.session.session_manager import SessionManager
from jiuwenswarm.runtime.session.model import (
    TEAM_ATTACH_ONLY_PARAM,
)
from jiuwenswarm.server.runtime.skill.skill_manager import SkillManager
from jiuwenswarm.server.utils.utils import is_team_params
from jiuwenswarm.common.config import get_config
from jiuwenswarm.common.e2a.constants import (
    E2A_CANCEL_SOURCE_CLIENT_DISCONNECT,
    E2A_INTERNAL_CANCEL_SOURCE_KEY,
)
from jiuwenswarm.agents.harness.code.prompt.plan_approval import (
    PLAN_EXECUTE_OPTION_VALUES,
    PLAN_REMINDER_ORIGINAL_QUERY_KEY,
    PLAN_REVISE_OPTION_VALUES,
    PLAN_SKIP_OPTION_VALUES,
    plan_skip_feedback,
)
from jiuwenswarm.agents.harness.common.rails.interrupt.permission_options import (
    ALLOW_ONCE,
    ALWAYS_ALLOW,
    REJECT,
    SESSION_ALLOW,
    is_keep_planning_value,
    normalize_option_value,
    resolve_permission_action,
)
from gateway.common.mode_matrix import (
    canonicalize_mode_text,
    is_code_profile_mode,
    is_team_mode as is_team_runtime_mode,
    is_team_plan_mode,
    is_web_composable_mode,
    read_request_work_mode,
)
from jiuwenswarm.common.context_keys import (
    JIUWENSWARM_CHANNEL_CONTEXT_KEY,
    JIUWENSWARM_SKIP_A2UI_CONTEXT_KEY,
)
from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponse, AgentResponseChunk
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.common.utils import (
    apply_free_search_runtime_defaults,
    get_agent_workspace_dir,
    get_env_file,
)
from jiuwenswarm.server.runtime.agent_adapter.deferred_capabilities import deferred
from jiuwenswarm.agents.harness.common.auto_memory import (
    _execute_auto_memory_extraction,
)
from jiuwenswarm.agents.harness.common.rails.interrupt.interrupt_helpers import (
    EVOLUTION_INTERRUPT_METADATA_SOURCES,
    is_interrupt_resume_payload,
)
from jiuwenswarm.agents.harness.common.rails.permissions.root_context import (
    HOST_USER_ORIGIN_EXTERNAL,
    HOST_USER_ORIGIN_INTERNAL,
)

TeamA2UIBlockBuffer = deferred("jiuwenswarm.server.runtime.a2ui.integration", "TeamA2UIBlockBuffer")
should_finalize_a2ui_content = deferred(
    "jiuwenswarm.server.runtime.a2ui.runtime.finalizer", "should_finalize_a2ui_content")


async def finalize_assistant_response_if_a2ui(*args, **kwargs):
    from jiuwenswarm.server.runtime.a2ui.integration import finalize_assistant_response_if_a2ui as finalize

    return await finalize(*args, **kwargs)


def _with_request_runtime_context(
    run: object,
    *,
    channel: str,
    skip_a2ui: bool,
) -> dict[str, Any]:
    """Return a copy of ``run`` carrying JiuwenSwarm request metadata."""
    run_data = dict(run) if isinstance(run, dict) else {}
    raw_context = run_data.get("context")
    context = dict(raw_context) if isinstance(raw_context, dict) else {}
    raw_extra = context.get("extra")
    extra = dict(raw_extra) if isinstance(raw_extra, dict) else {}
    extra[JIUWENSWARM_CHANNEL_CONTEXT_KEY] = channel
    if skip_a2ui:
        extra[JIUWENSWARM_SKIP_A2UI_CONTEXT_KEY] = True
    context["extra"] = extra
    run_data["context"] = context
    return run_data


def _cross_session_marker_from_params(params: Any) -> Any:
    """Extract the internal cross-Session marker from request params."""
    if not isinstance(params, dict):
        return None
    return params.get(SESSION_MESSAGE_INTERNAL_KEY)


from jiuwenswarm.server.runtime.agent_adapter.request_completion import (
    _session_message_owns_completion, _await_history_receipt,
    _announce_durable_completion, _complete_history_request,
)

def _person_drove_turn(request: AgentRequest, *, is_team_mode: bool) -> bool:
    """The person asked for this turn: a message they sent, or their answer to the agent's question.

    Background recommendation, team members, Heartbeat, cron and another Session's Agent start
    turns the person did not ask for (topic spec D4).
    """
    if is_team_mode or _session_message_owns_completion(request):
        return False
    if is_interrupt_resume_payload(request.params):
        return True
    return is_external_user_authored_dispatch(
        request.params, channel_id=request.channel_id, request_method=request.req_method, metadata=request.metadata,
    )


def _starter_interaction_context(params: Any, query: Any) -> str:
    lineage = (_history_user_extra(params) or {}).get("topic_starter")
    if not lineage:
        return ""
    from jiuwenswarm.agents.harness.common.recommendation.topic_product import (
        find_exact_terminal_card,
    )
    from jiuwenswarm.agents.harness.common.recommendation.topic_starter import (
        TopicStarterStore,
    )
    database = get_agent_workspace_dir() / "recommendation.db"
    if not database.exists():
        return ""
    card = find_exact_terminal_card(
        TopicStarterStore(database),
        **{key: value for key, value in lineage.items() if key != "exposure_id"},
    )
    if card is None or card.get("prompt") != query:
        return ""
    evidence = card.get("evidence_bundle") or {}
    from jiuwenswarm.agents.harness.common.recommendation.topic_controls import (
        recommendation_sources,
    )
    sources = recommendation_sources(TopicStarterStore(database), card.get("source_locators") or [])
    return (
        "Answer the clicked question using the stored topic context below. The user's "
        "own messages supply facts about the user and continuing constraints for this "
        "question. Earlier assistant replies, in this or another conversation, only show "
        "what was already discussed: a claim taken from them needs support like any other, "
        "and they are not documentation to quote. "
        "No modification forbids writes, not inspection or read-only checks. "
        "Explanation only asks for proposed steps rather than execution. It permits "
        "reading relevant files; running a disposable sample requires the user's explicit "
        "request for execution or operational verification. Keep conditions attached to their own action, preserve "
        "restrictions on changes, purchases and access, and follow the reply "
        "preferences the sources state, such as length.\n"
        "The clicked question is the answer scope; stored context must not add a request "
        "or premise. When the question takes an effect, cause or pattern for granted, that is the first "
        "claim to support: say whether it holds, holds only under stated conditions, or "
        "is not supported, and answer from there instead of explaining an effect you have "
        "not established. Every claim the answer "
        "relies on for it (what a command or query does, a quantity, a cause, or which "
        "of two cases is larger, longer or earlier) needs support you name in the "
        "reply: when the user explicitly requested execution or operational verification, "
        "a result you observed by running a disposable sample; or a short calculation "
        "from an established formula; or a page fetched "
        "during this reply that states that claim for the same case; quotation marks are "
        "only for words that page contains. A page about a neighbouring "
        "quantity, period or condition, a search snippet, or a page you could not "
        "fetch does not support it. Leave out a claim without such support or state "
        "it as unverified together with what would settle it. Distinguish established "
        "mechanisms from hypotheses and context-dependent exceptions.\n"
        "Separate source facts, observed tool results and proposed steps. A source_url "
        "beginning with project:// or profile:// names the stored record shown in "
        "source_context, not a file to look for. If a document "
        "or file is missing, state what is unknown; do not search outside the project "
        "or infer its runtime, required versions, dependencies or data structure from "
        "the topic alone. Explain what information would resolve the missing fact. "
        "Do not fill unknown requirements with example version numbers or unrelated "
        "toolchains; use the project's documented requirement or leave it unknown. "
        "For an explanation-only request, give concise steps with their prerequisites "
        "and the result each should produce, marking every step you did not run as "
        "not executed.\n"
        "When the user explicitly asks for execution or operational verification, run "
        "the needed commands, queries or code on a newly allocated disposable sample "
        "with known contents, preserving the user's files, and mark any step you could "
        "not run as not executed. Explanation, comparison and proposed-method questions "
        "do not themselves ask you to run a sample or create files. An explicit "
        "restriction not to run commands also applies to disposable samples. "
        "Never present output you did not observe. Check the actual result "
        "against those contents, not just the exit status. Test the claimed property: "
        "read-only access requires a rejected write through the same connection; "
        "unchanged contents require a full before/after comparison. Neither a file "
        "listing nor successful reads prove permissions, locks or backup completeness. "
        "Inspect a database's actual schema before referring to tables or columns. "
        "Describe only the exact tested procedure as verified, identify the sample, "
        "and keep all unexecuted commands and the user's own files explicitly unverified.\n"
        + json.dumps({
            "category": card.get("category"),
            "source_url": evidence.get("source_url"),
            "published_at": evidence.get("published_at"),
            "source_context": evidence.get("exact_spans") or evidence.get("content_boundary"),
            "user_source_constraints": sources,
            "publication_date_provenance": evidence.get("publication_date_provenance"),
        }, ensure_ascii=False)
    )

class _TeamPlanApprovalPayloadError(ValueError):
    """Raised when a structured team.plan approval payload is malformed."""


def compute_chat_send_mcp_needed(params: dict[str, Any] | None) -> list[str]:
    """``params.mcp`` ∪ connectors from ``agent_template_name`` / ``plugin_names``.

    Always returns a list (never ``None``). ``reconcile_session_mcp`` still
    filters/strips names. Absent/``[]`` ``mcp`` no longer drops connectors
    declared by this turn's equipment params.
    """
    p = params if isinstance(params, dict) else {}
    mcp = p.get("mcp")
    agent_template_id = p.get("agent_template_name")
    plugin_ids = p.get("plugin_names")
    has_template = isinstance(agent_template_id, str) and bool(agent_template_id.strip())
    has_plugins = isinstance(plugin_ids, list) and any(
        isinstance(item, str) and item.strip() for item in plugin_ids
    )
    if not has_template and not has_plugins:
        return list(dict.fromkeys(
            item.strip() for item in (mcp if isinstance(mcp, list) else [])
            if isinstance(item, str) and item.strip()
        ))

    from jiuwenswarm.server.runtime import extension_package_manager as package_manager

    if isinstance(agent_template_id, str) and agent_template_id.strip():
        try:
            agent_template_id = package_manager.resolve_equipment_runtime_id(
                "agent_templates", agent_template_id
            )
        except (TypeError, ValueError):
            pass
    plugin_ids = p.get("plugin_names")
    if isinstance(plugin_ids, list):
        resolved_plugin_ids: list[Any] = []
        for item in plugin_ids:
            if isinstance(item, str) and item.strip():
                try:
                    item = package_manager.resolve_equipment_runtime_id(
                        "plugin_packages", item
                    )
                except (TypeError, ValueError):
                    pass
            resolved_plugin_ids.append(item)
        plugin_ids = resolved_plugin_ids
    connectors = package_manager.collect_connectors_for_packages(
        agent_template_id=agent_template_id,
        plugin_ids=plugin_ids if isinstance(plugin_ids, list) else None,
        skip_missing=True,
    )
    out: list[str] = []
    seen: set[str] = set()
    for item in (mcp if isinstance(mcp, list) else []) + connectors:
        if not isinstance(item, str):
            continue
        name = item.strip()
        if not name or name in seen:
            continue
        seen.add(name)
        out.append(name)
    return out


def restore_chat_send_equipment_params(
    session_id: str | None,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Fill omitted equipment fields from the session's persisted snapshot.

    Explicit empty values are never overwritten: omission means "keep", while
    ``""`` / ``[]`` mean "clear".
    """
    if not session_id:
        return params
    mode = str(params.get("mode") or "").strip().lower()
    if mode == "auto_harness" or is_team_runtime_mode(mode):
        return params
    from jiuwenswarm.server.runtime.session.session_metadata import (
        get_session_equipment,
    )

    saved = get_session_equipment(session_id)
    for key in ("agent_template_name", "plugin_names", "mcp"):
        if key not in params and key in saved:
            value = saved[key]
            params[key] = list(value) if isinstance(value, list) else value
    return params


def _permission_card_ids_from_answers(answers: list[dict]) -> list[str]:
    """Normalize the single opaque permission-card locator."""

    if len(answers) != 1:
        return []
    answer = answers[0]
    if not isinstance(answer, dict):
        return []
    raw_card_id = answer.get("card_id")
    if not isinstance(raw_card_id, str):
        return []
    card_id = raw_card_id.strip()
    if not card_id or len(card_id) > 128:
        return []
    return [card_id]


def _history_user_content(params: Any, query: Any) -> Any:
    """返回写入历史记录的用户消息内容.

    追加补充/调整请求时，``query`` 是包装后的提示词模板，会把模型提示词暴露到
    历史记录里。这里优先使用原始用户输入 ``supplement_input`` 作为展示内容。

    进入 plan 的那一轮同理：``query`` 前面被拼了一段 <system-reminder>，历史里
    要还原成用户原文，否则重新加载会话会把提示词当成用户提问显示出来。

    Gateway 可能已把 ``@path`` 展开成 ``<file-content>`` 正文；历史只保留 ``@path``，
    避免 transcript 膨胀，也不影响当轮已发给模型的内联内容。
    """
    content: Any
    if not isinstance(params, dict):
        content = query
    elif params.get("is_supplement"):
        supplement_input = params.get("supplement_input")
        if isinstance(supplement_input, str) and supplement_input.strip():
            content = supplement_input
        else:
            content = query
    else:
        original_query = params.get(PLAN_REMINDER_ORIGINAL_QUERY_KEY)
        content = original_query if isinstance(original_query, str) else query

    if isinstance(content, str):
        return collapse_file_content_blocks(content)
    return content


def _warn_unrecognised_approval_option(
    branch: str,
    source: Any,
    request_id: Any,
    value: Any,
    outcome: str,
) -> None:
    """审批选项没认出来时留一条告警，然后才按拒绝兜底。

    兜底成拒绝是对的：认不出来就不该放行。但在此之前它是静默的——用户点了同意，
    工具被拒，现场只剩一句用户看不到的 feedback，排查时无从下手。选项文案是跨端
    约定，任何一端改了字面量都会以"用户拒绝"的形式出现，因此这条告警要带上原始
    取值和落到哪个分支。

    取值为空是渲染端的"取消"（TUI 无拒绝项时按 Esc 即回传空串），属于正常路径，
    不是解析失败，不告警。
    """
    if not normalize_option_value(value):
        return
    logger.warning(
        "[JiuWenSwarm] %s unrecognised approval option: source=%s request_id=%s "
        "value=%r outcome=%s. The answer is refused because it could not be decoded, "
        "not because the user declined; check that the option vocabulary in "
        "permission_options matches what the channel sends.",
        branch,
        source,
        request_id,
        value,
        outcome,
    )


def is_external_user_authored_dispatch(
    params: Any,
    *,
    channel_id: Any = "",
    request_method: Any = None,
    metadata: Any = None,
) -> bool:
    """Return whether Host ingress proved an external user-authored dispatch."""

    if not isinstance(params, dict):
        return False
    # Scheduled Heartbeats reuse the original channel, including web. Inspect
    # each ingress container separately so merging metadata cannot erase a marker.
    for container in (params, metadata, params.get("metadata")):
        if isinstance(container, dict) and isinstance(
            container.get(SESSION_MESSAGE_INTERNAL_KEY), dict
        ):
            return False
        automation = container.get("automation") if isinstance(container, dict) else None
        if isinstance(automation, dict) and str(automation.get("kind") or "").strip().lower() == "heartbeat":
            return False
    if params.get("log_as_user") is False:
        return False
    if params.get("attach_goal") is True:
        return False
    if is_interrupt_resume_payload(params):
        return False
    if request_method == ReqMethod.COMMAND_GOAL:
        return False
    if str(channel_id or "").strip().lower() in {"cron", "heartbeat"}:
        return False
    return not str(params.get("source") or "").strip()


def _should_record_user_history(params: Any) -> bool:
    # History visibility is not permission authority: retain Heartbeat turns.
    if not isinstance(params, dict):
        return True
    if params.get("log_as_user") is False:
        return False
    # Second-step goal attach is host control traffic, not a user utterance.
    if params.get("attach_goal") is True:
        return False
    # An attach-only subscription submits no input at all; a record here would
    # leave a dangling user turn that makes the frontend wait forever.
    if params.get(TEAM_ATTACH_ONLY_PARAM) is True:
        return False
    if is_interrupt_resume_payload(params):
        return False
    return str(params.get("source") or "") != "proactive_recommendation"


def _is_ask_user_answer_resume(params: Any) -> bool:
    """问题澄清答案 resume：source=ask_user_interrupt 且带非空 answers 数组。

    这类 resume 被 _should_record_user_history 排除（不写 user 消息），但答案需要
    单独以 chat.ask_user_answer assistant 记录落盘，刷新后才能回显。
    """
    if not isinstance(params, dict):
        return False
    if str(params.get("source") or "").strip() != "ask_user_interrupt":
        return False
    answers = params.get("answers")
    return isinstance(answers, list) and bool(answers)


async def _persist_ask_user_answer_history(
    request: AgentRequest,
    session_id: str,
    params: dict[str, Any],
) -> None:
    """Persist an answered question under the original question request ID."""
    if not _is_ask_user_answer_resume(params):
        return
    answer_request_id = str(params.get("request_id") or "").strip()
    if not answer_request_id:
        return

    from .sensitive_answers import redact_sensitive_answers

    await _run_history_io(
        append_history_record,
        wait_for_write=True,
        session_id=session_id,
        request_id=answer_request_id,
        channel_id=request.channel_id,
        role="assistant",
        event_type="chat.ask_user_answer",
        content="",
        timestamp=time.time(),
        extra={
            "request_id": answer_request_id,
            "source": "ask_user_interrupt",
            "answers": redact_sensitive_answers(params["answers"]),
        },
        mode=params.get("mode", "unknown"),
    )


def _resolve_final_record_timestamp(
    *,
    event_type: str,
    segment_started_at: float | None,
    extra_fields: dict[str, Any] | None,
) -> float:
    """chat.final 落盘用「气泡出现的时刻」，收尾时刻另存 completed_at。"""
    completed_at = time.time()
    if event_type != "chat.final" or not segment_started_at:
        return completed_at
    if segment_started_at >= completed_at:
        return completed_at
    if isinstance(extra_fields, dict):
        extra_fields.setdefault("completed_at", completed_at)
    return segment_started_at


def _history_media_string(item: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _history_media_size(item: dict[str, Any]) -> int | float | None:
    for key in ("size_bytes", "sizeBytes"):
        value = item.get(key)
        if isinstance(value, (int, float)) and value >= 0:
            return value
    return None


def _history_media_record(value: Any, *, default_type: str = "image") -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None

    path = _history_media_string(value, "path")
    url = _history_media_string(value, "url")
    if not path and not url:
        return None

    media_type = _history_media_string(value, "type") or default_type
    filename = _history_media_string(value, "filename", "name") or (
        Path(path).name if path else "image"
    )
    mime_type = _history_media_string(value, "mime_type", "mimeType")
    size = _history_media_size(value)

    record: dict[str, Any] = {
        "type": media_type,
        "filename": filename,
    }
    if mime_type:
        record["mime_type"] = mime_type
    if path:
        record["path"] = path
    if url:
        record["url"] = url
    if size is not None:
        record["size_bytes"] = size
    return record


def _with_heartbeat_history_metadata(
    extra: dict[str, Any] | None,
    params: Any,
) -> dict[str, Any] | None:
    """Persist the public Heartbeat marker in the same shape Web events use."""
    result = dict(extra or {})
    if not isinstance(params, dict):
        return result or None
    automation = params.get("automation")
    if not isinstance(automation, dict) or automation.get("kind") != "heartbeat":
        return result or None

    # Some event payloads may already contain metadata.  Preserve it while
    # ensuring restored history exposes ``metadata.automation`` just like the
    # live Web stream.
    metadata = (
        dict(result.get("metadata"))
        if isinstance(result.get("metadata"), dict)
        else {}
    )
    metadata["automation"] = dict(automation)
    result["metadata"] = metadata
    # Do not retain a second top-level copy added by generic parameter merging.
    result.pop("automation", None)
    return result


def _history_user_extra(params: Any) -> dict[str, Any] | None:
    """Extract media/files/skills from ``params`` for the history extra.

    Image attachments and uploaded files are scoped to the *current turn* only
    so they are visible in the UI history but do not leak into later prompt
    contexts.  Skills, on the other hand, are persisted without a scope.

    Input and assistance origins describe how the person composed the message,
    so a turn another Session's Agent or a Heartbeat started carries none.
    """
    if not isinstance(params, dict):
        return None

    extra = with_cross_session_history_metadata(
        None, _cross_session_marker_from_params(params)
    ) or {}
    automation = params.get("automation")
    typed_by_person = "message_origin" not in extra and not (
        isinstance(automation, dict) and automation.get("kind") == "heartbeat"
    )
    input_origin = str(params.get("input_origin") or "").strip()
    if input_origin not in {
        "manual_free_text",
        "starter_click",
        "default_reply",
        "tab_suggestion",
        "prompt_suggestion",
        "pasted_or_unknown",
    }:
        input_origin = "pasted_or_unknown"
    assistance_origin = str(params.get("assistance_origin") or "").strip()
    if assistance_origin not in {
        "none",
        "starter_click",
        "default_reply",
        "tab_suggestion",
        "prompt_suggestion",
        "unknown",
    }:
        assistance_origin = "unknown"
    if typed_by_person:
        extra["input_origin"] = input_origin
        extra["assistance_origin"] = assistance_origin
    topic_starter = params.get("topic_starter")
    _field_items = (
        "manifest_id",
        "generation_batch_id",
        "exposure_id",
        "card_revision_id",
        "context_scope",
    )
    _condition = (
        typed_by_person
        and input_origin == "starter_click"
        and assistance_origin == "starter_click"
        and isinstance(topic_starter, dict)
        and set(topic_starter)
        == {
            "manifest_id",
            "generation_batch_id",
            "exposure_id",
            "card_revision_id",
            "context_scope",
            "project_id",
        }
        and all(
            isinstance(topic_starter.get(field), str)
            and bool(str(topic_starter[field]).strip())
            for field in _field_items
        )
        and (
            topic_starter.get("project_id") is None
            or (
                isinstance(topic_starter.get("project_id"), str)
                and bool(str(topic_starter["project_id"]).strip())
            )
        )
    )
    if (
        _condition
    ):
        from jiuwenswarm.agents.harness.common.recommendation.topic_product import (
            scope_for_project,
        )

        scope = str(topic_starter["context_scope"])
        project_id = topic_starter.get("project_id")
        scope_matches = (scope == "global_new_chat" and project_id is None) or (
            isinstance(project_id, str) and scope == scope_for_project(project_id)
        )
        if scope_matches:
            extra["topic_starter"] = dict(topic_starter)
    prompt_suggestion = params.get("prompt_suggestion")
    # The in-conversation suggestion the person accepted, sent as it was or edited.
    _condition2 = (
        typed_by_person
        and input_origin == "prompt_suggestion"
        and assistance_origin == "prompt_suggestion"
        and isinstance(prompt_suggestion, dict)
        and set(prompt_suggestion) == {"suggestion_id", "edited"}
        and isinstance(prompt_suggestion["suggestion_id"], str)
        and bool(prompt_suggestion["suggestion_id"].strip())
        and isinstance(prompt_suggestion["edited"], bool)
    )
    if (
        _condition2
    ):
        extra["prompt_suggestion"] = dict(prompt_suggestion)
    raw_media_items = params.get("media_items")
    if isinstance(raw_media_items, list):
        media_items: list[dict[str, Any]] = []
        for raw_item in raw_media_items:
            item = _history_media_record(raw_item)
            if item is not None:
                media_items.append(item)
        if media_items:
            extra["media_items"] = {
                "items": media_items,
                "scope": "current_turn",
            }

    raw_files = params.get("files")
    if isinstance(raw_files, dict):
        files: dict[str, Any] = {}
        uploaded_images = raw_files.get("uploaded_images")
        if isinstance(uploaded_images, list):
            image_items: list[dict[str, Any]] = []
            for raw_item in uploaded_images:
                item = _history_media_record(raw_item, default_type="image")
                if item is not None:
                    image_items.append(item)
            if image_items:
                files["uploaded_images"] = {
                    "items": image_items,
                    "scope": "current_turn",
                }
        if files:
            extra["files"] = files

    raw_skills = params.get("skills")
    if isinstance(raw_skills, list):
        skills = [s.strip() for s in raw_skills if isinstance(s, str) and s.strip()]
        if skills:
            extra["skills"] = skills

    return _with_heartbeat_history_metadata(extra, params)


def _with_cross_session_history_metadata(
    extra: dict[str, Any] | None,
    params: Any,
) -> dict[str, Any] | None:
    """Persist the public origin marker on cross-Session assistant records."""
    result = dict(extra or {})
    if not isinstance(params, dict):
        return result or None
    raw_cross_session = params.get(SESSION_MESSAGE_INTERNAL_KEY)
    if not isinstance(raw_cross_session, dict):
        return result or None
    cross_session: dict[str, Any] = {}
    for key in (
        "message_id",
        "source_session_id",
        "source_title",
        "chain_id",
        "parent_message_id",
        "hop_count",
        "language",
    ):
        if key in raw_cross_session:
            cross_session[key] = raw_cross_session[key]
    result["message_origin"] = SESSION_MESSAGE_ORIGIN
    result["cross_session"] = cross_session
    message_id = str(cross_session.get("message_id") or "").strip()
    if message_id:
        result["session_message_id"] = message_id
    return result


def _web_agent_template_name(params: Any, channel_id: Any) -> str | None:
    """Return the explicitly selected Agent id for a Web single-Agent turn."""
    if str(channel_id or "").strip().lower() != "web":
        return None
    if not isinstance(params, dict):
        return None
    mode = str(params.get("mode") or "").strip().lower()
    if mode == "auto_harness" or is_team_params(params):
        return None
    value = params.get("agent_template_name")
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _with_web_agent_template_metadata(
    extra: dict[str, Any] | None,
    params: Any,
    channel_id: Any,
    *,
    event_type: str | None,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Attach the selected Agent id without changing non-Web event semantics."""
    result = dict(extra or {})
    if event_type not in {"chat.delta", "chat.final", "chat.reasoning", "chat.tool_call"}:
        return result
    if isinstance(payload, dict) and (
        payload.get("slash_command")
        or payload.get("source") in {"proactive_recommendation", "session_task_summary"}
    ):
        return result
    if isinstance(params, dict) and (
        params.get("slash_command")
        or params.get("source") in {"proactive_recommendation", "session_task_summary"}
    ):
        return result
    for candidate in (payload, params):
        if not isinstance(candidate, dict):
            continue
        automation = candidate.get("automation")
        if isinstance(automation, dict) and automation.get("kind") == "heartbeat":
            return result
    agent_template_name = _web_agent_template_name(params, channel_id)
    if agent_template_name:
        result["agent_template_name"] = agent_template_name
    return result


def _with_web_agent_template_payload(
    payload: dict[str, Any] | None,
    params: Any,
    channel_id: Any,
    *,
    event_type: str | None = None,
) -> dict[str, Any] | None:
    """Copy a Web assistant payload with its request-scoped Agent identity."""
    if not isinstance(payload, dict):
        return payload
    resolved_event_type = event_type or payload.get("event_type")
    if not isinstance(resolved_event_type, str):
        return payload
    metadata = _with_web_agent_template_metadata(
        None,
        params,
        channel_id,
        event_type=resolved_event_type,
        payload=payload,
    )
    agent_template_name = metadata.get("agent_template_name")
    if not agent_template_name:
        return payload
    return {**payload, "agent_template_name": agent_template_name}


def _compact_stats_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    stats: dict[str, Any] = {}
    for key in ("status", "phase", "processor", "model", "before", "after", "saved", "duration_ms"):
        if key in payload:
            stats[key] = payload.get(key)
    return stats


def _is_successful_compaction_payload(payload: dict[str, Any]) -> bool:
    if payload.get("error"):
        return False
    status = str(payload.get("status") or "").strip().lower()
    return status not in {"error", "failed", "skipped"}


def _append_compact_history_from_payload(
    *,
    payload: dict[str, Any],
    session_id: str,
    request_id: str,
    channel_id: str,
    mode: str,
) -> None:
    summary_text = str(payload.get("compact_summary") or "").strip()
    if not summary_text or not _is_successful_compaction_payload(payload):
        return
    append_compact_history_records(
        session_id=session_id,
        request_id=request_id,
        channel_id=channel_id,
        summary=summary_text,
        timestamp=time.time(),
        trigger="auto",
        stats=_compact_stats_from_payload(payload),
        mode=mode,
    )


def _contains_a2ui_marker(value: Any) -> bool:
    return isinstance(value, str) and should_finalize_a2ui_content(value)


_A2UI_STREAM_PROBE_WINDOW = 512
_A2UI_STREAM_PARTIAL_MARKERS = (
    "<a2ui-json>",
    "beginRendering",
    "surfaceUpdate",
    "dataModelUpdate",
    "deleteSurface",
)
_A2UI_PENDING_RENDER_DELTA = "<a2ui-json>\n"


def _make_a2ui_pending_render_chunk(*, request_id: str, channel_id: str) -> AgentResponseChunk:
    return AgentResponseChunk(
        request_id=request_id,
        channel_id=channel_id,
        payload={"event_type": "chat.delta", "content": _A2UI_PENDING_RENDER_DELTA},
        is_complete=False,
    )


def _make_a2ui_final_chunk(
        *,
        request_id: str,
        channel_id: str,
        session_id: str,
        content: str,
) -> AgentResponseChunk:
    return AgentResponseChunk(
        request_id=request_id,
        channel_id=channel_id,
        payload={
            "event_type": "chat.final",
            "session_id": session_id,
            "content": content,
        },
        is_complete=False,
    )


def _should_defer_a2ui_processing_status(
        *,
        suppress_a2ui_stream: bool,
        event_type: str,
        payload: dict[str, Any],
) -> bool:
    return (
        suppress_a2ui_stream
        and event_type == "chat.processing_status"
        and payload.get("is_processing") is False
    )


def _normalize_nested_stream_chunk(
        chunk: AgentResponseChunk,
) -> AgentResponseChunk | None:
    """Keep the facade stream open until its post-processing has finished."""
    if not chunk.is_complete:
        return chunk
    payload = chunk.payload
    if payload is None:
        return None
    if isinstance(payload, dict):
        is_complete = payload.get("is_complete") is True
        has_event_type = bool(payload.get("event_type"))
        if is_complete and not has_event_type:
            return None
    return replace(chunk, is_complete=False)


def _extend_a2ui_stream_probe(previous: str, content: str) -> str:
    probe = f"{previous}{content}"
    if len(probe) <= _A2UI_STREAM_PROBE_WINDOW:
        return probe
    return probe[-_A2UI_STREAM_PROBE_WINDOW:]


def _looks_like_partial_a2ui_marker(value: Any) -> bool:
    if not isinstance(value, str) or not value:
        return False
    tail = value[-_A2UI_STREAM_PROBE_WINDOW:]
    recent_lines = tail.splitlines()[-3:] or [tail]
    for line in recent_lines:
        candidate = line.strip().lstrip("[{,").strip().lstrip('"')
        match = re.match(r"<?[A-Za-z][A-Za-z0-9_-]*>?", candidate)
        if match is None:
            continue
        token = match.group(0)
        if len(token) < 2:
            continue
        rest = candidate[len(token):].strip()
        if rest and not any(marker.startswith(token + rest) for marker in _A2UI_STREAM_PARTIAL_MARKERS):
            continue
        if any(marker.startswith(token) and token != marker for marker in _A2UI_STREAM_PARTIAL_MARKERS):
            return True
    return False


def _stream_probe_has_a2ui_marker(value: Any) -> bool:
    return _contains_a2ui_marker(value) or _looks_like_partial_a2ui_marker(value)


def _should_probe_a2ui_stream(*, is_team_mode: bool) -> bool:
    """Use request-wide buffering only for streams with a finite boundary."""
    return not is_team_mode


_A2UI_STREAM_PROTOCOL_START_RE = re.compile(
    r"(?im)^(?P<marker>[ \t]*(?:[\[{,][ \t]*)*\"?"
    r"(?:beginRendering|surfaceUpdate|dataModelUpdate|deleteSurface)\"?[ \t]*(?::|$))"
)


def _recent_line_offsets(value: str) -> list[tuple[int, str]]:
    if not value:
        return []
    lines = value.splitlines(keepends=True)
    start = 0
    offsets: list[tuple[int, str]] = []
    for line in lines:
        offsets.append((start, line))
        start += len(line)
    if value.endswith("\n"):
        offsets.append((len(value), ""))
    return offsets[-3:] or [(0, value)]


def _a2ui_marker_start(value: Any) -> int | None:
    if not isinstance(value, str) or not value:
        return None

    tag_index = value.find("<a2ui-json")
    protocol_match = _A2UI_STREAM_PROTOCOL_START_RE.search(value)
    protocol_index = protocol_match.start("marker") if protocol_match is not None else -1
    indexes = [index for index in (tag_index, protocol_index) if index >= 0]
    if indexes:
        return min(indexes)

    for line_start, line in _recent_line_offsets(value):
        stripped_line = line.strip()
        if not stripped_line:
            continue
        leading = len(line) - len(line.lstrip())
        candidate = stripped_line.lstrip("[{,").strip().lstrip('"')
        match = re.match(r"<?[A-Za-z][A-Za-z0-9_-]*>?", candidate)
        if match is None:
            continue
        token = match.group(0)
        if len(token) < 2:
            continue
        rest = candidate[len(token):].strip()
        if rest and not any(marker.startswith(token + rest) for marker in _A2UI_STREAM_PARTIAL_MARKERS):
            continue
        if any(marker.startswith(token) and token != marker for marker in _A2UI_STREAM_PARTIAL_MARKERS):
            return line_start + leading
    return None


def _split_a2ui_stream_content(previous_probe: str, content: str) -> tuple[str, str] | None:
    combined = f"{previous_probe}{content}"
    marker_start = _a2ui_marker_start(combined)
    if marker_start is None:
        return None
    content_start = len(combined) - len(content)
    if marker_start <= content_start:
        return "", content
    split_index = marker_start - content_start
    return content[:split_index], content[split_index:]


load_dotenv_runtime(dotenv_path=get_env_file(), override=True)
apply_free_search_runtime_defaults()


def _trigger_auto_memory_extraction(
    adapter: Any,
    request: AgentRequest,
    session_id: str,
    is_stream: bool = False,
) -> None:
    """Trigger auto memory extraction after conversation ends.

    Extracted helper to avoid code duplication between process_message and process_message_stream.

    Args:
        adapter: The AgentAdapter instance (e.g., JiuwenSwarmCodeAdapter).
        request: The agent request containing project_dir.
        session_id: The session ID for context retrieval.
        is_stream: Whether this is from stream mode (for logging).
    """
    project_dir = request.params.get("project_dir") if isinstance(request.params, dict) else None

    if not project_dir:
        return

    messages = None

    # Directly read messages from session history file
    try:
        from jiuwenswarm.server.runtime.session.session_history import read_session_history_records
        history_records = read_session_history_records(session_id)

        # Convert history records to message format for memory extraction
        # Each history record has: role, content, timestamp, etc.
        messages = []
        for record in history_records:
            role = record.get("role", "unknown")
            content = record.get("content", "")
            # Skip empty content
            if not content or not isinstance(content, str):
                continue
            # Create message dict in standard format
            messages.append({"role": role, "content": content})
    except Exception as e:
        logger.warning("[auto_memory] Failed to read session history: %s", e, exc_info=True)
        messages = None

    # If we successfully got messages, proceed with auto memory extraction
    if messages is None or len(messages) == 0:
        return

    # Chat requests run on a session-scoped child adapter.  The root adapter
    # deliberately has no ``_instance``, while auto-memory needs the live
    # session adapter for its model, tools, and rails.
    # Launch auto memory extraction task
    try:
        parent_agent = adapter
        get_session_adapter = getattr(adapter, "_get_cached_session_adapter", None)
        if callable(get_session_adapter):
            session_adapter = get_session_adapter(session_id)
            if session_adapter is not None:
                parent_agent = session_adapter

        asyncio.create_task(
            _execute_auto_memory_extraction(
                session_id=session_id,
                project_dir=project_dir,
                messages=messages,
                parent_agent=parent_agent,  # Pass live adapter for cache sharing
            )
        )
        mode = request.params.get("mode", "unknown") if isinstance(request.params, dict) else "unknown"
        logger.info("[auto_memory] Extraction task launched successfully for mode=%s", mode)
    except Exception as e:
        logger.error("[auto_memory] Failed to launch extraction task: %s", e, exc_info=True)


logger = logging.getLogger(__name__)


# SkillDev 请求方法集合（统一委托给 SkillDevService）
_SKILL_COMMAND_REGEX = re.compile(
    r"^/skills use\s+(?P<skill_names>[^,]+)\s*,\s*(?P<query>.*)$"
)

# /statusline prompt-type 模式：
# 用户输入 "/statusline <描述>" → 让父代理调用内置 statusline-setup 子代理
# 排除已知子命令（set, padding, clear, help, json）——这些由 TUI 前端本地处理，
# 但如果消息经过 Gateway 传到 AgentServer，后端也需要区分。
_STATUSLINE_KNOWN_SUBCOMMANDS = {"set", "padding", "clear", "help", "json", "get"}
_STATUSLINE_PROMPT_REGEX = re.compile(
    r"^/statusline\s+(?P<description>.+)$"
)

# Backward-compatible alias for tests and callers that imported the old name.
# The text is now the dedicated subagent's system prompt, not a suffix appended
# to every parent-agent user turn.
_STATUSLINE_SETUP_PROMPT = STATUSLINE_SETUP_SYSTEM_PROMPT


def _handle_skills_use_slash_command(query: str) -> Tuple[list, str]:
    """Handle the /skills use slash command"""
    stripped = query.strip()
    if not stripped.startswith("/skills use"):
        return [], query

    skill_list = []
    matches = _SKILL_COMMAND_REGEX.match(stripped)
    if matches:
        skill_list.append(matches.group("skill_names")) # Currently only extracts one skill
        new_query = matches.group("query")
        return skill_list, new_query
    else:
        logger.warning(f"Couldn't parse command: {stripped}")
        return [], query


def _handle_statusline_prompt_command(query: str) -> Tuple[str, str]:
    """处理 /statusline <prompt>

    不调用 /skills 命令。返回一条让父代理通过 ``task_tool`` 调用内置
    ``statusline-setup`` 子代理的调度指令。

    Args:
        query: 用户原始输入（含 "/statusline" 前缀）

    Returns:
        (dispatch_prompt, description) — 子代理调度指令和提取的描述
        如果不是 /statusline prompt 模式，返回 ("", query)
    """
    stripped = query.strip()
    if not stripped.startswith("/statusline"):
        return "", query

    match = _STATUSLINE_PROMPT_REGEX.match(stripped)
    if match:
        description = match.group("description").strip()
        # 排除已知子命令——它们由 TUI 前端本地处理，不应被当作 prompt
        first_word = description.split()[0] if description else ""
        if first_word in _STATUSLINE_KNOWN_SUBCOMMANDS:
            return "", query
        if description:
            # 把用户的描述转化为让 Agent 自动配置状态栏的 prompt
            return build_statusline_setup_dispatch(description), description

    # /statusline 无参数 → 不是 prompt 模式（TUI 应已拦截处理 help）
    return "", query


def build_user_prompt(content: str | dict, files: dict, channel: str, language: str, *,
    trusted_dirs: list[str] | None = None, metadata: dict[str, Any] | None = None,
    skills: list[str] | None = None,
    origin_kind: str = HOST_USER_ORIGIN_INTERNAL) -> str:
    """Build the user prompt for an agent.

    Thin wrapper over :meth:`UserTurn.render` — the single renderer shared by
    single-agent and team runs. Kept for callers that hold loose arguments
    rather than a ``UserTurn``.

    Args:
        content: The user's message text, or an A2UI client-event dict.
        files: ``chat.send`` files mapping carrying uploaded attachments.
        channel: Originating channel id.
        language: Preferred response language.
        trusted_dirs: Directories the client declared as trusted.
        metadata: Request metadata (sender / chat_type / interaction context).
        skills: 显式传入的 skill 名列表（来自 params.skills，前端从 content 提取）。
            若提供，直接作为 skills_to_use，且 **不再对 content 做 /skills use 剥离**
            （content 原样保留，如 "帮我用 /doc写文档"）。
            若为 None，回退到从 content 文本解析 /skills use（兼容 IM/CLI 老路径），
            同样不剥离 content，仅提取 skill 名。

    Returns:
        The rendered prompt.
    """
    return UserTurn(
        text=content,
        channel=channel,
        language=language,
        files=files or {},
        trusted_dirs=trusted_dirs,
        skills=skills,
        metadata=metadata,
        origin_kind=origin_kind,
    ).render()


class JiuWenSwarm:
    """JiuWenSwarm 统一门面.

    提供：
    - SDK 工厂路由
    - 统一对外 API（create_instance, reload_agent_config, process_message, process_message_stream）
    - 公共编排（session 队列、Skills 路由、heartbeat、流式包装）
    """

    # Keep a small bounded hand-off buffer between the agent producer and the
    # WebSocket consumer.  An unbounded queue lets a fast Team runtime retain
    # every pending AgentResponseChunk while a slow client is sending earlier
    # chunks, which raises the process RSS across short-lived TUI sessions.
    STREAM_QUEUE_MAXSIZE = 64

    async def configure_process_cli_run(
        self, *, max_turns: int | None, host_tools: tuple[Any, ...]
    ) -> None:
        """Apply a transient machine invocation to the ready single-Agent adapter."""
        adapter = self._ensure_adapter()
        configure = getattr(adapter, "configure_process_cli_run", None)
        if not callable(configure):
            raise ValueError("process CLI run options require a single-Agent adapter")
        await configure(max_turns=max_turns, host_tools=host_tools)

    def __init__(self) -> None:
        self._adapter: AgentAdapter | None = None
        # PersonalContext Rail follows the Host runtime switch.  Keep the
        # latest snapshot on the facade so a lazily-created adapter inherits
        # the current state before its first rail synchronization.
        self._personal_context_runtime_enabled: bool = False
        self._sdk_name: str | None = None
        self._skill_manager = SkillManager(workspace_dir=str(get_agent_workspace_dir()))
        self._management_requests = None
        self._session_manager = SessionManager()
        self._heartbeat_service: Any | None = None
        self._permissions_changed_notifier: Callable[[], None] | None = None
        self._permissions_external_input_context_builder: Callable[..., Any] | None = None
        # Preserve an SDK-declared root Agent across the SkillNet rebuild hook.
        # Default Agent builds retain their historical no-argument reload path.
        self._runtime_agent_create_snapshot: tuple[
            dict[str, Any] | None,
            str,
            str | None,
            dict[str, Any],
        ] | None = None

    def set_hook_services(self, services) -> None:
        """Bind execution hooks supplied by the Host composition root."""
        self._hook_services = services
        adapter = getattr(self, "_adapter", None)
        setter = getattr(adapter, "set_hook_services", None)
        if callable(setter):
            setter(services)

    def _get_hook_services(self):
        services = getattr(self, "_hook_services", None)
        if services is None:
            from jiuwenswarm.server.runtime.agent_adapter.adapter_hooks import ExtensionAdapterHookServices

            services = self._hook_services = ExtensionAdapterHookServices()
        return services

    def _get_management_requests(self):
        provider = getattr(self, "_management_requests", None)
        if provider is None:
            from jiuwenswarm.server.runtime.agent_adapter.management_requests import ManagementRequests

            provider = ManagementRequests(self)
            self._management_requests = provider
        return provider

    @property
    def _session_asset_manager(self):
        return self._get_management_requests().session_asset_manager

    @_session_asset_manager.setter
    def _session_asset_manager(self, manager):
        self._get_management_requests()._session_asset_manager = manager

    @property
    def _skilldev_service(self):
        provider = getattr(self, "_management_requests", None)
        return provider._skilldev_service if provider is not None else None

    @_skilldev_service.setter
    def _skilldev_service(self, service):
        self._get_management_requests()._skilldev_service = service

    def set_heartbeat_service(self, service: Any | None) -> None:
        """Inject the AgentServer-owned Heartbeat runtime before adapter creation."""
        self._heartbeat_service = service
        # Team members are assembled declaratively outside the single-agent
        # adapter. Publish the same process-owned service to their build context;
        # this is dependency injection only, never scheduler ownership.
        from jiuwenswarm.common.heartbeat_service import set_heartbeat_job_service

        set_heartbeat_job_service(service)
        if self._adapter is not None and hasattr(self._adapter, "set_heartbeat_service"):
            self._adapter.set_heartbeat_service(service)

    @staticmethod
    def _prepare_skill_library() -> None:
        """Explicit compatibility entry for Team Skill storage preparation.

        Ordinary facade construction only uses the single-Agent library. Team
        assembly and visibility RPCs prepare their own storage before reading it.
        """
        from jiuwenswarm.agents.harness.team.bootstrap import prepare_team_storage

        prepare_team_storage()

    def _get_skilldev_service(self):
        """Delegate to the optional management provider."""
        return self._get_management_requests()._get_skilldev_service()

    def _ensure_adapter(self, *, mode: str = "agent") -> AgentAdapter:
        """确保 adapter 已初始化，如果未初始化则根据环境变量和 mode 创建."""
        if self._adapter is None:
            self._sdk_name = resolve_sdk_choice()
            self._adapter = create_adapter(self._sdk_name, mode=mode)
            hook_setter = getattr(self._adapter, "set_hook_services", None)
            if callable(hook_setter):
                hook_setter(self._get_hook_services())
            if hasattr(self._adapter, "set_skill_manager"):
                self._adapter.set_skill_manager(self._skill_manager)
            if hasattr(self._adapter, "set_heartbeat_service"):
                self._adapter.set_heartbeat_service(self._heartbeat_service)
            setter = getattr(
                self._adapter, "set_personal_context_runtime_enabled", None
            )
            if callable(setter):
                setter(self._personal_context_runtime_enabled)
            if hasattr(self._adapter, "set_permissions_changed_notifier"):
                self._adapter.set_permissions_changed_notifier(
                    self._permissions_changed_notifier
                )
            if hasattr(self._adapter, "set_permissions_external_input_context_builder"):
                self._adapter.set_permissions_external_input_context_builder(
                    self._permissions_external_input_context_builder
                )
            self._skill_manager.set_skillnet_install_complete_hook(
                self._on_skillnet_install_complete
            )
            logger.info("[JiuWenSwarm] Initialized adapter: sdk=%s, mode=%s", self._sdk_name, mode)
        return self._adapter

    def set_personal_context_runtime_enabled(self, enabled: bool) -> None:
        """Store and forward the PersonalContext Host runtime switch."""

        self._personal_context_runtime_enabled = bool(enabled)
        adapter = self._adapter
        setter = (
            getattr(adapter, "set_personal_context_runtime_enabled", None)
            if adapter is not None
            else None
        )
        if callable(setter):
            setter(self._personal_context_runtime_enabled)

    async def refresh_personal_context_rail(self) -> None:
        """Refresh the PersonalContext Rail without creating an Agent."""

        adapter = self._adapter
        refresher = (
            getattr(adapter, "refresh_personal_context_rail", None)
            if adapter is not None
            else None
        )
        if callable(refresher):
            await refresher()

    def set_permissions_changed_notifier(
        self,
        notifier: Callable[[], None] | None,
    ) -> None:
        """Inject the host composition callback for persisted permission changes."""
        self._permissions_changed_notifier = notifier
        if self._adapter is not None and hasattr(
            self._adapter, "set_permissions_changed_notifier"
        ):
            self._adapter.set_permissions_changed_notifier(notifier)

    def set_permissions_external_input_context_builder(
        self,
        builder: Callable[..., Any] | None,
    ) -> None:
        """Inject the Host external-input permission publication context."""
        self._permissions_external_input_context_builder = builder
        if self._adapter is not None and hasattr(
            self._adapter, "set_permissions_external_input_context_builder"
        ):
            self._adapter.set_permissions_external_input_context_builder(builder)

    @staticmethod
    def _adapter_mode_for_request(request: AgentRequest) -> str:
        """选择 Deep / Code adapter。

        Web 请求（显式携带 ``work_mode``）由 ``work_mode`` 决定 profile：
        ``code`` 走 CodeAdapter，``work`` 走 DeepAdapter。TUI 等历史客户端不带
        ``work_mode``，继续按完整 mode 串判定，行为不变。
        """
        params = request.params if isinstance(request.params, dict) else {}
        work_mode = read_request_work_mode(params)
        raw_mode = params.get("mode", "")
        mode = canonicalize_mode_text(raw_mode)
        if work_mode is not None and is_web_composable_mode(mode or "agent"):
            return "code" if work_mode == "code" else "agent"
        if is_code_profile_mode(mode) or mode == "code":
            return "code"
        return "agent"

    async def create_instance(
        self,
        config: dict[str, Any] | None = None,
        *,
        mode: str = "agent",
        sub_mode: str = None,
        agent_definition: dict[str, Any] | None = None,
    ) -> None:
        """初始化 Agent 实例.

        Args:
            config: 可选配置，透传给底层 adapter.
            mode: 实例化模式，"claw"（默认）或 "code"，透传给底层 adapter.
            sub_mode: 子模式
        """
        runtime_agent_snapshot = (
            (
                deepcopy(config) if config is not None else None,
                mode,
                sub_mode,
                deepcopy(agent_definition),
            )
            if agent_definition is not None
            else None
        )
        adapter = self._ensure_adapter(mode=mode)
        create_kwargs: dict[str, Any] = {"mode": mode, "sub_mode": sub_mode}
        if agent_definition is not None:
            create_kwargs["agent_definition"] = dict(agent_definition)
        await adapter.create_instance(config, **create_kwargs)
        self._runtime_agent_create_snapshot = runtime_agent_snapshot
        logger.info(
            "[JiuWenSwarm] Agent instance created: sdk=%s, mode=%s, sub_mode=%s",
            self._sdk_name, mode, sub_mode,
        )

        sm = self._session_manager
        if hasattr(adapter, "try_start_dreaming"):
            asyncio.create_task(adapter.try_start_dreaming(
                busy_checker=lambda: sm.has_active_tasks(),))

    async def _on_skillnet_install_complete(self) -> None:
        """Reload the agent and refresh live team skill rails after async install."""
        snapshot = self._runtime_agent_create_snapshot
        if snapshot is None:
            await self.create_instance()
        else:
            config, mode, sub_mode, definition = deepcopy(snapshot)
            await self.create_instance(
                config,
                mode=mode,
                sub_mode=sub_mode,
                agent_definition=definition,
            )
        await self._reload_team_skill_rails()

    @staticmethod
    async def _reload_team_skill_rails(session_id: str | None = None) -> None:
        """Re-scan the shared Skill library for live team members.

        Replaces the old shared-link refresh: teams no longer own a mirrored
        ``skills/`` directory, so nothing has to be re-linked. What still needs
        a nudge is each running member's in-memory Skill rail, which otherwise
        keeps serving the library listing from before the change.

        Args:
            session_id: Restrict the reload to one session; None reloads all.
        """
        try:
            from jiuwenswarm.agents.harness.team import reload_team_skill_views_across_managers

            await reload_team_skill_views_across_managers(session_id)
        except Exception as exc:
            logger.warning("[JiuWenSwarm] team skill view reload failed: %s", exc)

    async def _refresh_skill_rails_after_change(self) -> None:
        """轻量刷新 skill rail，避免 uninstall 后全量重建 agent 实例.

        SkillUseRail 通过 reload_skills() 重新扫描 skills_dir 并清除已删除的 skill 缓存，
        无需重建整个 agent（省去 _get_tool_cards + _build_agent_rails + create_deep_agent 开销）。
        """
        adapter = self._adapter
        if adapter is None:
            return
        if hasattr(adapter, "refresh_skill_rails"):
            await adapter.refresh_skill_rails()

    async def reload_agent_config(
            self,
            config_base: dict[str, Any] | None = None,
            env_overrides: dict[str, Any] | None = None,
            target_session_id: str | None = None,
            reload_scopes: set[str] | None = None,
    ) -> None:
        """从配置重新加载.

        Args:
            config_base: 可选的完整配置快照；传入时优先使用它而不是读取本地 config.yaml。
            env_overrides: 可选的环境变量增量；仅覆盖请求中出现的 key。
            target_session_id: 可选的目标 session id；传入时限制 session adapter 级联热更新范围。
            reload_scopes: 可选的精确配置作用域，用于定向热更新。
        """
        adapter = self._ensure_adapter()
        if hasattr(adapter, "try_stop_dreaming"):
            await adapter.try_stop_dreaming()
        await adapter.reload_agent_config(
            config_base,
            env_overrides,
            target_session_id=target_session_id,
            reload_scopes=reload_scopes,
        )
        logger.info("[JiuWenSwarm] Agent config reloaded: sdk=%s", self._sdk_name)
        if hasattr(adapter, "try_start_dreaming"):
            sm = self._session_manager
            asyncio.create_task(adapter.try_start_dreaming(
                busy_checker=lambda: sm.has_active_tasks(),))

    def has_smart_permission_lifecycle(self, config: dict[str, Any]) -> bool:
        checker = getattr(self._adapter, "has_smart_permission_lifecycle", None)
        return bool(callable(checker) and checker(config))

    async def reload_permissions_config(self, config: dict[str, Any], *, include_legacy: bool) -> None:
        if self.has_smart_permission_lifecycle(config):
            await self._adapter.notify_permissions_changed(config, include_legacy=include_legacy)
        elif include_legacy:
            await self.reload_agent_config(config_base=config, env_overrides={})

    async def prepare_kernel(self, *, mode: str, config_base: dict[str, Any] | None = None) -> Any:
        from jiuwenswarm.server.worker.profile_prewarm import prewarm_checkpoint

        await prewarm_checkpoint()
        adapter = self._ensure_adapter(mode="code" if mode.startswith("code") else "agent")
        await prewarm_checkpoint()
        prepare = getattr(adapter, "prepare_kernel", None)
        if callable(prepare):
            if config_base is None:
                return await prepare()
            else:
                return await prepare(config_base=config_base)

    async def prepare_session(
        self,
        *,
        session_id: str,
        channel_id: str,
        mode: str,
        project_dir: str | None = None,
    ) -> None:
        """Initialize and start the session-owned DeepAgent without sending input."""
        adapter = self._ensure_adapter(mode="code" if mode.startswith("code") else "agent")
        prepare = getattr(adapter, "prepare_session", None)
        if not callable(prepare):
            raise RuntimeError("active adapter does not support session prewarming")
        await prepare(
            session_id=session_id,
            channel_id=channel_id,
            mode=mode,
            project_dir=project_dir,
        )

    def build_inputs(self, request: AgentRequest) -> Tuple[dict[str, Any], str, UserTurn]:
        """构建 adapter 所需的 inputs 字典（公共接口）."""
        return self._build_inputs(request)

    def _build_inputs(self, request: AgentRequest) -> Tuple[dict[str, Any], str, UserTurn]:
        """构建 adapter 所需的 inputs 字典.

        Returns:
            ``(inputs, memory_mode, turn)`` — ``turn`` is the rendered user turn;
            ``turn.text`` keeps the user's own words for callers that parse them.
        """
        from openjiuwen.core.session.interaction.interactive_input import InteractiveInput
        from jiuwenswarm.server.runtime.agent_adapter.request_context import RequestContext
        from jiuwenswarm.server.runtime.debug_trace.directives import strip_debug_directive

        config_base = get_config()
        memory_mode = get_memory_mode(config_base)
        context = RequestContext.from_request(request, config_base, strip_debug=strip_debug_directive)
        params, query, metadata = context.params, context.query, context.metadata
        starter_context = _starter_interaction_context(params, query)
        if starter_context:
            previous_context = metadata.get("interaction_context")
            metadata["interaction_context"] = (
                f"{previous_context}\n\n{starter_context}" if previous_context else starter_context
            )
        request_channel, channel, language = context.request_channel, context.channel, context.language
        trusted_dirs, skills = context.trusted_dirs, context.skills
        project_dir, cwd = context.project_dir, context.cwd
        _request_debug = context.request_debug
        if self._is_malformed_team_plan_approval_payload(params):
            raise _TeamPlanApprovalPayloadError(self._team_plan_approval_payload_error_message())
        origin_kind = (
            HOST_USER_ORIGIN_EXTERNAL
            if is_external_user_authored_dispatch(
                params, channel_id=channel, request_method=request.req_method, metadata=request.metadata,
            )
            else HOST_USER_ORIGIN_INTERNAL
        )
        if request.metadata and request.metadata.get("interaction_context"):
            logger.info(
                "[_build_inputs][DEBUG] request.params.query=\n%s",
                query[:2000] if isinstance(query, str) else str(query)[:2000],
            )

        # One turn, one renderer: single-agent and team both deliver
        # ``turn.render()``. The team path additionally keeps ``turn.text`` to
        # parse directives / ``$member`` routing before it renders.
        turn = UserTurn(
            text=query,
            channel=channel,
            language=language,
            files=params.get("files", {}) or {},
            trusted_dirs=trusted_dirs,
            skills=skills,
            metadata=metadata,
            origin_kind=origin_kind,
        )

        if isinstance(query, InteractiveInput):
            final_query = query
        else:
            answers = params.get("answers", [])
            if answers:
                request_id = params.get("request_id", "")
                source = params.get("source", "")
                interactive_input = self._build_interactive_input_from_answers(
                    request_id,
                    answers,
                    source,
                )
                if interactive_input is not None:
                    final_query = interactive_input
                    turn = turn.with_text(interactive_input)
                else:
                    final_query = turn.render()
            else:
                final_query = turn.render()
                # 调试日志：确认 /statusline 是否已改写为内置子代理调度
                if isinstance(query, str) and "/statusline" in query:
                    logger.info(
                        "[_build_inputs][STATUSLINE] 原始 query=%s, 最终 prompt 长度=%d, "
                        "包含 statusline-setup 调度=%s",
                        query[:200],
                        len(final_query) if isinstance(final_query, str) else 0,
                        "statusline-setup" in final_query if isinstance(final_query, str) else False,
                    )

        inputs: dict[str, Any] = {
            "conversation_id": request.session_id,
            "query": final_query,
            "channel": channel,
            "language": language,
            # Only an explicit false disables interactive tools. Existing
            # clients that omit this capability remain backward compatible.
            "supports_user_interaction": params.get("supports_user_interaction") is not False,
        }
        if _request_debug:
            inputs["_request_debug"] = True
        skip_a2ui = bool(
            request.metadata and request.metadata.get("skip_a2ui") is True
        )
        if skip_a2ui:
            inputs["skip_a2ui"] = True

        # 传递 enable_memory 参数
        enable_memory = request.metadata.get("enable_memory", True) if request.metadata else True
        inputs["enable_memory"] = enable_memory

        # 传递 trusted_dirs 参数（用于 RuntimePromptRail 添加路径限制策略）
        if trusted_dirs:
            inputs["trusted_dirs"] = trusted_dirs
        if project_dir:
            inputs["project_dir"] = project_dir
        if cwd:
            inputs["cwd"] = cwd

        run = params.get("run")
        if run:
            inputs["run"] = run

        # 处理 cron 字段：将 params.cron 转换为 run 结构
        # scheduler 使用 params.cron 标识定时任务，需要转换为 run.kind="cron"
        # cron 信息放到 RunContext.extra 中
        cron = params.get("cron")
        if cron:
            inputs["run"] = {
                "kind": "cron",
                "context": {"extra": {"cron": cron}},
            }

        # DeepAgent normalizes inputs to a fixed InvokeInputs schema, so loose
        # top-level fields such as ``channel`` do not reach model-call rails.
        # RunContext.extra is the SDK-supported request-scoped extension point
        # and survives every ReAct iteration independently of the Agent mode.
        inputs["run"] = _with_request_runtime_context(
            inputs.get("run"),
            channel=request_channel,
            skip_a2ui=skip_a2ui,
        )
        # Only Host-authenticated provenance may select Agent/tool input. Never
        # trust a client-supplied copy inside the SDK's extensible run context.
        run_extra = inputs["run"]["context"]["extra"]
        run_extra.pop(SESSION_MESSAGE_INTERNAL_KEY, None)
        cross_session = metadata.get(SESSION_MESSAGE_INTERNAL_KEY)
        if isinstance(cross_session, dict):
            run_extra[SESSION_MESSAGE_INTERNAL_KEY] = dict(cross_session)

        # Per-request workspace_dir scopes one prompt's cwd to the given
        # directory; threaded into inputs["cwd"] which downstream init_cwd
        # installs onto openjiuwen's CwdState ContextVar. See E2A-protocol.md
        # section 11.6 for the wire contract and precedence rules.
        workspace_dir = params.get("workspace_dir")
        if isinstance(workspace_dir, str) and workspace_dir.strip():
            expanded = Path(workspace_dir).expanduser().resolve()
            try:
                expanded.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                logger.warning(
                    "[JiuWenSwarm] workspace_dir %s mkdir failed (%s); "
                    "request falls back to params.cwd or the global default",
                    workspace_dir, exc,
                )
            else:
                # Scope BOTH cwd and workspace so the agent's tools (which
                # read get_cwd() for relative-path resolution) AND its
                # fs_operation sandbox (which gates absolute-path writes by
                # workspace membership) agree on the per-request root.
                inputs["cwd"] = str(expanded)
                inputs["workspace_dir"] = str(expanded)

        # The turn carries both the user's own words (``turn.text``, needed by
        # the team path for directive / ``$member`` / slash parsing) and the
        # single renderer that produced ``inputs["query"]``.
        return inputs, memory_mode, turn

    def _make_retry_without_a2ui_call(
            self,
            *,
            adapter: AgentAdapter,
            request: AgentRequest,
    ):
        async def retry_without_a2ui_call(query: str) -> str | None:
            if getattr(adapter, "_instance", None) is None:
                return None
            try:
                modified_request = AgentRequest(
                    request_id=request.request_id,
                    channel_id=request.channel_id,
                    session_id=request.session_id,
                    chat_id=request.chat_id,
                    req_method=request.req_method,
                    params={**request.params, "query": query},
                    is_stream=False,
                    timestamp=request.timestamp,
                    metadata={**(request.metadata or {}), "skip_a2ui": True},
                )
                retry_inputs, _, _ = self._build_inputs(modified_request)
                retry_inputs["_invoke_turn_id"] = request.request_id
                result = await adapter.process_message_impl(modified_request, retry_inputs)
                if result.ok and result.payload.get("content"):
                    return str(result.payload["content"])
            except Exception as exc:
                logger.warning(
                    "Retry without A2UI failed: request_id=%s error=%s",
                    request.request_id,
                    exc,
                )
            return None

        return retry_without_a2ui_call

    @staticmethod
    def _team_plan_approval_payload_error_message() -> str:
        return (
            "Malformed team.plan approval answer: expected structured "
            "`confirm_interrupt` payload with `plan_approval_kind`, "
            "`plan_content`, and `plan_language`."
        )

    @classmethod
    def _is_malformed_team_plan_approval_payload(cls, params: dict[str, Any]) -> bool:
        return (
            is_team_plan_mode(params.get("mode"))
            and str(params.get("source") or "").strip() == "confirm_interrupt"
            and isinstance(params.get("answers"), list)
            and bool(params.get("answers"))
            and "plan_approval_kind" in params
            and not cls._is_team_plan_confirm_answer(params)
        )

    def _make_retry_without_a2ui_call(
            self,
            *,
            adapter: AgentAdapter,
            request: AgentRequest,
    ):
        async def retry_without_a2ui_call(query: str) -> str | None:
            if getattr(adapter, "_instance", None) is None:
                return None
            try:
                modified_request = AgentRequest(
                    request_id=request.request_id,
                    channel_id=request.channel_id,
                    session_id=request.session_id,
                    chat_id=request.chat_id,
                    req_method=request.req_method,
                    params={**request.params, "query": query},
                    is_stream=False,
                    timestamp=request.timestamp,
                    metadata={**(request.metadata or {}), "skip_a2ui": True},
                )
                retry_inputs, _, _ = self._build_inputs(modified_request)
                retry_inputs["_invoke_turn_id"] = request.request_id
                result = await adapter.process_message_impl(modified_request, retry_inputs)
                if result.ok and result.payload.get("content"):
                    return str(result.payload["content"])
            except Exception as exc:
                logger.warning(
                    "Retry without A2UI failed: request_id=%s error=%s",
                    request.request_id,
                    exc,
                )
            return None

        return retry_without_a2ui_call

    @staticmethod
    def _build_interactive_input_from_answers(
            request_id: str,
            answers: list[dict],
            source: str = "",
    ) -> Any:
        """从用户答案构建 InteractiveInput.

        Args:
            request_id: 工具调用 ID
            answers: 用户答案列表，每个答案对应一个问题
            source: 中断来源，用于区分 PermissionRail 和 AskUserRail

        Returns:
            InteractiveInput 实例
        """
        from openjiuwen.core.session.interaction.interactive_input import InteractiveInput

        interactive_input = InteractiveInput()

        if source == "ask_user_interrupt":
            answers_dict = {}
            free_text_answer: str | None = None
            for answer in answers:
                if isinstance(answer, dict):
                    question_text = str(answer.get("question", "") or "").strip()
                    selected_options = answer.get("selected_options", [])
                    custom_input = str(answer.get("custom_input", "") or "").strip()
                    if selected_options and isinstance(selected_options, list):
                        # Normalize each option to a stripped string, drop empties.
                        cleaned_options = [
                            str(raw_option or "").strip()
                            for raw_option in selected_options
                            if str(raw_option or "").strip()
                        ]
                        if custom_input:
                            # "Other" is only a UI placeholder. Preserve normal
                            # multi-select choices and append the user's text.
                            normal_options = [
                                option for option in cleaned_options if option != "Other"
                            ]
                            if normal_options:
                                answer_value: Any = [*normal_options, custom_input]
                            else:
                                answer_value = custom_input
                        elif len(cleaned_options) == 1:
                            # Bare "Other" without custom text is incomplete (#2330).
                            sole = cleaned_options[0]
                            answer_value = "" if sole == "Other" else sole
                        elif cleaned_options:
                            # Multi-select: preserve real choices; drop placeholder-only Other.
                            normal_options = [
                                option for option in cleaned_options if option != "Other"
                            ]
                            answer_value = normal_options if normal_options else ""
                        else:
                            answer_value = ""
                    elif custom_input:
                        answer_value = custom_input
                    else:
                        answer_value = ""
                    if question_text and answer_value:
                        answers_dict[question_text] = answer_value
                    elif answer_value:
                        free_text_answer = (
                            answer_value
                            if isinstance(answer_value, str)
                            else ", ".join(answer_value)
                        )
            if not answers_dict and free_text_answer:
                answers_dict["__free_text__"] = free_text_answer
            interactive_input.update(request_id, {"answers": answers_dict})
            logger.info(
                "[JiuWenSwarm] AskUserRail InteractiveInput.update: request_id=%s "
                "answer_count=%s",
                request_id,
                len(answers_dict),
            )
            return interactive_input

        if source in EVOLUTION_INTERRUPT_METADATA_SOURCES:
            answer = answers[0] if answers else {}
            selected_options = answer.get("selected_options", []) if isinstance(answer, dict) else []
            custom_input = answer.get("custom_input", "") if isinstance(answer, dict) else ""
            value = str(selected_options[0] if selected_options else "").strip()
            action_by_value = {
                "accept": "allow_once",
                "接收": "allow_once",
                "接受": "allow_once",
                "allow_once": "allow_once",
                "本次允许": "allow_once",
                "Allow Once": "allow_once",
                "allow_always": "allow_always",
                "总是允许": "allow_always",
                "Always Allow": "allow_always",
                "reject": "reject",
                "拒绝": "reject",
                "Reject": "reject",
            }
            action = action_by_value.get(value)
            if action is None:
                _warn_unrecognised_approval_option(
                    "SkillEvolutionApproval", source, request_id, value, "rejected_as_unknown_option"
                )
                action = "reject"
            payload = {"action": action}
            if custom_input:
                payload["feedback"] = custom_input
            interactive_input.update(request_id, payload)
            logger.info(
                "[JiuWenSwarm] SkillEvolutionApproval InteractiveInput.update: request_id=%s payload=%s",
                request_id, payload
            )
            return interactive_input

        if source and source not in {
            "permission_interrupt",
            "confirm_interrupt",
        }:
            return None

        answer = answers[0] if answers else {}
        selected_options = answer.get("selected_options", []) if isinstance(answer, dict) else []
        custom_input = answer.get("custom_input", "") if isinstance(answer, dict) else ""

        value = selected_options[0] if selected_options else ""

        # 选项字符串来自各渲染端：web / CLI 回传 ``value``，TUI 回传 ``label``。
        # 统一走 ``permission_options`` 的词表解析，别在这里再维护一份字面量元组。
        action = resolve_permission_action(value)

        if action == ALLOW_ONCE:
            confirm_payload = {"approved": True, "auto_confirm": False, "feedback": ""}
        elif action == SESSION_ALLOW:
            confirm_payload = {
                "approved": True,
                "auto_confirm": True,
                "persist_allow": False,
                "feedback": "",
            }
        elif action == ALWAYS_ALLOW:
            confirm_payload = {
                "approved": True,
                "auto_confirm": True,
                "persist_allow": True,
                "feedback": "",
            }
        elif value in PLAN_EXECUTE_OPTION_VALUES:
            # Web 的"执行"：批准并退出 plan，但本轮不再调模型。真正的执行由前端
            # 紧接着补发的普通消息开启新一轮完成。``plan_execute`` 是额外键，
            # ConfirmPayload 会忽略它，rail 在校验前从原始 dict 读取。
            confirm_payload = {
                "approved": True,
                "auto_confirm": False,
                "feedback": "",
                "plan_execute": True,
            }
        elif value in PLAN_SKIP_OPTION_VALUES:
            # Web 的"跳过"：不退出 plan，也不继续修改，直接结束本轮。
            # ``plan_skip`` 是额外键，ConfirmPayload 会忽略它；
            # PlanApprovalInterruptRail 在校验前从原始 dict 读取该标记。
            confirm_payload = {
                "approved": False,
                "auto_confirm": False,
                "feedback": custom_input
                or plan_skip_feedback(get_config().get("preferred_language")),
                "plan_skip": True,
            }
        elif value in PLAN_REVISE_OPTION_VALUES:
            # Web 的"下一步"：不退出 plan，按修改意见续跑。``plan_revise`` 是额外键，
            # ConfirmPayload 会忽略它；rail 只在看到它时才给假回执包修订前缀。
            # TUI 仍发 ``reject``，不会进这个分支。
            confirm_payload = {
                "approved": False,
                "auto_confirm": False,
                "feedback": custom_input or "用户希望继续规划",
                "plan_revise": True,
            }
        elif action == REJECT:
            feedback = custom_input or (
                "用户希望继续规划" if is_keep_planning_value(value) else "用户拒绝"
            )
            confirm_payload = {"approved": False, "auto_confirm": False, "feedback": feedback}
        elif custom_input:
            # 选项没认出来，但用户填了自由文本，就用文本当反馈继续拒绝。取值仍然
            # 是没解出来的，同样要告警。
            _warn_unrecognised_approval_option(
                "PermissionRail", source, request_id, value, "rejected_with_custom_input"
            )
            confirm_payload = {"approved": False, "auto_confirm": False, "feedback": custom_input}
        else:
            _warn_unrecognised_approval_option(
                "PermissionRail", source, request_id, value, "rejected_as_unknown_option"
            )
            confirm_payload = {"approved": False, "auto_confirm": False, "feedback": f"未知选项: {value}"}

        card_ids = (
            _permission_card_ids_from_answers(answers)
            if source == "permission_interrupt" else []
        )
        if not card_ids:
            interactive_input.update(request_id, confirm_payload)
            return interactive_input
        interactive_input.update(card_ids[0], confirm_payload)
        logger.info(
            "[JiuWenSwarm] PermissionRail card InteractiveInput.update: "
            "request_id=%s card_id=%s",
            request_id,
            card_ids[0],
        )
        return interactive_input

    async def _handle_skilldev_request(self, request: AgentRequest) -> AgentResponse | None:
        """Delegate to the optional management provider."""
        if request.req_method not in _SKILLDEV_METHODS:
            return None
        return await self._get_management_requests()._handle_skilldev_request(request)

    async def _handle_skills_request(self, request: AgentRequest) -> AgentResponse | None:
        """Delegate to the optional management provider."""
        if request.req_method not in (_SKILL_ROUTES.keys() | _SKILL_SYNC_WS_REJECTED):
            return None
        return await self._get_management_requests()._handle_skills_request(request)

    async def _handle_session_asset_request(self, request: AgentRequest) -> AgentResponse | None:
        """Delegate to the optional management provider."""
        if request.req_method not in _SESSION_ASSET_ROUTES:
            return None
        return await self._get_management_requests()._handle_session_asset_request(request)

    @staticmethod
    def _is_skills_rebuild_followup(payload: Any) -> bool:
        """Delegate to the optional management provider."""
        from jiuwenswarm.server.runtime.agent_adapter.management_requests import ManagementRequests

        return ManagementRequests._is_skills_rebuild_followup(payload)

    @staticmethod
    def _is_skills_create_from_knowledge_followup(payload: Any) -> bool:
        """Delegate to the optional management provider."""
        from jiuwenswarm.server.runtime.agent_adapter.management_requests import ManagementRequests

        return ManagementRequests._is_skills_create_from_knowledge_followup(payload)

    async def _run_skills_rebuild_followup(
        self,
        request: AgentRequest,
        payload: dict[str, Any],
    ) -> None:
        """Delegate to the optional management provider."""
        return await self._get_management_requests()._run_skills_rebuild_followup(request, payload)

    @staticmethod
    def _coerce_optional_str_list(raw: Any) -> list[str]:
        """Delegate to the optional management provider."""
        from jiuwenswarm.server.runtime.agent_adapter.management_requests import ManagementRequests

        return ManagementRequests._coerce_optional_str_list(raw)

    @staticmethod
    def _build_skills_knowledge_followup_request(
        request: AgentRequest,
        *,
        followup: str,
        skills: list[str],
        trusted_dirs: list[str],
        input_file: str,
    ) -> AgentRequest:
        """Delegate to the optional management provider."""
        from jiuwenswarm.server.runtime.agent_adapter.management_requests import ManagementRequests

        return ManagementRequests._build_skills_knowledge_followup_request(request, followup=followup, skills=skills, trusted_dirs=trusted_dirs, input_file=input_file)

    @staticmethod
    def _cleanup_knowledge_upload_file(input_file: str) -> None:
        """Delegate to the optional management provider."""
        from jiuwenswarm.server.runtime.agent_adapter.management_requests import ManagementRequests

        return ManagementRequests._cleanup_knowledge_upload_file(input_file)

    async def _run_skills_create_from_knowledge_silent(
        self,
        request: AgentRequest,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Delegate to the optional management provider."""
        return await self._get_management_requests()._run_skills_create_from_knowledge_silent(request, payload)

    async def _handle_plugins_request(self, request: AgentRequest) -> AgentResponse | None:
        """Delegate to the optional management provider."""
        if request.req_method not in _PLUGIN_ROUTES:
            return None
        return await self._get_management_requests()._handle_plugins_request(request)

    async def _handle_package_catalog_request(self, request: AgentRequest) -> AgentResponse | None:
        """Delegate to the optional management provider."""
        if request.req_method not in _PACKAGE_ROUTES:
            return None
        from jiuwenswarm.server.runtime.agent_adapter.management_requests import ManagementRequests

        # Catalog read operations also support the legacy unbound entry point.
        provider = self._get_management_requests() if isinstance(self, JiuWenSwarm) else ManagementRequests(self)
        return await provider._handle_package_catalog_request(request)

    async def _process_interrupt(self, request: AgentRequest) -> AgentResponse:
        """处理 interrupt 请求.
    
        根据 intent 分流：
        - pause: 暂停 ReAct 循环（不取消任务）
        - resume: 恢复已暂停的 ReAct 循环
        - cancel: 取消当前 session 正在运行的任务
        - supplement: 取消当前任务但保留 todo
    
        Args:
            request: AgentRequest，params 中可包含：
                - intent: 中断意图 ('pause' | 'cancel' | 'resume' | 'supplement')
                - new_input: 新的用户输入（用于切换任务）
    
        Returns:
            AgentResponse 包含 interrupt_result 事件数据
        """
        intent = request.params.get("intent", "cancel")
        session_id = self._session_manager.get_session_id(request.session_id)
        is_team_mode = is_team_params(request.params if isinstance(request.params, dict) else None)
    
        if is_team_mode:
            return await self._process_team_interrupt(
                request=request,
                intent=intent,
                session_id=session_id,
            )
    
        adapter = self._ensure_adapter(mode=self._adapter_mode_for_request(request))
        external_session_checker = getattr(
            adapter,
            "is_external_harness_session",
            None,
        )
        is_external_session = bool(
            callable(external_session_checker)
            and external_session_checker(request.session_id)
        )
    
        if intent == "pause":
            # 暂停：不取消任务，只暂停 ReAct 循环
            return await adapter.process_interrupt(request)
    
        if intent == "resume":
            # 恢复：恢复 ReAct 循环
            return await adapter.process_interrupt(request)
    
        if intent == "supplement":
            # 取消当前 session 的任务
            response = await adapter.process_interrupt(request)
            if is_external_session:
                return response
            await self._session_manager.cancel_session_task(session_id, "interrupt(supplement): ")
            return response
    
        # cancel: 先调用 adapter.process_interrupt（此时 session 仍在 _active_session_ids 中，
        # guard 能通过），再 cancel_session_task（其 finally 会把 session 从 _active_session_ids 移除）。
        # 顺序不能反，否则 process_interrupt 的 session guard 会误判为 "not active" 而跳过 abort。
        response = await adapter.process_interrupt(request)
        if is_external_session:
            return response
        await self._cancel_team_work_for_session(
            session_id,
            request.channel_id,
            log_prefix=f"interrupt(intent={intent}): ",
        )
        await self._session_manager.cancel_session_task(
            session_id,
            f"interrupt(intent={intent}): ",
            wait_timeout=5.0,
        )
        return response

    @staticmethod
    def _build_interrupt_result_response(
        request: AgentRequest,
        *,
        intent: str,
        success: bool,
        message: str,
    ) -> AgentResponse:
        return AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=True,
            payload={
                "event_type": "chat.interrupt_result",
                "intent": intent,
                "success": success,
                "message": message,
            },
            metadata=request.metadata,
        )

    async def _process_team_interrupt(
        self,
        *,
        request: AgentRequest,
        intent: str,
        session_id: str,
    ) -> AgentResponse:
        """Handle interrupt requests for Team mode.
    
        Team runtime is persistent and owned by openjiuwen Runner pool. For team sessions:
        - pause stops the foreground stream and parks the runtime in paused state
          via Runner.pause_agent_team, allowing same-session resume.
        - cancel removes the runtime from Runner pool via Runner.stop_agent_team,
          preventing pool/DB inconsistency for subsequent sessions.
        - resume is not a first-class runtime action. Users should send the next
          message directly to continue a paused session.
        """
        from jiuwenswarm.agents.harness.team import get_team_manager
    
        team_manager = get_team_manager(request.channel_id)
        reason = f"interrupt(intent={intent}): "
    
        if intent == "resume":
            return self._build_interrupt_result_response(
                request,
                intent=intent,
                success=True,
                message="团队暂停后，直接发送下一条消息即可继续。",
            )
    
        if intent in {"pause", "cancel"}:
            if intent == "pause":
                paused = await team_manager.pause_session_runtime(session_id, reason=reason)
                if paused:
                    # The persistent foreground stream survives pause, so the
                    # gateway's stream-exit fallback never fires.  Emit the
                    # terminal frame explicitly; shape matches the old
                    # gateway fallback exactly.
                    await team_manager.broadcast_event(
                        session_id,
                        {
                            "event_type": "chat.processing_status",
                            "session_id": session_id,
                            "is_processing": False,
                            "is_complete": True,
                        },
                        channel_id=request.channel_id,
                    )
                await self._session_manager.cancel_session_task(
                    session_id,
                    reason,
                    wait_timeout=5.0,
                )
                message = "团队已暂停" if paused else "当前没有可暂停的团队任务"
            else:
                # 断连兜底（client_disconnect）→ pause_all 保账本（可冷启动续跑）；用户主动终止 → stop_all 落 seal。
                metadata = request.metadata if isinstance(request.metadata, dict) else {}
                cancel_source = metadata.get(E2A_INTERNAL_CANCEL_SOURCE_KEY)
                workflow_disposition = (
                    "pause"
                    if cancel_source == E2A_CANCEL_SOURCE_CLIENT_DISCONNECT
                    else "stop"
                )
                cancelled = await team_manager.cancel_session_runtime(
                    session_id, reason=reason, workflow_disposition=workflow_disposition
                )
                await self._session_manager.cancel_session_task(
                    session_id,
                    reason,
                    wait_timeout=5.0,
                )
                message = "团队当前执行已结束" if cancelled else "当前没有可取消的团队任务"
            success = paused if intent == "pause" else cancelled
            return self._build_interrupt_result_response(
                request,
                intent=intent,
                success=success,
                message=message,
            )
    
        return self._build_interrupt_result_response(
            request,
            intent=intent,
            success=False,
            message=f"团队模式暂不支持中断意图: {intent}",
        )

    async def _cancel_team_work_for_session(
        self,
        session_id: str,
        channel_id: str | None = None,
        log_prefix: str = "",
    ) -> bool:
        """终止当前 session 的 Team runtime（若存在）。"""
        from jiuwenswarm.common.team_runtime_registry import get_existing_team_manager

        try:
            team_manager = get_existing_team_manager()
            if team_manager is None:
                return False
            return await team_manager.terminate_session_runtime(session_id, reason=log_prefix)
        except Exception:
            logger.exception(
                "[JiuWenSwarm] failed to terminate team runtime: session_id=%s",
                session_id,
            )
            return False

    @staticmethod
    def _is_team_plan_confirm_answer(params: dict[str, Any]) -> bool:
        """Return True for structured team.plan approval answers."""
        request_id = str(params.get("request_id") or "").strip()
        answers = params.get("answers")
        if not request_id or not isinstance(answers, list) or not answers:
            return False

        source = str(params.get("source") or "").strip()
        if source != "confirm_interrupt":
            return False
        if str(params.get("plan_approval_kind") or "").strip() != "plan_approval":
            return False
        if "plan_content" not in params:
            return False
        plan_language = str(params.get("plan_language") or "").strip().lower()
        return plan_language in {"cn", "en"}

    async def process_message(self, request: AgentRequest) -> AgentResponse:
        """Process a request through the facade-owned Session scheduler."""
        return await self._process_message(request, schedule_session=True)

    async def execute_message(self, request: AgentRequest) -> AgentResponse:
        """Execute one request when scheduling is owned by AgentRuntime."""
        return await self._process_message(request, schedule_session=False)

    async def _process_message(
        self,
        request: AgentRequest,
        *,
        schedule_session: bool,
    ) -> AgentResponse:
        """处理非流式请求.
    
        支持多 session 并发执行，同 session 内任务按先进后出顺序执行.
        """
        if request.req_method == ReqMethod.CHAT_CANCEL:
            return await self._process_interrupt(request)
    
        if request.req_method == ReqMethod.CHAT_ANSWER:
            adapter = self._ensure_adapter(mode=self._adapter_mode_for_request(request))
            validator = getattr(
                adapter, "validate_auto_permission_workspace_request", None
            )
            if callable(validator):
                validator(request)
            return await adapter.handle_user_answer(request)
    
        if request.req_method == ReqMethod.CHAT_SWARMFLOW_REPLY:
            adapter = self._ensure_adapter(mode=self._adapter_mode_for_request(request))
            return await adapter.handle_swarmflow_reply(request)
    
        # Non-stream goal command (GET, PAUSE, CLEAR)
        if request.req_method == ReqMethod.COMMAND_GOAL:
            try:
                adapter = self._ensure_adapter(mode=self._adapter_mode_for_request(request))
                params = request.params if isinstance(request.params, dict) else {}
                action = params.get("action", "get")
                session_id = self._session_manager.get_session_id(request.session_id)
                logger.info(
                    "[Goal] COMMAND_GOAL received: request_id=%s action=%s "
                    "resolved_session_id=%s",
                    request.request_id, action, session_id,
                )
                # Pass protocol fields straight to the Goal capability adapter.
                goal_result = await adapter.handle_goal_command_structured(params, session_id)
                if goal_result is not None:
                    result_type = goal_result.get("result_type")
                    ok = result_type not in {"goal_error", "goal_confirm_required"}
                    # Only set writes user history (objective as the user turn).
                    # pause / resume / clear / get stay control-only.
                    # 忙碌时与流式路径同一 helper：推迟到上一轮收尾再落盘。
                    if ok and str(action or "").strip().lower() == "set":
                        goal_obj = goal_result.get("goal")
                        record_fn = getattr(adapter, "_record_goal_set_history_if_needed", None)
                        if callable(record_fn):
                            history_result = record_fn(
                                request,
                                action=str(action),
                                result_type=str(result_type) if result_type else None,
                                goal_payload=goal_obj if isinstance(goal_obj, dict) else None,
                            )
                            if inspect.isawaitable(history_result):
                                await history_result
                        else:
                            objective = str(params.get("objective") or "").strip()
                            if objective:
                                goal_id = (
                                    str(goal_obj.get("goal_id") or "").strip() or None
                                    if isinstance(goal_obj, dict)
                                    else None
                                )
                                await _run_history_io(
                                    append_history_record,
                                    session_id=session_id,
                                    request_id=request.request_id,
                                    channel_id=request.channel_id,
                                    role="user",
                                    content=objective,
                                    timestamp=time.time(),
                                    channel_metadata=request.metadata,
                                    mode=params.get("mode", "unknown"),
                                    extra={
                                        "goal_id": goal_id,
                                        "is_goal_objective_message": True,
                                    },
                                )
                    # Keep message for callers that read payload.message; also
                    # mirror into error on failure so Gateway top-level error
                    # forwarding and older clients stay consistent.
                    human_text = goal_result.get("output", goal_result.get("error", ""))
                    payload = {
                        "action": goal_result.get("action", action),
                        "message": human_text,
                        "goal": goal_result.get("goal"),
                        # Keep this field for the existing TUI command
                        # surface; it is a copy of the authoritative goal.
                        "record": goal_result.get("goal"),
                        "cleared_goal": goal_result.get("cleared_goal"),
                        "existing_goal": goal_result.get("existing_goal"),
                        "requested_objective": goal_result.get("requested_objective"),
                        "code": goal_result.get("error_code"),
                    }
                    if not ok and human_text:
                        payload["error"] = human_text
                    return AgentResponse(
                        request_id=request.request_id,
                        channel_id=request.channel_id,
                        ok=ok,
                        payload=payload,
                        metadata=request.metadata,
                    )
                return AgentResponse(
                    request_id=request.request_id,
                    channel_id=request.channel_id,
                    ok=False,
                    payload={"error": "Goal command not handled"},
                    metadata=request.metadata,
                )
            except Exception as exc:
                logger.exception(
                    "[JiuWenSwarm] COMMAND_GOAL 处理失败: request_id=%s error=%s",
                    request.request_id,
                    exc,
                )
                return AgentResponse(
                    request_id=request.request_id,
                    channel_id=request.channel_id,
                    ok=False,
                    payload={"error": f"Goal command error: {exc}"},
                    metadata=request.metadata,
                )
    
        # 无状态请求（skills / skilldev / plugins / Skill Graph）不需要 adapter，
        # 在 _ensure_adapter 之前检查，避免触发 adapter 懒初始化。
        # COMMAND_GOAL 已在上方单独处理（其内部按需 ensure），不影响本顺序。
        skilldev_response = await self._handle_skilldev_request(request)
        if skilldev_response is not None:
            return skilldev_response
    
        skills_response = await self._handle_skills_request(request)
        if skills_response is not None:
            return skills_response
    
        session_asset_response = await self._handle_session_asset_request(request)
        if session_asset_response is not None:
            return session_asset_response
    
        plugins_response = await self._handle_plugins_request(request)
        if plugins_response is not None:
            return plugins_response
    
        package_catalog_response = await self._handle_package_catalog_request(request)
        if package_catalog_response is not None:
            return package_catalog_response
    
        adapter = self._ensure_adapter(mode=self._adapter_mode_for_request(request))
        validator = getattr(adapter, "validate_auto_permission_workspace_request", None)
        if callable(validator):
            validator(request)
    
        heartbeat_response = await adapter.handle_heartbeat(request)
        if heartbeat_response is not None:
            return heartbeat_response
    
        session_id = self._session_manager.get_session_id(request.session_id)
        if isinstance(request.params, dict):
            restore_chat_send_equipment_params(session_id, request.params)
        from jiuwenswarm.agents.harness.common.recommendation import prompt_suggestion
        prompt_suggestion.forks.request_started(
            session_id,
            person_turn=_person_drove_turn(
                request,
                is_team_mode=(
                    request.params.get("team", False)
                    or is_team_runtime_mode(request.params.get("mode", ""))
                ),
            ),
        )
        query = request.params.get("query", "")
        feedback_scheduled = _session_message_owns_completion(request)
        completion_receipt: Future[None] | None = None
        waiting_for_user = False

        def _schedule_feedback_once(terminal_status: str) -> None:
            nonlocal feedback_scheduled, completion_receipt
            if feedback_scheduled:
                return
            completion_receipt = _complete_history_request(
                session_id,
                request.request_id,
                terminal_status=terminal_status,
            )
            feedback_scheduled = True
            prompt_suggestion.forks.turn_ended(
                session_id, request.request_id, status=terminal_status,
                waiting_for_user=waiting_for_user, completion=completion_receipt,
            )
        # proactive_recommendation 是系统触发的推荐指令（不是用户说的话），不写 user
        # history——否则刷新页面会显示"[主动推荐指令] xxx"这种用户没说过的消息。
        if _should_record_user_history(request.params):
            await _run_history_io(
                append_history_record,
                session_id=session_id,
                request_id=request.request_id,
                channel_id=request.channel_id,
                role="user",
                content=_history_user_content(request.params, query),
                timestamp=time.time(),
                extra=_history_user_extra(request.params),
                channel_metadata=request.metadata,
                mode=request.params.get("mode", "unknown"),
            )
        # 注：ask_user_answer 的落盘在 deliver_control_input 里——ask_user_interrupt
        # resume 走 control-input 路径，不经过本方法。
    
        logger.info(
            "[JiuWenSwarm] 处理请求: request_id=%s channel_id=%s session_id=%s sdk=%s",
            request.request_id, request.channel_id, session_id, self._sdk_name,
        )
    
        try:
            inputs, memory_mode, user_turn = self._build_inputs(request)
        except asyncio.CancelledError:
            _schedule_feedback_once("cancelled")
            raise
        except _TeamPlanApprovalPayloadError as exc:
            _schedule_feedback_once("error")
            return AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(exc)},
                metadata=request.metadata,
            )
        except Exception:
            _schedule_feedback_once("error")
            raise

        # Session-level MCP enable: reconcile to explicit mcp ∪ equipment
        # connectors before the agent runs. Always pass a list (never None):
        # empty clears selection when neither side contributes names.
        params = request.params if isinstance(request.params, dict) else {}
        try:
            runtime_binding = await self._resolve_runtime_binding_for_request(
                adapter,
                request,
                session_id,
            )
        except (OSError, TypeError, ValueError) as exc:
            return AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(exc)},
                metadata=request.metadata,
            )
        reconcile_kwargs: dict[str, Any] = {
            "model_name": params.get("model_name"),
            "history_before_request_id": request.request_id,
        }
        if runtime_binding is not None:
            reconcile_kwargs["runtime_binding"] = runtime_binding
        if params.get("model_selection") is not None:
            reconcile_kwargs["model_selection"] = params["model_selection"]
        if "moa_enabled" in params:
            reconcile_kwargs["moa_enabled"] = params["moa_enabled"]
        moa_access = getattr(adapter, "moa_access_kwargs", None)
        if callable(moa_access):
            reconcile_kwargs.update(moa_access(request))
        reconcile_kwargs["execution_request"] = request
        try:
            await self.reconcile_session_mcp(
                request.session_id,
                compute_chat_send_mcp_needed(params),
                **reconcile_kwargs,
            )
        except asyncio.CancelledError:
            _schedule_feedback_once("cancelled")
            raise
        except Exception:
            _schedule_feedback_once("error")
            raise

        # The composition boundary owns concrete extension hooks.
        if memory_mode == "cloud":
            inputs["memory_block"] = await self._get_hook_services().before_memory(request)

        async def run_agent_task():
            return await adapter.process_message_impl(request, inputs)

        try:
            if schedule_session:
                result = await self._session_manager.submit_and_wait(
                    session_id, run_agent_task
                )
            else:
                result = await run_agent_task()
        except asyncio.CancelledError:
            _schedule_feedback_once("cancelled")
            raise
        except Exception:
            _schedule_feedback_once("error")
            raise

        waiting_for_user = (
            result.payload.get("event_type") in USER_INTERACTION_EVENTS
            or any(chunk.get("event_type") in USER_INTERACTION_EVENTS
                   for chunk in result.payload.get("approval_chunks", []) if isinstance(chunk, dict))
        )
        if result.ok and result.payload.get("content"):
            content = result.payload["content"]
            content_str = content if isinstance(content, str) else str(content)
            repair_call = getattr(adapter, "repair_model_response", None)
            retry_without_a2ui_call = self._make_retry_without_a2ui_call(
                adapter=adapter,
                request=request,
            )
            try:
                content_str = await finalize_assistant_response_if_a2ui(
                    content_str,
                    channel=request.channel_id,
                    user_query=user_turn.text,
                    request_id=request.request_id or "",
                    repair_call=repair_call,
                    retry_without_a2ui_call=retry_without_a2ui_call,
                )
            except asyncio.CancelledError:
                _schedule_feedback_once("cancelled")
                raise
            except Exception:
                _schedule_feedback_once("error")
                raise
            if isinstance(content, str):
                result.payload["content"] = content_str
            result.payload = _with_web_agent_template_payload(
                result.payload,
                request.params,
                request.channel_id,
                event_type="chat.final",
            )
            await _run_history_io(
                append_history_record,
                session_id=session_id,
                request_id=request.request_id,
                channel_id=request.channel_id,
                role="assistant",
                event_type="chat.final",
                content=content_str,
                timestamp=time.time(),
                extra=_with_heartbeat_history_metadata(
                    _with_web_agent_template_metadata(
                        None,
                        request.params,
                        request.channel_id,
                        event_type="chat.final",
                        payload=result.payload,
                    ),
                    request.params,
                ),
                mode=request.params.get("mode", "unknown"),
            )
            _schedule_feedback_once("success")

            # cloud memory: after chat hook
            if memory_mode == "cloud":
                await self._get_hook_services().after_memory(request, content_str)
    
            # auto memory: extract memories after conversation ends
            # 需要 auto_memory_enabled 和 memory.enabled 都为 true 才触发
            mode = request.params.get("mode", "code") if isinstance(request.params, dict) else "code"
            config = get_config()
            if is_auto_memory_enabled(mode, config) and is_memory_enabled(mode, config):
                _trigger_auto_memory_extraction(adapter, request, session_id, is_stream=False)

        if not feedback_scheduled:
            _schedule_feedback_once("success" if result.ok else "error")
        if completion_receipt is not None:
            await _await_history_receipt(completion_receipt)
        return result

    async def deliver_session_input(
        self, request: AgentRequest
    ) -> AsyncIterator[AgentResponseChunk]:
        """Submit new text to this Session, independently of question answers."""
        from jiuwenswarm.runtime.session_input import resolve_session_input_mode, validate_session_input

        input_mode = resolve_session_input_mode(request.params)
        if is_interrupt_resume_payload(request.params) or input_mode is None:
            raise ValueError("supplemental input requires an explicit input mode")
        validate_session_input(request.params)
        adapter = self._adapter
        deliver = getattr(adapter, "deliver_session_input_impl", None)
        if not callable(deliver):
            raise RuntimeError("active agent does not support supplemental input")
        session_id = self._session_manager.get_session_id(request.session_id)
        restore_chat_send_equipment_params(session_id, request.params)
        inputs, _memory_mode, _user_turn = self._build_inputs(request)
        history_persisted = False
        async with aclosing(deliver(request, inputs)) as stream:
            async for chunk in stream:
                payload = chunk.payload if isinstance(chunk.payload, dict) else {}
                if not history_persisted and input_mode.value == "steer":
                    if (
                        payload.get("event_type") == "runtime.accepted"
                        and payload.get("input_boundary") != "stream"
                    ):
                        params = request.params if isinstance(request.params, dict) else {}
                        history_extra = _history_user_extra(params) or {}
                        history_extra.update({
                            "is_supplemental_input": True,
                            "supplemental_input": {
                                "execution_id": str(params.get("expected_execution_id") or ""),
                                "stream_offset": 0,
                            },
                        })
                        query = params.get("query") or params.get("content") or ""
                        await _run_history_io(
                            append_history_record,
                            session_id=session_id,
                            request_id=request.request_id,
                            channel_id=request.channel_id,
                            role="user",
                            content=_history_user_content(params, query),
                            timestamp=time.time(),
                            extra=history_extra,
                            channel_metadata=request.metadata,
                            mode=params.get("mode", "unknown"),
                        )
                        history_persisted = True
                yield chunk

    async def deliver_control_input(
        self, request: AgentRequest
    ) -> AsyncIterator[AgentResponseChunk]:
        """Delegate continuation execution and persistence to its owned service."""
        from jiuwenswarm.server.runtime.agent_adapter.control_input_execution import (
            ControlInputExecutionService, ControlInputPorts,
        )

        service = ControlInputExecutionService(self, request, ControlInputPorts(
            is_interrupt_resume_payload=is_interrupt_resume_payload,
            restore_equipment=restore_chat_send_equipment_params,
            compute_mcp_needed=compute_chat_send_mcp_needed,
            persist_answer=_persist_ask_user_answer_history,
            run_history_io=_run_history_io,
            append_history_record=append_history_record,
            decorate_web=_with_web_agent_template_metadata,
            decorate_heartbeat=_with_heartbeat_history_metadata,
            decorate_cross_session=_with_cross_session_history_metadata,
            resolve_final_timestamp=_resolve_final_record_timestamp,
            person_drove_turn=_person_drove_turn,
            complete_history_request=_complete_history_request,
            session_message_owns_completion=_session_message_owns_completion,
            await_history_receipt=_await_history_receipt,
        ))
        owners = getattr(self, "_control_input_executions", None)
        if owners is None:
            owners = self._control_input_executions = set()
        owners.add(service)
        try:
            async with aclosing(service.run()) as stream:
                async for chunk in stream:
                    yield chunk
        finally:
            if service.closed:
                owners.discard(service)

    async def _close_control_input_executions(self, session_id=None) -> None:
        owners = getattr(self, "_control_input_executions", ())
        for service in tuple(owners):
            if session_id is None or service.session_id == session_id:
                await service.aclose()
                owners.discard(service)

    async def process_message_stream(
        self, request: AgentRequest
    ) -> AsyncIterator[AgentResponseChunk]:
        """Dispatch execution through request-owned projection/history services."""
        from jiuwenswarm.server.runtime.agent_adapter.stream_execution import (
            StreamExecutionPorts, StreamExecutionService,
        )

        ports = StreamExecutionPorts(
            restore_chat_send_equipment_params=restore_chat_send_equipment_params,
            compute_chat_send_mcp_needed=compute_chat_send_mcp_needed,
            append_compact_history_from_payload=_append_compact_history_from_payload,
            history_user_content=_history_user_content,
            history_user_extra=_history_user_extra,
            make_a2ui_final_chunk=_make_a2ui_final_chunk,
            make_a2ui_pending_render_chunk=_make_a2ui_pending_render_chunk,
            normalize_nested_stream_chunk=_normalize_nested_stream_chunk,
            persist_ask_user_answer_history=_persist_ask_user_answer_history,
            resolve_final_record_timestamp=_resolve_final_record_timestamp,
            run_history_io=_run_history_io,
            should_defer_a2ui_processing_status=_should_defer_a2ui_processing_status,
            should_probe_a2ui_stream=_should_probe_a2ui_stream,
            should_record_user_history=_should_record_user_history,
            split_a2ui_stream_content=_split_a2ui_stream_content,
            extend_a2ui_stream_probe=_extend_a2ui_stream_probe,
            trigger_auto_memory_extraction=_trigger_auto_memory_extraction,
            with_cross_session_history_metadata=_with_cross_session_history_metadata,
            with_heartbeat_history_metadata=_with_heartbeat_history_metadata,
            with_web_agent_template_metadata=_with_web_agent_template_metadata,
            with_web_agent_template_payload=_with_web_agent_template_payload,
            append_history_record=append_history_record,
            finalize_assistant_response_if_a2ui=finalize_assistant_response_if_a2ui,
            get_config=get_config,
            is_auto_memory_enabled=is_auto_memory_enabled,
            is_memory_enabled=is_memory_enabled,
            TeamA2UIBlockBuffer=TeamA2UIBlockBuffer,
            TeamPlanApprovalPayloadError=_TeamPlanApprovalPayloadError,
            person_drove_turn=_person_drove_turn,
            complete_history_request=_complete_history_request,
            session_message_owns_completion=_session_message_owns_completion,
            await_history_receipt=_await_history_receipt,
        )
        service = StreamExecutionService(self, ports)
        async with aclosing(service.run(request)) as stream:
            async for chunk in stream:
                yield chunk

    # ---------- 实例获取 ----------

    def get_instance(self):
        return self._adapter._instance

    async def ensure_instance(self):
        """Return the adapter's DeepAgent, building the root one on first use.

        ``get_instance`` stays a plain accessor and may return None before the
        root DeepAgent has been built; callers that need a live handle outside
        the chat path should await this instead.

        Returns:
            The DeepAgent instance, or None when there is no adapter yet (the
            stateless-RPC fallback wrapper never calls ``create_instance``) or
            the adapter cannot build one.
        """
        if self._adapter is None:
            return None
        return await self._adapter.ensure_instance()

    def get_live_session_instance(self, session_id: str | None):
        """Return the DeepAgent already running ``session_id``, if any.

        Returns None when no adapter exists, when the adapter predates this
        accessor, or when the session has not started a child adapter yet.
        """
        adapter = self._adapter
        if adapter is None:
            return None
        getter = getattr(adapter, "get_live_session_instance", None)
        if getter is None:
            return None
        return getter(session_id)

    async def ensure_live_session_instance(self, session_id: str | None):
        """Start the session-scoped adapter if needed and return its DeepAgent.

        Used by plan-mode sync so the first turn writes ``plan_mode`` onto the
        same Session the upcoming ``chat.send`` will invoke, not a throwaway.
        """
        adapter = self._adapter
        if adapter is None:
            return None
        starter = getattr(adapter, "ensure_live_session_instance", None)
        if starter is None:
            return self.get_live_session_instance(session_id)
        return await starter(session_id)

    # --- Phase-2: targeted single-MCP control (forwarded to the deep adapter) ---
    async def apply_mcp_change(self, name: str, action: str, *, enabled: bool = True) -> bool:
        """Apply a single-MCP change without a full config reload.

        action ∈ {"add", "remove", "toggle"}.
        - add: register the MCP (spawn stdio / connect SSE-HTTP)
        - remove: unregister (disconnect)
        - toggle: enable=register / disable=unregister

        Returns True if the adapter applied it. The adapter reads the merged
        get_mcp_servers() list (config.yaml + state.json), so a state.json
        write done just before this call is visible.
        """
        adapter = self._adapter
        if adapter is None:
            return False
        if action == "add" or (action == "toggle" and enabled):
            return bool(await adapter.register_mcp_by_name(name))
        if action == "remove" or (action == "toggle" and not enabled):
            return bool(await adapter.unregister_mcp_by_name(name))
        return False

    async def reconcile_session_mcp(
        self,
        session_id: str | None,
        needed: list[str] | None,
        *,
        model_name: str | None = None,
        history_before_request_id: str | None = None,
        execution_request: AgentRequest | None = None,
        runtime_binding: dict[str, Any] | None = None,
        moa_enabled: bool | None = None,
        model_selection: Any = None,
        moa_can_access: Any = None,
        retain_moa_snapshot: bool = False,
    ) -> None:
        """Reconcile this session's MCP set to ``needed`` (idempotent diff).
    
        Session-level enable driven by chat.send: callers should pass the union
        of ``params.mcp`` and connectors from ``agent_template_name`` /
        ``plugin_names`` (see ``compute_chat_send_mcp_needed``). ``None`` /
        ``[]`` both clear the session's selection. See
        ``JiuWenSwarmDeepAdapter.reconcile_session_mcp`` for the diff logic.
        ``history_before_request_id`` keeps a lazily-created session adapter
        from restoring the current chat.send as disk history.
        """
        adapter = self._adapter
        if adapter is None:
            return
        reconcile = getattr(adapter, "reconcile_session_mcp", None)
        if reconcile is None:
            # Adapter doesn't support session-level MCP (e.g. a stub/mock
            # adapter) — session-level enable is a no-op for it.
            return
        parameters = {}
        accepts_kwargs = False
        try:
            parameters = inspect.signature(reconcile).parameters
        except (TypeError, ValueError):
            # Preserve the previous fallback for opaque callables. Production
            # adapters are Python methods and take the inspected branch below.
            supported_kwargs = {"model_name": model_name}
        else:
            accepts_kwargs = any(
                parameter.kind is inspect.Parameter.VAR_KEYWORD
                for parameter in parameters.values()
            )
            supported_kwargs = {}
            if "model_name" in parameters or accepts_kwargs:
                supported_kwargs["model_name"] = model_name
            if "history_before_request_id" in parameters or accepts_kwargs:
                supported_kwargs["history_before_request_id"] = (
                    history_before_request_id
                )
            if execution_request is not None and "execution_request" in parameters:
                supported_kwargs["execution_request"] = execution_request
            if "runtime_binding" in parameters or accepts_kwargs:
                supported_kwargs["runtime_binding"] = runtime_binding
            if model_selection is not None and ("model_selection" in parameters or accepts_kwargs):
                supported_kwargs["model_selection"] = model_selection
            if moa_enabled is not None and ("moa_enabled" in parameters or accepts_kwargs):
                supported_kwargs["moa_enabled"] = moa_enabled
            if moa_can_access is not None and ("moa_can_access" in parameters or accepts_kwargs):
                supported_kwargs["moa_can_access"] = moa_can_access
            if retain_moa_snapshot and ("retain_moa_snapshot" in parameters or accepts_kwargs):
                supported_kwargs["retain_moa_snapshot"] = True
        await reconcile(session_id, needed, **supported_kwargs)

    def sync_mcp_credentials(self) -> bool:
        """Sync connected MCPs' tokens into os.environ (skill scripts).

        Forwarded to the deep adapter. os.environ is process-global, so a
        single live agent's sync covers the whole process; called from the
        connect/disconnect handlers after a state.json write. Returns False
        when no adapter is live yet (cold-start race) so the caller can fall
        through to another live agent.
        """
        adapter = self._adapter
        if adapter is None:
            return False
        sync = getattr(adapter, "_sync_mcp_credentials_environment", None)
        if sync is None:
            return False
        return bool(sync())

    def clear_mcp_credentials(self, name: str) -> bool:
        """Clear a disconnected MCP's token env vars.

        Returns False when no adapter is live yet so the caller can fall
        through to another live agent.
        """
        adapter = self._adapter
        if adapter is None:
            return False
        clear = getattr(adapter, "_clear_mcp_credentials_environment", None)
        if clear is None:
            return False
        clear(name)
        return True

    async def refresh_skill_rails(self) -> None:
        """Reload SkillUseRail so newly installed/uninstalled MCP bundled
        skills surface to the agent without a full reload.

        Called from the connect/disconnect handlers after skill_installer
        copies/removes skill dirs. Without this the agent's SkillUseRail keeps
        its cached skill set and the MCP's skills stay invisible even though
        the files are on disk.
        """
        adapter = self._adapter
        if adapter is None:
            return
        refresh = getattr(adapter, "refresh_skill_rails", None)
        if refresh is not None:
            await refresh()

    async def apply_package_change_to_session_adapters(
        self,
        operation: str,
        config_path: str,
    ) -> None:
        """Propagate a harness package load/unload to all live session adapters."""
        adapter = self._adapter
        if adapter is None:
            return
        method = getattr(adapter, "apply_package_change_to_session_adapters", None)
        if method is None:
            return
        await method(operation, config_path)

    async def apply_rsi_harness_install(
        self,
        operation: str,
        *,
        config_path: str,
        installation_id: str,
    ) -> dict[str, Any]:
        """Apply an RSI-published Harness through the adapter-owned LoadRecord."""

        adapter = self._adapter
        if adapter is None:
            return {"status": "SKIPPED", "resources": []}
        method = getattr(adapter, "apply_rsi_harness_install", None)
        if method is None:
            return {"status": "SKIPPED", "resources": []}
        return await method(
            operation,
            config_path=config_path,
            installation_id=installation_id,
        )

    async def _unload_live_equipment(self, kind: str, package_id: str) -> None:
        """Unload a catalog package from live session adapters before delete.

        Missing adapter is a no-op (nothing loaded). Unload errors propagate
        so uninstall does not delete the package.
        """
        adapter = getattr(self, "_adapter", None)
        unload = getattr(adapter, "unload_equipment_if_loaded", None)
        if unload is None:
            return
        await unload(kind, package_id)

    async def compress_context(
            self,
            session_id: str,
            session: Any = None,
            *,
            return_state: bool = False,
            processor_types: list[str] | None = None,
    ) -> dict[str, Any]:
        """主动触发上下文压缩。

        Args:
            session_id: 会话ID
            session: Session 对象（可选）
            processor_types: 可选的上下文压缩处理器白名单

        Returns:
            包含压缩结果的字典:
            - result: "busy" | "compressed" | "noop"
            - stats: 压缩统计信息（仅当 result == "compressed" 时）
        """
        adapter = self._adapter
        if adapter is None:
            raise ValueError("Agent adapter not available")
        return await adapter.compress_context(
            session_id=session_id,
            session=session,
            return_state=return_state,
            processor_types=processor_types,
        )

    async def get_context_usage(self, session_id: str) -> dict[str, Any]:
        """获取当前上下文窗口占用统计。

        - 上下文窗口总量与当前占用量
        - 系统提示词、对话消息、工具定义各自的 token 消耗
        - 上下文窗口占用百分比

        Args:
            session_id: 会话ID

        Returns:
            包含上下文使用情况统计的字典
        """
        adapter = self._adapter
        if adapter is None:
            raise ValueError("Agent adapter not available")
        return await adapter.get_context_usage(session_id=session_id)

    async def get_context_usage_event(
        self,
        session_id: str,
        *,
        request_id: str,
    ) -> dict[str, Any] | None:
        """Build a local usage event after an out-of-band context change."""
        adapter = self._adapter
        if adapter is None:
            raise ValueError("Agent adapter not available")
        build_event = getattr(adapter, "get_context_usage_event", None)
        if not callable(build_event):
            return None
        return await build_event(session_id=session_id, request_id=request_id)

    async def generate_recap(
        self,
        session_id: str,
        current_mode: str | None = None,
    ) -> dict[str, Any]:
        """生成会话快速回顾（read-only，不修改对话历史）。

        取最近30条消息 → fast model → 1-2句摘要。

        Args:
            session_id: 会话ID
            current_mode: 触发 recap 时的 canonical runtime mode。

        Returns:
            包含 recap 结果的字典:
            - status: "ok" | "no_turn" | "aborted" | "failed"
            - summary: 摘要文本（仅当 status == "ok" 时）
            - error: 错误信息（仅当 status == "failed" 时）
        """
        adapter = self._adapter
        if adapter is None:
            raise ValueError("Agent adapter not available")
        return await adapter.generate_recap(
            session_id=session_id,
            current_mode=current_mode,
        )

    async def compact_partial(
        self,
        session_id: str,
        turn_index: int,
        direction: str = "from",
    ) -> dict[str, Any]:
        """部分对话压缩 — 对指定 turn 之前或之后的消息进行 LLM 摘要。

        Args:
            session_id: 会话ID
            turn_index: 基准 turn 号
            direction: "from" (摘要 turn 及之后) 或 "up_to" (摘要 turn 之前)

        Returns:
            包含压缩结果的字典:
            - status: "ok" | "no_turn" | "failed"
            - summary: 摘要文本（仅当 status == "ok" 时）
            - summarized_count: 被摘要的消息数
            - error: 错误信息（仅当 status == "failed" 时）
        """
        adapter = self._adapter
        if adapter is None:
            raise ValueError("Agent adapter not available")
        return await adapter.compact_partial(
            session_id=session_id,
            turn_index=turn_index,
            direction=direction,
        )

    async def generate_btw_answer(self, session_id: str, question: str) -> dict[str, Any]:
        """回答 /btw 侧问题：独立、无工具、单轮 LLM 查询。

        将最近对话上下文 + 用户问题发送给模型，模型仅基于已有上下文回答，
        不使用工具、不修改对话历史。

        Args:
            session_id: 会话ID
            question: 用户侧问题

        Returns:
            包含 btw 结果的字典:
            - status: "ok" | "no_context" | "failed"
            - answer: 回答文本（仅当 status == "ok" 时）
            - error: 错误信息（仅当 status == "failed" 时）
        """
        adapter = self._adapter
        if adapter is None:
            raise ValueError("Agent adapter not available")
        return await adapter.generate_btw_answer(session_id=session_id, question=question)

    # ---------- 资源清理 ----------

    async def cancel_session_tasks(self, session_id: str) -> None:
        """Cancel and join request tasks without treating idle caches as work."""
        cancel = getattr(self._adapter, "cancel_session_tasks", None)
        if callable(cancel):
            await cancel(session_id)

    async def cleanup_session_runtime(self, session_id: str) -> bool:
        """Release in-memory runtime owned by one session while keeping persisted history."""
        processor_cleaned = await self._session_manager.close_session(session_id)
        await self._close_control_input_executions(session_id)
        adapter = self._adapter
        if adapter is None:
            return processor_cleaned
        cleanup_fn = getattr(adapter, "cleanup_session_adapter", None)
        if not callable(cleanup_fn):
            return processor_cleaned
        adapter_cleaned = bool(await cleanup_fn(session_id))
        return processor_cleaned or adapter_cleaned

    def has_session_runtime(self, session_id: str | None = None) -> bool:
        """Return whether this facade still owns session-scoped runtime."""
        if any(
            session_id is None or service.session_id == session_id
            for service in getattr(self, "_control_input_executions", ())
        ):
            return True
        if self._session_manager.has_session_runtime(session_id):
            return True
        adapter = self._adapter
        if adapter is None:
            return False
        has_runtime = getattr(adapter, "has_session_runtime", None)
        if not callable(has_runtime):
            return True
        if session_id is None:
            return bool(has_runtime())
        return bool(has_runtime(session_id))

    def has_active_goal(self, session_id: str) -> bool:
        """Inspect existing Goal work without creating or attaching a session."""
        checker = getattr(self._adapter, "has_active_goal", None)
        return bool(callable(checker) and checker(session_id))

    def has_auto_permission_session(self, session_id: str | None) -> bool:
        adapter = self._adapter
        checker = getattr(adapter, "has_auto_permission_session", None)
        return bool(callable(checker) and checker(session_id))

    def validate_auto_permission_workspace_request(self, request: AgentRequest) -> None:
        adapter = self._adapter
        validator = getattr(
            adapter, "validate_auto_permission_workspace_request", None
        )
        if callable(validator):
            validator(request)

    async def cancel_inflight_work(
        self,
        log_prefix: str = "[gateway disconnect] ",
        *,
        exclude_session_ids: set[str] | None = None,
    ) -> None:
        """Gateway 与 AgentServer 的 WebSocket 断开时调用：取消 session 流式任务并中止 adapter 内层循环。"""
        protected = set(exclude_session_ids or ())
        await self._session_manager.cancel_all_session_tasks(
            log_prefix,
            exclude_session_ids=protected,
        )
        adapter = self._adapter
        if adapter is None:
            return
        abort_fn = getattr(adapter, "abort_on_gateway_disconnect", None)
        if not callable(abort_fn):
            return
        try:
            await abort_fn(exclude_session_ids=protected)
        except Exception:
            logger.exception("[JiuWenSwarm] adapter.abort_on_gateway_disconnect failed")

    async def cleanup(self) -> None:
        """清理资源，准备销毁实例.

        每次 initialize 重建 agent 时调用。
        不清理记忆数据（记忆数据保留在文件系统中）。
        """
        logger.info("[JiuWenSwarm] cleanup: 清理资源")
        await self._session_manager.close_all_sessions()
        await self._close_control_input_executions()

        if self._adapter is not None:
            try:
                if hasattr(self._adapter, "cleanup"):
                    await self._adapter.cleanup()
            except Exception as e:
                logger.warning("[JiuWenSwarm] Adapter cleanup failed: %s", e)
                # Keep the exact owner available for a retry. A failed child
                # cleanup can still hold SDK tasks and Kernel leases.
                raise
            self._adapter = None

        logger.info("[JiuWenSwarm] cleanup: 完成")

    @staticmethod
    async def _resolve_runtime_binding_for_request(
        adapter: AgentAdapter,
        request: AgentRequest,
        session_id: str,
    ) -> dict[str, Any] | None:
        """Resolve one chat's runtime before any session-child side effects."""
        if request.req_method is not ReqMethod.CHAT_SEND:
            return None
        resolver = getattr(adapter, "resolve_runtime_binding", None)
        if not callable(resolver):
            return None
        params = request.params if isinstance(request.params, dict) else {}
        runtime_binding = resolver(session_id, params)
        if inspect.isawaitable(runtime_binding):
            runtime_binding = await runtime_binding
        request.trusted_runtime_binding = runtime_binding
        return runtime_binding


def __getattr__(name: str):
    if name == "package_manager":
        from jiuwenswarm.server.runtime import extension_package_manager

        return extension_package_manager
    raise AttributeError(name)

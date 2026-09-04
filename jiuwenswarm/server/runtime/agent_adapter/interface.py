# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""JiuWenSwarm Facade - 统一入口与 SDK 适配层.

此模块提供：
- 统一的 JiuWenSwarm 公开 API
- SDK 工厂路由（通过环境变量选择）
- 公共编排逻辑（session 队列、Skills 路由、heartbeat、流式包装）
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
import logging
import re
import time
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Tuple

from jiuwenswarm.dotenv_early import load_dotenv_runtime

from jiuwenswarm.server.runtime.agent_adapter.agent_adapters import (
    AgentAdapter,
    create_adapter,
    resolve_sdk_choice,
)
from jiuwenswarm.agents.harness.common.memory.config import get_memory_mode, is_auto_memory_enabled, is_memory_enabled
from jiuwenswarm.server.runtime.session.session_history import (
    append_compact_history_records,
    append_history_record,
    collapse_file_content_blocks,
)
from jiuwenswarm.server.runtime.agent_adapter.user_turn import TEAM_USER_TURN_KEY, UserTurn
from jiuwenswarm.server.runtime.agent_adapter.statusline_setup_agent import (
    STATUSLINE_SETUP_SYSTEM_PROMPT,
    build_statusline_setup_dispatch,
)
from jiuwenswarm.server.runtime.session.session_manager import SessionManager
from jiuwenswarm.server.runtime.skill.skill_manager import SkillManager
from jiuwenswarm.server.runtime.model_registry import ModelRegistry
from jiuwenswarm.agents.harness.common.tools.third_agent_toolkits import handle_third_agents_list
from jiuwenswarm.server.runtime.hardware.npu_monitor import handle_hardware_npu_status
from jiuwenswarm.server.runtime.user_registry import (
    handle_users_create,
    handle_users_delete,
    handle_users_list,
)
from jiuwenswarm.server.utils.utils import is_team_params
from jiuwenswarm.common.config import get_config
from jiuwenswarm.agents.harness.code.prompt.plan_approval import (
    PLAN_EXECUTE_OPTION_VALUES,
    PLAN_REMINDER_ORIGINAL_QUERY_KEY,
    PLAN_REVISE_OPTION_VALUES,
    PLAN_SKIP_OPTION_VALUES,
    plan_skip_feedback,
)
from jiuwenswarm.common.mode_matrix import (
    canonicalize_mode_text,
    is_code_profile_mode,
    is_team_mode as is_team_runtime_mode,
    is_team_plan_mode,
    is_web_composable_mode,
    read_request_work_mode,
)
from jiuwenswarm.extensions.registry import ExtensionRegistry
from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponse, AgentResponseChunk
from jiuwenswarm.common.chat_final import ensure_final_mode_inplace
from jiuwenswarm.extensions.hook_event import AgentServerHookEvents
from jiuwenswarm.extensions.hooks_context import MemoryHookContext
from jiuwenswarm.common.schema.message import EventType, ReqMethod
from jiuwenswarm.common.utils import (
    configure_skill_library,
    get_agent_home_dir,
    get_agent_workspace_dir,
    get_env_file,
    migrate_team_skill_views,
    reset_free_search_runtime_flags,
)
from jiuwenswarm.server.runtime.a2ui.integration import (
    TeamA2UIBlockBuffer,
    finalize_assistant_response_if_a2ui,
)
from jiuwenswarm.server.runtime.a2ui.runtime.finalizer import should_finalize_a2ui_content
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


class _TeamPlanApprovalPayloadError(ValueError):
    """Raised when a structured team.plan approval payload is malformed."""


def resolve_request_query(params: Any) -> Any:
    """Return the request payload accepted by the AgentOS input contract."""
    if not isinstance(params, dict):
        return ""
    query = params.get("query")
    return params.get("content", "") if query is None or query == "" else query


def _permission_card_ids_from_answers(answers: list[dict]) -> list[str]:
    """Normalize the single opaque permission-card locator."""

    if len(answers) != 1:
        return []
    for answer in answers:
        if not isinstance(answer, dict):
            return []
        raw_card_id = answer.get("card_id")
        if not isinstance(raw_card_id, str):
            return []
        card_id = raw_card_id.strip()
        if not card_id or len(card_id) > 128:
            return []
        return [card_id]
    return []


def _permission_confirm_payload(
    answer: Any,
    *,
    allow_plan_actions: bool = False,
) -> dict[str, Any]:
    selected_options = answer.get("selected_options", []) if isinstance(answer, dict) else []
    custom_input = answer.get("custom_input", "") if isinstance(answer, dict) else ""
    value = selected_options[0] if selected_options else ""

    if value in ("approve", "本次允许", "Approve", "Proceed", "批准", "开始执行"):
        return {"approved": True, "auto_confirm": False, "feedback": ""}
    if value in ("session_allow", "会话内记住", "Session Allow"):
        return {
            "approved": True,
            "auto_confirm": True,
            "persist_allow": False,
            "feedback": "",
        }
    if value in (
        "always_allow",
        "allow_always",
        "永久记住",
        "总是允许",
        "Always Allow",
    ):
        return {
            "approved": True,
            "auto_confirm": True,
            "persist_allow": True,
            "feedback": "",
        }
    if allow_plan_actions and value in PLAN_EXECUTE_OPTION_VALUES:
        return {
            "approved": True,
            "auto_confirm": False,
            "feedback": "",
            "plan_execute": True,
        }
    if allow_plan_actions and value in PLAN_SKIP_OPTION_VALUES:
        return {
            "approved": False,
            "auto_confirm": False,
            "feedback": custom_input
            or plan_skip_feedback(get_config().get("preferred_language")),
            "plan_skip": True,
        }
    if allow_plan_actions and value in PLAN_REVISE_OPTION_VALUES:
        return {
            "approved": False,
            "auto_confirm": False,
            "feedback": custom_input or "用户希望继续规划",
            "plan_revise": True,
        }
    if value in ("reject", "拒绝", "Reject", "继续规划", "其他意见"):
        feedback = custom_input or (
            "用户希望继续规划" if value in ("Keep planning", "继续规划", "其他意见") else "用户拒绝"
        )
        return {"approved": False, "auto_confirm": False, "feedback": feedback}
    if custom_input:
        return {"approved": False, "auto_confirm": False, "feedback": custom_input}
    return {
        "approved": False,
        "auto_confirm": False,
        "feedback": f"未知选项: {value}",
    }


def _schedule_symphony_session_feedback(
    session_id: str,
    request_id: str,
    *,
    terminal_status: str = "success",
) -> None:
    """Submit session-based Symphony learning without delaying the response."""

    try:
        from jiuwenswarm.symphony.evolution.session_consumer import (
            schedule_session_evolution_consume,
        )

        schedule_session_evolution_consume(
            session_id,
            request_id,
            terminal_status=terminal_status,
        )
    except Exception as exc:  # noqa: BLE001
        logging.getLogger(__name__).debug(
            "Failed to schedule Symphony session feedback: %s",
            exc,
        )


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


def is_external_user_authored_dispatch(
    params: Any,
    *,
    channel_id: Any = "",
    request_method: Any = None,
) -> bool:
    """Return whether Host ingress proved an external user-authored dispatch."""

    if not isinstance(params, dict):
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


def _should_record_user_history(
    params: Any,
    *,
    channel_id: Any = "",
    request_method: Any = None,
) -> bool:
    """Keep history and permission authority on the same Host predicate."""

    if (
        isinstance(params, dict)
        and str(params.get("source") or "").strip() == "skill_evolution_approval"
    ):
        # AgentOS keeps passive skill-evolution approvals in the transcript.
        # This is display history only; interrupt resumes remain non-external
        # for permission-authority and root-intent classification.
        return True

    return is_external_user_authored_dispatch(
        params,
        channel_id=channel_id,
        request_method=request_method,
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


def _history_user_extra(params: Any) -> dict[str, Any] | None:
    if not isinstance(params, dict):
        return None

    extra: dict[str, Any] = {}
    raw_media_items = params.get("media_items")
    if isinstance(raw_media_items, list):
        media_items: list[dict[str, Any]] = []
        for raw_item in raw_media_items:
            item = _history_media_record(raw_item)
            if item is not None:
                media_items.append(item)
        if media_items:
            extra["media_items"] = media_items

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
                files["uploaded_images"] = image_items
        if files:
            extra["files"] = files

    return extra or None


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
reset_free_search_runtime_flags()


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
_SKILLDEV_METHODS: frozenset[ReqMethod] = frozenset(
    m for m in ReqMethod if m.value.startswith("skilldev.")
)

_SKILL_ROUTES: dict[ReqMethod, str] = {
    ReqMethod.SKILLS_LIST: "handle_skills_list",
    ReqMethod.SKILLS_INSTALLED: "handle_skills_installed",
    ReqMethod.SKILLS_GET: "handle_skills_get",
    ReqMethod.SKILLS_TOGGLE: "handle_skills_toggle",
    ReqMethod.SKILLS_MARKETPLACE_LIST: "handle_skills_marketplace_list",
    ReqMethod.SKILLS_INSTALL: "handle_skills_install",
    ReqMethod.SKILLS_UNINSTALL: "handle_skills_uninstall",
    ReqMethod.SKILLS_IMPORT_LOCAL: "handle_skills_import_local",
    ReqMethod.SKILLS_MARKETPLACE_ADD: "handle_skills_marketplace_add",
    ReqMethod.SKILLS_MARKETPLACE_REMOVE: "handle_skills_marketplace_remove",
    ReqMethod.SKILLS_MARKETPLACE_TOGGLE: "handle_skills_marketplace_toggle",
    ReqMethod.SKILLS_ONLINE_SEARCH: "handle_skills_online_search",
    ReqMethod.SKILLS_SKILLNET_SEARCH: "handle_skills_skillnet_search",
    ReqMethod.SKILLS_SKILLNET_INSTALL: "handle_skills_skillnet_install",
    ReqMethod.SKILLS_SKILLNET_INSTALL_STATUS: "handle_skills_skillnet_install_status",
    ReqMethod.SKILLS_SKILLNET_EVALUATE: "handle_skills_skillnet_evaluate",
    ReqMethod.SKILLS_CLAWHUB_GET_TOKEN: "handle_skills_clawhub_get_token",
    ReqMethod.SKILLS_CLAWHUB_SET_TOKEN: "handle_skills_clawhub_set_token",
    ReqMethod.SKILLS_CLAWHUB_SEARCH: "handle_skills_clawhub_search",
    ReqMethod.SKILLS_CLAWHUB_DOWNLOAD: "handle_skills_clawhub_download",
    ReqMethod.SKILLS_TEAMSKILLS_HUB_INFO: "handle_skills_team_skills_hub_info",
    ReqMethod.SKILLS_TEAMSKILLS_HUB_INIT: "handle_skills_team_skills_hub_init",
    ReqMethod.SKILLS_TEAMSKILLS_HUB_VALIDATE: "handle_skills_team_skills_hub_validate",
    ReqMethod.SKILLS_TEAMSKILLS_HUB_PACK: "handle_skills_team_skills_hub_pack",
    ReqMethod.SKILLS_TEAMSKILLS_HUB_SEARCH: "handle_skills_team_skills_hub_search",
    ReqMethod.SKILLS_TEAMSKILLS_HUB_INSTALL: "handle_skills_team_skills_hub_install",
    ReqMethod.SKILLS_TEAMSKILLS_HUB_PUBLISH: "handle_skills_team_skills_hub_publish",
    ReqMethod.SKILLS_TEAMSKILLS_HUB_DELETE: "handle_skills_team_skills_hub_delete",
    ReqMethod.SKILLS_RETRIEVAL_STATUS: "handle_skills_retrieval_status",
    ReqMethod.SKILLS_RETRIEVAL_INDEX_BUILD: "handle_skills_retrieval_index_build",
    ReqMethod.SKILLS_RETRIEVAL_INDEX_CANCEL: "handle_skills_retrieval_index_cancel",
    ReqMethod.SKILLS_RETRIEVAL_SEARCH: "handle_skills_retrieval_search",
    ReqMethod.SKILLS_RETRIEVAL_TREE: "handle_skills_retrieval_tree",
    ReqMethod.SKILLS_GRAPH_BUILD: "handle_skills_graph_build",
    ReqMethod.SKILLS_GRAPH_STATUS: "handle_skills_graph_status",
    ReqMethod.SKILLS_GRAPH_GET: "handle_skills_graph_get",
    ReqMethod.SKILLS_GRAPH_CANCEL: "handle_skills_graph_cancel",
    ReqMethod.SKILLS_EVOLUTION_STATUS: "handle_skills_evolution_status",
    ReqMethod.SKILLS_EVOLUTION_GET: "handle_skills_evolution_get",
    ReqMethod.SKILLS_EVOLUTION_SAVE: "handle_skills_evolution_save",
    ReqMethod.SKILLS_VISIBILITY_GET: "handle_skills_visibility_get",
    ReqMethod.SKILLS_VISIBILITY_SET: "handle_skills_visibility_set",
    ReqMethod.SKILLS_VISIBILITY_UPDATE: "handle_skills_visibility_update",
}

# Handlers that persist a Skill visibility document; every one of them must
# trigger a rail refresh so a grant or a revocation takes effect on the next
# turn. The read-only ``get`` deliberately stays out.
_SKILL_VISIBILITY_WRITE_HANDLERS: frozenset[str] = frozenset(
    {
        "handle_skills_visibility_set",
        "handle_skills_visibility_update",
    }
)

_PLUGIN_ROUTES: dict[ReqMethod, str] = {
    ReqMethod.PLUGINS_LIST: "handle_plugins_list",
    ReqMethod.PLUGINS_INSTALL: "handle_plugins_install",
    ReqMethod.PLUGINS_UNINSTALL: "handle_plugins_uninstall",
    ReqMethod.PLUGINS_ENABLE: "handle_plugins_enable",
    ReqMethod.PLUGINS_DISABLE: "handle_plugins_disable",
    ReqMethod.PLUGINS_RELOAD: "handle_plugins_reload",
}

# 模型注册表 RPC（只读,运行状态由 ModelRegistry 实时健康检查合成）
_MODEL_ROUTES: dict[ReqMethod, str] = {
    ReqMethod.MODEL_REGISTRY_LIST: "handle_models_list",
}

# 无状态 RPC：handler 为模块级 async 函数（third_agent_toolkits / npu_monitor /
# user_store）。三方 Agent、用户管理与聊天工具读同一状态文件，保证逻辑唯一来源。
_STATELESS_FUNC_ROUTES: dict[ReqMethod, Any] = {
    ReqMethod.THIRD_AGENTS_LIST: handle_third_agents_list,
    ReqMethod.HARDWARE_NPU_STATUS: handle_hardware_npu_status,
    ReqMethod.USERS_LIST: handle_users_list,
    ReqMethod.USERS_CREATE: handle_users_create,
    ReqMethod.USERS_DELETE: handle_users_delete,
}

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

    def __init__(self) -> None:
        self._prepare_skill_library()
        self._adapter: AgentAdapter | None = None
        self._sdk_name: str | None = None
        self._skill_manager = SkillManager(workspace_dir=str(get_agent_workspace_dir()))
        self._model_registry = ModelRegistry()
        self._session_manager = SessionManager()
        self._permissions_changed_notifier: Callable[[], None] | None = None
        self._permissions_external_input_context_builder: Callable[..., Any] | None = None
        # SkillDev 模式：懒初始化，首次 skilldev.* 请求时构造
        self._skilldev_service = None

    @staticmethod
    def _prepare_skill_library() -> None:
        """Pin the single Skill library and retire legacy per-workspace views.

        Runs before any adapter, team or rail is built so legacy view directories
        are gone by the time anything reads a team workspace. Correctness no
        longer depends on that ordering: the migration seeds its allow lists at
        ``AUTHORITY_MIGRATION``, which outranks the config seeds written during
        assembly whichever one lands first. The agent-teams home still has to be
        pinned before the scan, otherwise it would look under openJiuWen's own
        default root instead of this instance's workspace.

        Single-agent mode is untouched by all of this: it reads the same library
        directory it always did and owns no visibility document.
        """
        try:
            from jiuwenswarm.agents.harness.team.bootstrap import configure_agent_teams_home

            configure_agent_teams_home()
            configure_skill_library()
            migrate_team_skill_views()
        except Exception as exc:
            logger.warning("[JiuWenSwarm] skill library preparation failed: %s", exc)

    def _get_skilldev_service(self):
        """懒初始化并返回 SkillDevService 实例.

        SkillDevService 是无状态的，单实例即可服务所有请求。
        首次调用时从当前 JiuWenSwarm 配置中提取最小依赖并构造。
        """
        if self._skilldev_service is not None:
            return self._skilldev_service

        from jiuwenswarm.server.runtime.skill.skilldev import (SkillDevDeps, SkillDevService,
                                                              StateStore, WorkspaceProvider)
        from jiuwenswarm.common.utils import get_workspace_dir
        from jiuwenswarm.agents.harness.common.tools.mcp_toolkits import get_mcp_tools

        skilldev_base = get_workspace_dir() / "skilldev"
        state_store = StateStore(skilldev_base)
        workspace_provider = WorkspaceProvider(skilldev_base)

        config = get_config()
        model_configs = config.get("models", {})
        default_model = model_configs.get("default", {})

        deps = SkillDevDeps(
            model_name=default_model.get("model_name", ""),
            model_client_config=default_model.get("model_client_config", {}),
            mcp_tools_factory=get_mcp_tools,  # 直接复用已加载的 MCP 工具工厂
            sysop_config=None,
            state_store=state_store,
            workspace_provider=workspace_provider,
        )
        self._skilldev_service = SkillDevService(deps)
        logger.info("[JiuWenSwarm] SkillDevService 初始化完成")
        return self._skilldev_service

    def _ensure_adapter(self, *, mode: str = "agent") -> AgentAdapter:
        """确保 adapter 已初始化，如果未初始化则根据环境变量和 mode 创建."""
        if self._adapter is None:
            self._sdk_name = resolve_sdk_choice()
            self._adapter = create_adapter(self._sdk_name, mode=mode)
            if hasattr(self._adapter, "set_skill_manager"):
                self._adapter.set_skill_manager(self._skill_manager)
            self._adapter.set_permissions_changed_notifier(
                self._permissions_changed_notifier
            )
            self._adapter.set_permissions_external_input_context_builder(
                self._permissions_external_input_context_builder
            )
            self._skill_manager.set_skillnet_install_complete_hook(
                self._on_skillnet_install_complete
            )
            logger.info("[JiuWenSwarm] Initialized adapter: sdk=%s, mode=%s", self._sdk_name, mode)
        return self._adapter

    def set_permissions_changed_notifier(
        self,
        notifier: Callable[[], None] | None,
    ) -> None:
        """Inject the host composition callback for persisted permission changes."""
        self._permissions_changed_notifier = notifier
        if self._adapter is not None:
            self._adapter.set_permissions_changed_notifier(notifier)

    def set_permissions_external_input_context_builder(
        self,
        builder: Callable[..., Any] | None,
    ) -> None:
        """Inject the Host external-input permission publication context."""
        self._permissions_external_input_context_builder = builder
        if self._adapter is not None:
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

    async def create_instance(self, config: dict[str, Any] | None = None, *,
                              mode: str = "agent", sub_mode: str = None) -> None:
        """初始化 Agent 实例.

        Args:
            config: 可选配置，透传给底层 adapter.
            mode: 实例化模式，"claw"（默认）或 "code"，透传给底层 adapter.
            sub_mode: 子模式
        """
        adapter = self._ensure_adapter(mode=mode)
        await adapter.create_instance(config, mode=mode, sub_mode=sub_mode)
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
        await self.create_instance()
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
    ) -> None:
        """从配置重新加载.

        Args:
            config_base: 可选的完整配置快照；传入时优先使用它而不是读取本地 config.yaml。
            env_overrides: 可选的环境变量增量；仅覆盖请求中出现的 key。
            target_session_id: 可选的目标 session id；传入时限制 session adapter 级联热更新范围。
        """
        adapter = self._ensure_adapter()
        if hasattr(adapter, "try_stop_dreaming"):
            await adapter.try_stop_dreaming()
        await adapter.reload_agent_config(
            config_base,
            env_overrides,
            target_session_id=target_session_id,
        )
        logger.info("[JiuWenSwarm] Agent config reloaded: sdk=%s", self._sdk_name)
        if hasattr(adapter, "try_start_dreaming"):
            sm = self._session_manager
            asyncio.create_task(adapter.try_start_dreaming(
                busy_checker=lambda: sm.has_active_tasks(),))

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
        from jiuwenswarm.common.schema.chat_send import ChatSendParams

        config_base = get_config()
        memory_mode = get_memory_mode(config_base)
        params: ChatSendParams = request.params if isinstance(request.params, dict) else {}
        query = resolve_request_query(params)
        # /debug 请求级指令：仅 agent/code 在此剥离前缀；team 自行从
        # ``turn.text`` 解析 /debug（见 team_helpers），故此处对 team 不剥离。
        _request_debug = False
        _dbg_mode = params.get("mode")
        _dbg_mode_s = _dbg_mode.strip().lower() if isinstance(_dbg_mode, str) else ""
        if not (params.get("team") or is_team_runtime_mode(_dbg_mode_s)):
            if isinstance(query, str):
                from jiuwenswarm.server.runtime.debug_trace.directives import strip_debug_directive
                query, _request_debug = strip_debug_directive(query)
        if self._is_malformed_team_plan_approval_payload(params):
            raise _TeamPlanApprovalPayloadError(self._team_plan_approval_payload_error_message())
        channel = request.channel_id or (request.session_id.split('_')[0] if request.session_id else "web")
        language = config_base.get("preferred_language", "zh")

        # Get trusted directories from request params (passed by TUI)
        trusted_dirs: list[str] = []
        raw_trusted_dirs = params.get("trusted_dirs")
        if isinstance(raw_trusted_dirs, list):
            for d in raw_trusted_dirs:
                if isinstance(d, str) and d.strip():
                    trusted_dirs.append(d.strip())
        # 用户选中的 skill 名列表（前端从 content 提取，如 /doc /review）。
        # 若提供，build_user_prompt 直接用作 skills_to_use、且不剥离 content。
        skills: list[str] | None = None
        raw_skills = params.get("skills")
        if isinstance(raw_skills, list):
            skills = [s.strip() for s in raw_skills if isinstance(s, str) and s.strip()] or None
        metadata = request.metadata or {}
        origin_kind = (
            HOST_USER_ORIGIN_EXTERNAL
            if is_external_user_authored_dispatch(
                params,
                channel_id=channel,
                request_method=request.req_method,
            )
            else HOST_USER_ORIGIN_INTERNAL
        )
        param_project_dir = params.get("project_dir")
        metadata_project_dir = metadata.get("project_dir") if isinstance(metadata, dict) else None
        project_dir = (
            param_project_dir.strip()
            if isinstance(param_project_dir, str) and param_project_dir.strip()
            else metadata_project_dir.strip()
            if isinstance(metadata_project_dir, str) and metadata_project_dir.strip()
            else None
        )
        param_cwd = params.get("cwd")
        metadata_cwd = metadata.get("cwd") if isinstance(metadata, dict) else None
        cwd = (
            param_cwd.strip()
            if isinstance(param_cwd, str) and param_cwd.strip()
            else metadata_cwd.strip()
            if isinstance(metadata_cwd, str) and metadata_cwd.strip()
            else None
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
            metadata=request.metadata,
            origin_kind=origin_kind,
        )

        if isinstance(query, InteractiveInput):
            final_query = query
        else:
            answers = params.get("answers", [])
            if answers:
                request_id = params.get("request_id", "")
                source = params.get("source", "")
                raw_original_request = (
                    params.get("original_request")
                    if source == "ask_user_interrupt"
                    else ""
                )
                original_request = (
                    raw_original_request.strip()
                    if isinstance(raw_original_request, str)
                    else ""
                )
                interactive_input = self._build_interactive_input_from_answers(
                    request_id,
                    answers,
                    source,
                )
                has_ask_user_context = (
                    source == "ask_user_interrupt"
                    and bool(original_request)
                    and interactive_input is not None
                )
                if has_ask_user_context and str(request_id or "").strip():
                    bound_id = str(request_id).strip()
                    bound_payload = interactive_input.user_inputs.get(bound_id)
                    if isinstance(bound_payload, dict) and isinstance(
                        bound_payload.get("answers"), dict
                    ):
                        # AgentOS display context is attached only after the
                        # strict call/question adapter has accepted the answer.
                        # Permission/root authority never derives from it.
                        interactive_input.update(
                            bound_id,
                            {**bound_payload, "original_request": original_request},
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
        if request.metadata and request.metadata.get("skip_a2ui") is True:
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
            request_id = str(request_id or "").strip()
            if not request_id:
                return interactive_input
            answers_dict = {}
            has_question_key = False
            for answer in answers:
                if isinstance(answer, dict):
                    question_text = str(answer.get("question", "") or "").strip()
                    has_question_key = has_question_key or bool(question_text)
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
            if not has_question_key:
                return interactive_input
            payload: dict[str, Any] = {"answers": answers_dict}
            interactive_input.update(request_id, payload)
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

        if source == "confirm_interrupt":
            answer = answers[0] if answers else {}
            confirm_payload = _permission_confirm_payload(
                answer,
                allow_plan_actions=True,
            )
            interactive_input.update(request_id, confirm_payload)
            return interactive_input

        if source != "permission_interrupt":
            return None

        card_ids = _permission_card_ids_from_answers(answers)
        if not card_ids:
            return interactive_input
        interactive_input.update(card_ids[0], _permission_confirm_payload(answers[0]))
        logger.info(
            "[JiuWenSwarm] PermissionRail card InteractiveInput.update: "
            "request_id=%s card_id=%s",
            request_id,
            card_ids[0],
        )
        return interactive_input

    async def _handle_skilldev_request(self, request: AgentRequest) -> AgentResponse | None:
        """处理 SkillDev 相关请求，返回 None 表示不是 SkillDev 请求."""
        if request.req_method not in _SKILLDEV_METHODS:
            return None

        service = self._get_skilldev_service()
        try:
            chunks = []
            async for chunk in service.handle(request):
                chunks.append(chunk)
            final = chunks[-1] if chunks else None
            payload = final.payload if final else {}
        except Exception as exc:
            logger.error("[JiuWenSwarm] skilldev 请求处理失败: %s", exc)
            return AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(exc)},
                metadata=request.metadata,
            )
        return AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=True,
            payload=payload,
            metadata=request.metadata,
        )

    async def _handle_skills_request(self, request: AgentRequest) -> AgentResponse | None:
        """处理 Skills 相关请求，返回 None 表示不是 Skills 请求."""
        if request.req_method not in _SKILL_ROUTES:
            return None

        handler_name = _SKILL_ROUTES[request.req_method]
        handler = getattr(self._skill_manager, handler_name)
        try:
            payload = await handler(request.params)
            _reload_after_skills = handler_name in [
                "handle_skills_install",
                "handle_skills_import_local",
                "handle_skills_toggle",
                "handle_skills_skillnet_install",
                "handle_skills_clawhub_download",
                "handle_skills_team_skills_hub_install",
            ]
            if handler_name == "handle_skills_skillnet_install" and payload.get("pending"):
                _reload_after_skills = False
            if _reload_after_skills:
                await self.create_instance()
                await self._reload_team_skill_rails(request.session_id)
            elif handler_name == "handle_skills_uninstall" and payload.get("success"):
                # 卸载只需轻量刷新 skill rail，不需要全量重建 agent 实例。
                # SkillUseRail 会通过文件系统签名检测到目录删除并自动刷新，
                # 这里主动调用 reload_skills() 确保立即生效，避免延迟到下一次模型调用。
                await self._refresh_skill_rails_after_change()
                await self._reload_team_skill_rails(request.session_id)
            elif handler_name in _SKILL_VISIBILITY_WRITE_HANDLERS and payload.get("success"):
                # 可见性只改 metadata，库内容没变：agent 无需重建，只要让各
                # skill rail 重新读一次 metadata，授权/撤权即刻生效。
                await self._refresh_skill_rails_after_change()
                await self._reload_team_skill_rails(request.session_id)
        except Exception as exc:
            logger.error("[JiuWenSwarm] skills 请求处理失败: %s", exc)
            return AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(exc)},
                metadata=request.metadata,
            )
        return AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=True,
            payload=payload,
            metadata=request.metadata,
        )

    async def _handle_models_request(self, request: AgentRequest) -> AgentResponse | None:
        """处理模型注册表相关请求,返回 None 表示不是 models 请求.

        与 skills 不同:注册/注销只发生在 harness 工具侧,RPC 只有只读 list,
        处理后无需重建 agent 实例。
        """
        if request.req_method not in _MODEL_ROUTES:
            return None

        handler_name = _MODEL_ROUTES[request.req_method]
        handler = getattr(self._model_registry, handler_name)
        try:
            payload = await handler(request.params)
        except Exception as exc:
            logger.error("[JiuWenSwarm] models 请求处理失败: %s", exc)
            return AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(exc)},
                metadata=request.metadata,
            )
        return AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=True,
            payload=payload,
            metadata=request.metadata,
        )

    async def _handle_plugins_request(self, request: AgentRequest) -> AgentResponse | None:
        """处理 Plugin 相关请求，返回 None 表示不是 Plugin 请求."""
        if request.req_method not in _PLUGIN_ROUTES:
            return None

        handler_name = _PLUGIN_ROUTES[request.req_method]
        handler = getattr(self._skill_manager, handler_name)
        try:
            payload = await handler(request.params)
            # install / uninstall / reload 之后重建 Agent 实例
            _reload_after = handler_name in [
                "handle_plugins_install",
                "handle_plugins_uninstall",
                "handle_plugins_reload",
            ]
            if _reload_after:
                await self.create_instance()
        except Exception as exc:
            logger.error("[JiuWenSwarm] plugins 请求处理失败: %s", exc)
            return AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(exc)},
                metadata=request.metadata,
            )
        return AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=True,
            payload=payload,
            metadata=request.metadata,
        )

    async def _handle_stateless_func_request(self, request: AgentRequest) -> AgentResponse | None:
        """处理无状态 RPC（三方 Agent / 硬件监控 / 用户管理），返回 None 表示未命中."""
        if request.req_method not in _STATELESS_FUNC_ROUTES:
            return None

        handler = _STATELESS_FUNC_ROUTES[request.req_method]
        try:
            payload = await handler(request.params)
        except Exception as exc:
            logger.error("[JiuWenSwarm] stateless 请求处理失败(%s): %s", request.req_method, exc)
            return AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(exc)},
                metadata=request.metadata,
            )
        return AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=True,
            payload=payload,
            metadata=request.metadata,
        )

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

        if intent == "pause":
            # 暂停：不取消任务，只暂停 ReAct 循环
            return await adapter.process_interrupt(request)

        if intent == "resume":
            # 恢复：恢复 ReAct 循环
            return await adapter.process_interrupt(request)

        if intent == "supplement":
            # 取消当前 session 的任务
            response = await adapter.process_interrupt(request)
            await self._session_manager.cancel_session_task(session_id, "interrupt(supplement): ")
            return response

        # cancel: 先调用 adapter.process_interrupt（此时 session 仍在 _active_session_ids 中，
        # guard 能通过），再 cancel_session_task（其 finally 会把 session 从 _active_session_ids 移除）。
        # 顺序不能反，否则 process_interrupt 的 session guard 会误判为 "not active" 而跳过 abort。
        response = await adapter.process_interrupt(request)
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
                await self._session_manager.cancel_session_task(
                    session_id,
                    reason,
                    wait_timeout=5.0,
                )
                message = "团队已暂停" if paused else "当前没有可暂停的团队任务"
            else:
                # Use cancel_session_runtime to remove from Runner pool
                cancelled = await team_manager.cancel_session_runtime(session_id, reason=reason)
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
        from jiuwenswarm.agents.harness.team import get_team_manager

        try:
            team_manager = get_team_manager(channel_id)
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
                            record_fn(
                                request,
                                action=str(action),
                                result_type=str(result_type) if result_type else None,
                                goal_payload=goal_obj if isinstance(goal_obj, dict) else None,
                            )
                        else:
                            objective = str(params.get("objective") or "").strip()
                            if objective:
                                goal_id = (
                                    str(goal_obj.get("goal_id") or "").strip() or None
                                    if isinstance(goal_obj, dict)
                                    else None
                                )
                                append_history_record(
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

        plugins_response = await self._handle_plugins_request(request)
        if plugins_response is not None:
            return plugins_response

        models_response = await self._handle_models_request(request)
        if models_response is not None:
            return models_response

        stateless_response = await self._handle_stateless_func_request(request)
        if stateless_response is not None:
            return stateless_response

        adapter = self._ensure_adapter(mode=self._adapter_mode_for_request(request))
        validator = getattr(adapter, "validate_auto_permission_workspace_request", None)
        if callable(validator):
            validator(request)

        heartbeat_response = await adapter.handle_heartbeat(request)
        if heartbeat_response is not None:
            return heartbeat_response

        session_id = self._session_manager.get_session_id(request.session_id)
        query = request.params.get("query", "")
        feedback_scheduled = False

        def _schedule_feedback_once(terminal_status: str) -> None:
            nonlocal feedback_scheduled
            if feedback_scheduled:
                return
            _schedule_symphony_session_feedback(
                session_id,
                request.request_id,
                terminal_status=terminal_status,
            )
            feedback_scheduled = True
        # proactive_recommendation 是系统触发的推荐指令（不是用户说的话），不写 user
        # history——否则刷新页面会显示"[主动推荐指令] xxx"这种用户没说过的消息。
        if _should_record_user_history(
            request.params,
            channel_id=request.channel_id,
            request_method=request.req_method,
        ):
            append_history_record(
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

        # cloud memory: before chat hook
        if memory_mode == "cloud":
            mem_ctx = MemoryHookContext(
                session_id=request.session_id or "default",
                request_id=request.request_id or "",
                channel_id=request.channel_id,
                agent_name="main_agent",
                workspace_dir=str(get_agent_home_dir()),
                extra=request.params,
            )
            try:
                await ExtensionRegistry.get_instance().trigger(AgentServerHookEvents.MEMORY_BEFORE_CHAT, mem_ctx)
            except asyncio.CancelledError:
                _schedule_feedback_once("cancelled")
                raise
            except Exception:
                _schedule_feedback_once("error")
                raise
            memory_block = "\n\n".join(b for b in mem_ctx.memory_blocks if b)
            inputs["memory_block"] = memory_block

        async def run_agent_task():
            return await adapter.process_message_impl(request, inputs)

        try:
            result = await self._session_manager.submit_and_wait(session_id, run_agent_task)
        except asyncio.CancelledError:
            _schedule_feedback_once("cancelled")
            raise
        except Exception:
            _schedule_feedback_once("error")
            raise

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
            append_history_record(
                session_id=session_id,
                request_id=request.request_id,
                channel_id=request.channel_id,
                role="assistant",
                event_type="chat.final",
                content=content_str,
                timestamp=time.time(),
                mode=request.params.get("mode", "unknown"),
            )
            _schedule_feedback_once("success")

            # cloud memory: after chat hook
            if memory_mode == "cloud":
                after_ctx = MemoryHookContext(
                    session_id=request.session_id or "default",
                    request_id=request.request_id or "",
                    channel_id=request.channel_id,
                    agent_name="main_agent",
                    workspace_dir=str(get_agent_home_dir()),
                    assistant_message=content_str,
                    extra=request.params,
                )
                await ExtensionRegistry.get_instance().trigger(AgentServerHookEvents.MEMORY_AFTER_CHAT, after_ctx)

            # auto memory: extract memories after conversation ends
            # 需要 auto_memory_enabled 和 memory.enabled 都为 true 才触发
            mode = request.params.get("mode", "code") if isinstance(request.params, dict) else "code"
            config = get_config()
            if is_auto_memory_enabled(mode, config) and is_memory_enabled(mode, config):
                _trigger_auto_memory_extraction(adapter, request, session_id, is_stream=False)

        if not feedback_scheduled:
            _schedule_feedback_once("success" if result.ok else "error")
        return result

    async def process_message_stream(
            self, request: AgentRequest
    ) -> AsyncIterator[AgentResponseChunk]:
        """处理流式请求.

        支持多 session 并发执行，同 session 内任务按先进后出顺序执行.
        """
        # Streaming command.goal: get/pause/clear stay one-shot; set/resume
        # continue into the DeepAdapter attach→set/resume→read path below.
        if request.req_method == ReqMethod.COMMAND_GOAL:
            params = request.params if isinstance(request.params, dict) else {}
            action = str(params.get("action", "get") or "get").strip().lower()
            if action not in {"set", "resume"}:
                try:
                    adapter = self._ensure_adapter(mode=self._adapter_mode_for_request(request))
                    session_id = self._session_manager.get_session_id(request.session_id)
                    goal_result = await adapter.handle_goal_command_structured(params, session_id)
                    if goal_result is None:
                        yield AgentResponseChunk(
                            request_id=request.request_id,
                            channel_id=request.channel_id,
                            payload={"event_type": "chat.error", "error": "Goal command not handled"},
                            is_complete=True,
                        )
                        return
                    result_type = goal_result.get("result_type")
                    if result_type == "goal_error":
                        yield AgentResponseChunk(
                            request_id=request.request_id,
                            channel_id=request.channel_id,
                            payload={
                                "event_type": "chat.error",
                                "code": goal_result.get("error_code", "goal_error"),
                                "error": goal_result.get("error", "goal operation failed"),
                                "goal": goal_result.get("goal"),
                            },
                            is_complete=True,
                        )
                        return
                    yield AgentResponseChunk(
                        request_id=request.request_id,
                        channel_id=request.channel_id,
                        payload={
                            "event_type": "goal.snapshot",
                            "action": goal_result.get("action", action),
                            "goal": goal_result.get("goal"),
                            "cleared_goal": goal_result.get("cleared_goal"),
                            "message": goal_result.get("output", ""),
                        },
                        is_complete=True,
                    )
                except Exception as exc:
                    logger.exception(
                        "[JiuWenSwarm] streaming COMMAND_GOAL failed: request_id=%s error=%s",
                        request.request_id,
                        exc,
                    )
                    yield AgentResponseChunk(
                        request_id=request.request_id,
                        channel_id=request.channel_id,
                        payload={"event_type": "chat.error", "error": f"Goal command error: {exc}"},
                        is_complete=True,
                    )
                return
            # set/resume: fall through to streaming adapter (no user-history write).

        # SkillDev 流式请求：直接委托给 SkillDevService，绕过 ReActAgent
        if request.req_method in _SKILLDEV_METHODS:
            service = self._get_skilldev_service()
            try:
                async for chunk in service.handle(request):
                    yield chunk
            except Exception as exc:
                logger.error("[JiuWenSwarm] skilldev 流式请求处理失败: %s", exc)
                yield AgentResponseChunk(
                    request_id=request.request_id,
                    channel_id=request.channel_id,
                    payload={"event_type": "skilldev.error", "error": str(exc)},
                    is_complete=True,
            )
            return

        # 无状态 RPC（skills / plugins / Skill Graph）不需要 adapter，
        # 委托给非流式 handler 并包装为单个 chunk，避免触发 adapter 懒初始化。
        # skilldev 已由上面的流式分支处理，这里不会再命中。
        for stateless_handler in (
            self._handle_skills_request,
            self._handle_plugins_request,
            self._handle_models_request,
            self._handle_stateless_func_request,
        ):
            stateless_response = await stateless_handler(request)
            if stateless_response is not None:
                if stateless_response.ok:
                    chunk_payload = stateless_response.payload
                else:
                    # handler 内部捕获异常并以 ok=False 返回时，注入 chat.error
                    # 事件标记，使下游能识别为错误（与非流式 resp.ok 传播等价）。
                    raw_payload = stateless_response.payload
                    error_msg = (
                        raw_payload.get("error", "unknown error")
                        if isinstance(raw_payload, dict)
                        else "unknown error"
                    )
                    chunk_payload = {"event_type": "chat.error", "error": error_msg}
                yield AgentResponseChunk(
                    request_id=request.request_id,
                    channel_id=request.channel_id,
                    payload=chunk_payload,
                    is_complete=True,
                )
                return

        adapter = self._ensure_adapter(mode=self._adapter_mode_for_request(request))
        validator = getattr(adapter, "validate_auto_permission_workspace_request", None)
        if callable(validator):
            validator(request)

        session_id = self._session_manager.get_session_id(request.session_id)
        query = request.params.get("query", "")

        mode = request.params.get("mode", "") if isinstance(request.params, dict) else ""
        team_flag = request.params.get("team", False) if isinstance(request.params, dict) else False
        is_team_mode = team_flag or is_team_runtime_mode(mode)
        is_auto_harness_resume = (
            isinstance(mode, str)
            and mode.strip().lower() == "auto_harness"
            and isinstance(request.params.get("activate_response"), dict)
        )

        # proactive_recommendation 是系统触发的推荐指令（不是用户说的话），不写 user
        # history——否则刷新页面会显示"[主动推荐指令] xxx"这种用户没说过的消息。
        # command.goal set history is written only after a successful set inside
        # the DeepAdapter stream path (same success gate as unary process_message).
        params_for_history = request.params if isinstance(request.params, dict) else {}
        if (
            request.req_method != ReqMethod.COMMAND_GOAL
            and _should_record_user_history(
                params_for_history,
                channel_id=request.channel_id,
                request_method=request.req_method,
            )
        ):
            append_history_record(
                session_id=session_id,
                request_id=request.request_id,
                channel_id=request.channel_id,
                role="user",
                content=_history_user_content(params_for_history, query),
                timestamp=time.time(),
                extra=_history_user_extra(params_for_history),
                channel_metadata=request.metadata,
                mode=params_for_history.get("mode", "unknown"),
            )

        logger.info(
            "[JiuWenSwarm] 处理流式请求: request_id=%s channel_id=%s session_id=%s sdk=%s",
            request.request_id, request.channel_id, session_id, self._sdk_name,
        )

        rid = request.request_id
        cid = request.channel_id
        feedback_scheduled = False

        def _schedule_feedback_once(terminal_status: str) -> None:
            nonlocal feedback_scheduled
            if feedback_scheduled:
                return
            _schedule_symphony_session_feedback(
                session_id,
                rid,
                terminal_status=terminal_status,
            )
            feedback_scheduled = True

        try:
            inputs, memory_mode, user_turn = self._build_inputs(request)
        except asyncio.CancelledError:
            _schedule_feedback_once("cancelled")
            raise
        except _TeamPlanApprovalPayloadError as exc:
            _schedule_feedback_once("error")
            yield AgentResponseChunk(
                request_id=rid,
                channel_id=cid,
                payload={"event_type": "chat.error", "error": str(exc)},
                is_complete=False,
            )
            yield AgentResponseChunk(
                request_id=rid,
                channel_id=cid,
                payload=None,
                is_complete=True,
            )
            return
        except Exception:
            _schedule_feedback_once("error")
            raise

        # Team 模式：把整个 turn 交给 team_helpers。它先用 turn.text（用户原
        # 文）解析 /debug、$member 与 slash，再用同一个 render() 投递，因此
        # leader 收到的信封与单 agent 逐字段一致。
        team_query_is_interactive_input = False
        if is_team_mode:
            from openjiuwen.core.session.interaction.interactive_input import InteractiveInput

            team_query_is_interactive_input = isinstance(inputs.get("query"), InteractiveInput)
            inputs[TEAM_USER_TURN_KEY] = user_turn
            logger.info(
                "[JiuWenSwarm] Team模式 user turn: interactive_input=%s text=%s",
                team_query_is_interactive_input,
                str(user_turn.text)[:100],
            )

        # cloud memory: before chat hook
        if memory_mode == "cloud":
            mem_ctx = MemoryHookContext(
                session_id=request.session_id or "default",
                request_id=request.request_id or "",
                channel_id=request.channel_id,
                agent_name="main_agent",
                workspace_dir=str(get_agent_home_dir()),
                extra=request.params,
            )
            try:
                await ExtensionRegistry.get_instance().trigger(AgentServerHookEvents.MEMORY_BEFORE_CHAT, mem_ctx)
            except asyncio.CancelledError:
                _schedule_feedback_once("cancelled")
                raise
            except Exception:
                _schedule_feedback_once("error")
                raise
            memory_block = "\n\n".join(b for b in mem_ctx.memory_blocks if b)
            inputs["memory_block"] = memory_block

        # Team 模式: 检查是否是后续请求（需要绕过 Session Manager）
        is_team_first_request = True
        if is_team_mode:
            from jiuwenswarm.agents.harness.team import get_team_manager
            from jiuwenswarm.server.runtime.agent_adapter.team_helpers import _team_session_has_runtime

            team_manager = get_team_manager(request.channel_id)
            if team_query_is_interactive_input:
                # Interrupt-resume answers must bypass the session queue and
                # flow straight into team_helpers, which knows how to wait for
                # or recover a paused runtime before calling interact().
                is_team_first_request = False
            else:
                try:
                    is_team_first_request = not await _team_session_has_runtime(
                        team_manager, session_id
                    )
                except asyncio.CancelledError:
                    _schedule_feedback_once("cancelled")
                    raise
                except Exception:
                    _schedule_feedback_once("error")
                    raise
            logger.info(
                "[JiuWenSwarm] Team模式: session_id=%s is_first=%s interactive_input=%s",
                session_id,
                is_team_first_request,
                team_query_is_interactive_input,
            )

        stream_queue = asyncio.Queue(maxsize=self.STREAM_QUEUE_MAXSIZE)
        stream_done = asyncio.Event()
        producer_cancellation: asyncio.CancelledError | None = None
        final_answer_content = ""
        final_answer_chunks: list[str] = []
        durable_pending_final_chunks: list[str] = []
        durable_pending_final_started_at: float | None = None
        durable_pending_reasoning_chunks: list[str] = []
        # reasoning 首/末帧时刻（epoch ms）：随 reasoning_content 一起落盘，供刷新后
        # 恢复耗时终点；即使 final/closeReasoning 丢失，末帧也是真实事件时刻。
        durable_pending_reasoning_started_at: float | None = None
        durable_pending_reasoning_updated_at: float | None = None
        durable_final_content = ""
        # 这条流是否带过 Goal 事件。Goal 仍 active 时流结束是不发 chat.final 的
        # （见 interface_deep._should_emit_stream_end_chat_final），气泡里的正文
        # 就没人落盘；收尾时按这个标记补一次，只影响 Goal 流。
        saw_goal_stream_output = False

        def _note_durable_reasoning_delta() -> None:
            nonlocal durable_pending_reasoning_started_at, durable_pending_reasoning_updated_at
            now_ms = time.time() * 1000
            if durable_pending_reasoning_started_at is None:
                durable_pending_reasoning_started_at = now_ms
            durable_pending_reasoning_updated_at = now_ms

        def _consume_durable_reasoning_content() -> tuple[str, float | None, float | None]:
            nonlocal \
                durable_pending_reasoning_chunks, \
                durable_pending_reasoning_started_at, \
                durable_pending_reasoning_updated_at
            reasoning_text = "".join(durable_pending_reasoning_chunks)
            started_at = durable_pending_reasoning_started_at
            updated_at = durable_pending_reasoning_updated_at
            durable_pending_reasoning_chunks = []
            durable_pending_reasoning_started_at = None
            durable_pending_reasoning_updated_at = None
            return reasoning_text, started_at, updated_at

        def _attach_reasoning_content(extra_fields: dict[str, Any] | None = None) -> dict[str, Any] | None:
            reasoning_text, started_at, updated_at = _consume_durable_reasoning_content()
            if not reasoning_text.strip():
                return extra_fields
            merged = dict(extra_fields) if isinstance(extra_fields, dict) else {}
            merged["reasoning_content"] = reasoning_text
            merged["reasoning_updated_at"] = updated_at or started_at or (time.time() * 1000)
            return merged

        def _persist_pending_reasoning() -> None:
            """异常/非 Goal 结束兜底：把未随 tool_call/final 落盘的思考补落一条记录。

            正常完成时 reasoning 已被最后一条 tool_call/final 附走、缓冲为空，这里 no-op。
            单独落成 chat.reasoning 记录（不带正文 final），前端按 reasoning_content 提取。
            """
            started_at = durable_pending_reasoning_started_at
            updated_at = durable_pending_reasoning_updated_at
            reasoning_text, _, _ = _consume_durable_reasoning_content()
            if not reasoning_text.strip():
                return
            now_ms = time.time() * 1000
            append_history_record(
                session_id=session_id,
                request_id=rid,
                channel_id=cid,
                role="assistant",
                event_type="chat.reasoning",
                content="",
                timestamp=(started_at or now_ms) / 1000,
                extra={
                    "reasoning_content": reasoning_text,
                    "reasoning_updated_at": updated_at or started_at or now_ms,
                },
                mode=request.params.get("mode", "unknown"),
            )

        def _reset_durable_pending_final() -> None:
            nonlocal durable_pending_final_chunks, durable_pending_final_started_at
            durable_pending_final_chunks = []
            durable_pending_final_started_at = None

        def _note_durable_pending_final_delta(content: str) -> None:
            nonlocal durable_pending_final_started_at
            if durable_pending_final_started_at is None:
                durable_pending_final_started_at = time.time()
            durable_pending_final_chunks.append(content)

        def _note_goal_stream_payload(event_type: str, payload: dict[str, Any]) -> None:
            nonlocal saw_goal_stream_output
            if saw_goal_stream_output:
                return
            if event_type.startswith("goal.") or payload.get("goal_intermediate"):
                saw_goal_stream_output = True

        def _persist_pending_final_text() -> None:
            nonlocal durable_final_content
            pending_text = "".join(durable_pending_final_chunks)
            segment_started_at = durable_pending_final_started_at
            _reset_durable_pending_final()
            if not pending_text or pending_text == durable_final_content:
                return
            extra_fields = _attach_reasoning_content({
                k: v for k, v in request.params.items()
                if k in ("source", "proactive_type", "proactive_target")
            })
            if not isinstance(extra_fields, dict):
                extra_fields = {}
            record_timestamp = _resolve_final_record_timestamp(
                event_type="chat.final",
                segment_started_at=segment_started_at,
                extra_fields=extra_fields,
            )
            append_history_record(
                session_id=session_id,
                request_id=rid,
                channel_id=cid,
                role="assistant",
                event_type="chat.final",
                content=pending_text,
                timestamp=record_timestamp,
                # 透传 proactive 标记到 history——刷新页面时前端靠 payload.source===
                # 'proactive_recommendation' 渲染推荐卡片，不带则退化白色气泡。
                extra=extra_fields if extra_fields else None,
                mode=request.params.get("mode", "unknown"),
            )
            durable_final_content = pending_text

        async def run_stream_task():
            nonlocal producer_cancellation
            logger.info("[JiuWenSwarm] run_stream_task started: request_id=%s session_id=%s", rid, session_id)
            _put_count = 0
            producer_stream: AsyncIterator[AgentResponseChunk] | None = None
            try:
                producer_stream = adapter.process_message_stream_impl(request, inputs)
                async for chunk in producer_stream:
                    _put_count += 1
                    if _put_count <= 3:
                        _pl = getattr(chunk, "payload", None) or {}
                        _et = _pl.get("event_type", "") if isinstance(_pl, dict) else ""
                        logger.info(
                            "[JiuWenSwarm] run_stream_task chunk #%s: request_id=%s event_type=%s",
                            _put_count, rid, _et,
                        )
                    await stream_queue.put(("chunk", chunk))
            except asyncio.CancelledError as exc:
                producer_cancellation = exc
                logger.info("[JiuWenSwarm] 流式任务被取消: request_id=%s session_id=%s", rid, session_id)
                # The outer consumer owns cancellation and awaits this task in
                # its finally block.  Do not enqueue into a potentially full
                # bounded queue after that consumer has gone away.
                raise
            except Exception as exc:
                logger.exception("[JiuWenSwarm] 流式任务异常: %s", exc)
                try:
                    await stream_queue.put(("error", exc))
                except asyncio.CancelledError as cancel_exc:
                    producer_cancellation = cancel_exc
                    raise
            finally:
                try:
                    if producer_stream is not None:
                        close_stream = getattr(producer_stream, "aclose", None)
                        if callable(close_stream):
                            try:
                                await close_stream()
                            except asyncio.CancelledError as exc:
                                producer_cancellation = exc
                                raise
                            except Exception:
                                logger.debug(
                                    "[JiuWenSwarm] stream producer close failed: request_id=%s",
                                    rid,
                                    exc_info=True,
                                )
                finally:
                    logger.info(
                        "[JiuWenSwarm] run_stream_task finished: request_id=%s total_chunks=%s",
                        rid, _put_count,
                    )
                    stream_done.set()

        # Team 模式: 后续请求直接执行，绕过 Session Manager 队列
        # 因为 Team 是长期运行的(persistent)，interact 调用不需要等待前一个任务完成
        # 且 team_helpers 内部已有请求锁保证同一 session 的请求串行执行
        if is_team_mode and not is_team_first_request:
            logger.info(
                "[JiuWenSwarm] Team模式后续请求，直接执行: request_id=%s session_id=%s",
                rid, session_id,
            )
            stream_task = asyncio.create_task(run_stream_task())
        elif is_auto_harness_resume:
            logger.info(
                "[JiuWenSwarm] Auto-Harness resume请求，绕过Session队列: request_id=%s session_id=%s",
                rid, session_id,
            )
            stream_task = asyncio.create_task(run_stream_task())
        else:
            # DeepAgentRuntimeController is the session scheduler for ordinary
            # chat.  Starting this facade task immediately lets runtime_send()
            # atomically route an arriving user input as a steer, follow-up, or
            # replacement round; an outer SessionManager queue would otherwise
            # wait behind the long-lived output consumer.
            stream_task = asyncio.create_task(run_stream_task())

        suppress_a2ui_stream = False
        a2ui_pending_render_sent = False
        a2ui_stream_probe = ""
        team_a2ui_blocks = TeamA2UIBlockBuffer()
        repair_call = getattr(adapter, "repair_model_response", None)
        retry_without_a2ui_call = self._make_retry_without_a2ui_call(
            adapter=adapter,
            request=request,
        )

        team_a2ui_tasks: dict[tuple[str, str], asyncio.Task] = {}
        team_a2ui_pending_finals: dict[tuple[str, str], dict[str, Any]] = {}

        async def _finalize_team_a2ui_block(payload: dict[str, Any], decision: Any) -> None:
            try:
                finalized = await finalize_assistant_response_if_a2ui(
                    decision.raw_block,
                    channel=cid,
                    user_query=user_turn.text,
                    request_id=f"{rid}:{decision.key[0]}:{decision.key[1]}",
                    repair_call=repair_call,
                    retry_without_a2ui_call=retry_without_a2ui_call,
                )
            except Exception:  # noqa: BLE001
                logger.exception(
                    "Team A2UI local finalization failed: request_id=%s round=%s member=%s",
                    rid,
                    decision.key[0],
                    decision.key[1],
                )
                finalized = decision.raw_block
            await stream_queue.put((
                "team_a2ui_finalized",
                (payload, decision, finalized),
            ))

        def _schedule_team_a2ui_block(payload: dict[str, Any], decision: Any) -> None:
            logger.info(
                "Team A2UI block finalization scheduled: request_id=%s round=%s member=%s",
                rid,
                decision.key[0],
                decision.key[1],
            )
            team_a2ui_tasks[decision.key] = asyncio.create_task(
                _finalize_team_a2ui_block(payload, decision)
            )

        def _process_team_a2ui_payload(
                payload: dict[str, Any],
        ) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
            """Schedule member-local finalization without pausing teammates."""
            event_type = str(payload.get("event_type") or "")
            content = str(payload.get("content") or "")
            key = team_a2ui_blocks.key_for(payload)
            if event_type == "chat.final" and key in team_a2ui_tasks:
                team_a2ui_pending_finals[key] = payload
                return [], None

            decision = team_a2ui_blocks.consume(payload, event_type, content)
            if decision is None:
                return [], payload

            direct_payloads: list[dict[str, Any]] = []
            if decision.passthrough:
                direct_payloads.append({**payload, "content": decision.passthrough})

            if decision.replacement is not None:
                return direct_payloads, {**payload, "content": decision.replacement}

            if decision.raw_block:
                _schedule_team_a2ui_block(payload, decision)
            if decision.suppress:
                return direct_payloads, None
            return direct_payloads, payload

        _yielded_from_queue = 0
        logger.info(
            "[JiuWenSwarm] consumer loop starting: request_id=%s is_team=%s is_first=%s",
            rid, is_team_mode, is_team_first_request,
        )
        stream_aborted = False
        abort_terminal_status = "cancelled"
        completion_status = "success"
        terminal_final_persisted = False

        try:
            while (
                    not stream_done.is_set()
                    or not stream_queue.empty()
                    or bool(team_a2ui_tasks)
            ):
                try:
                    item = await asyncio.wait_for(stream_queue.get(), timeout=0.1)
                except asyncio.TimeoutError:
                    continue

                event_type, data = item
                if event_type == "team_a2ui_finalized":
                    original_payload, decision, finalized = data
                    team_a2ui_tasks.pop(decision.key, None)
                    pending_final = team_a2ui_pending_finals.pop(decision.key, None)
                    if pending_final is not None:
                        team_a2ui_blocks.remember_finalized(
                            decision.key,
                            decision.raw_block,
                            finalized,
                        )
                        replay = team_a2ui_blocks.consume(
                            pending_final,
                            "chat.final",
                            str(pending_final.get("content") or ""),
                        )
                        if replay is not None and replay.raw_block:
                            _schedule_team_a2ui_block(pending_final, replay)
                            continue
                        replay_content = replay.replacement if replay is not None else None
                        output_payload = {
                            **pending_final,
                            "content": replay_content or finalized,
                            "session_id": session_id,
                        }
                    elif decision.finalize_whole_event:
                        output_payload = {
                            **original_payload,
                            "event_type": "chat.final",
                            "content": finalized,
                            "session_id": session_id,
                        }
                    else:
                        team_a2ui_blocks.remember_finalized(
                            decision.key,
                            decision.raw_block,
                            finalized,
                        )
                        output_payload = {
                            **original_payload,
                            "content": f"{finalized}{decision.trailing}",
                        }
                    output_payload["_team_a2ui_finalized"] = True
                    data = AgentResponseChunk(
                        request_id=rid,
                        channel_id=cid,
                        payload=output_payload,
                        is_complete=False,
                    )
                    event_type = "chunk"
                _yielded_from_queue += 1
                if _yielded_from_queue <= 3:
                    _pl = getattr(data, "payload", None) if event_type == "chunk" else None
                    _et = _pl.get("event_type", "") if isinstance(_pl, dict) else ""
                    logger.info(
                        "[JiuWenSwarm] consumer loop yield #%s: request_id=%s event_type=%s item_type=%s",
                        _yielded_from_queue, rid, _et, event_type,
                    )

                if event_type == "error":
                    if isinstance(data, asyncio.CancelledError):
                        logger.info("[JiuWenSwarm] 流式处理被中断: request_id=%s", rid)
                        raise data
                    # Surface exception class so consumers can classify
                    # failures structurally instead of regexing the message.
                    error_type = (
                        type(data).__name__ if isinstance(data, BaseException) else ""
                    )
                    error_payload: dict[str, Any] = {
                        "event_type": "chat.error",
                        "error": str(data),
                    }
                    if error_type:
                        error_payload["error_type"] = error_type
                    append_history_record(
                        session_id=session_id,
                        request_id=rid,
                        channel_id=cid,
                        role="assistant",
                        event_type="chat.error",
                        content=str(data),
                        timestamp=time.time(),
                        mode=request.params.get("mode", "unknown"),
                        extra={"error_type": error_type} if error_type else None,
                    )
                    completion_status = "error"
                    yield AgentResponseChunk(
                        request_id=rid,
                        channel_id=cid,
                        payload=error_payload,
                        is_complete=False,
                    )
                else:
                    if isinstance(data, AgentResponseChunk):
                        if suppress_a2ui_stream:
                            data = _normalize_nested_stream_chunk(data)
                            if data is None:
                                continue
                        if isinstance(data.payload, dict) and isinstance(data.payload.get("event_type"), str):
                            et = str(data.payload.get("event_type"))
                            if et == "chat.error":
                                completion_status = "error"
                                abort_terminal_status = "error"
                            _note_goal_stream_payload(et, data.payload)
                            should_record = et.startswith("chat.")
                            final_segment_started_at: float | None = None
                            if not should_record and et == EventType.TEAM_MESSAGE.value:
                                should_record = True
                            if et == "context.compression_state":
                                _append_compact_history_from_payload(
                                    payload=data.payload,
                                    session_id=session_id,
                                    request_id=rid,
                                    channel_id=cid,
                                    mode=request.params.get("mode", "unknown"),
                                )

                            payload_content = str(data.payload.get("content", ""))
                            locally_finalized = bool(data.payload.get("_team_a2ui_finalized"))
                            if locally_finalized:
                                next_payload = dict(data.payload)
                                next_payload.pop("_team_a2ui_finalized", None)
                                data = replace(data, payload=next_payload)
                            if (
                                    is_team_mode
                                    and not locally_finalized
                                    and et in {"chat.delta", "chat.final"}
                            ):
                                direct_payloads, next_payload = _process_team_a2ui_payload(data.payload)
                                for direct_payload in direct_payloads:
                                    yield replace(
                                        data,
                                        payload=direct_payload,
                                        is_complete=False,
                                    )
                                if next_payload is None:
                                    continue
                                data = replace(data, payload=next_payload)
                                et = str(next_payload.get("event_type") or et)
                                payload_content = str(next_payload.get("content", ""))
                            a2ui_split = None
                            if (
                                    _should_probe_a2ui_stream(is_team_mode=is_team_mode)
                                    and et in {"chat.delta", "chat.final"}
                                    and payload_content
                            ):
                                a2ui_split = _split_a2ui_stream_content(a2ui_stream_probe, payload_content)
                                a2ui_stream_probe = _extend_a2ui_stream_probe(a2ui_stream_probe, payload_content)
                            if _should_defer_a2ui_processing_status(
                                    suppress_a2ui_stream=suppress_a2ui_stream,
                                    event_type=et,
                                    payload=data.payload,
                            ):
                                logger.info(
                                    "A2UI processing_status=false deferred until finalization: "
                                    "request_id=%s",
                                    rid,
                                )
                                continue
                            if et == "chat.delta":
                                final_answer_chunks.append(payload_content)
                                if suppress_a2ui_stream or a2ui_split is not None:
                                    first_a2ui_suppression = not suppress_a2ui_stream
                                    if first_a2ui_suppression:
                                        logger.info(
                                            "A2UI stream suppression activated: request_id=%s event_type=%s",
                                            rid,
                                            et,
                                        )
                                    suppress_a2ui_stream = True
                                    if a2ui_split is not None and a2ui_split[0]:
                                        prefix_payload = dict(data.payload)
                                        prefix_payload["content"] = a2ui_split[0]
                                        yield AgentResponseChunk(
                                            request_id=data.request_id,
                                            channel_id=data.channel_id,
                                            payload=prefix_payload,
                                            is_complete=False,
                                        )
                                    if first_a2ui_suppression and not a2ui_pending_render_sent:
                                        yield _make_a2ui_pending_render_chunk(request_id=rid, channel_id=cid)
                                        a2ui_pending_render_sent = True
                                    continue
                                _note_durable_pending_final_delta(payload_content)
                                should_record = False
                            elif et == "chat.reasoning":
                                _note_durable_reasoning_delta()
                                durable_pending_reasoning_chunks.append(payload_content)
                                should_record = False
                            elif et == "chat.tool_call":
                                _persist_pending_final_text()
                            elif et == "chat.final":
                                if isinstance(data.payload, dict):
                                    ensure_final_mode_inplace(data.payload)
                                if suppress_a2ui_stream or a2ui_split is not None:
                                    first_a2ui_suppression = not suppress_a2ui_stream
                                    if first_a2ui_suppression:
                                        logger.info(
                                            "A2UI stream suppression activated: request_id=%s event_type=%s",
                                            rid,
                                            et,
                                        )
                                    suppress_a2ui_stream = True
                                    if first_a2ui_suppression and not a2ui_pending_render_sent:
                                        yield _make_a2ui_pending_render_chunk(request_id=rid, channel_id=cid)
                                        a2ui_pending_render_sent = True
                                    if payload_content:
                                        final_answer_content = payload_content
                                        final_answer_chunks.clear()
                                    _reset_durable_pending_final()
                                    continue
                                # 先记住本段起始时刻：下面的 reset/flush 会把它清掉。
                                final_segment_started_at = durable_pending_final_started_at
                                if payload_content:
                                    _reset_durable_pending_final()
                                else:
                                    # 空 final 只是收尾/拆气泡信号（Goal 中间态 final 被降级成
                                    # chat.delta、流末尾的兜底 final），气泡里留下的正文就是前面
                                    # 那些 delta。这里必须落盘同一份，否则历史里整段回答会消失。
                                    _persist_pending_final_text()
                                    final_segment_started_at = None

                            if should_record:
                                payload_dict = dict(data.payload)
                                extra_fields = {k: v for k, v in payload_dict.items() if
                                                k not in ("event_type", "content")}
                                if et == EventType.TEAM_MESSAGE.value and "event" in payload_dict:
                                    event_data = payload_dict.get("event", {})
                                    if isinstance(event_data, dict):
                                        for k, v in event_data.items():
                                            if k not in ("type", "timestamp", "content"):
                                                extra_fields[k] = v
                                if et in {"chat.final", "chat.tool_call"}:
                                    extra_fields = _attach_reasoning_content(extra_fields)
                                # 透传 proactive 标记——刷新页面时前端靠 source 识别卡片
                                for pk in ("source", "proactive_type", "proactive_target"):
                                    if pk not in extra_fields and pk in request.params:
                                        extra_fields[pk] = request.params[pk]
                                if not isinstance(extra_fields, dict):
                                    extra_fields = {}
                                record_timestamp = _resolve_final_record_timestamp(
                                    event_type=et,
                                    segment_started_at=(
                                        final_segment_started_at if et == "chat.final" else None
                                    ),
                                    extra_fields=extra_fields,
                                )
                                append_history_record(
                                    session_id=session_id,
                                    request_id=rid,
                                    channel_id=cid,
                                    role="assistant",
                                    event_type=et,
                                    content=data.payload.get("content") or data.payload.get("error") or "",
                                    timestamp=record_timestamp,
                                    extra=extra_fields if extra_fields else None,
                                    mode=request.params.get("mode", "unknown"),
                                )
                                if et == "chat.final":
                                    durable_final_content = str(data.payload.get("content", ""))
                                    if not is_team_mode or data.payload.get("role") != "teammate":
                                        terminal_final_persisted = True
                            if et == "chat.final":
                                next_final_content = str(data.payload.get("content", ""))
                                if next_final_content:
                                    final_answer_content = next_final_content
                                    final_answer_chunks.clear()
                        yield data
                    elif isinstance(data, dict) and isinstance(data.get("event_type"), str):
                        et = str(data.get("event_type"))
                        if et == "chat.error":
                            completion_status = "error"
                            abort_terminal_status = "error"
                        _note_goal_stream_payload(et, data)
                        should_record = et.startswith("chat.")
                        final_segment_started_at = None
                        if not should_record and et == EventType.TEAM_MESSAGE.value:
                            should_record = True
                        if et == "context.compression_state":
                            _append_compact_history_from_payload(
                                payload=data,
                                session_id=session_id,
                                request_id=rid,
                                channel_id=cid,
                                mode=request.params.get("mode", "unknown"),
                            )

                        payload_content = str(data.get("content", ""))
                        locally_finalized = bool(data.get("_team_a2ui_finalized"))
                        if locally_finalized:
                            data = dict(data)
                            data.pop("_team_a2ui_finalized", None)
                        if (
                                is_team_mode
                                and not locally_finalized
                                and et in {"chat.delta", "chat.final"}
                        ):
                            direct_payloads, next_payload = _process_team_a2ui_payload(data)
                            for direct_payload in direct_payloads:
                                yield AgentResponseChunk(
                                    request_id=rid,
                                    channel_id=cid,
                                    payload=direct_payload,
                                    is_complete=False,
                                )
                            if next_payload is None:
                                continue
                            data = next_payload
                            et = str(next_payload.get("event_type") or et)
                            payload_content = str(next_payload.get("content", ""))
                        a2ui_split = None
                        if (
                                _should_probe_a2ui_stream(is_team_mode=is_team_mode)
                                and et in {"chat.delta", "chat.final"}
                                and payload_content
                        ):
                            a2ui_split = _split_a2ui_stream_content(a2ui_stream_probe, payload_content)
                            a2ui_stream_probe = _extend_a2ui_stream_probe(a2ui_stream_probe, payload_content)
                        if _should_defer_a2ui_processing_status(
                                suppress_a2ui_stream=suppress_a2ui_stream,
                                event_type=et,
                                payload=data,
                        ):
                            logger.info(
                                "A2UI processing_status=false deferred until finalization: "
                                "request_id=%s",
                                rid,
                            )
                            continue
                        if et == "chat.final":
                            ensure_final_mode_inplace(data)
                        if et == "chat.delta":
                            final_answer_chunks.append(payload_content)
                            if suppress_a2ui_stream or a2ui_split is not None:
                                first_a2ui_suppression = not suppress_a2ui_stream
                                if first_a2ui_suppression:
                                    logger.info(
                                        "A2UI stream suppression activated: request_id=%s event_type=%s",
                                        rid,
                                        et,
                                    )
                                suppress_a2ui_stream = True
                                if a2ui_split is not None and a2ui_split[0]:
                                    prefix_payload = dict(data)
                                    prefix_payload["content"] = a2ui_split[0]
                                    yield AgentResponseChunk(
                                        request_id=rid,
                                        channel_id=cid,
                                        payload=prefix_payload,
                                        is_complete=False,
                                    )
                                if first_a2ui_suppression and not a2ui_pending_render_sent:
                                    yield _make_a2ui_pending_render_chunk(request_id=rid, channel_id=cid)
                                    a2ui_pending_render_sent = True
                                continue
                            _note_durable_pending_final_delta(payload_content)
                            should_record = False
                        elif et == "chat.reasoning":
                            _note_durable_reasoning_delta()
                            durable_pending_reasoning_chunks.append(payload_content)
                            should_record = False
                        elif et == "chat.tool_call":
                            _persist_pending_final_text()
                        elif et == "chat.final":
                            if suppress_a2ui_stream or a2ui_split is not None:
                                first_a2ui_suppression = not suppress_a2ui_stream
                                if first_a2ui_suppression:
                                    logger.info(
                                        "A2UI stream suppression activated: request_id=%s event_type=%s",
                                        rid,
                                        et,
                                    )
                                suppress_a2ui_stream = True
                                if first_a2ui_suppression and not a2ui_pending_render_sent:
                                    yield _make_a2ui_pending_render_chunk(request_id=rid, channel_id=cid)
                                    a2ui_pending_render_sent = True
                                if payload_content:
                                    final_answer_content = payload_content
                                    final_answer_chunks.clear()
                                _reset_durable_pending_final()
                                continue
                            final_segment_started_at = durable_pending_final_started_at
                            if payload_content:
                                _reset_durable_pending_final()
                            else:
                                # 同上：空 final 收尾时把气泡正文落盘，别丢历史。
                                _persist_pending_final_text()
                                final_segment_started_at = None

                        if should_record:
                            extra_fields = {k: v for k, v in data.items() if k not in ("event_type", "content")}
                            if et == EventType.TEAM_MESSAGE.value and "event" in data:
                                event_data = data.get("event", {})
                                if isinstance(event_data, dict):
                                    for k, v in event_data.items():
                                        if k not in ("type", "timestamp", "content"):
                                            extra_fields[k] = v
                            if et in {"chat.final", "chat.tool_call"}:
                                extra_fields = _attach_reasoning_content(extra_fields)
                            # 透传 proactive 标记——刷新页面时前端靠 source 识别卡片
                            for pk in ("source", "proactive_type", "proactive_target"):
                                if pk not in extra_fields and pk in request.params:
                                    extra_fields[pk] = request.params[pk]
                            if not isinstance(extra_fields, dict):
                                extra_fields = {}
                            record_timestamp = _resolve_final_record_timestamp(
                                event_type=et,
                                segment_started_at=(
                                    final_segment_started_at if et == "chat.final" else None
                                ),
                                extra_fields=extra_fields,
                            )
                            append_history_record(
                                session_id=session_id,
                                request_id=rid,
                                channel_id=cid,
                                role="assistant",
                                event_type=et,
                                content=data.get("content") or data.get("error") or "",
                                timestamp=record_timestamp,
                                extra=extra_fields if extra_fields else None,
                                mode=request.params.get("mode", "unknown"),
                            )
                            if et == "chat.final":
                                durable_final_content = str(data.get("content", ""))
                                if not is_team_mode or data.get("role") != "teammate":
                                    terminal_final_persisted = True
                        if et == "chat.final":
                            next_final_content = str(data.get("content", ""))
                            if next_final_content:
                                final_answer_content = next_final_content
                                final_answer_chunks.clear()
                        yield AgentResponseChunk(
                            request_id=rid,
                            channel_id=cid,
                            payload=data,
                            is_complete=False,
                        )
        except asyncio.CancelledError:
            logger.info("[JiuWenSwarm] 流式处理被中断: request_id=%s", rid)
            stream_aborted = True
            raise
        except GeneratorExit:
            logger.info("[JiuWenSwarm] 流式连接已关闭: request_id=%s", rid)
            stream_aborted = True
            raise
        except Exception:
            stream_aborted = True
            abort_terminal_status = "error"
            raise
        finally:
            # Goal 还在跑时这条流不会收到收尾的 chat.final，气泡里已经展示的正文
            # 也就没有任何一处落盘。补一次，否则重新打开历史记录时这段回答凭空
            # 消失，和实时看到的不是一回事。非 Goal 流不进这里。
            if saw_goal_stream_output:
                _persist_pending_final_text()
            else:
                # 非 Goal 异常结束（LLM 错误/断连，或流结束无收尾事件）时，pending
                # reasoning 不会随 tool_call/final 落盘；补落一条，刷新后思考内容
                # 与耗时终点（末帧时刻）都能恢复。正文 final 故意不落——半截回答
                # 不该当 chat.final 写进历史。
                _persist_pending_reasoning()
            # The adapter producer owns RuntimeOutputStream.  Cancelling and
            # awaiting it releases the runtime output lease and aborts the
            # in-flight round when the outer WebSocket consumer disappears.
            unfinished_a2ui_tasks = [
                task for task in team_a2ui_tasks.values() if not task.done()
            ]
            for task in unfinished_a2ui_tasks:
                task.cancel()
            if not stream_task.done():
                stream_task.cancel()
            if stream_aborted:
                terminal_status = abort_terminal_status
                if terminal_status == "cancelled" and terminal_final_persisted:
                    terminal_status = "success"
                _schedule_feedback_once(terminal_status)
            if unfinished_a2ui_tasks:
                await asyncio.gather(*unfinished_a2ui_tasks, return_exceptions=True)
            try:
                await stream_task
            except asyncio.CancelledError:
                pass
            except Exception:
                # run_stream_task normally converts producer failures into a
                # queue item.  Do not let a cleanup failure mask cancellation.
                logger.debug(
                    "[JiuWenSwarm] stream producer cleanup failed: request_id=%s",
                    rid,
                    exc_info=True,
                )

        # A producer may cancel itself without the outer WebSocket consumer
        # being cancelled.  Keep that terminal state out of the bounded queue
        # (which may be full after a disconnect), but preserve the public
        # cancellation contract once all already-produced chunks are drained.
        if producer_cancellation is not None:
            _schedule_feedback_once(
                "success" if terminal_final_persisted else "cancelled"
            )
            raise producer_cancellation

        assistant_message = final_answer_content or "".join(final_answer_chunks)
        try:
            finalized_assistant_message = await finalize_assistant_response_if_a2ui(
                assistant_message,
                channel=cid,
                user_query=user_turn.text,
                request_id=rid or "",
                repair_call=repair_call,
                retry_without_a2ui_call=retry_without_a2ui_call,
            )
        except asyncio.CancelledError:
            _schedule_feedback_once(
                "success" if terminal_final_persisted else "cancelled"
            )
            raise
        except Exception:
            _schedule_feedback_once("error")
            raise
        if finalized_assistant_message and (
                finalized_assistant_message != assistant_message or suppress_a2ui_stream
        ):
            append_history_record(
                session_id=session_id,
                request_id=rid,
                channel_id=cid,
                role="assistant",
                event_type="chat.final",
                content=finalized_assistant_message,
                timestamp=time.time(),
                extra=_attach_reasoning_content({
                    k: v for k, v in request.params.items()
                    if k in ("source", "proactive_type", "proactive_target")
                }),
                mode=request.params.get("mode", "unknown"),
            )
            final_answer_content = finalized_assistant_message
            final_answer_chunks = []
            _schedule_feedback_once(completion_status)
            yield _make_a2ui_final_chunk(
                request_id=rid,
                channel_id=cid,
                session_id=session_id,
                content=finalized_assistant_message,
            )

        _schedule_feedback_once(completion_status)

        # cloud memory: after chat hook
        if memory_mode == "cloud":
            assistant_message = final_answer_content or "".join(final_answer_chunks)
            after_ctx = MemoryHookContext(
                session_id=request.session_id or "default",
                request_id=request.request_id or "",
                channel_id=request.channel_id,
                agent_name="main_agent",
                workspace_dir=str(get_agent_home_dir()),
                assistant_message=assistant_message,
                extra=request.params,
            )
            await ExtensionRegistry.get_instance().trigger(AgentServerHookEvents.MEMORY_AFTER_CHAT, after_ctx)

        # auto memory: extract memories after conversation ends
        # 需要 auto_memory_enabled 和 memory.enabled 都为 true 才触发
        mode = request.params.get("mode", "code") if isinstance(request.params, dict) else "code"
        config = get_config()
        if is_auto_memory_enabled(mode, config) and is_memory_enabled(mode, config):
            _trigger_auto_memory_extraction(adapter, request, session_id, is_stream=True)

        yield AgentResponseChunk(
            request_id=rid,
            channel_id=cid,
            payload={"is_complete": True},
            is_complete=True,
        )

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

    async def apply_package_change_to_session_adapters(
        self,
        operation: str,
        config_path: str,
    ) -> None:
        """Propagate a harness package load/unload to all live session adapters.
        """
        adapter = self._adapter
        if adapter is None:
            return
        method = getattr(adapter, "apply_package_change_to_session_adapters", None)
        if method is None:
            return
        await method(operation, config_path)

    async def compress_context(
            self,
            session_id: str,
            session: Any = None,
            *,
            return_state: bool = False,
    ) -> dict[str, Any]:
        """主动触发上下文压缩。

        Args:
            session_id: 会话ID
            session: Session 对象（可选）

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

    async def generate_recap(self, session_id: str) -> dict[str, Any]:
        """生成会话快速回顾（read-only，不修改对话历史）。

        取最近30条消息 → fast model → 1-2句摘要。

        Args:
            session_id: 会话ID

        Returns:
            包含 recap 结果的字典:
            - status: "ok" | "no_turn" | "aborted" | "failed"
            - summary: 摘要文本（仅当 status == "ok" 时）
            - error: 错误信息（仅当 status == "failed" 时）
        """
        adapter = self._adapter
        if adapter is None:
            raise ValueError("Agent adapter not available")
        return await adapter.generate_recap(session_id=session_id)

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

    async def cleanup_session_runtime(self, session_id: str) -> bool:
        """Release in-memory runtime owned by one session while keeping persisted history."""
        processor_cleaned = await self._session_manager.close_session(session_id)
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

    async def cancel_inflight_work(self, log_prefix: str = "[gateway disconnect] ") -> None:
        """Gateway 与 AgentServer 的 WebSocket 断开时调用：取消 session 流式任务并中止 adapter 内层循环。"""
        await self._session_manager.cancel_all_session_tasks(log_prefix)
        adapter = self._adapter
        if adapter is None:
            return
        abort_fn = getattr(adapter, "abort_on_gateway_disconnect", None)
        if not callable(abort_fn):
            return
        try:
            await abort_fn()
        except Exception:
            logger.exception("[JiuWenSwarm] adapter.abort_on_gateway_disconnect failed")

    async def cleanup(self) -> None:
        """清理资源，准备销毁实例.

        每次 initialize 重建 agent 时调用。
        不清理记忆数据（记忆数据保留在文件系统中）。
        """
        logger.info("[JiuWenSwarm] cleanup: 清理资源")
        await self._session_manager.close_all_sessions()

        if self._adapter is not None:
            try:
                if hasattr(self._adapter, "cleanup"):
                    await self._adapter.cleanup()
            except Exception as e:
                logger.warning("[JiuWenSwarm] Adapter cleanup failed: %s", e)
            self._adapter = None

        logger.info("[JiuWenSwarm] cleanup: 完成")

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
import inspect
import json
import logging
import re
import time
from pathlib import Path
from typing import Any, AsyncIterator, Tuple

from datetime import datetime, timedelta, timezone
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
)
from jiuwenswarm.server.runtime.session.session_manager import SessionManager
from jiuwenswarm.server.runtime.skill.skill_manager import SkillManager
from jiuwenswarm.server.utils.utils import is_team_params
from jiuwenswarm.common.config import get_config
from jiuwenswarm.agents.harness.code.prompt.plan_approval import (
    PLAN_EXECUTE_OPTION_VALUES,
    PLAN_REMINDER_ORIGINAL_QUERY_KEY,
    PLAN_SKIP_OPTION_VALUES,
    plan_skip_feedback,
)
from jiuwenswarm.common.mode_matrix import (
    is_web_composable_mode,
    read_request_work_mode,
)
from jiuwenswarm.extensions.registry import ExtensionRegistry
from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponse, AgentResponseChunk
from jiuwenswarm.common.chat_final import ensure_final_mode_inplace
from jiuwenswarm.extensions.hook_event import AgentServerHookEvents
from jiuwenswarm.extensions.hooks_context import MemoryHookContext
from jiuwenswarm.common.schema.message import EventType, ReqMethod
from jiuwenswarm.server.utils.stream_utils import is_retry_notice_payload
from jiuwenswarm.common.utils import (
    get_agent_home_dir,
    get_agent_workspace_dir,
    get_env_file,
    reset_free_search_runtime_flags,
)
from jiuwenswarm.server.runtime.a2ui.integration import finalize_assistant_response_if_a2ui
from jiuwenswarm.server.runtime.a2ui.runtime.finalizer import should_finalize_a2ui_content
from jiuwenswarm.agents.harness.common.auto_memory import (
    _execute_auto_memory_extraction,
)
from jiuwenswarm.agents.harness.common.rails.interrupt.interrupt_helpers import (
    EVOLUTION_INTERRUPT_METADATA_SOURCES,
    is_interrupt_resume_payload,
)


def _extract_error_code_message(exc: BaseException) -> tuple[str | None, str | None]:
    """从模型/上游异常中提取业务错误码和错误消息，优先原始 cause/body。"""

    def _pick(obj: Any, names: tuple[str, ...]) -> str | None:
        for name in names:
            value = getattr(obj, name, None)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    outer_code = _pick(exc, ("error_code", "code"))
    outer_message = _pick(exc, ("error_message", "message"))

    body_code: str | None = None
    body_message: str | None = None
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        inner = body.get("error")
        if not isinstance(inner, dict):
            inner = body
        body_code = _pick(inner, ("code", "error_code"))
        body_message = _pick(inner, ("message", "error_message"))

    cause = getattr(exc, "cause", None)
    if cause is None:
        cause = getattr(exc, "__cause__", None)
    cause_code: str | None = None
    cause_message: str | None = None
    if cause is not None and cause is not exc:
        cause_code, cause_message = _extract_error_code_message(cause)

    return (
        cause_code or body_code or outer_code,
        cause_message or body_message or outer_message,
    )


class _TeamPlanApprovalPayloadError(ValueError):
    """Raised when a structured team.plan approval payload is malformed."""


def _schedule_symphony_session_feedback(session_id: str, request_id: str) -> None:
    """Submit session-based Symphony learning without delaying the response."""

    try:
        from jiuwenswarm.symphony.evolution.session_consumer import (
            schedule_session_evolution_consume,
        )

        schedule_session_evolution_consume(session_id, request_id)
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
    """
    if not isinstance(params, dict):
        return query
    if params.get("is_supplement"):
        supplement_input = params.get("supplement_input")
        if isinstance(supplement_input, str) and supplement_input.strip():
            return supplement_input
    original_query = params.get(PLAN_REMINDER_ORIGINAL_QUERY_KEY)
    if isinstance(original_query, str):
        return original_query
    return query


def _should_record_user_history(params: Any) -> bool:
    if not isinstance(params, dict):
        return True
    if params.get("log_as_user") is False:
        return False
    # Second-step goal attach is host control traffic, not a user utterance.
    if params.get("attach_goal") is True:
        return False
    if is_interrupt_resume_payload(params):
        return False
    return str(params.get("source") or "") != "proactive_recommendation"


def _memory_hook_extra(request: AgentRequest) -> dict[str, Any]:
    """构造 Memory Hook 的请求参数，并补齐会话已锁定的项目绑定。

    常规续聊只携带 ``session_id``，不会重复传 ``project_id``。项目归属已经在
    AgentServer 收包时写入 session metadata；Memory Hook 若只透传
    ``request.params``，GaussPD 的 MemoryAdd 会退化到默认隔离桶。MCP Rail 已按
    session metadata 取值，此处使用同一真源，保持两条记忆链路一致。

    ``project_id`` 与 ``project_dir`` 都优先采用本轮请求值。后者是桌面工作区
    的稳定隔离候选值：旧桌面会话仍可能只有 ``project_id=default``，GaussPD
    胶水会在这种情况下以该目录作为动态 ``project_id``。
    """
    extra = dict(request.params) if isinstance(request.params, dict) else {}
    project_id = extra.get("project_id")
    project_dir = extra.get("project_dir")
    has_project_id = isinstance(project_id, str) and bool(project_id.strip())
    has_project_dir = isinstance(project_dir, str) and bool(project_dir.strip())
    if has_project_id and has_project_dir:
        return extra

    session_id = str(request.session_id or "").strip()
    if not session_id:
        return extra
    try:
        from jiuwenswarm.server.runtime.session.session_metadata import (
            get_session_metadata,
        )

        metadata = get_session_metadata(session_id)
    except Exception as exc:  # noqa: BLE001
        logging.getLogger(__name__).debug(
            "[JiuWenSwarm] Memory Hook project binding metadata lookup failed: %s",
            exc,
        )
        return extra

    if not isinstance(metadata, dict):
        return extra
    if not has_project_id:
        project_id = metadata.get("project_id")
        if isinstance(project_id, str) and project_id.strip():
            extra["project_id"] = project_id.strip()
    if not has_project_dir:
        project_dir = metadata.get("project_dir")
        if isinstance(project_dir, str) and project_dir.strip():
            extra["project_dir"] = project_dir.strip()
    return extra


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

    # Launch auto memory extraction task
    try:
        asyncio.create_task(
            _execute_auto_memory_extraction(
                session_id=session_id,
                project_dir=project_dir,
                messages=messages,
                parent_agent=adapter,  # Pass adapter for cache sharing
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
    ReqMethod.SKILLS_EVOLUTION_STATUS: "handle_skills_evolution_status",
    ReqMethod.SKILLS_EVOLUTION_GET: "handle_skills_evolution_get",
    ReqMethod.SKILLS_EVOLUTION_SAVE: "handle_skills_evolution_save",
}

_PLUGIN_ROUTES: dict[ReqMethod, str] = {
    ReqMethod.PLUGINS_LIST: "handle_plugins_list",
    ReqMethod.PLUGINS_INSTALL: "handle_plugins_install",
    ReqMethod.PLUGINS_UNINSTALL: "handle_plugins_uninstall",
    ReqMethod.PLUGINS_ENABLE: "handle_plugins_enable",
    ReqMethod.PLUGINS_DISABLE: "handle_plugins_disable",
    ReqMethod.PLUGINS_RELOAD: "handle_plugins_reload",
}

_SYMPHONY_METHODS: frozenset[ReqMethod] = frozenset(
    {
        ReqMethod.SYMPHONY_BUILD_SCORE,
        ReqMethod.SYMPHONY_PAUSE_BUILD,
        ReqMethod.SYMPHONY_SCORE_STATUS,
        ReqMethod.SYMPHONY_GRAPH,
        ReqMethod.SYMPHONY_PLAN,
    }
)

_SKILL_COMMAND_REGEX = re.compile(
    r"^/skills use\s+(?P<skill_names>[^,]+)\s*,\s*(?P<query>.*)$"
)

# /statusline prompt-type 模式：
# 用户输入 "/statusline <描述>" → 直接注入 statusline-setup 指令到 prompt
# 排除已知子命令（set, padding, clear, help, json）——这些由 TUI 前端本地处理，
# 但如果消息经过 Gateway 传到 AgentServer，后端也需要区分。
_STATUSLINE_KNOWN_SUBCOMMANDS = {"set", "padding", "clear", "help", "json", "get"}
_STATUSLINE_PROMPT_REGEX = re.compile(
    r"^/statusline\s+(?P<description>.+)$"
)

# 不调用 /skills，直接把指令文本嵌入 prompt
_STATUSLINE_SETUP_PROMPT = """\
You are a status line setup agent. Your job is to configure the user's TUI status line \
by generating a shell command and writing it to the config file so the bottom bar \
updates immediately.

This is NOT about writing Python scripts or creating files — it's about writing a \
**shell command** that runs every 2 seconds and whose stdout becomes the status bar text.

## How the Status Line Works

1. The TUI runs the configured shell command every 2 seconds
2. Each time, it pipes a JSON object with session info as stdin to the command
3. The command's stdout is displayed at the bottom of the TUI screen
4. Config is stored in ~/.jiuwenswarm-tui/config.json under the "statusLine" field

The shell command can do anything a normal shell command can — read JSON fields, \
run git, check files, call system utilities, etc. The JSON input is just one \
convenient data source, not a constraint.

## Three Command Styles

**Style A: Pure JSON fields** — for session info (model, tokens, mode, etc.)
```
input=$(cat); field1=$(echo "$input" | jq -r '.field1 // "default"'); \
echo "label:$field1"
```

**Style B: Pure shell utilities** — for system info (git branch, disk, \
time, etc.) — no `input=$(cat)` needed
```
branch=$(git branch --show-current 2>/dev/null || echo "?"); \
time=$(date +%H:%M:%S); echo "$branch | $time"
```

**Style C: Mixed** — JSON fields + shell utilities (most common)
```
input=$(cat); model=$(echo "$input" | jq -r '.model // "?"'); \
branch=$(git branch --show-current 2>/dev/null || echo "?"); \
echo "$model | git:$branch"
```

## JSON Input Field Reference

The command receives this JSON via stdin every 2 seconds:

| Field | Description |
|-------|-------------|
| session_id | Current session ID |
| session_name | Session title (set via /rename) |
| cwd | Current working directory |
| mode | Current mode (agent / code.normal / code.team / team) |
| model | Current model name |
| provider | Model provider |
| version | jiuwenswarm version |
| connection | Connection state (idle / connecting / connected / reconnecting / auth_failed) |
| is_processing | Is agent currently processing |
| last_error | Most recent error message or null |
| evolution_status | Evolution state (idle / running) |
| active_subtask_count | Number of active subtasks |
| todo_count | Number of todo items |
| trusted_dirs | Trusted directory paths (array) |
| usage.total_input_tokens | Session total input tokens |
| usage.total_output_tokens | Session total output tokens |
| usage.total_tokens | Session total tokens |
| context_window.context_window_size | Max context window tokens |
| context_window.used_percentage | Context used percentage (0-100) |
| context_window.remaining_percentage | Context remaining percentage (0-100) |

Common non-JSON shell approaches: git branch --show-current, \
df -h, date, hostname -s, whoami, etc.

## How to Apply the Config

DO NOT use `python -c "..."` one-liners — they break on Windows due \
to quoting and escaping issues. Instead, write a Python script file \
and then execute it. This is the ONLY reliable way on Windows.

Step 1: Write a Python script file (e.g. /tmp/update_statusline.py) \
that merges the new statusLine into the config:
```python
import json, os
d = os.path.expanduser('~/.jiuwenswarm-tui')
os.makedirs(d, exist_ok=True)
p = os.path.join(d, 'config.json')
if not os.path.exists(p):
    with open(p, 'w') as f:
        f.write('{}\\n')
with open(p) as f:
    c = json.load(f)
c['statusLine'] = {
    'type': 'command',
    'command': 'YOUR_COMMAND_HERE',
    'padding': 0
}
with open(p, 'w') as f:
    json.dump(c, f, indent=2)
    f.write('\\n')
print('StatusLine configured')
```

Step 2: Execute the script:
```bash
python /tmp/update_statusline.py
```

IMPORTANT: The TUI polls config.json every 2 seconds, so the status \
bar updates automatically within 2 seconds after you write the config. \
No restart needed.

Guidelines:
- Only write to ~/.jiuwenswarm-tui/config.json — never overwrite \
  system files
- Always merge with existing config — preserve trustedDirs, theme, etc.
- Never hardcode secrets or API keys in the command
- The statusLine command runs in bash (sh -c) context, NOT in \
  PowerShell — so `$(cat)`, `$var`, `jq`, `echo` etc. are all \
  standard bash/sh syntax
- Commands should handle failures gracefully: use 2>/dev/null, \
  || echo "fallback"
- On Windows, $(cat) is automatically patched to read from a temp \
  file by the TUI
- DO NOT use `python -c` one-liners for config updates — they \
  break on Windows. Always write a .py script file and execute it.
- DO NOT read config.json with `cat` — use Python os.path.expanduser \
  instead, as `~` may not resolve correctly in some shell environments
"""


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

    不调用 /skills 命令，不依赖 SkillUseRail，
    直接把 statusline-setup 指令文本嵌入 user prompt。

    _handle_statusline_prompt_command() → 返回 (statusline_prompt, description)
    build_user_prompt() 把 statusline_prompt 嵌入到 user prompt 后面

    Args:
        query: 用户原始输入（含 "/statusline" 前缀）

    Returns:
        (statusline_prompt, description) — 注入的 prompt 文本和提取的描述
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
            return _STATUSLINE_SETUP_PROMPT, description

    # /statusline 无参数 → 不是 prompt 模式（TUI 应已拦截处理 help）
    return "", query


def build_user_prompt(content: str | dict, files: dict, channel: str, language: str, *,
    trusted_dirs: list[str] | None = None, metadata: dict[str, Any] | None = None,
    skills: list[str] | None = None) -> str:
    """Build user prompt for the agent.

    Args:
        skills: 显式传入的 skill 名列表（来自 params.skills，前端从 content 提取）。
            若提供，直接作为 skills_to_use，且 **不再对 content 做 /skills use 剥离**
            （content 原样保留，如 "帮我用 /doc写文档"）。
            若为 None，回退到从 content 文本解析 /skills use（兼容 IM/CLI 老路径），
            同样不剥离 content，仅提取 skill 名。
    """
    from jiuwenswarm.server.runtime.a2ui.integration import build_user_prompt_if_a2ui_event

    a2ui_prompt = build_user_prompt_if_a2ui_event(content, channel=channel, language=language)
    if a2ui_prompt is not None:
        return a2ui_prompt

    interaction_prefix = ""
    if metadata:
        interaction_ctx = str(metadata.get("interaction_context") or "").strip()
        if interaction_ctx:
            interaction_prefix = f"\n{interaction_ctx}\n\n"

    # skills 来源：优先显式参数（params.skills），否则回退从 content 文本解析（兼容老路径）。
    # 两条路径都 **不剥离 content**——skill 名单独进 skills_to_use，content 原样保留语义通顺。
    skills_to_use: list[str]
    if skills:
        skills_to_use = skills
    elif isinstance(content, str):
        parsed_skills, _stripped = _handle_skills_use_slash_command(content)
        skills_to_use = parsed_skills  # 仅取 skill 名，忽略 _stripped（content 不剥离）
    else:
        skills_to_use = []

    if isinstance(content, str):
        # /statusline <prompt> prompt-type 命令（仿 Claude Code，不调用 /skills）
        statusline_prompt, statusline_content = _handle_statusline_prompt_command(content)
        if statusline_prompt:
            content = statusline_content
    else:
        statusline_prompt = ""

    if language == "zh":
        prompt = "你收到一条消息：\n"
        if channel == "cron":
            prompt = "你收到一条消息，对于查询类任务必须输出查询到的内容，不要只回复确认，不要记录到memory：\n"
    else:
        prompt = "You receive a new message:\n"
        if channel == "cron":
            prompt = ("You receive a new message. For query tasks, you must output the queried content"
                      "—don't just reply with confirmation, don't record to memory:\n")
    msg_data: dict[str, Any] = {
        "source": channel,
        "preferred_response_language": language,
        "content": content,
        "type": "user input",
    }
    if channel in ["cron", "heartbeat"]:
        msg_data["source"] = "system"
        msg_data["type"] = channel
    if metadata:
        chat_type = str(metadata.get("chat_type") or metadata.get("im_chat_type") or "").strip()
        if chat_type:
            msg_data["chat_type"] = chat_type
        sender_name = str(metadata.get("sender_name") or "").strip()
        if sender_name:
            msg_data["sender"] = sender_name
    if channel not in ["cron", "heartbeat"]:
        msg_data["files_updated_by_user"] = json.dumps(files, ensure_ascii=False)
    final_prompt = interaction_prefix + prompt + json.dumps(msg_data, ensure_ascii=False)
    if interaction_prefix:
        logger.info(
            "[build_user_prompt][DEBUG] interaction_context 存在，最终 prompt=\n%s",
            final_prompt,
        )

    now = datetime.now(timezone(timedelta(hours=8)))
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")

    user_message_context = {
        "source": channel,
        "timezone": "Asia/Shanghai",
        "timestamp": now_str,
        "preferred_response_language": language,
        "content": content,
        "files_updated_by_user": json.dumps(files, ensure_ascii=False),
        "type": "user input",
    }
    if skills_to_use:
        user_message_context["skills_to_use"] = skills_to_use
    if trusted_dirs:
        user_message_context["trusted_dirs"] = json.dumps(trusted_dirs, ensure_ascii=False)

    # 仿 Claude Code statusline-setup: 把指令文本直接嵌入 prompt
    base_prompt = interaction_prefix + prompt + json.dumps(user_message_context, ensure_ascii=False)
    if statusline_prompt:
        if language == "zh":
            return base_prompt + "\n\n你必须按照以下指令配置状态栏：\n" + statusline_prompt
        else:
            return (
                base_prompt
                + "\n\nYou must follow these instructions "
                + "to configure the status line:\n"
                + statusline_prompt
            )
    return base_prompt



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
        self._adapter: AgentAdapter | None = None
        self._sdk_name: str | None = None
        self._skill_manager = SkillManager(workspace_dir=str(get_agent_workspace_dir()))
        self._session_manager = SessionManager()
        # SkillDev 模式：懒初始化，首次 skilldev.* 请求时构造
        self._skilldev_service = None

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
            self._skill_manager.set_skillnet_install_complete_hook(
                self._on_skillnet_install_complete
            )
            logger.info("[JiuWenSwarm] Initialized adapter: sdk=%s, mode=%s", self._sdk_name, mode)
        return self._adapter

    @staticmethod
    def _adapter_mode_for_request(request: AgentRequest) -> str:
        """选择 Deep / Code adapter。

        Web 请求（显式携带 ``work_mode``）由 ``work_mode`` 决定 profile：
        ``code`` / ``design`` 走 CodeAdapter（design 派生自 code，复用其 rails/tools
        装配与 CodeAdapter 实例化路径，仅 system prompt 按 design 切换），
        ``work`` 走 DeepAdapter。TUI 等历史客户端不带 ``work_mode``，继续按
        完整 mode 串判定，行为不变。
        """
        params = request.params if isinstance(request.params, dict) else {}
        work_mode = read_request_work_mode(params)
        raw_mode = params.get("mode", "")
        mode = raw_mode.strip().lower() if isinstance(raw_mode, str) else ""
        if work_mode is not None and is_web_composable_mode(mode or "agent"):
            if work_mode == "code":
                return "code"
            if work_mode == "design":
                return "design"
            return "agent"
        if mode == "team.plan":
            return "code"
        if mode == "code" or mode.startswith("code."):
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
        """Reload the agent and refresh active team shared skill links after async install."""
        await self.create_instance()
        self._refresh_team_shared_skill_links()

    @staticmethod
    def _refresh_team_shared_skill_links(session_id: str | None = None) -> None:
        """Refresh team shared skill links after the global skill root changes."""
        try:
            from jiuwenswarm.agents.harness.team import refresh_team_shared_skill_links_across_managers

            refresh_team_shared_skill_links_across_managers(session_id)
        except Exception as exc:
            logger.warning("[JiuWenSwarm] team shared skill link refresh failed: %s", exc)

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

    def build_inputs(self, request: AgentRequest) -> Tuple[dict[str, Any], str, str]:
        """构建 adapter 所需的 inputs 字典（公共接口）."""
        return self._build_inputs(request)

    def _build_inputs(self, request: AgentRequest) -> Tuple[dict[str, Any], str, str]:
        """构建 adapter 所需的 inputs 字典."""
        from openjiuwen.core.session.interaction.interactive_input import InteractiveInput
        from jiuwenswarm.common.schema.chat_send import ChatSendParams

        config_base = get_config()
        memory_mode = get_memory_mode(config_base)
        params: ChatSendParams = request.params if isinstance(request.params, dict) else {}
        query = params.get("query")
        if query is None or query == "":
            query = params.get("content", "")
        # /debug 请求级指令：仅 agent/code 在此剥离前缀；team 自行从原始
        # query 解析 /debug（process_message_stream 用 raw_query 覆写
        # inputs["query"]），故此处对 team 不剥离。
        _request_debug = False
        _dbg_mode = params.get("mode")
        _dbg_mode_s = _dbg_mode.strip().lower() if isinstance(_dbg_mode, str) else ""
        if not (params.get("team") or _dbg_mode_s in {"team", "team.plan", "code.team"}):
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

        if isinstance(query, InteractiveInput):
            final_query = query
        else:
            answers = params.get("answers", [])
            if answers:
                request_id = params.get("request_id", "")
                source = params.get("source", "")
                raw_original_request = params.get("original_request") if source == "ask_user_interrupt" else ""
                original_request = raw_original_request.strip() if isinstance(raw_original_request, str) else ""
                interactive_input = self._build_interactive_input_from_answers(
                    request_id,
                    answers,
                    source,
                    original_request=original_request,
                )
                if interactive_input is not None:
                    final_query = interactive_input
                else:
                    final_query = build_user_prompt(
                        query,
                        files=params.get("files", {}),
                        channel=channel,
                        language=language,
                        trusted_dirs=trusted_dirs,
                        metadata=request.metadata,
                        skills=skills,
                    )
            else:
                final_query = build_user_prompt(
                    query,
                    files=params.get("files", {}),
                    channel=channel,
                    language=language,
                    trusted_dirs=trusted_dirs,
                    metadata=request.metadata,
                    skills=skills,
                )
                # 调试日志：确认 /statusline prompt 注入是否生效
                if isinstance(query, str) and "/statusline" in query:
                    logger.info(
                        "[_build_inputs][STATUSLINE] 原始 query=%s, 最终 prompt 长度=%d, "
                        "包含 statusline-setup 指令=%s",
                        query[:200],
                        len(final_query) if isinstance(final_query, str) else 0,
                        "status line setup agent" in final_query if isinstance(final_query, str) else False,
                    )

        inputs: dict[str, Any] = {
            "conversation_id": request.session_id,
            "query": final_query,
            "channel": channel,
            "language": language,
            # Xiaoyi's mobile client cannot render/answer the ask_user card;
            # keep interaction disabled there even when the legacy client omits
            # the capability flag. Other channels remain backward compatible.
            "supports_user_interaction": (
                channel.strip().lower() != "xiaoyi"
                and params.get("supports_user_interaction") is not False
            ),
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

        # 返回原始 query（未经 build_user_prompt 包装）
        # Team 模式需要使用原始 query，而不是 JSON 包装后的 prompt
        return inputs, memory_mode, query

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
            str(params.get("mode") or "").strip().lower() == "team.plan"
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
            *,
            original_request: str = "",
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
            free_text_answer = ""
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
            payload: dict[str, Any] = {"answers": answers_dict}
            if isinstance(original_request, str) and original_request.strip():
                payload["original_request"] = original_request.strip()
            interactive_input.update(request_id, payload)
            logger.info(
                "[JiuWenSwarm] AskUserRail InteractiveInput.update: request_id=%s "
                "answer_count=%s has_original_request=%s",
                request_id,
                len(answers_dict),
                "original_request" in payload,
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

        if source and source not in {
            "permission_interrupt",
            "confirm_interrupt",
        }:
            return None

        answer = answers[0] if answers else {}
        selected_options = answer.get("selected_options", []) if isinstance(answer, dict) else []
        custom_input = answer.get("custom_input", "") if isinstance(answer, dict) else ""

        value = selected_options[0] if selected_options else ""

        if value in ("approve", "本次允许", "Approve", "Proceed", "批准", "开始执行"):
            confirm_payload = {"approved": True, "auto_confirm": False, "feedback": ""}
        elif value in ("session_allow", "会话内记住", "Session Allow"):
            confirm_payload = {
                "approved": True,
                "auto_confirm": True,
                "persist_allow": False,
                "feedback": "",
            }
        elif value in (
            "always_allow",
            "allow_always",
            "永久记住",
            "总是允许",
            "Always Allow",
        ):
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
        elif value in ("reject", "拒绝", "Reject", "继续规划", "其他意见"):
            feedback = custom_input or (
                "用户希望继续规划" if value in ("Keep planning", "继续规划", "其他意见") else "用户拒绝"
            )
            confirm_payload = {"approved": False, "auto_confirm": False, "feedback": feedback}
        elif custom_input:
            confirm_payload = {"approved": False, "auto_confirm": False, "feedback": custom_input}
        else:
            confirm_payload = {"approved": False, "auto_confirm": False, "feedback": f"未知选项: {value}"}

        interactive_input.update(request_id, confirm_payload)
        logger.info(
            "[JiuWenSwarm] PermissionRail InteractiveInput.update: request_id=%s payload=%s",
            request_id, confirm_payload
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
                self._refresh_team_shared_skill_links(request.session_id)
                if handler_name == "handle_skills_toggle":
                    # 启停即时生效：root 重建不级联 session adapter，存量会话的
                    # SkillUseRail 仍持旧 disabled_skills（每轮构建提示词时过滤），
                    # 这里把最新状态扇出到全部存活 session adapter。
                    fan_out = getattr(self._adapter, "refresh_skill_rails_for_all_sessions", None)
                    if callable(fan_out):
                        try:
                            await fan_out()
                        except Exception as exc:
                            logger.warning("[JiuWenSwarm] skills.toggle 扇出刷新失败: %s", exc)
            elif handler_name == "handle_skills_uninstall" and payload.get("success"):
                # 卸载只需轻量刷新 skill rail，不需要全量重建 agent 实例。
                # SkillUseRail 会通过文件系统签名检测到目录删除并自动刷新，
                # 这里主动调用 reload_skills() 确保立即生效，避免延迟到下一次模型调用。
                await self._refresh_skill_rails_after_change()
                self._refresh_team_shared_skill_links(request.session_id)
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

    async def _handle_symphony_request(self, request: AgentRequest) -> AgentResponse | None:
        """处理 Symphony extension RPC 请求."""
        if request.req_method not in _SYMPHONY_METHODS:
            return None

        method = request.req_method.value
        try:
            handler = ExtensionRegistry.get_instance().get_rpc_handler(method)
            if handler is None:
                payload = {
                    "success": False,
                    "detail": f"Symphony extension RPC unavailable: {method}: handler not registered",
                }
            else:
                result = handler(request.params or {}, request=request)
                payload = await result if inspect.isawaitable(result) else result
                if not isinstance(payload, dict):
                    payload = {"success": True, "result": payload}
        except Exception as exc:  # noqa: BLE001
            logger.exception("[JiuWenSwarm] Symphony RPC failed: %s", method)
            return AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"success": False, "detail": f"{method}: {exc}"},
                metadata=request.metadata,
            )

        return AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=True,
            payload=payload,
            metadata=request.metadata,
        )

    async def _handle_symphony_request_stream(
        self,
        request: AgentRequest,
    ) -> AsyncIterator[AgentResponseChunk]:
        """Stream Symphony RPC progress events, then the final RPC payload."""
        if request.req_method not in _SYMPHONY_METHODS:
            return

        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

        async def progress_callback(event: dict[str, Any]) -> None:
            await queue.put(event)

        metadata = dict(request.metadata or {})
        metadata["symphony_progress_callback"] = progress_callback
        stream_request = replace(request, metadata=metadata)
        task = asyncio.create_task(self._handle_symphony_request(stream_request))

        try:
            while not task.done() or not queue.empty():
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=0.1)
                except asyncio.TimeoutError:
                    continue
                yield AgentResponseChunk(
                    request_id=request.request_id,
                    channel_id=request.channel_id,
                    payload={
                        "event_type": event.get("type")
                        or "symphony.beam_search.update",
                        "beam_search_event": event,
                    },
                    is_complete=False,
                )
            response = await task
        except Exception as exc:
            logger.exception("[JiuWenSwarm] Symphony stream failed: %s", exc)
            yield AgentResponseChunk(
                request_id=request.request_id,
                channel_id=request.channel_id,
                payload={"event_type": "chat.error", "error": str(exc)},
                is_complete=False,
            )
            yield AgentResponseChunk(
                request_id=request.request_id,
                channel_id=request.channel_id,
                payload=None,
                is_complete=True,
            )
            return

        payload = dict(response.payload or {}) if response is not None else {}
        payload.setdefault(
            "event_type",
            f"{request.req_method.value if request.req_method else 'symphony'}.result",
        )
        yield AgentResponseChunk(
            request_id=request.request_id,
            channel_id=request.channel_id,
            payload=payload,
            is_complete=False,
        )
        yield AgentResponseChunk(
            request_id=request.request_id,
            channel_id=request.channel_id,
            payload=None,
            is_complete=True,
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
                    if ok and str(action or "").strip().lower() == "set":
                        objective = str(params.get("objective") or "").strip()
                        if objective:
                            goal_obj = goal_result.get("goal")
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

        # 无状态请求（skills / skilldev / plugins / symphony）不需要 adapter，
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

        symphony_response = await self._handle_symphony_request(request)
        if symphony_response is not None:
            return symphony_response

        adapter = self._ensure_adapter(mode=self._adapter_mode_for_request(request))

        heartbeat_response = await adapter.handle_heartbeat(request)
        if heartbeat_response is not None:
            return heartbeat_response

        session_id = self._session_manager.get_session_id(request.session_id)
        query = request.params.get("query", "")
        # proactive_recommendation 是系统触发的推荐指令（不是用户说的话），不写 user
        # history——否则刷新页面会显示"[主动推荐指令] xxx"这种用户没说过的消息。
        if _should_record_user_history(request.params):
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
            inputs, memory_mode, raw_query = self._build_inputs(request)
        except _TeamPlanApprovalPayloadError as exc:
            return AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(exc)},
                metadata=request.metadata,
            )

        # cloud memory: before chat hook
        if memory_mode == "cloud":
            mem_ctx = MemoryHookContext(
                session_id=request.session_id or "default",
                request_id=request.request_id or "",
                channel_id=request.channel_id,
                agent_name="main_agent",
                workspace_dir=str(get_agent_home_dir()),
                extra=_memory_hook_extra(request),
            )
            await ExtensionRegistry.get_instance().trigger(AgentServerHookEvents.MEMORY_BEFORE_CHAT, mem_ctx)
            memory_block = "\n\n".join(b for b in mem_ctx.memory_blocks if b)
            inputs["memory_block"] = memory_block

        async def run_agent_task():
            return await adapter.process_message_impl(request, inputs)

        result = await self._session_manager.submit_and_wait(session_id, run_agent_task)

        if result.ok and result.payload.get("content"):
            content = result.payload["content"]
            content_str = content if isinstance(content, str) else str(content)
            repair_call = getattr(adapter, "repair_model_response", None)
            retry_without_a2ui_call = self._make_retry_without_a2ui_call(
                adapter=adapter,
                request=request,
            )
            content_str = await finalize_assistant_response_if_a2ui(
                content_str,
                channel=request.channel_id,
                user_query=raw_query,
                request_id=request.request_id or "",
                repair_call=repair_call,
                retry_without_a2ui_call=retry_without_a2ui_call,
            )
            if isinstance(content, str):
                result.payload["content"] = content_str
            from jiuwenswarm.server.runtime.expert.expert_service import (
                history_expert_identity_extra,
            )

            append_history_record(
                session_id=session_id,
                request_id=request.request_id,
                channel_id=request.channel_id,
                role="assistant",
                event_type="chat.final",
                content=content_str,
                timestamp=time.time(),
                # 按消息记录"当时是谁答的"（专家身份快照；卸载/换绑后历史身份不变）
                extra=history_expert_identity_extra(session_id),
                mode=request.params.get("mode", "unknown"),
            )

            # cloud memory: after chat hook
            if memory_mode == "cloud":
                after_ctx = MemoryHookContext(
                    session_id=request.session_id or "default",
                    request_id=request.request_id or "",
                    channel_id=request.channel_id,
                    agent_name="main_agent",
                    workspace_dir=str(get_agent_home_dir()),
                    assistant_message=content_str,
                    extra=_memory_hook_extra(request),
                )
                await ExtensionRegistry.get_instance().trigger(AgentServerHookEvents.MEMORY_AFTER_CHAT, after_ctx)

            # auto memory: extract memories after conversation ends
            # 需要 auto_memory_enabled 和 memory.enabled 都为 true 才触发
            mode = request.params.get("mode", "code") if isinstance(request.params, dict) else "code"
            config = get_config()
            if is_auto_memory_enabled(mode, config) and is_memory_enabled(mode, config):
                _trigger_auto_memory_extraction(adapter, request, session_id, is_stream=False)

        _schedule_symphony_session_feedback(session_id, request.request_id)
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

        if request.req_method in _SYMPHONY_METHODS:
            async for chunk in self._handle_symphony_request_stream(request):
                yield chunk
            return

        # 无状态 RPC（skills / plugins / symphony）不需要 adapter，
        # 委托给非流式 handler 并包装为单个 chunk，避免触发 adapter 懒初始化。
        # skilldev 已由上面的流式分支处理，这里不会再命中。
        for stateless_handler in (
            self._handle_skills_request,
            self._handle_plugins_request,
            self._handle_symphony_request,
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

        session_id = self._session_manager.get_session_id(request.session_id)
        query = request.params.get("query", "")

        mode = request.params.get("mode", "") if isinstance(request.params, dict) else ""
        team_flag = request.params.get("team", False) if isinstance(request.params, dict) else False
        is_team_mode = team_flag or (
            isinstance(mode, str) and mode.strip().lower() in {"team", "team.plan", "code.team"}
        )
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
            and _should_record_user_history(params_for_history)
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
        try:
            inputs, memory_mode, raw_query = self._build_inputs(request)
        except _TeamPlanApprovalPayloadError as exc:
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

        # Team 模式：使用原始 query，而不是 build_user_prompt 包装后的内容
        team_query_is_interactive_input = False
        if is_team_mode:
            from openjiuwen.core.session.interaction.interactive_input import InteractiveInput

            team_query_is_interactive_input = isinstance(inputs.get("query"), InteractiveInput)
            if not team_query_is_interactive_input:
                inputs["query"] = raw_query
            logger.info(
                "[JiuWenSwarm] Team模式使用原始query: %s",
                raw_query[:100] if isinstance(raw_query, str) and raw_query else type(inputs.get("query")).__name__,
            )

        # cloud memory: before chat hook
        if memory_mode == "cloud":
            mem_ctx = MemoryHookContext(
                session_id=request.session_id or "default",
                request_id=request.request_id or "",
                channel_id=request.channel_id,
                agent_name="main_agent",
                workspace_dir=str(get_agent_home_dir()),
                extra=_memory_hook_extra(request),
            )
            await ExtensionRegistry.get_instance().trigger(AgentServerHookEvents.MEMORY_BEFORE_CHAT, mem_ctx)
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
                is_team_first_request = not await _team_session_has_runtime(
                    team_manager, session_id
                )
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
        durable_pending_reasoning_chunks: list[str] = []
        durable_final_content = ""
        # usage 并入终稿（共享出口层）：内容非空的主应答/leader
        # chat.final 截留到流尾，并入用量字段后统一落盘+下发——agent/team/code
        # 全模式同路径；usage_summary 帧载荷暂存（取累计 usage/model/占用率/上限）
        pending_final_chunk: AgentResponseChunk | None = None
        round_usage_summary_payload: dict[str, Any] | None = None
        # 直播 context.usage 推送帧暂存（与环面直播同帧同值）：占用率/上限/占用实值
        round_context_usage_payload: dict[str, Any] | None = None

        def _consume_durable_reasoning_content() -> str:
            nonlocal durable_pending_reasoning_chunks
            reasoning_text = "".join(durable_pending_reasoning_chunks)
            durable_pending_reasoning_chunks = []
            return reasoning_text if reasoning_text.strip() else ""

        def _attach_reasoning_content(extra_fields: dict[str, Any] | None = None) -> dict[str, Any] | None:
            reasoning_text = _consume_durable_reasoning_content()
            if not reasoning_text:
                return extra_fields
            merged = dict(extra_fields) if isinstance(extra_fields, dict) else {}
            merged["reasoning_content"] = reasoning_text
            return merged

        def _persist_pending_final_text(aborted: bool = False) -> None:
            nonlocal durable_pending_final_chunks, durable_final_content
            pending_text = "".join(durable_pending_final_chunks)
            durable_pending_final_chunks = []
            if not pending_text or pending_text == durable_final_content:
                return
            from jiuwenswarm.server.runtime.expert.expert_service import (
                history_expert_identity_extra,
            )

            append_history_record(
                session_id=session_id,
                request_id=rid,
                channel_id=cid,
                role="assistant",
                event_type="chat.final",
                content=pending_text,
                timestamp=time.time(),
                # 透传 proactive 标记到 history——刷新页面时前端靠 payload.source===
                # 'proactive_recommendation' 渲染推荐卡片，不带则退化白色气泡。
                # 专家身份快照：按写盘时刻会话绑定记录"当时是谁答的"。
                # aborted：中断 flush（CancelledError 路径）写"已停止"语义标记——
                # 重启后前端据它显示「已停止」而非回落「已完成」。
                extra=_attach_reasoning_content({
                    **{
                        k: v for k, v in request.params.items()
                        if k in ("source", "proactive_type", "proactive_target")
                    },
                    **history_expert_identity_extra(session_id),
                    **({"aborted": True} if aborted else {}),
                }),
                mode=request.params.get("mode", "unknown"),
            )
            durable_final_content = pending_text

        async def _prepare_rewind_for_error():
            try:
                _get_sa = getattr(adapter, "_get_or_create_session_adapter", None)
                _sa = await _get_sa(session_id) if callable(_get_sa) else adapter
                _prep = getattr(_sa, "_prepare_rewind_session_for_next_invoke", None)
                if callable(_prep):
                    await _prep(session_id, reason="stream_error")
            except Exception as _rewind_err:
                logger.warning("[JiuWenSwarm] chat.error 后预置 rewind session 失败: %s", _rewind_err)

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
                    _get_sa = getattr(adapter, "_get_or_create_session_adapter", None)
                    _sa = await _get_sa(session_id) if callable(_get_sa) else adapter
                    _prep_rewind = getattr(_sa, "_prepare_rewind_session_for_next_invoke", None)
                    if callable(_prep_rewind):
                        await _prep_rewind(session_id, reason="stream_error")
                except Exception as _prep_err:
                    logger.warning("[JiuWenSwarm] 错误后预置 rewind session 失败: %s", _prep_err)
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
        _yielded_from_queue = 0
        logger.info(
            "[JiuWenSwarm] consumer loop starting: request_id=%s is_team=%s is_first=%s",
            rid, is_team_mode, is_team_first_request,
        )

        def _persist_chat_event(et: str, payload: dict[str, Any]) -> None:
            """消费循环 chat.* 落盘（抽取共享：循环内即时落盘 + 流尾截留终稿补落）。

            extra 拍平、reasoning 附挂、专家身份快照、proactive 透传与原内联块同规。
            """
            payload_dict = dict(payload)
            # role/error/rid 等事件路由键不能进 extra：append_history_record 的
            # item.update(extra) 会把记录 role 覆盖成 payload 的 role（team_helpers
            # 给 leader 帧打 role="leader" → 落盘记录 role 被污染成 leader），
            # 且与回答记录共用 <rid>:assistant id 混淆对账
            extra_fields = {k: v for k, v in payload_dict.items() if
                            k not in ("event_type", "content", "role")}
            if et == EventType.TEAM_MESSAGE.value and "event" in payload_dict:
                event_data = payload_dict.get("event", {})
                if isinstance(event_data, dict):
                    for k, v in event_data.items():
                        if k not in ("type", "timestamp", "content"):
                            extra_fields[k] = v
            if et in {"chat.final", "chat.tool_call", "chat.error"}:
                extra_fields = _attach_reasoning_content(extra_fields)
            # 主应答落盘携带专家身份（无绑定显式写空串=默认角色作答标记）
            if et in ("chat.final", "chat.error") and "expert_id" not in extra_fields:
                from jiuwenswarm.server.runtime.expert.expert_service import (
                    history_expert_identity_extra,
                )
                extra_fields.update(history_expert_identity_extra(session_id))
            # 透传 proactive 标记——刷新页面时前端靠 source 识别卡片
            for pk in ("source", "proactive_type", "proactive_target"):
                if pk not in extra_fields and pk in request.params:
                    extra_fields[pk] = request.params[pk]
            append_history_record(
                session_id=session_id,
                request_id=rid,
                channel_id=cid,
                role="assistant",
                event_type=et,
                content=payload.get("content") or payload.get("error") or "",
                timestamp=time.time(),
                extra=extra_fields if extra_fields else None,
                mode=request.params.get("mode", "unknown"),
            )

        async def _build_round_usage_fields() -> dict[str, Any]:
            """回合用量字段（并入终稿 chat.final）。

            三来源按序补缺：直播 context.usage 推送帧（占用率/上限/占用实值——与环面
            直播同帧同值，回放=直播）→ usage_summary 帧载荷（累计 usage/model）
            → adapter.get_context_usage 现算（三类分项）。任一缺失降级不阻塞。
            """
            fields: dict[str, Any] = {}
            # 优先级 1：直播 context.usage 推送帧（与环面直播同帧同值，回放=直播）
            push = round_context_usage_payload
            if isinstance(push, dict):
                rate = push.get("rate")
                if isinstance(rate, (int, float)):
                    fields["usage_percent"] = float(rate)
                cw = push.get("context_max")
                if isinstance(cw, (int, float)) and cw > 0:
                    fields["context_window_tokens"] = int(cw)
                used = push.get("tokens_used")
                if isinstance(used, (int, float)) and used > 0:
                    fields["context_tokens_used"] = int(used)
                # 三类分项（rail 在 after_model_call 按真实发送窗口现算）——
                # 团队工具集回合末即 teardown，只有推送帧带的是真实值
                for key in ("system_prompt_tokens", "tools_tokens", "messages_tokens"):
                    value = push.get(key)
                    if isinstance(value, (int, float)) and value > 0:
                        fields[key] = int(value)
            # 优先级 2：usage_summary 帧载荷（累计 usage/model；占用率/上限补缺）
            payload = round_usage_summary_payload
            if isinstance(payload, dict):
                usage = payload.get("usage")
                if isinstance(usage, dict) and usage.get("total_tokens"):
                    fields["usage"] = usage
                model = payload.get("model")
                if isinstance(model, str) and model:
                    fields["model"] = model
                pct = payload.get("usage_percent")
                if isinstance(pct, (int, float)) and "usage_percent" not in fields:
                    fields["usage_percent"] = float(pct)
                cw = payload.get("context_window_tokens")
                if isinstance(cw, (int, float)) and cw > 0 and "context_window_tokens" not in fields:
                    fields["context_window_tokens"] = int(cw)
            try:
                get_usage = getattr(adapter, "get_context_usage", None)
                if callable(get_usage):
                    detail = await get_usage(session_id)
                    if isinstance(detail, dict):
                        limit = detail.get("context_window_limit") or 0
                        if limit and "context_window_tokens" not in fields:
                            fields["context_window_tokens"] = int(limit)
                        rate = detail.get("occupancy_rate")
                        if rate is not None and "usage_percent" not in fields:
                            fields["usage_percent"] = float(rate)
                        total = detail.get("total_tokens")
                        if total and "context_tokens_used" not in fields:
                            fields["context_tokens_used"] = int(total)
                        for key in ("system_prompt_tokens", "tools_tokens", "messages_tokens"):
                            value = detail.get(key)
                            if isinstance(value, (int, float)) and value > 0 and key not in fields:
                                fields[key] = int(value)
            except Exception:
                logger.debug(
                    "[JiuWenSwarm] build round usage fields failed: request_id=%s",
                    rid, exc_info=True,
                )
            if "model" not in fields:
                resolve_name = getattr(adapter, "_resolve_model_name", None)
                if callable(resolve_name):
                    name = resolve_name()
                    if name and name != "unknown":
                        fields["model"] = name
            return fields
        try:
            while not stream_done.is_set() or not stream_queue.empty():
                try:
                    item = await asyncio.wait_for(stream_queue.get(), timeout=0.1)
                except asyncio.TimeoutError:
                    continue

                event_type, data = item
                _yielded_from_queue += 1
                if _yielded_from_queue <= 3:
                    _pl = getattr(data, "payload", None) if event_type == "chunk" else None
                    _et = _pl.get("event_type", "") if isinstance(_pl, dict) else ""
                    logger.info(
                        "[JiuWenSwarm] consumer loop yield #%s: request_id=%s event_type=%s item_type=%s",
                        _yielded_from_queue, rid, _et, event_type,
                    )

                if event_type == "error":
                    await _prepare_rewind_for_error()
                    if isinstance(data, asyncio.CancelledError):
                        logger.info("[JiuWenSwarm] 流式处理被中断: request_id=%s", rid)
                        raise data
                    # Surface exception class so consumers can classify
                    # failures structurally instead of regexing the message.
                    error_type = (
                        type(data).__name__ if isinstance(data, BaseException) else ""
                    )
                    error_code, error_message = _extract_error_code_message(data)
                    error_payload: dict[str, Any] = {
                        "event_type": "chat.error",
                        "error": str(data),
                    }
                    if error_type:
                        error_payload["error_type"] = error_type
                    if error_code:
                        error_payload["code"] = error_code
                    if error_message:
                        error_payload["message"] = error_message
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
                            should_record = et.startswith("chat.")
                            # usage_summary 帧载荷暂存（流尾并入终稿；本帧照常透传下发，
                            # 独立落盘由空壳过滤器拦截——usage 不单独成行）
                            if et == "chat.usage_summary":
                                round_usage_summary_payload = data.payload
                            # 直播 context.usage 推送帧暂存（成员帧带 member_name 不取——
                            # 只统计主理人，与前端环面同口径；回放=直播同帧同值）
                            if et == "context.usage" and not data.payload.get("member_name"):
                                round_context_usage_payload = data.payload
                            if not should_record and et == EventType.TEAM_MESSAGE.value:
                                should_record = True
                            if et == "chat.error":
                                await _prepare_rewind_for_error()
                            if et == "context.compression_state":
                                _append_compact_history_from_payload(
                                    payload=data.payload,
                                    session_id=session_id,
                                    request_id=rid,
                                    channel_id=cid,
                                    mode=request.params.get("mode", "unknown"),
                                )

                            payload_content = str(data.payload.get("content", ""))
                            a2ui_split = None
                            if et in {"chat.delta", "chat.final"} and payload_content:
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
                                # 成员 delta 不进主应答待落盘 buffer——防成员文本被
                                # 后续冲刷混进 leader 的历史记录（与下方豁免同规）
                                if not data.payload.get("member_name"):
                                    durable_pending_final_chunks.append(payload_content)
                                should_record = False
                            elif et == "chat.reasoning":
                                durable_pending_reasoning_chunks.append(payload_content)
                                should_record = False
                            elif et == "chat.tool_call":
                                # 成员帧不触发主应答待落盘冲刷——否则成员的工具调用会把
                                # leader 累计的 delta 提前落盘成无主重复记录（与下方
                                # member_name 落盘豁免同规）
                                if not data.payload.get("member_name"):
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
                                    durable_pending_final_chunks = []
                                    continue
                                durable_pending_final_chunks = []

                            # 团队成员帧（member_name 归属）的落盘所有权在 team_helpers
                            # （_persist_member_final_output/_persist_member_tool_event，
                            # 带成员归因与内容规则）；通用路径跳过防双写——否则成员
                            # chat.final 双份、tool_result 多出 content 为空的冗余记录。
                            if should_record and data.payload.get("member_name"):
                                should_record = False
                            # 团队模式 chat.error 落盘所有权在 team_helpers（带 warning
                            # 分级：轮内可恢复故障 warning:true，与终态错误区分）；
                            # 通用路径再写一条无 warning 标记的同事件 = 双写，
                            # 前端 mapper 会把无标记记录误判为终态错误（historyError）
                            if should_record and is_team_mode and et == "chat.error":
                                should_record = False
                            # 重试通知（rail 的"模型调用异常…第 N 次重试"）任何模式都不落盘：
                            # 是过程不是结果——落盘则无标记记录被 mapper 打成 historyError，
                            # 普通会话每次透明重试都在历史上留"曾发生错误"噪音
                            if (
                                should_record
                                and et == "chat.error"
                                and is_retry_notice_payload(data.payload)
                            ):
                                should_record = False
                            # 终稿截留：内容非空的主应答/leader chat.final
                            # 截留到流尾并入用量字段后统一落盘+下发（全模式共享本出口）；
                            # 成员帧（member_name）不归此路径（team_helpers 落盘所有权），
                            # 空 content 的边界/合成 final 不截留（本就不落盘）
                            if (
                                et == "chat.final"
                                and not data.payload.get("member_name")
                                and str(data.payload.get("content") or "").strip()
                            ):
                                if pending_final_chunk is not None:
                                    _persist_chat_event("chat.final", pending_final_chunk.payload)
                                    yield pending_final_chunk
                                pending_final_chunk = data
                                durable_final_content = str(data.payload.get("content", ""))
                                final_answer_content = durable_final_content
                                final_answer_chunks.clear()
                                continue
                            if should_record:
                                _persist_chat_event(et, data.payload)
                                if et == "chat.final":
                                    durable_final_content = str(data.payload.get("content", ""))
                            if et == "chat.final":
                                next_final_content = str(data.payload.get("content", ""))
                                if next_final_content:
                                    final_answer_content = next_final_content
                                    final_answer_chunks.clear()
                        yield data
                    elif isinstance(data, dict) and isinstance(data.get("event_type"), str):
                        et = str(data.get("event_type"))
                        should_record = et.startswith("chat.")
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
                        a2ui_split = None
                        if et in {"chat.delta", "chat.final"} and payload_content:
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
                            # 成员 delta 不进主应答待落盘 buffer（与 chunk 分支同规）
                            if not data.get("member_name"):
                                durable_pending_final_chunks.append(payload_content)
                            should_record = False
                        elif et == "chat.reasoning":
                            durable_pending_reasoning_chunks.append(payload_content)
                            should_record = False
                        elif et == "chat.tool_call":
                            # 成员帧不触发主应答待落盘冲刷（与 chunk 分支同规）
                            if not data.get("member_name"):
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
                                durable_pending_final_chunks = []
                                continue
                            durable_pending_final_chunks = []

                        # 团队成员帧（member_name 归属）由 team_helpers 成员归因落盘负责，
                        # 通用路径跳过防双写（与上方 chunk 分支同规）
                        if should_record and data.get("member_name"):
                            should_record = False
                        if should_record:
                            extra_fields = {k: v for k, v in data.items() if k not in ("event_type", "content")}
                            if et == EventType.TEAM_MESSAGE.value and "event" in data:
                                event_data = data.get("event", {})
                                if isinstance(event_data, dict):
                                    for k, v in event_data.items():
                                        if k not in ("type", "timestamp", "content"):
                                            extra_fields[k] = v
                            if et in {"chat.final", "chat.tool_call", "chat.error"}:
                                extra_fields = _attach_reasoning_content(extra_fields)
                            # 主应答落盘携带专家身份（无绑定显式写空串=默认角色作答标记）
                            # 前端据"键存在但空"区分默认角色与存量无字段；漏写会导致
                            # 中途绑专家后历史刷新把本轮身份回落成新专家
                            # （chat.error 同规：错误轮历史恢复 historyError 语义后身份行
                            #  也读 extra 的 expert_id——与 chunk 分支 :2690 对齐）
                            if et in ("chat.final", "chat.error") and "expert_id" not in extra_fields:
                                from jiuwenswarm.server.runtime.expert.expert_service import (
                                    history_expert_identity_extra,
                                )
                                extra_fields.update(history_expert_identity_extra(session_id))
                            # 透传 proactive 标记——刷新页面时前端靠 source 识别卡片
                            for pk in ("source", "proactive_type", "proactive_target"):
                                if pk not in extra_fields and pk in request.params:
                                    extra_fields[pk] = request.params[pk]
                            append_history_record(
                                session_id=session_id,
                                request_id=rid,
                                channel_id=cid,
                                role="assistant",
                                event_type=et,
                                content=data.get("content") or data.get("error") or "",
                                timestamp=time.time(),
                                extra=extra_fields if extra_fields else None,
                                mode=request.params.get("mode", "unknown"),
                            )
                            if et == "chat.final":
                                durable_final_content = str(data.get("content", ""))
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
            # 中断/停止留痕：pending 缓冲里是本轮已流式的半截正文（思考随
            # _persist_pending_final_text 一并附挂，aborted 标记"已停止"语义），
            # 不落盘则重启后该轮只剩前端本地台账的「已停止」标记。
            # 恢复重跑（pause→resume）的同 rid 最终 final 经读侧 last-wins
            # 覆盖本条半截记录（aborted 标记随之消失），语义安全。
            # 留痕是磁盘 I/O，失败不得顶替 CancelledError 向上传播（否则取消
            # 链路中断：下方 log/raise 不执行，调用方收到普通异常）。
            try:
                _persist_pending_final_text(aborted=True)
                # 截留终稿在取消路径不丢：原样落盘（不并用量），不下发（消费者已断）。
                # 在半截留痕之后写——last-wins 下完整终稿覆盖半截记录
                if pending_final_chunk is not None and isinstance(pending_final_chunk.payload, dict):
                    _persist_chat_event("chat.final", pending_final_chunk.payload)
                    pending_final_chunk = None
            except Exception:
                logger.warning(
                    "[JiuWenSwarm] failed to persist aborted final text: request_id=%s",
                    rid,
                    exc_info=True,
                )
            logger.info("[JiuWenSwarm] 流式处理被中断: request_id=%s", rid)
            raise
        finally:
            # The adapter producer owns RuntimeOutputStream.  Cancelling and
            # awaiting it releases the runtime output lease and aborts the
            # in-flight round when the outer WebSocket consumer disappears.
            if not stream_task.done():
                stream_task.cancel()
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
            raise producer_cancellation

        # 流尾：截留终稿并入用量字段后统一落盘+下发。
        # 在 a2ui 修复之前——修复产出不同终稿时其记录（同样带用量字段）last-wins 覆盖本条
        round_usage_fields = await _build_round_usage_fields()
        if pending_final_chunk is not None:
            _held_final = pending_final_chunk
            pending_final_chunk = None
            if isinstance(_held_final.payload, dict) and round_usage_fields:
                _held_final.payload.update(round_usage_fields)
            _persist_chat_event("chat.final", _held_final.payload)
            yield _held_final

        assistant_message = final_answer_content or "".join(final_answer_chunks)
        repair_call = getattr(adapter, "repair_model_response", None)
        retry_without_a2ui_call = self._make_retry_without_a2ui_call(
            adapter=adapter,
            request=request,
        )
        
        finalized_assistant_message = await finalize_assistant_response_if_a2ui(
            assistant_message,
            channel=cid,
            user_query=raw_query,
            request_id=rid or "",
            repair_call=repair_call,
            retry_without_a2ui_call=retry_without_a2ui_call,
        )
        if finalized_assistant_message and (
                finalized_assistant_message != assistant_message or suppress_a2ui_stream
        ):
            from jiuwenswarm.server.runtime.expert.expert_service import (
                history_expert_identity_extra,
            )

            append_history_record(
                session_id=session_id,
                request_id=rid,
                channel_id=cid,
                role="assistant",
                event_type="chat.final",
                content=finalized_assistant_message,
                timestamp=time.time(),
                extra=_attach_reasoning_content({
                    **{
                        k: v for k, v in request.params.items()
                        if k in ("source", "proactive_type", "proactive_target")
                    },
                    **history_expert_identity_extra(session_id),
                    # a2ui 修复重写的终稿同样携带用量（last-wins 覆盖截留终稿的记录）
                    **round_usage_fields,
                }),
                mode=request.params.get("mode", "unknown"),
            )
            final_answer_content = finalized_assistant_message
            final_answer_chunks = []
            yield _make_a2ui_final_chunk(
                request_id=rid,
                channel_id=cid,
                session_id=session_id,
                content=finalized_assistant_message,
            )

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
                extra=_memory_hook_extra(request),
            )
            await ExtensionRegistry.get_instance().trigger(AgentServerHookEvents.MEMORY_AFTER_CHAT, after_ctx)

        # auto memory: extract memories after conversation ends
        # 需要 auto_memory_enabled 和 memory.enabled 都为 true 才触发
        mode = request.params.get("mode", "code") if isinstance(request.params, dict) else "code"
        config = get_config()
        if is_auto_memory_enabled(mode, config) and is_memory_enabled(mode, config):
            _trigger_auto_memory_extraction(adapter, request, session_id, is_stream=True)

        _schedule_symphony_session_feedback(session_id, rid)
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

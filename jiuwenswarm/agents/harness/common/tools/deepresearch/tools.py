# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Secure process client for the external DeepResearch skill runner."""

from __future__ import annotations

import asyncio
import base64
import contextvars
import hashlib
import io
import json
import logging
import os
import pickle
import re
import sqlite3
import stat
import tempfile
import threading
import time
import unicodedata
import uuid
import zipfile
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import AsyncExitStack, contextmanager, suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from openjiuwen.core.foundation.tool import tool
from openjiuwen.core.sys_operation.cwd import get_cwd

from jiuwenswarm.agents.harness.common.tools.deepresearch.runtime import (
    DeepResearchRuntimeError,
    build_child_env,
    resolve_python_executable,
    stylize_report_archive,
)
from jiuwenswarm.agents.harness.common.tools.deepresearch.path_safety import (
    is_direct_directory,
    is_direct_regular_file,
    private_mode_is_compatible,
)
from jiuwenswarm.agents.harness.common.tools.deepresearch.stream_router import (
    FINAL_REPORT_NODES,
    MAX_TERMINAL_RESULT_JSON_NODES,
    MAX_TERMINAL_RESULT_TEXT_CHARS,
    ROUTER_LIMIT_ERROR,
    RouterState,
    _format_outline_card_markdown,
    advance_stage,
    build_interrupt_prompt,
    collected_questions,
    complete_final_report_processing,
    is_outline_status_placeholder,
    is_terminal_result_chunk,
    route_chunk,
    stage_for_resume_node,
    start_final_report_processing,
)
from jiuwenswarm.agents.harness.common.tools.deepresearch.tls import (
    normalize_child_tls_config,
)
from jiuwenswarm.agents.harness.common.tools.deepresearch.todo_progress import (
    deepresearch_todo_path,
    persist_deepresearch_task_update,
)
from jiuwenswarm.agents.harness.common.tools.deepresearch.usage import (
    normalize_workflow_llm_token_usage,
)
from jiuwenswarm.agents.harness.common.tools.deepresearch_task_manager import (
    extract_deepresearch_section_titles,
    get_deepresearch_manager,
    load_deepresearch_config,
)
from jiuwenswarm.common.config import get_config
from jiuwenswarm.common.local_env_config import (
    build_effective_env_overlay,
    get_task_env_overlay,
    parse_default_headers,
)
from jiuwenswarm.common.utils import (
    JIUWENSWARM_SHARED_SKILLS_DIRS_ENV,
    get_shared_agent_skills_dirs,
    parse_shared_skills_dirs_raw,
)
from jiuwenswarm.perf.context import DeepResearchReportType
from jiuwenswarm.server.gateway_push import WebSocketGatewayPushTransport
from jiuwenswarm.server.runtime.runtime_scope import RuntimeScopeKey

logger = logging.getLogger(__name__)

DEEPRESEARCH_CONFIG_PROTOCOL_VERSION = 1
DEEPRESEARCH_CONFIG_MAX_BYTES = 64 * 1024
DEEPRESEARCH_STOP_FRAME_PROTOCOL_VERSION = 1
DEEPRESEARCH_GRACEFUL_STOP_TIMEOUT_SECONDS = 30.0
# 修复A:停止-完成竞速交付报告的超时上限。markdown 产物管线含 HTML 样式
# 生成可能较慢;超时放弃交付时,professional 报告仍有 raw_report.md 修复7
# 止损兜底("继续"时认领失败 salvage 交付)。
DEEPRESEARCH_RACE_DELIVERY_TIMEOUT_SECONDS = 60.0
# 修复A:子进程退出后,帧感知排空读完管道剩余缓冲(至 EOF)的有界窗口。
# 真实管道在子进程退出后毫秒级 EOF;窗口只防异常管道/测试桩永久不 EOF。
DEEPRESEARCH_DRAIN_EOF_GRACE_SECONDS = 2.0
DEEPRESEARCH_STDOUT_PENDING_MAX_BYTES = 16 * 1024 * 1024
# A UTF-8 result can require four bytes per character. This is a per-frame
# wire bound; ordinary frames still retain their original 16 MiB limit.
DEEPRESEARCH_STDOUT_TERMINAL_MAX_BYTES = 4 * MAX_TERMINAL_RESULT_TEXT_CHARS
DEEPRESEARCH_STDERR_TAIL_MAX_BYTES = 20_000
DEEPRESEARCH_ERROR_TEXT_MAX_CHARS = 2048
DEEPRESEARCH_STDERR_OUTCOME_MAX_CHARS = 2048
_HUAWEI_MAAS_PLACEHOLDER_API_KEY = "huawei-maas-session"
DEEPRESEARCH_STREAM_JSON_MAX_DEPTH = 64
DEEPRESEARCH_STREAM_JSON_MAX_NODES = MAX_TERMINAL_RESULT_JSON_NODES
DEEPRESEARCH_STREAM_JSON_MAX_TEXT_CHARS = 16 * 1024 * 1024
DEEPRESEARCH_FINAL_REPORT_PROGRESS_INTERVAL_SECONDS = 60.0
DEEPRESEARCH_OUTLINE_CACHE_TENANTS = 256
DEEPRESEARCH_OUTLINE_CACHE_CONVERSATIONS = 128
DEEPRESEARCH_OUTLINE_CACHE_TITLES = 128
DEEPRESEARCH_OUTLINE_CACHE_TEXT_CHARS = 64 * 1024
DEEPRESEARCH_ZIP_MAX_MEMBERS = 1024
DEEPRESEARCH_ZIP_MEMBER_MAX_BYTES = 64 * 1024 * 1024
DEEPRESEARCH_ZIP_TOTAL_MAX_BYTES = 128 * 1024 * 1024
DEEPRESEARCH_ZIP_INPUT_MAX_BYTES = 192 * 1024 * 1024
_REPORT_PUBLICATION_ATTEMPTS = 8

_CHILD_ERROR_CODE_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,63}")
_CHILD_NUMERIC_ERROR_CODE_PATTERN = re.compile(r"[0-9]{6}", re.ASCII)
_PROTOCOL_IDENTIFIER_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,127}")
_LOG_CORRELATION_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")
_PROTOCOL_FIXED_KEYS = frozenset(
    {
        "__deepsearch_status__",
        "agent",
        "completed_tasks",
        "content",
        "conversation_id",
        "current_task",
        "error",
        "error_code",
        "event",
        "event_type",
        "files",
        "final_result",
        "in_progress_tasks",
        "interaction_policy",
        "is_complete",
        "is_processing",
        "marker",
        "message_id",
        "message_type",
        "metadata",
        "mime_type",
        "name",
        "node_id",
        "outline",
        "path",
        "pending_tasks",
        "prompt",
        "questions",
        "reasoning_content",
        "report",
        "report_delivered",
        "response_content",
        "response_content_type",
        "returncode",
        "section_idx",
        "section_title",
        "section_total",
        "status",
        "stream_source_id",
        "task_content",
        "task_id",
        "tasks",
        "timing",
        "total_tasks",
    }
)


def _is_valid_child_error_code(value: object) -> bool:
    return isinstance(value, str) and bool(
        _CHILD_ERROR_CODE_PATTERN.fullmatch(value)
        or _CHILD_NUMERIC_ERROR_CODE_PATTERN.fullmatch(value)
    )


_PROTOCOL_STATUS_VALUES = frozenset(
    {
        "cancelled",
        "completed",
        "error",
        "in_progress",
        "interrupted",
        "pending",
        "resuming",
        "started",
        "stopped",
    }
)
_SECRET_CONFIG_KEY_MARKERS = (
    "API_KEY",
    "AUTHORIZATION",
    "CREDENTIAL",
    "HEADER",
    "PASSWORD",
    "SECRET",
    "TOKEN",
)

_PROVIDER_TO_TYPE = {
    "openai": "openai",
    "openrouter": "openai",
    "zhipu": "zhipu",
    "glm": "zhipu",
    "qwen": "qwen",
    "dashscope": "qwen",
    "deepseek": "deepseek",
    "modelarts": "qwen",
}
_SKIPPED_FEEDBACK_HANDLER_PAYLOAD = (
    '{"feedback":"","interaction_status":"skipped"}'
)

_deepresearch_route_ctx: contextvars.ContextVar[dict[str, object] | None] = (
    contextvars.ContextVar("jiuwenswarm_deepresearch_route", default=None)
)
_deepresearch_router_state_ctx: contextvars.ContextVar[object | None] = (
    contextvars.ContextVar("jiuwenswarm_deepresearch_router_state", default=None)
)
# 停止续跑(resume node='stopped')的前端进度起始阶段提示:认领侧
# (execution.py)按被停止节点推导后经本上下文传入 _consume_stream,
# 避免进度列表错误回退到第一步(详见 stream_router.stage_for_resume_node)。
_deepresearch_stage_hint_ctx: contextvars.ContextVar[int | None] = (
    contextvars.ContextVar("jiuwenswarm_deepresearch_stage_hint", default=None)
)
_OUTLINE_TITLE_CACHES: dict[
    tuple[str, str, str], dict[str, dict[str, str]]
] = {}
_OUTLINE_TITLE_CACHES_GUARD = threading.Lock()
_OUTLINE_JSON_CACHES: dict[
    tuple[str, str, str], dict[str, dict[str, Any]]
] = {}
_OUTLINE_JSON_CACHES_GUARD = threading.Lock()


class _ReportPublicationCollision(FileExistsError):
    """Internal retry signal after reserving a colliding allocation."""


class _StreamProtocolInvalid(ValueError):
    """A child-controlled JSON structure cannot be forwarded safely."""


def _safe_log_correlation_id(value: object) -> str:
    """Return a bounded identifier for logs without echoing arbitrary input."""
    candidate = str(value or "").strip()
    if _LOG_CORRELATION_ID_PATTERN.fullmatch(candidate):
        return candidate
    return "-"


@dataclass(frozen=True, slots=True)
class _ProgressArtifact:
    path: Path
    directory_identity: tuple[int, int]
    file_identity: tuple[int, int]


@dataclass(slots=True)
class _KeyedLock:
    lock: threading.Lock
    references: int = 0


_REPORT_OUTPUT_LOCKS: dict[Path, _KeyedLock] = {}
_REPORT_OUTPUT_LOCKS_GUARD = threading.Lock()
_TIMED_SDK_NODES = frozenset({
    "intent_recognition",
    "generate_questions",
    "feedback_handler",
    "outline",
    "outline_interaction",
    "editor_team",
    "plan_reasoning",
    "info_collector",
    "collector_query_generation",
    "collector_info_retrieval",
    "collector_supervisor",
    "collector_summary",
    "sub_reporter",
    "reporter",
    "source_tracer",
    "source_tracer_infer",
    "vlm_chart_generator",
    "brief_outline",
    "brief_info_collector",
    "brief_evidence_reviewer",
    "brief_sub_reporter",
    "brief_reporter",
    "brief_mermaid_generator",
    "brief_source_tracer",
})
_RESUME_NODE_CURRENT_STAGE = {
    "feedback_handler": 1,
    "outline_interaction": 2,
    "user_feedback_processor": 3,
}
_CHECKPOINT_WORKFLOW_ID = "research_workflow"
_CHECKPOINT_INTERRUPT_STATUS = "__interrupt__"
_QUESTION_PARAGRAPH_SPLIT = re.compile(r"\n\s*\n(?=\s*\d+[.、)]\s)")


def push_deepresearch_route(
    request_id: str,
    channel_id: str,
    session_id: str,
    *,
    service_id: str = "default",
    agent_id: str = "default",
    workspace_key: str = "default",
    output_dir: str = "",
) -> contextvars.Token:
    """Bind request routing and its trusted DeepResearch artifact root."""
    return _deepresearch_route_ctx.set(
        {
            "request_id": request_id or "",
            "channel_id": channel_id or "",
            "session_id": session_id or "",
            "service_id": (service_id or "default").strip() or "default",
            "agent_id": (agent_id or "default").strip() or "default",
            "workspace_key": (workspace_key or "default").strip() or "default",
            "output_dir": str(output_dir or "").strip(),
        }
    )


def reset_deepresearch_route(token: contextvars.Token) -> None:
    """Restore the previous request route."""
    _deepresearch_route_ctx.reset(token)


def _get_route() -> dict[str, object]:
    route = _deepresearch_route_ctx.get() or {}
    return {
        "request_id": route.get("request_id", ""),
        "channel_id": route.get("channel_id", ""),
        "session_id": route.get("session_id", ""),
        "service_id": route.get("service_id", "default") or "default",
        "agent_id": route.get("agent_id", "default") or "default",
    }


def _route_scope() -> RuntimeScopeKey:
    route = _get_route()
    return RuntimeScopeKey.from_ids(
        str(route.get("service_id") or "default"),
        str(route.get("agent_id") or "default"),
        str(route.get("session_id") or ""),
    )


def _outline_cache_key(route: dict[str, object]) -> tuple[str, str, str]:
    return (
        str(route.get("service_id") or "default"),
        str(route.get("agent_id") or "default"),
        str(route.get("session_id") or ""),
    )


def _get_cached_outline_titles(
    route: dict[str, object], conversation_id: object
) -> dict[str, str]:
    conversation = str(conversation_id or "").strip()
    if not conversation:
        return {}
    key = _outline_cache_key(route)
    with _OUTLINE_TITLE_CACHES_GUARD:
        cache = _OUTLINE_TITLE_CACHES.get(key)
        return dict(cache.get(conversation, {})) if cache is not None else {}


def _cache_outline_titles(
    route: dict[str, object],
    conversation_id: object,
    titles: dict[str, str],
) -> None:
    conversation = str(conversation_id or "").strip()
    if (
        not conversation
        or not isinstance(titles, dict)
        or len(titles) > DEEPRESEARCH_OUTLINE_CACHE_TITLES
    ):
        return
    safe_titles: dict[str, str] = {}
    text_chars = 0
    for raw_index, raw_title in titles.items():
        if not isinstance(raw_index, str) or not isinstance(raw_title, str):
            return
        text_chars += len(raw_index) + len(raw_title)
        if text_chars > DEEPRESEARCH_OUTLINE_CACHE_TEXT_CHARS:
            return
        safe_titles[raw_index] = raw_title
    if not safe_titles:
        return

    key = _outline_cache_key(route)
    with _OUTLINE_TITLE_CACHES_GUARD:
        cache = _OUTLINE_TITLE_CACHES.get(key)
        if cache is None:
            while len(_OUTLINE_TITLE_CACHES) >= DEEPRESEARCH_OUTLINE_CACHE_TENANTS:
                _OUTLINE_TITLE_CACHES.pop(next(iter(_OUTLINE_TITLE_CACHES)))
            cache = {}
            _OUTLINE_TITLE_CACHES[key] = cache
        if conversation in cache:
            cache.pop(conversation)
        while len(cache) >= DEEPRESEARCH_OUTLINE_CACHE_CONVERSATIONS:
            cache.pop(next(iter(cache)))
        cache[conversation] = safe_titles


def _clear_outline_title_cache(
    route: dict[str, object], conversation_id: object
) -> None:
    conversation = str(conversation_id or "").strip()
    if not conversation:
        return
    key = _outline_cache_key(route)
    with _OUTLINE_TITLE_CACHES_GUARD:
        cache = _OUTLINE_TITLE_CACHES.get(key)
        if cache is None:
            return
        cache.pop(conversation, None)
        if not cache:
            _OUTLINE_TITLE_CACHES.pop(key, None)


def _get_cached_outline_json(
    route: dict[str, object], conversation_id: object
) -> dict[str, Any]:
    conversation = str(conversation_id or "").strip()
    if not conversation:
        return {}
    key = _outline_cache_key(route)
    with _OUTLINE_JSON_CACHES_GUARD:
        cache = _OUTLINE_JSON_CACHES.get(key)
        return dict(cache.get(conversation, {})) if cache is not None else {}


def _cache_outline_json(
    route: dict[str, object],
    conversation_id: object,
    outline_json: dict[str, Any],
) -> None:
    conversation = str(conversation_id or "").strip()
    if not conversation or not isinstance(outline_json, dict):
        return
    serialized = json.dumps(outline_json, ensure_ascii=False)
    if len(serialized) > DEEPRESEARCH_OUTLINE_CACHE_TEXT_CHARS:
        return
    key = _outline_cache_key(route)
    with _OUTLINE_JSON_CACHES_GUARD:
        cache = _OUTLINE_JSON_CACHES.get(key)
        if cache is None:
            while len(_OUTLINE_JSON_CACHES) >= DEEPRESEARCH_OUTLINE_CACHE_TENANTS:
                _OUTLINE_JSON_CACHES.pop(next(iter(_OUTLINE_JSON_CACHES)))
            cache = {}
            _OUTLINE_JSON_CACHES[key] = cache
        if conversation in cache:
            cache.pop(conversation)
        while len(cache) >= DEEPRESEARCH_OUTLINE_CACHE_CONVERSATIONS:
            cache.pop(next(iter(cache)))
        cache[conversation] = dict(outline_json)


def _clear_outline_json_cache(
    route: dict[str, object], conversation_id: object
) -> None:
    conversation = str(conversation_id or "").strip()
    if not conversation:
        return
    key = _outline_cache_key(route)
    with _OUTLINE_JSON_CACHES_GUARD:
        cache = _OUTLINE_JSON_CACHES.get(key)
        if cache is None:
            return
        cache.pop(conversation, None)
        if not cache:
            _OUTLINE_JSON_CACHES.pop(key, None)


def _map_provider_to_type(provider: str) -> str:
    value = provider.strip().lower()
    return _PROVIDER_TO_TYPE.get(value, value)


def _build_deepresearch_config(source: dict[str, str]) -> dict[str, str]:
    """Select only fields required by the external runner."""
    resolved = load_deepresearch_config(source)
    config: dict[str, str] = {}
    for target, value in (
        ("LLM_API_KEY", resolved.get("LLM_API_KEY", "")),
        ("LLM_MODEL_NAME", resolved.get("LLM_MODEL_NAME", "")),
        ("LLM_BASE_URL", resolved.get("LLM_BASE_URL", "")),
        (
            "LLM_MODEL_TYPE",
            _map_provider_to_type(str(resolved.get("LLM_MODEL_TYPE", ""))),
        ),
    ):
        if value:
            config[target] = str(value)

    engine = str(resolved.get("WEB_SEARCH_ENGINE_NAME", "")).strip().lower()
    search_key = str(resolved.get("WEB_SEARCH_API_KEY", "")).strip()
    search_url = str(resolved.get("WEB_SEARCH_URL", "")).strip()
    search_configured = engine and search_key and (engine != "petal" or search_url)
    if search_configured:
        config["WEB_SEARCH_ENGINE_NAME"] = engine
        config["WEB_SEARCH_API_KEY"] = search_key
        if search_url:
            config["WEB_SEARCH_URL"] = search_url

    for key in ("MAX_WEB_SEARCH_RESULTS", "EXECUTION_METHOD"):
        value = resolved.get(key, "")
        if value:
            config[key] = str(value)
    for source_key, target_key in (
        ("VISION_API_KEY", "VISION_API_KEY"),
        ("VISION_API_URL", "VISION_API_BASE"),
        ("VISION_PROVIDER", "VISION_PROVIDER"),
        ("VISION_MODEL_NAME", "VISION_MODEL_NAME"),
    ):
        value = resolved.get(source_key, "")
        if value:
            config[target_key] = str(value)
    for header_name in (
        "default_headers",
        "DEFAULT_HEADERS",
        "OPENAI_DEFAULT_HEADERS",
    ):
        value = str(source.get(header_name, "") or "").strip()
        if value:
            config["default_headers"] = value
            break
    config["LLM_SSL_VERIFY"] = str(source.get("LLM_SSL_VERIFY", "false"))
    config["TOOL_SSL_VERIFY"] = str(source.get("TOOL_SSL_VERIFY", "false"))
    # SSRF 旁路开关：优先按 source（service/agent 粒度）读取，兜底进程环境，默认开启
    config["SEARCH_SERVICE_ALLOW_UNSAFE_URL"] = str(
        source.get(
            "SEARCH_SERVICE_ALLOW_UNSAFE_URL",
            os.environ.get("SEARCH_SERVICE_ALLOW_UNSAFE_URL", "true"),
        )
    )
    _attach_deepresearch_thinking_config(config)
    return config


def _attach_deepresearch_thinking_config(config: dict[str, str]) -> None:
    """Pass auto-default thinking depth into the isolated DeepResearch child.

    SDK defaults to ``llm_thinking_enabled=False`` (injects ``thinking.type=disabled``),
    which breaks GLM-5.3. Parent resolves vendor-mapped kwargs (currently only
    GLM-5.3 family on ``default``) and ships them as ``LLM_EXTENSION`` +
    ``LLM_THINKING_ENABLED`` for the runner to apply.
    """
    model_name = str(config.get("LLM_MODEL_NAME", "") or "").strip()
    if not model_name:
        return
    try:
        from jiuwenswarm.common.thinking.adapter import adapt_thinking
        from jiuwenswarm.common.thinking.types import thaw_llm_call_kwargs
        from jiuwenswarm.common.thinking.vendor_map import (
            llm_call_kwargs_to_extension,
        )
    except ImportError:
        return
    profile = adapt_thinking("default", model_name=model_name)
    if not profile.injected or not profile.llm_call_kwargs:
        return
    extension = llm_call_kwargs_to_extension(
        thaw_llm_call_kwargs(profile.llm_call_kwargs)
    )
    if not extension:
        return
    config["LLM_EXTENSION"] = json.dumps(extension, ensure_ascii=False)
    # Never ask the SDK to disable thinking when we ship an explicit depth profile.
    config["LLM_THINKING_ENABLED"] = "true"


def _build_deepresearch_request_config(
    *,
    interactive_ask: bool,
    service_id: str = "default",
    agent_id: str = "default",
) -> dict[str, str]:
    task_overlay = get_task_env_overlay()
    if task_overlay is None:
        source = build_effective_env_overlay(
            service_id=service_id, agent_id=agent_id
        )
    else:
        source = {
            str(key): str(value)
            for key, value in task_overlay.items()
            if value is not None
        }
    config = _build_deepresearch_config(source)
    config["DEEPSEARCH_HITL"] = "true" if interactive_ask else "false"
    # 宿主会话归属标识:随 config frame 注入 runner,SDK StartNode 写入
    # 会话 config 随快照落盘;认领侧(probe/扫库)校验归属防跨会话误认领。
    host_session_id = str(_get_route().get("session_id") or "").strip()
    if host_session_id:
        config["DEEPSEARCH_HOST_SESSION_ID"] = host_session_id
    return config


def _build_styled_export_llm_config(
    runtime_config: Mapping[str, str] | None = None,
) -> dict[str, dict[str, object]]:
    """Build the exact JSON-safe LLM config consumed by the isolated SDK."""
    resolved = load_deepresearch_config(runtime_config)
    api_key = str(resolved.get("LLM_API_KEY", "")).strip()
    model_name = str(resolved.get("LLM_MODEL_NAME", "")).strip()
    base_url = str(resolved.get("LLM_BASE_URL", "")).strip()
    model_type = _map_provider_to_type(
        str(resolved.get("LLM_MODEL_TYPE", ""))
    ).lower()
    invalid_llm_config = (
        not api_key
        or not model_name
        or not base_url
        or "example.com" in base_url.lower()
    )
    if invalid_llm_config:
        raise ValueError("styled HTML LLM configuration is invalid")
    if model_type not in {"openai", "siliconflow"}:
        model_type = "openai"
    # Prefer GLM-5.3 auto-default depth (vendor-mapped). Fall back to disable
    # when adapt does not inject — never disable GLM-5.3 when it does inject.
    extension: dict[str, object] = {
        "extra_body": {"thinking": {"type": "disabled"}},
    }
    try:
        from jiuwenswarm.common.thinking.adapter import adapt_thinking
        from jiuwenswarm.common.thinking.types import thaw_llm_call_kwargs
        from jiuwenswarm.common.thinking.vendor_map import (
            llm_call_kwargs_to_extension,
        )

        profile = adapt_thinking("default", model_name=model_name)
        if profile.injected and profile.llm_call_kwargs:
            mapped = llm_call_kwargs_to_extension(
                thaw_llm_call_kwargs(profile.llm_call_kwargs)
            )
            if mapped:
                extension = mapped
    except ImportError:
        pass
    return {
        "general": {
            "model_name": model_name,
            "model_type": model_type,
            "base_url": base_url,
            "api_key": api_key,
            "extension": extension,
        }
    }


def _build_styled_export_llm_auth(
    runtime_config: Mapping[str, str],
    llm_config: Mapping[str, object],
) -> dict[str, str]:
    """Carry only request-scoped MaaS Authorization into the style child."""
    general = llm_config.get("general")
    api_key = (
        str(general.get("api_key", "") or "").strip()
        if isinstance(general, Mapping)
        else ""
    )
    if api_key != _HUAWEI_MAAS_PLACEHOLDER_API_KEY:
        return {}

    headers = parse_default_headers(
        str(runtime_config.get("default_headers", "") or "")
    )
    authorization = [
        value
        for key, value in (headers or {}).items()
        if key.lower() == "authorization" and value.strip()
    ]
    if len(authorization) != 1:
        raise ValueError(
            "styled HTML Huawei MaaS Authorization is unavailable"
        )
    return {
        "default_headers": json.dumps(
            {"Authorization": authorization[0]},
            ensure_ascii=False,
            separators=(",", ":"),
        )
    }


def _build_deepresearch_child_env(
    *,
    interactive_ask: bool,
    service_id: str = "default",
    agent_id: str = "default",
    runtime_config: dict[str, str] | None = None,
    executable: Path | None = None,
) -> dict[str, str]:
    """Return a credential-free environment for the isolated child."""
    del interactive_ask, service_id, agent_id, runtime_config
    executable = executable or resolve_python_executable()
    env = build_child_env(executable)
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONUTF8"] = "1"
    return env


def _validate_deepresearch_search_config(config: dict[str, str]) -> str | None:
    engine = config.get("WEB_SEARCH_ENGINE_NAME", "").strip().lower()
    search_key = config.get("WEB_SEARCH_API_KEY", "").strip()
    search_url = config.get("WEB_SEARCH_URL", "").strip()
    search_configured = engine and search_key and (engine != "petal" or search_url)
    if search_configured:
        return None
    return (
        "DeepResearch 搜索配置缺失：请配置完整的 Petal（URL 与凭据）"
        "或 Bocha（API Key）搜索凭据。"
    )


def _encode_deepresearch_config(config: dict[str, str]) -> bytes:
    tls = normalize_child_tls_config(
        {
            "LLM_SSL_VERIFY": config.get("LLM_SSL_VERIFY", "false"),
            "TOOL_SSL_VERIFY": config.get("TOOL_SSL_VERIFY", "false"),
        }
    )
    frame = json.dumps(
        {
            "version": DEEPRESEARCH_CONFIG_PROTOCOL_VERSION,
            "config": config,
            "tls": tls,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8") + b"\n"
    if len(frame) > DEEPRESEARCH_CONFIG_MAX_BYTES:
        raise ValueError("DeepResearch config frame exceeds 64 KiB")
    return frame


def _config_secret_values(config: dict[str, str]) -> tuple[str, ...]:
    """Return request-scoped secret values without retaining ambient state."""
    values = set()
    try:
        for server in json.loads(config.get("MCP_SERVERS_JSON", "[]")):
            values.update(value for value in server.get("headers", {}).values() if isinstance(value, str) and value)
            values.update(value for _, value in parse_qsl(urlsplit(server.get("server_url", "")).query) if value)
    except (ValueError, TypeError, AttributeError):
        pass
    for key, value in config.items():
        is_secret = value and any(
            marker in str(key).upper() for marker in _SECRET_CONFIG_KEY_MARKERS
        )
        if is_secret:
            values.add(str(value))
    return tuple(sorted(values, key=len, reverse=True))


def _redact_known_secrets(text: str, secrets: tuple[str, ...]) -> str:
    for secret in secrets:
        text = text.replace(secret, "[REDACTED]")
    return text


def _is_protocol_control_value(key: str, value: str) -> bool:
    if key in {"__deepsearch_status__", "status", "current_task"}:
        return value in _PROTOCOL_STATUS_VALUES
    if key == "error_code":
        return _is_valid_child_error_code(value)
    if key in {"mime_type", "response_content_type"}:
        return value in {"text/markdown", "text/html"}
    if key in {
        "agent",
        "event",
        "event_type",
        "interaction_policy",
        "message_type",
        "node_id",
        "stream_source_id",
        "task_id",
    }:
        return bool(_PROTOCOL_IDENTIFIER_PATTERN.fullmatch(value))
    if key in {"section_idx", "section_total"}:
        return value.isdecimal() and len(value) <= 10
    return False


def _bounded_diagnostic(
    value: object,
    *,
    secrets: tuple[str, ...],
    limit: int,
    fallback: str,
    keep_tail: bool = False,
) -> str:
    if not isinstance(value, str):
        return fallback
    text = _redact_known_secrets(value, secrets)
    text = "".join(
        character if character >= " " or character in "\n\t" else " "
        for character in text
    ).strip()
    bounded = text[-limit:] if keep_tail else text[:limit]
    return bounded or fallback


def _redact_json_value(value: Any, secrets: tuple[str, ...]) -> Any:
    """Iteratively copy and redact a bounded JSON key/value tree."""
    if isinstance(value, str):
        return _redact_known_secrets(value, secrets)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if not isinstance(value, (dict, list)):
        raise _StreamProtocolInvalid("unsupported JSON value")

    root: dict[str, Any] | list[Any] = {} if isinstance(value, dict) else []
    stack: list[tuple[Any, Any, int]] = [(value, root, 0)]
    nodes = 0
    text_chars = 0
    text_limit = (
        MAX_TERMINAL_RESULT_TEXT_CHARS if is_terminal_result_chunk(value)
        else DEEPRESEARCH_STREAM_JSON_MAX_TEXT_CHARS
    )
    while stack:
        source, target, depth = stack.pop()
        if depth > DEEPRESEARCH_STREAM_JSON_MAX_DEPTH:
            raise _StreamProtocolInvalid("JSON depth limit exceeded")
        entries = source.items() if isinstance(source, dict) else enumerate(source)
        for raw_key, item in entries:
            nodes += 1
            if nodes > DEEPRESEARCH_STREAM_JSON_MAX_NODES:
                raise _StreamProtocolInvalid("JSON node limit exceeded")
            if isinstance(source, dict):
                if not isinstance(raw_key, str):
                    raise _StreamProtocolInvalid("JSON key is not text")
                key = (
                    raw_key
                    if raw_key in _PROTOCOL_FIXED_KEYS
                    else _redact_known_secrets(raw_key, secrets)
                )
                text_chars += len(key)
                if key in target:
                    raise _StreamProtocolInvalid("redacted JSON key collision")
            else:
                key = raw_key

            if isinstance(item, str):
                child = (
                    item
                    if isinstance(source, dict)
                    and _is_protocol_control_value(raw_key, item)
                    else _redact_known_secrets(item, secrets)
                )
                text_chars += len(child)
            elif item is None or isinstance(item, (bool, int, float)):
                child = item
            elif isinstance(item, dict):
                child = {}
                stack.append((item, child, depth + 1))
            elif isinstance(item, list):
                child = []
                stack.append((item, child, depth + 1))
            else:
                raise _StreamProtocolInvalid("unsupported JSON value")

            if isinstance(target, dict):
                target[key] = child
            else:
                target.append(child)
            if text_chars > text_limit:
                raise _StreamProtocolInvalid("JSON text limit exceeded")
    return root


def _sanitize_terminal_outcome(
    outcome: dict[str, Any], config: dict[str, str]
) -> dict[str, Any]:
    secrets = _config_secret_values(config)
    try:
        sanitized = _redact_json_value(outcome, secrets)
    except _StreamProtocolInvalid:
        return _stream_protocol_error()
    if not isinstance(sanitized, dict):
        return {
            "status": "error",
            "error_code": "workflow_error",
            "error": "DeepResearch workflow failed",
        }
    if sanitized.get("status") != "error":
        return sanitized

    error_code = sanitized.get("error_code")
    if not _is_valid_child_error_code(error_code):
        error_code = "workflow_error"
    sanitized["error_code"] = error_code
    sanitized["error"] = _bounded_diagnostic(
        sanitized.get("error"),
        secrets=secrets,
        limit=DEEPRESEARCH_ERROR_TEXT_MAX_CHARS,
        fallback="DeepResearch workflow failed",
    )
    if "stderr_tail" in sanitized:
        sanitized["stderr_tail"] = _bounded_diagnostic(
            sanitized["stderr_tail"],
            secrets=secrets,
            limit=DEEPRESEARCH_STDERR_OUTCOME_MAX_CHARS,
            fallback="DeepResearch child diagnostics unavailable",
            keep_tail=True,
        )
    return sanitized


def _stream_protocol_error() -> dict[str, str]:
    return {
        "status": "error",
        "error_code": "stream_protocol_invalid",
        "error": "DeepResearch stream protocol is invalid",
    }


def _validate_stream_json_shape(value: Any) -> bool:
    """Bound parsed JSON shape before routing any child-controlled structure."""
    stack: list[tuple[Any, int]] = [(value, 0)]
    nodes = 0
    text_chars = 0
    text_limit = (
        MAX_TERMINAL_RESULT_TEXT_CHARS if is_terminal_result_chunk(value)
        else DEEPRESEARCH_STREAM_JSON_MAX_TEXT_CHARS
    )
    while stack:
        item, depth = stack.pop()
        nodes += 1
        if (
            nodes > DEEPRESEARCH_STREAM_JSON_MAX_NODES
            or depth > DEEPRESEARCH_STREAM_JSON_MAX_DEPTH
        ):
            return False
        if isinstance(item, dict):
            for key, child in item.items():
                if not isinstance(key, str):
                    return False
                text_chars += len(key)
                stack.append((child, depth + 1))
        elif isinstance(item, list):
            stack.extend((child, depth + 1) for child in item)
        elif isinstance(item, str):
            text_chars += len(item)
        elif item is not None and not isinstance(item, (bool, int, float)):
            return False
        if text_chars > text_limit:
            return False
    return True


async def _close_deepresearch_stdin(proc: Any) -> None:
    stdin = getattr(proc, "stdin", None)
    if stdin is None:
        return
    with suppress(Exception):
        stdin.close()
    wait_closed = getattr(stdin, "wait_closed", None)
    if callable(wait_closed):
        with suppress(Exception):
            await wait_closed()


async def _write_deepresearch_config(proc: Any, frame: bytes) -> None:
    if proc.stdin is None:
        raise RuntimeError("DeepResearch config stdin is unavailable")
    try:
        proc.stdin.write(frame)
        await proc.stdin.drain()
    except BaseException:
        # 写失败时子进程多半已死:关闭管道避免句柄泄漏。
        await _close_deepresearch_stdin(proc)
        raise
    # 写成功时保持 stdin 打开:run/resume 期间随时可写停止帧
    # (run_deepsearch.py 的停止帧监听线程依赖该通道做优雅停止)。


async def _await_cleanup_task(task: asyncio.Task[Any]) -> None:
    """Finish cleanup despite repeated cancellation of the caller task."""
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
        except BaseException:
            break
    with suppress(BaseException):
        task.result()


async def _stop_deepresearch_process(proc: Any, timeout: float = 10.0) -> None:
    """Terminate, kill if necessary, and always reap one child."""
    if getattr(proc, "returncode", None) is None:
        with suppress(ProcessLookupError):
            proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            if getattr(proc, "returncode", None) is None:
                with suppress(ProcessLookupError):
                    proc.kill()
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(proc.wait(), timeout=timeout)
    else:
        with suppress(Exception):
            await proc.wait()


def _capture_completed_frame(line: bytes, captured: dict[str, Any]) -> None:
    """修复A：从停止排空窗口的输出行中捕获 completed 终局帧。

    仅捕获带 response_content 的 completed 帧（自然完成竞速的唯一存活
    报告载体）；stopped/error/interrupted 终态帧由既有路径处理，不捕获。
    多个 completed 帧时保留最后一个。
    """
    try:
        chunk = json.loads(line.decode("utf-8").strip())
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        return
    if not isinstance(chunk, dict):
        return
    if not _validate_stream_json_shape(chunk):
        return
    status_value = chunk.get("__deepsearch_status__") or chunk.get("status")
    if status_value != "completed":
        return
    final_result = chunk.get("final_result")
    if not isinstance(final_result, dict):
        return
    response_content = final_result.get("response_content")
    if not isinstance(response_content, str) or not response_content:
        return
    captured.clear()
    captured.update(chunk)


async def _drain_stream_while_stopping(
    stream: Any, captured: dict[str, Any] | None = None
) -> None:
    """停止等待期间持续排空子进程输出,防止管道写满阻塞落盘退出。

    帧感知(修复A):停止-完成竞速窗口内,子进程可能在停止信号生效前按
    自然完成路径输出 completed 终局帧(含报告全文)。原盲排空把该帧连同
    报告一起丢弃——快照又按 workflow_completed 清理,报告彻底丢失。排空
    同时逐行解析 NDJSON,捕获 completed 帧写入 captured,供调用方在
    优雅停止完成后照常交付。captured 为 None 时保持纯排空行为。
    """
    read = getattr(stream, "read", None)
    if not callable(read):
        return
    if captured is not None:
        try:
            async for line in _iter_ndjson_lines(stream):
                _capture_completed_frame(line, captured)
            return
        except (OSError, ValueError):
            # Keep draining malformed output so the stopping child can exit.
            pass
    try:
        while await read(64 * 1024):
            pass
    except (OSError, ValueError):
        return


async def _request_graceful_stop(
    proc: Any,
    timeout: float = DEEPRESEARCH_GRACEFUL_STOP_TIMEOUT_SECONDS,
    *,
    captured: dict[str, Any] | None = None,
) -> None:
    """优雅停止三步走:发停止帧 → 等子进程落盘退出 → 超时硬杀兜底。

    停止帧是 ``{"version":1,"action":"stop"}`` 换行 JSON,子进程后台线程
    收到后调用 ``request_stop()``,工作流在节点边界抛 StopRequested,
    异常保存分支落盘 checkpoint 并输出 stopped 终态后正常退出。

    captured 非空时,排空任务同时捕获停止-完成竞速窗口内自然完成输出
    的 completed 终局帧(修复A),供调用方交付报告。
    """
    stdin = getattr(proc, "stdin", None)
    if stdin is not None and getattr(proc, "returncode", None) is None:
        frame = (
            json.dumps(
                {
                    "version": DEEPRESEARCH_STOP_FRAME_PROTOCOL_VERSION,
                    "action": "stop",
                },
                ensure_ascii=False,
            )
            + "\n"
        ).encode("utf-8")
        try:
            stdin.write(frame)
            await stdin.drain()
        except (OSError, ValueError):
            # 子进程已退出或管道断开:跳过等待,直接走硬停止兜底。
            logger.info(
                "[deepresearch_stream] stop frame not delivered "
                "(child gone or pipe closed); falling back to hard stop"
            )
    if getattr(proc, "returncode", None) is None:
        # 调用方已被取消,原消费循环不再读 stdout:必须持续排空,
        # 否则子进程写 stopped 终态时管道满写阻塞,永远无法落盘退出。
        drain_task = asyncio.create_task(
            _drain_stream_while_stopping(proc.stdout, captured)
        )
        try:
            await asyncio.wait_for(proc.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning(
                "[deepresearch_stream] graceful stop timed out after %ss; "
                "falling back to hard stop",
                timeout,
            )
        finally:
            # 子进程已退出(或超时走硬停止)。帧感知排空时管道中可能仍有
            # 未读完的竞速 completed 帧:子进程退出后管道必达 EOF,给
            # drain 一个有界窗口读完剩余缓冲;纯排空(captured=None)与
            # 异常管道/测试桩永久不 EOF 的场景由超时+取消兜底,清理路径
            # 不会挂起。
            if captured is not None:
                with suppress(asyncio.TimeoutError, asyncio.CancelledError):
                    await asyncio.wait_for(
                        asyncio.shield(drain_task),
                        timeout=DEEPRESEARCH_DRAIN_EOF_GRACE_SECONDS,
                    )
            drain_task.cancel()
            await _await_cleanup_task(drain_task)
    await _stop_deepresearch_process(proc)


async def _iter_ndjson_lines(
    stream: Any,
    read_size: int = 64 * 1024,
    *,
    request_id: str = "",
    session_id: str = "",
    conversation_id: str = "",
):
    if stream is None:
        return
    correlation_ids = (
        _safe_log_correlation_id(request_id),
        _safe_log_correlation_id(session_id),
        _safe_log_correlation_id(conversation_id),
    )

    def check_limit(size: int, limit: int) -> None:
        if size > limit:
            logger.error(
                "[deepresearch_stream] stdout frame limit exceeded "
                "pending_bytes=%s limit_bytes=%s request_id=%s session_id=%s "
                "conversation_id=%s",
                size, limit, *correlation_ids,
            )
            raise ValueError("deepresearch_stdout_limit_exceeded")

    def validate_frame(line: bytes) -> None:
        check_limit(len(line), DEEPRESEARCH_STDOUT_TERMINAL_MAX_BYTES)
        if len(line) <= DEEPRESEARCH_STDOUT_PENDING_MAX_BYTES:
            return
        try:
            value = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
            value = None
        if not is_terminal_result_chunk(value):
            check_limit(len(line), DEEPRESEARCH_STDOUT_PENDING_MAX_BYTES)

    read = getattr(stream, "read", None)
    if not callable(read):
        async for line in stream:
            validate_frame(line)
            yield line
        return
    pending = bytearray()
    while True:
        chunk = await read(read_size)
        if not chunk:
            break
        search_from = len(pending)
        pending.extend(chunk)
        while True:
            newline = pending.find(b"\n", search_from)
            if newline < 0:
                break
            check_limit(newline, DEEPRESEARCH_STDOUT_TERMINAL_MAX_BYTES)
            line = bytes(pending[:newline])
            del pending[: newline + 1]
            validate_frame(line)
            yield line
            search_from = 0
        check_limit(len(pending), DEEPRESEARCH_STDOUT_TERMINAL_MAX_BYTES)
    if pending:
        line = bytes(pending)
        validate_frame(line)
        yield line


async def _await_with_periodic_progress(
    awaitable: Awaitable[Any],
    progress: Callable[[], Awaitable[None]],
) -> Any:
    """Wait without cancelling the underlying operation on each progress tick."""
    pending = asyncio.ensure_future(awaitable)
    try:
        while True:
            done, _ = await asyncio.wait(
                {pending},
                timeout=DEEPRESEARCH_FINAL_REPORT_PROGRESS_INTERVAL_SECONDS,
            )
            if done:
                return pending.result()
            await progress()
    finally:
        if not pending.done():
            pending.cancel()
            with suppress(BaseException):
                await pending


async def _iter_with_periodic_progress(
    lines: AsyncIterator[bytes],
    progress: Callable[[], Awaitable[None]],
) -> AsyncIterator[bytes]:
    iterator = lines.__aiter__()
    while True:
        try:
            line = await _await_with_periodic_progress(anext(iterator), progress)
        except StopAsyncIteration:
            return
        yield line


def _interaction_answer_has_user_input(item: Any) -> bool:
    if not isinstance(item, dict):
        return True
    selected = item.get("selected_options")
    if isinstance(selected, list):
        if any(
            not isinstance(option, str) or bool(option.strip())
            for option in selected
        ):
            return True
    elif selected is not None:
        return True
    custom_input = item.get("custom_input")
    if custom_input is None:
        return False
    return not isinstance(custom_input, str) or bool(custom_input.strip())


def _normalize_feedback_handler_resume_feedback(
    feedback: str, interaction_result: str
) -> str:
    if not interaction_result:
        return feedback
    try:
        result = json.loads(interaction_result)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("interaction_result 必须是合法 JSON") from exc
    if not isinstance(result, dict):
        raise ValueError("interaction_result 必须是 JSON 对象")
    status_value = str(result.get("status") or "").strip().lower()
    if status_value not in {"answered", "skipped"}:
        raise ValueError(
            f"feedback_handler interaction_result status={status_value or 'missing'}"
        )
    answers = result.get("answers", [])
    if not isinstance(answers, list):
        raise ValueError("interaction_result.answers 必须是数组")
    has_user_input = any(_interaction_answer_has_user_input(item) for item in answers)
    if status_value == "skipped" and has_user_input:
        raise ValueError("skipped interaction_result 不能包含有效回答")
    if status_value == "skipped" or not has_user_input:
        return _SKIPPED_FEEDBACK_HANDLER_PAYLOAD
    return feedback


_OUTLINE_EDITED_HEADING_RE = re.compile(r"^###\s*P(\d+):\s*(.*)$")
_OUTLINE_CORE_SUFFIX = "（重点）"
_OUTLINE_CONFIRM_CHOICES = {
    "outline_confirm",
    "确认大纲，继续研究",
    "确认",
}
_OUTLINE_EDIT_CHOICES = {
    "outline_use_edited",
    "需要修改",
    "其他",
    "Other",
}


def _resolve_outline_interaction_feedback(
    interaction_result: str,
    *,
    conversation_id: str,
    route: dict[str, object],
) -> str:
    """Resolve resume feedback for the ``outline_interaction`` node.

    Returns a JSON string ready for the SDK ``--feedback`` argument:

    * ``outline_confirm``       -> ``{"interrupt_feedback":"accepted","feedback":""}``
    * ``outline_use_edited``    -> ``{"interrupt_feedback":"revise_outline","feedback":"<outline JSON string>"}``
    * user cancel/error status  -> ``{"interrupt_feedback":"cancel","feedback":""}``

    Raises ``ValueError`` on any malformed ``interaction_result`` (fail closed);
    the caller returns the ``outline_interaction_invalid_result`` error outcome
    without spawning the subprocess.
    """
    if not interaction_result:
        raise ValueError("interaction_result is empty")
    try:
        result = json.loads(interaction_result)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("interaction_result 必须是合法 JSON") from exc
    if not isinstance(result, dict):
        raise ValueError("interaction_result 必须是 JSON 对象")

    status_value = str(result.get("status") or "").strip().lower()
    if status_value in {"cancelled", "error"}:
        return '{"interrupt_feedback":"cancel","feedback":""}'

    answers = result.get("answers")
    if status_value == "skipped":
        if isinstance(answers, list) and any(
            _interaction_answer_has_user_input(answer) for answer in answers
        ):
            raise ValueError("skipped interaction_result 不能包含有效回答")
        return '{"interrupt_feedback":"accepted","feedback":""}'

    if not isinstance(answers, list) or not answers:
        raise ValueError("interaction_result.answers 缺失或为空")
    first_answer = answers[0]
    if not isinstance(first_answer, dict):
        raise ValueError("interaction_result.answers[0] 必须是 JSON 对象")
    selected_options = first_answer.get("selected_options")
    if not isinstance(selected_options, list) or not selected_options:
        raise ValueError("selected_options 缺失或为空")
    choice = selected_options[0]

    if choice in _OUTLINE_CONFIRM_CHOICES:
        return '{"interrupt_feedback":"accepted","feedback":""}'

    if choice in _OUTLINE_EDIT_CHOICES:
        cached_outline = _get_cached_outline_json(route, conversation_id)
        sections = cached_outline.get("sections") if isinstance(cached_outline, dict) else None
        if not cached_outline or not isinstance(sections, list) or not sections:
            raise ValueError("cached Outline JSON missing or empty")

        custom_input = str(first_answer.get("custom_input") or "")
        edited_titles: dict[int, str] = {}
        for line in custom_input.split("\n"):
            match = _OUTLINE_EDITED_HEADING_RE.match(line)
            if not match:
                continue
            page_num = int(match.group(1))
            raw_title = match.group(2).rstrip()
            if raw_title.endswith(_OUTLINE_CORE_SUFFIX):
                raw_title = raw_title[: -len(_OUTLINE_CORE_SUFFIX)].rstrip()
            edited_titles[page_num] = raw_title
        if not edited_titles:
            raise ValueError("no ### P{N}: title patterns in custom_input")

        # Copy sections to avoid mutating the cache (the getter returns a
        # shallow copy whose nested ``sections`` list is still shared).
        new_sections = [dict(section) for section in sections]
        new_outline = {**cached_outline, "sections": new_sections}
        for page_num, edited_title in edited_titles.items():
            idx = page_num - 1
            if 0 <= idx < len(new_sections):
                new_sections[idx]["title"] = edited_title
            # Page count mismatch: skip out-of-range pages, do NOT add/remove
            # sections. Caller's contract: only matching indices update.

        outline_json_str = json.dumps(new_outline, ensure_ascii=False)
        return json.dumps(
            {
                "interrupt_feedback": "revise_outline",
                "feedback": outline_json_str,
            },
            ensure_ascii=False,
        )

    expected_choices = ", ".join(
        sorted(_OUTLINE_CONFIRM_CHOICES | _OUTLINE_EDIT_CHOICES)
    )
    raise ValueError(
        f"unknown selected_options[0]={choice!r}; "
        f"expected one of: {expected_choices}"
    )


def _build_interrupted_resume_hint(
    node_id: str, conversation_id: str
) -> dict[str, Any]:
    """Build the self-describing resume hint for one interrupted outcome.

    The stream tool hands the interrupted outcome back to the caller LLM
    verbatim. Without an explicit contract the model has historically
    improvised resume arguments (e.g. selected_options=["approve"]) and
    looped on validation errors instead of asking the user, so the hint
    pins the exact resume parameters for the two interactive nodes.
    """
    if node_id == "outline_interaction":
        return {
            "action": "resume",
            "node": "outline_interaction",
            "conversation_id": conversation_id,
            "interaction_result_required": True,
            "valid_selected_options": sorted(
                _OUTLINE_CONFIRM_CHOICES | _OUTLINE_EDIT_CHOICES
            ),
            "interaction_result_example": json.dumps(
                {
                    "status": "answered",
                    "answers": [
                        {
                            "question": "请审阅生成的研究报告大纲",
                            "selected_options": ["outline_confirm"],
                            "custom_input": "",
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            "guidance": (
                "The workflow paused for outline review. Present the outline "
                "to the user, collect the user's decision, then resume with "
                "action='resume', this conversation_id, "
                "node='outline_interaction', and the user's answer encoded as "
                "interaction_result (see interaction_result_example; "
                "selected_options[0] must be one of valid_selected_options). "
                "Never answer on the user's behalf or resume without the "
                "user's input."
            ),
        }
    return {
        "action": "resume",
        "node": "feedback_handler",
        "conversation_id": conversation_id,
        "feedback_required": True,
        "interaction_result_example": json.dumps(
            {
                "status": "answered",
                "answers": [
                    {
                        "question": "研究主题澄清",
                        "selected_options": [],
                        "custom_input": "用户输入的反馈内容",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        "guidance": (
            "The workflow paused for research-direction feedback. Ask the "
            "user the questions from marker.questions, collect the user's "
            "answers, then resume with action='resume', this "
            "conversation_id, node='feedback_handler', and the user's answer "
            "text as feedback. interaction_result is optional and only "
            "records whether the user actually answered. Never answer on "
            "the user's behalf or resume without the user's input."
        ),
    }


def _resolve_skill_root() -> str:
    """Resolve only from configured shared skill roots."""
    task_overlay = get_task_env_overlay()
    if task_overlay is None:
        route = _get_route()
        source = build_effective_env_overlay(
            service_id=str(route["service_id"]),
            agent_id=str(route["agent_id"]),
        )
        roots = parse_shared_skills_dirs_raw(
            str(source.get(JIUWENSWARM_SHARED_SKILLS_DIRS_ENV) or "")
        )
    else:
        roots = get_shared_agent_skills_dirs()
    for root in roots:
        candidate = Path(root) / "deepresearch"
        runner = candidate / "scripts" / "run_deepsearch.py"
        try:
            metadata = runner.lstat()
        except OSError:
            continue
        if (
            stat.S_ISREG(metadata.st_mode)
            and not stat.S_ISLNK(metadata.st_mode)
            and metadata.st_nlink == 1
        ):
            return str(candidate)
    return ""


def _resolve_run_script() -> str:
    root = _resolve_skill_root()
    if not root:
        return ""
    runner = Path(root) / "scripts" / "run_deepsearch.py"
    try:
        metadata = runner.lstat()
    except OSError:
        return ""
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_nlink != 1
    ):
        return ""
    return str(runner)


def _read_checkpoint_kv_value(cursor: Any, key: str) -> Any:
    cursor.execute("SELECT value FROM kv_store WHERE key = ?", (key,))
    row = cursor.fetchone()
    return row[0] if row else None


def _decode_checkpoint_blob(raw: Any) -> Any:
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    if not isinstance(raw, str) or not raw:
        return None
    payload = raw.split(":", 1)[1] if raw.startswith("__BYTES__:") else raw
    try:
        return pickle.loads(base64.b64decode(payload))
    except Exception:  # pylint: disable=broad-exception-caught
        return None


_CHECKPOINT_ERROR_STATUS = "__error__"
# 扫库认领候选窗口上限：宽松取值，防 LIMIT 过紧把本会话目标挤出窗口
# （服务级全局单库，窗口内可能混有其他会话快照）造成漏认领。
_SCAN_CHECKPOINT_KEY_LIMIT = 500
# 与 deepsearch stop_control.STOP_REQUESTED_MARKER 保持一致:StopRequested
# 经 __reduce__ 降级为携带该标记的 RuntimeError 后跨进程可反序列化。
_CHECKPOINT_STOP_MARKER = "deepresearch task stopped by user"


def _is_stop_requested_exception(exc: Any) -> bool:
    """识别 pending_node.exception 中的停止请求(类型名或消息标记)。

    CancelledError 同样按可续跑快照处理:SDK 在取消路径会为被取消节点
    写入 __error__+CancelledError 快照,恢复语义与用户停止一致(从中断
    节点续跑,已完成节点不重跑)。
    """
    if not isinstance(exc, BaseException):
        return False
    if type(exc).__name__ == "StopRequested":
        return True
    if type(exc).__name__ == "CancelledError":
        return True
    return _CHECKPOINT_STOP_MARKER in str(exc)


def _parse_resumable_snapshot(graph_state: Any) -> dict[str, Any] | None:
    """解析 graph 快照的 pending_node,判定可续跑状态(stopped/interrupt)。

    两遍扫描:停止快照优先。用户已在某个后续环节手动停止时,
    pending_node 中可能残留早期交互(如 outline_interaction)的
    stale interrupt 条目;若 interrupt 优先,会把已停止的会话错认
    为中断认领,拉回早期交互重弹卡。先全量识别停止快照,未命中
    才认领 interrupt。

    返回 {"node", "stopped": True}(停止快照,调用方直接 resume
    node='stopped' 无需重弹卡)或 {"node"}(interrupt 快照);
    无可续跑状态返回 None。
    """
    pending_nodes = getattr(graph_state, "pending_node", None) or {}
    for node_name, pending in pending_nodes.items():
        if str(getattr(pending, "status", "")) != _CHECKPOINT_ERROR_STATUS:
            continue
        # 停止快照:StopRequested 异常经 __reduce__ 降级为带停止
        # 标记的 RuntimeError,父进程无需安装 deepsearch 包即可识别。
        exceptions = getattr(pending, "exception", None) or []
        if any(_is_stop_requested_exception(exc) for exc in exceptions):
            return {"node": str(node_name), "stopped": True}
    for node_name, pending in pending_nodes.items():
        if str(getattr(pending, "status", "")) == _CHECKPOINT_INTERRUPT_STATUS:
            return {"node": str(node_name)}
    return None


def _read_snapshot_workflow_state(cursor: Any, cid: str) -> Mapping[str, Any]:
    """读取并解码会话 workflow_state 快照；不可用返回空 Mapping。

    归属字段(global_state.config.host_session_id)与重弹卡字段
    (global_state.search_context)同源于 workflow_state，单次解码即可
    同时供归属校验与 search_context 读取（旧实现两处各解码一次，
    扫库/probe 每候选重复解码两遍）。
    """
    workflow_state = _decode_checkpoint_blob(
        _read_checkpoint_kv_value(
            cursor,
            f"{cid}:workflow:{_CHECKPOINT_WORKFLOW_ID}:workflow_state_blobs",
        )
    )
    return workflow_state if isinstance(workflow_state, Mapping) else {}


def _snapshot_global_state(
    workflow_state: Mapping[str, Any],
) -> Mapping[str, Any]:
    global_state = workflow_state.get("global_state")
    return global_state if isinstance(global_state, Mapping) else {}


def _global_state_search_context(
    global_state: Mapping[str, Any],
) -> Mapping[str, Any]:
    candidate = global_state.get("search_context")
    return candidate if isinstance(candidate, Mapping) else {}


def _global_state_host_session_id(
    global_state: Mapping[str, Any],
) -> str | None:
    """读取快照 global_state.config.host_session_id；字段缺失返回 None。

    归属字段由宿主随 config frame 注入(DEEPSEARCH_HOST_SESSION_ID)，
    SDK StartNode 白名单写入会话 config 随快照落盘；resume 不重跑
    StartNode，故归属始终保持原始创建者。
    """
    config = global_state.get("config")
    if not isinstance(config, Mapping) or "host_session_id" not in config:
        return None
    return str(config.get("host_session_id") or "")


def _owner_matches_current_session(owner: str | None) -> tuple[bool, str]:
    """快照归属判定：防跨会话/跨用户误认领陈旧快照。

    实机事故(2026-10-13 20:42):新会话跑同 query,扫库兜底命中 10-10
    陈旧快照,误判"用户在继续那次研究"而 resume,快照冻结的旧模型配置
    与当前进程脱钩,5 个章节子代理零 LLM 空转,13.4s 失败。checkpointer
    为服务级全局单库且快照无宿主身份字段,故认领侧必须显式校验归属。

    返回 (matches, reason):
    - owner == 当前宿主会话 → (True, "match") 正常认领
    - owner 缺失/为空 → (False, "missing_owner") 旧快照一律拒绝(拍板)
    - owner 与当前不同 → (False, "ownership_mismatch") 跨会话拒绝
    - 当前请求无 session_id → (False, "requester_unverifiable") 严格拒绝
    """
    current = str(_get_route().get("session_id") or "").strip()
    if not current:
        return False, "requester_unverifiable"
    if owner is None or not owner.strip():
        return False, "missing_owner"
    if owner.strip() != current:
        return False, "ownership_mismatch"
    return True, "match"


def _log_ownership_reject(
    source: str, cid: str, reason: str, node: object, query: object = None
) -> None:
    """归属拒绝观察哨:与 scan-claim matched 同风格,供实机审计。"""
    if query is None:
        logger.warning(
            "deepresearch ownership rejected source=%s conversation_id=%s "
            "reason=%s node=%s",
            source,
            cid,
            reason,
            node,
        )
    else:
        logger.warning(
            "deepresearch ownership rejected source=%s conversation_id=%s "
            "reason=%s node=%s query=%r",
            source,
            cid,
            reason,
            node,
            query,
        )


def _extract_interaction_fields(
    search_context: Mapping[str, Any],
) -> dict[str, Any]:
    """从 search_context 提取重弹交互卡所需字段(questions/outline_json)。"""
    result: dict[str, Any] = {}
    questions = search_context.get("questions")
    if isinstance(questions, str) and questions.strip():
        result["questions"] = [
            question.strip()
            for question in _QUESTION_PARAGRAPH_SPLIT.split(questions.strip())
            if question.strip()
        ]
    outline = search_context.get("current_outline")
    sections = []
    for section in getattr(outline, "sections", None) or []:
        sections.append(
            {
                "title": str(getattr(section, "title", "") or ""),
                "is_core_section": bool(
                    getattr(section, "is_core_section", False)
                ),
            }
        )
    if outline is not None and sections:
        result["outline_json"] = {
            "title": str(getattr(outline, "title", "") or ""),
            "thought": str(getattr(outline, "thought", "") or ""),
            "sections": sections,
        }
    return result


def _open_checkpoint_db_ro() -> sqlite3.Connection | None:
    """以只读模式打开 checkpointer.db,不可用返回 None。"""
    root = _resolve_skill_root()
    if not root:
        return None
    db_path = os.path.join(root, "data", "checkpointer.db")
    if not os.path.isfile(db_path):
        return None
    try:
        return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=1.0)
    except sqlite3.Error:
        return None


def _probe_resumable_checkpoint(conversation_id: str) -> dict[str, Any] | None:
    """Probe checkpointer.db for an interrupted DeepResearch conversation.

    Reads only the pending interrupt node plus the search-context fields
    needed to re-present an interaction card after a process restart.
    Also recognizes graceful-stop snapshots (``__error__`` pending nodes
    whose exception is a stop request) so the caller can resume them
    directly with ``node='stopped'`` without re-presenting any card.
    Returns None whenever the conversation has no resumable state, so a
    failed probe simply falls back to a fresh start.
    """
    cid = str(conversation_id or "").strip()
    if not cid:
        return None
    connection = _open_checkpoint_db_ro()
    if connection is None:
        return None
    try:
        cursor = connection.cursor()
        graph_state = _decode_checkpoint_blob(
            _read_checkpoint_kv_value(
                cursor,
                f"{cid}:workflow-graph:{_CHECKPOINT_WORKFLOW_ID}:checkpoint_data_value",
            )
        )
        if graph_state is None:
            return None
        snapshot = _parse_resumable_snapshot(graph_state)
        if snapshot is None:
            return None
        # workflow_state 单次解码：归属字段与 search_context 同源共用。
        global_state = _snapshot_global_state(
            _read_snapshot_workflow_state(cursor, cid)
        )
        # 归属校验:快照 host_session_id 必须等于当前宿主会话,否则拒绝
        # 认领(不匹配/旧快照缺字段均拒绝,防 LLM 幻觉 cid 撞别家会话)。
        matches, reason = _owner_matches_current_session(
            _global_state_host_session_id(global_state)
        )
        if not matches:
            _log_ownership_reject("probe", cid, reason, snapshot.get("node"))
            return None
        if snapshot.get("stopped"):
            # 优雅停止快照:无需重弹交互卡,由调用方直接 resume node='stopped'
            # (node 仅用于日志诊断)。
            return snapshot
        result: dict[str, Any] = dict(snapshot)
        result.update(
            _extract_interaction_fields(
                _global_state_search_context(global_state)
            )
        )
        return result
    except Exception:  # pylint: disable=broad-exception-caught
        return None
    finally:
        with suppress(sqlite3.Error):
            connection.close()


def _scan_resumable_checkpoint_by_query(query: str) -> dict[str, Any] | None:
    """修复9':不信任 LLM 传参 cid,按入参 query 扫库认领可续跑会话。

    LLM 续跑传参可能幻觉 conversation_id(实机事故:两次幻觉 id 均在
    认领入口失效,probe 落空后直接全新起跑,丢弃已停止会话的全部研究
    成果)。本函数在 probe(cid) 落空时兜底:枚举 kv_store 全部 graph
    快照键(rowid 倒序——kv_store 无时间戳列,rowid 随插入递增,最新
    落盘快照即用户最近操作的会话,优先认领),逐键解码判定可续跑状态,
    以 workflow_state 的 search_context.original_query 与入参 query
    完全一致作为认领条件(换主题不认领旧会话),并校验快照
    config.host_session_id 归属当前宿主会话(不匹配/旧快照缺字段均
    跳过该候选,防跨会话误认领)。

    注意匹配语义与 probe 场景的差异:probe 时 cid 是可靠线索、query
    仅作意图确认(原 query 为空即认领);扫库时 query 是唯一线索,
    original_query 缺失或不同一律不认领,避免认错会话。

    返回 {"conversation_id", "node", "stopped"}(interrupt 命中时附加
    questions/outline_json 重弹卡字段),未命中返回 None。单键解码
    失败不阻断扫描。
    """
    incoming = str(query or "").strip()
    if not incoming:
        return None
    connection = _open_checkpoint_db_ro()
    if connection is None:
        return None
    try:
        cursor = connection.cursor()
        # 性能护栏：扫库现挂 stream action='start' 热路径（每次全新起跑
        # 前都会扫一遍），LIMIT 截断无界全表扫描。窗口取宽松值——服务级
        # 全局单库下窗口内可能混有其他会话快照，过紧会把本会话目标挤出
        # 窗口造成漏认领（退回"丢全部研究成果"事故），500 个候选足够
        # 容纳单机会话积压。
        cursor.execute(
            "SELECT key FROM kv_store WHERE key LIKE ? "
            "ORDER BY rowid DESC LIMIT ?",
            (
                f"%:workflow-graph:{_CHECKPOINT_WORKFLOW_ID}"
                ":checkpoint_data_value",
                _SCAN_CHECKPOINT_KEY_LIMIT,
            ),
        )
        keys = [str(row[0] or "") for row in cursor.fetchall()]
        for key in keys:
            cid = key.split(":workflow-graph:", 1)[0].strip()
            if not cid:
                continue
            # 先轻后重：workflow_state（单次解码，归属+主题双过滤）先行，
            # foreign/换主题候选不再解码重量级 graph 快照（旧实现每候选
            # 必解码 graph，且 workflow_state 被归属与 search_context
            # 两处重复解码）。
            global_state = _snapshot_global_state(
                _read_snapshot_workflow_state(cursor, cid)
            )
            # 主题过滤先行（与旧实现口径一致：换主题静默跳过，不打观察哨）。
            original = str(
                _global_state_search_context(global_state).get(
                    "original_query"
                )
                or ""
            ).strip()
            if original != incoming:
                continue
            # 归属校验:query 一致不代表是本会话的研究(实机事故:新会话同
            # query 命中陈旧快照误认领);owner 不匹配/缺失继续扫下一个候选。
            # （先轻后重重排后归属拒绝发生在 graph 解码前，观察哨日志的
            # node 字段为空——拒绝原因与 cid 不受影响。）
            matches, reason = _owner_matches_current_session(
                _global_state_host_session_id(global_state)
            )
            if not matches:
                _log_ownership_reject("scan", cid, reason, "", incoming)
                continue
            # 双过滤通过才解码重量级 graph 快照，判定可续跑状态。
            graph_state = _decode_checkpoint_blob(
                _read_checkpoint_kv_value(cursor, key)
            )
            if graph_state is None:
                continue
            snapshot = _parse_resumable_snapshot(graph_state)
            if snapshot is None:
                continue
            result: dict[str, Any] = {"conversation_id": cid, **snapshot}
            if not snapshot.get("stopped"):
                result.update(
                    _extract_interaction_fields(
                        _global_state_search_context(global_state)
                    )
                )
            # 审计观察哨:扫库认领命中即记录命中对象与快照新旧,便于实机
            # 排查"认错陈旧/跨会话快照"类事故(扫库是 probe 落空后的兜底
            # 路径,主路径不应走到这里;命中陈旧对象往往意味着停止快照
            # 被误删或 probe cid 脱钩)。
            logger.warning(
                "deepresearch scan-claim matched conversation_id=%s node=%s "
                "stopped=%s query=%r",
                cid,
                snapshot.get("node"),
                bool(snapshot.get("stopped")),
                incoming,
            )
            return result
        return None
    except Exception:  # pylint: disable=broad-exception-caught
        return None
    finally:
        with suppress(sqlite3.Error):
            connection.close()


def _get_effective_request_output_dir() -> Path:
    route = _deepresearch_route_ctx.get() or {}
    output_dir = route.get("output_dir")
    if isinstance(output_dir, str) and output_dir:
        return Path(os.path.abspath(os.path.expanduser(output_dir)))
    return Path(get_cwd()).expanduser().resolve()


def _create_progress_artifact() -> _ProgressArtifact:
    """Create a private, pre-owned progress file before child startup."""
    directory = Path(tempfile.mkdtemp(prefix="deepresearch-progress-"))
    try:
        directory_metadata = directory.lstat()
        if (
            not is_direct_directory(directory_metadata)
            or not private_mode_is_compatible(directory_metadata, 0o700)
        ):
            raise OSError("unsafe DeepResearch progress directory")
        path = directory / "progress.jsonl"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags, 0o600)
        try:
            file_metadata = os.fstat(descriptor)
            if (
                not is_direct_regular_file(file_metadata)
                or file_metadata.st_nlink != 1
                or not private_mode_is_compatible(file_metadata, 0o600)
            ):
                raise OSError("unsafe DeepResearch progress file")
        finally:
            os.close(descriptor)
        return _ProgressArtifact(
            path=path,
            directory_identity=(
                directory_metadata.st_dev,
                directory_metadata.st_ino,
            ),
            file_identity=(file_metadata.st_dev, file_metadata.st_ino),
        )
    except BaseException:
        with suppress(OSError):
            (directory / "progress.jsonl").unlink()
        with suppress(OSError):
            directory.rmdir()
        raise


def _remove_progress_artifact(artifact: _ProgressArtifact) -> None:
    """Remove only the exact private file and directory created by this call."""
    path = artifact.path
    try:
        metadata = path.lstat()
    except OSError:
        metadata = None
    owned_file = (
        metadata is not None
        and stat.S_ISREG(metadata.st_mode)
        and not stat.S_ISLNK(metadata.st_mode)
        and metadata.st_nlink == 1
        and (metadata.st_dev, metadata.st_ino) == artifact.file_identity
    )
    if owned_file:
        with suppress(OSError):
            path.unlink()
    try:
        directory_metadata = path.parent.lstat()
    except OSError:
        return
    if (
        stat.S_ISDIR(directory_metadata.st_mode)
        and not stat.S_ISLNK(directory_metadata.st_mode)
        and (directory_metadata.st_dev, directory_metadata.st_ino)
        == artifact.directory_identity
    ):
        with suppress(OSError):
            path.parent.rmdir()


def _validate_zip_member(member_name: str) -> str:
    if not isinstance(member_name, str) or not member_name or "\x00" in member_name:
        raise ValueError("unsafe ZIP member")
    normalized = member_name.replace("\\", "/")
    posix = PurePosixPath(normalized)
    windows = PureWindowsPath(member_name)
    invalid_member = (
        posix.is_absolute()
        or windows.is_absolute()
        or windows.drive
        or any(part in {"", ".", ".."} for part in posix.parts)
    )
    if invalid_member:
        raise ValueError("unsafe ZIP member")
    return normalized


def _zip_namespace_key(normalized: str) -> str:
    return "/".join(
        unicodedata.normalize("NFC", part).casefold()
        for part in PurePosixPath(normalized).parts
    )


def _write_all(descriptor: int, payload: bytes) -> int:
    view = memoryview(payload)
    offset = 0
    while offset < len(view):
        written = os.write(descriptor, view[offset:])
        if written <= 0:
            raise OSError("ZIP output write failed")
        offset += written
    return offset


def _clear_owned_directory_fd(descriptor: int) -> None:
    """Remove contents through an already-open private directory handle."""
    for name in os.listdir(descriptor):
        metadata = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        if stat.S_ISDIR(metadata.st_mode) and not stat.S_ISLNK(metadata.st_mode):
            flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            child = os.open(name, flags, dir_fd=descriptor)
            try:
                opened = os.fstat(child)
                if (opened.st_dev, opened.st_ino) != (
                    metadata.st_dev,
                    metadata.st_ino,
                ):
                    raise OSError("ZIP rollback directory changed")
                _clear_owned_directory_fd(child)
            finally:
                os.close(child)
            os.rmdir(name, dir_fd=descriptor)
        else:
            os.unlink(name, dir_fd=descriptor)


def _rollback_owned_zip_destination(
    *,
    destination: Path,
    parent_descriptor: int,
    destination_descriptor: int,
    identity: tuple[int, int],
) -> None:
    """Clear the opened tree, then quarantine only an identity-matching name."""
    with suppress(OSError):
        _clear_owned_directory_fd(destination_descriptor)

    quarantine = f".deepresearch-rollback-{uuid.uuid4().hex}"
    try:
        os.rename(
            destination.name,
            quarantine,
            src_dir_fd=parent_descriptor,
            dst_dir_fd=parent_descriptor,
        )
    except OSError:
        return

    try:
        moved = os.stat(
            quarantine, dir_fd=parent_descriptor, follow_symlinks=False
        )
        if (
            not stat.S_ISDIR(moved.st_mode)
            or stat.S_ISLNK(moved.st_mode)
            or (moved.st_dev, moved.st_ino) != identity
        ):
            try:
                os.rename(
                    quarantine,
                    destination.name,
                    src_dir_fd=parent_descriptor,
                    dst_dir_fd=parent_descriptor,
                )
            except OSError:
                pass
            return
        os.rmdir(quarantine, dir_fd=parent_descriptor)
    except OSError:
        return


def _clear_owned_directory_path(directory: Path) -> None:
    for child in directory.iterdir():
        metadata = child.lstat()
        if stat.S_ISDIR(metadata.st_mode) and not stat.S_ISLNK(metadata.st_mode):
            _clear_owned_directory_path(child)
            child.rmdir()
        else:
            child.unlink()


def _rollback_owned_zip_destination_path(
    destination: Path, identity: tuple[int, int]
) -> None:
    """Windows best effort: identity checks around named path cleanup."""
    try:
        metadata = destination.lstat()
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or (metadata.st_dev, metadata.st_ino) != identity
        ):
            return
        _clear_owned_directory_path(destination)
        after = destination.lstat()
        if (
            stat.S_ISDIR(after.st_mode)
            and not stat.S_ISLNK(after.st_mode)
            and (after.st_dev, after.st_ino) == identity
        ):
            destination.rmdir()
    except OSError:
        return


def _extract_validated_zip_members(
    archive: zipfile.ZipFile,
    validated: list[tuple[zipfile.ZipInfo, str, bool]],
    destination: Path,
) -> None:
    for member, normalized, is_directory in validated:
        target = destination.joinpath(*PurePosixPath(normalized).parts)
        if is_directory:
            target.mkdir(mode=0o700, parents=True, exist_ok=True)
            continue
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(target, flags, 0o600)
        written = 0
        try:
            with archive.open(member, "r") as source:
                while True:
                    chunk = source.read(64 * 1024)
                    if not chunk:
                        break
                    written += _write_all(descriptor, chunk)
                    if written > member.file_size:
                        raise ValueError("styled report archive size changed")
            if written != member.file_size:
                raise ValueError("styled report archive size changed")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _open_zip_directory_at(parent_descriptor: int, name: str) -> int:
    try:
        os.mkdir(name, mode=0o700, dir_fd=parent_descriptor)
    except FileExistsError:
        pass
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(name, flags, dir_fd=parent_descriptor)
    metadata = os.fstat(descriptor)
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        os.close(descriptor)
        raise OSError("unsafe ZIP output directory")
    return descriptor


def _open_zip_directory_chain(
    destination_descriptor: int, parts: tuple[str, ...]
) -> int:
    current = os.dup(destination_descriptor)
    try:
        for part in parts:
            child = _open_zip_directory_at(current, part)
            os.close(current)
            current = child
        return current
    except BaseException:
        os.close(current)
        raise


def _extract_validated_zip_members_fd(
    archive: zipfile.ZipFile,
    validated: list[tuple[zipfile.ZipInfo, str, bool]],
    destination_descriptor: int,
) -> None:
    """Extract only through openat-style operations rooted at the held fd."""
    for member, normalized, is_directory in validated:
        parts = PurePosixPath(normalized).parts
        if is_directory:
            directory = _open_zip_directory_chain(
                destination_descriptor, parts
            )
            os.close(directory)
            continue

        parent = _open_zip_directory_chain(
            destination_descriptor, parts[:-1]
        )
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = -1
        try:
            descriptor = os.open(parts[-1], flags, 0o600, dir_fd=parent)
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise OSError("unsafe ZIP output file")
            written = 0
            with archive.open(member, "r") as source:
                while True:
                    chunk = source.read(64 * 1024)
                    if not chunk:
                        break
                    written += _write_all(descriptor, chunk)
                    if written > member.file_size:
                        raise ValueError("styled report archive size changed")
            if written != member.file_size:
                raise ValueError("styled report archive size changed")
            os.fsync(descriptor)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            os.close(parent)


def _verify_named_zip_destination(
    parent_descriptor: int, destination_name: str, identity: tuple[int, int]
) -> None:
    try:
        metadata = os.stat(
            destination_name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
    except OSError as exc:
        raise OSError("ZIP destination changed") from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or (metadata.st_dev, metadata.st_ino) != identity
    ):
        raise OSError("ZIP destination changed")


def _extract_styled_bundle(convert_content: str, destination: Path) -> Path:
    """Validate the entire archive before manually extracting regular files."""
    if not isinstance(convert_content, str):
        raise ValueError("invalid styled report base64 payload")
    if len(convert_content.encode("ascii", errors="ignore")) > DEEPRESEARCH_ZIP_INPUT_MAX_BYTES:
        raise ValueError("styled report archive exceeds input limit")
    try:
        archive_bytes = base64.b64decode(convert_content, validate=True)
    except ValueError as exc:
        raise ValueError("invalid styled report base64 payload") from exc
    if len(archive_bytes) > DEEPRESEARCH_ZIP_INPUT_MAX_BYTES:
        raise ValueError("styled report archive exceeds input limit")
    destination = Path(destination)
    with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
        members = archive.infolist()
        if len(members) > DEEPRESEARCH_ZIP_MAX_MEMBERS:
            raise ValueError("styled report archive member limit exceeded")
        normalized_names: set[str] = set()
        normalized_files: set[str] = set()
        total_size = 0
        validated: list[tuple[zipfile.ZipInfo, str, bool]] = []
        for member in members:
            normalized = _validate_zip_member(member.filename)
            collision_key = _zip_namespace_key(normalized)
            if collision_key in normalized_names:
                raise ValueError("duplicate ZIP member")
            mode = (member.external_attr >> 16) & 0xFFFF
            is_directory = member.is_dir()
            file_type = stat.S_IFMT(mode)
            allowed_types = {0, stat.S_IFDIR if is_directory else stat.S_IFREG}
            if file_type not in allowed_types:
                raise ValueError("unsafe ZIP special member")
            parts = collision_key.split("/")
            ancestors = {"/".join(parts[:index]) for index in range(1, len(parts))}
            if ancestors & normalized_files or (
                not is_directory
                and any(name.startswith(collision_key + "/") for name in normalized_names)
            ):
                raise ValueError("unsafe ZIP namespace conflict")
            normalized_names.add(collision_key)
            if not is_directory:
                normalized_files.add(collision_key)
            if member.file_size > DEEPRESEARCH_ZIP_MEMBER_MAX_BYTES:
                raise ValueError("styled report archive member limit exceeded")
            total_size += member.file_size
            if total_size > DEEPRESEARCH_ZIP_TOTAL_MAX_BYTES:
                raise ValueError("styled report archive total limit exceeded")
            validated.append((member, normalized, is_directory))
        if "report_bundle/report.html" not in {
            name for _, name, _ in validated
        }:
            raise ValueError(
                "styled report bundle is missing report_bundle/report.html"
            )
        if _uses_windows_path_publication():
            parent_metadata = destination.parent.lstat()
            if (
                not stat.S_ISDIR(parent_metadata.st_mode)
                or stat.S_ISLNK(parent_metadata.st_mode)
            ):
                raise OSError("ZIP destination parent changed")
            parent_identity = (
                parent_metadata.st_dev,
                parent_metadata.st_ino,
            )
            destination.mkdir(mode=0o700, parents=True, exist_ok=False)
            metadata = destination.lstat()
            identity = (metadata.st_dev, metadata.st_ino)
            try:
                _extract_validated_zip_members(
                    archive, validated, destination
                )
                after_parent = destination.parent.lstat()
                after_destination = destination.lstat()
                destination_changed = (
                    not stat.S_ISDIR(after_parent.st_mode)
                    or stat.S_ISLNK(after_parent.st_mode)
                    or (after_parent.st_dev, after_parent.st_ino)
                    != parent_identity
                    or not stat.S_ISDIR(after_destination.st_mode)
                    or stat.S_ISLNK(after_destination.st_mode)
                    or (after_destination.st_dev, after_destination.st_ino)
                    != identity
                )
                if destination_changed:
                    raise OSError("ZIP destination changed")
            except BaseException:
                _rollback_owned_zip_destination_path(destination, identity)
                raise
            return destination / "report_bundle"

        parent_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        parent_flags |= getattr(os, "O_NOFOLLOW", 0)
        parent_descriptor = os.open(destination.parent, parent_flags, mode=0o700)
        try:
            os.mkdir(destination.name, mode=0o700, dir_fd=parent_descriptor)
            destination_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            destination_flags |= getattr(os, "O_NOFOLLOW", 0)
            destination_descriptor = os.open(
                destination.name,
                destination_flags,
                dir_fd=parent_descriptor,
            )
            try:
                destination_metadata = os.fstat(destination_descriptor)
                destination_identity = (
                    destination_metadata.st_dev,
                    destination_metadata.st_ino,
                )
                try:
                    _extract_validated_zip_members_fd(
                        archive, validated, destination_descriptor
                    )
                    _verify_named_zip_destination(
                        parent_descriptor,
                        destination.name,
                        destination_identity,
                    )
                except BaseException:
                    _rollback_owned_zip_destination(
                        destination=destination,
                        parent_descriptor=parent_descriptor,
                        destination_descriptor=destination_descriptor,
                        identity=destination_identity,
                    )
                    raise
            finally:
                os.close(destination_descriptor)
        finally:
            os.close(parent_descriptor)
    return destination / "report_bundle"


def _extract_styled_archive(archive_path: Path, destination: Path) -> Path:
    """Reuse the hardened Task 5 extractor for a bridge-produced ZIP."""
    archive_bytes = _read_regular_file(
        Path(archive_path),
        limit=DEEPRESEARCH_ZIP_TOTAL_MAX_BYTES,
        label="styled report archive",
    )
    return _extract_styled_bundle(
        base64.b64encode(archive_bytes).decode("ascii"), destination
    )


def _normalize_citation_artifacts(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    normalized = {}
    for key in ("raw_report_path", "citations_preview_path"):
        item = value.get(key)
        if isinstance(item, str) and item.strip():
            normalized[key] = item.strip()
    return normalized


def _normalize_response_content_type(
    final_result: dict[str, Any],
) -> str | None:
    """Classify the final body without guessing from its contents."""
    value = final_result.get("response_content_type")
    if value is None or value == "":
        return "text/markdown"
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    if not normalized:
        return "text/markdown"
    if normalized in {"text/markdown", "text/html"}:
        return normalized
    return None


def _build_related_artifact_bundle(
    value: object, markdown_index: int
) -> dict[str, object] | None:
    artifacts = _normalize_citation_artifacts(value)
    related: list[dict[str, object]] = []
    if "raw_report_path" in artifacts:
        related.append(
            {
                "type": "raw_report",
                "path": artifacts["raw_report_path"],
                "contentType": "text/markdown",
                "relatedToPathIndex": markdown_index,
            }
        )
    if "citations_preview_path" in artifacts:
        related.append(
            {
                "type": "citations_preview",
                "path": artifacts["citations_preview_path"],
                "contentType": "application/json",
                "schemaVersion": "1.1",
                "relatedToPathIndex": markdown_index,
            }
        )
    return (
        {"schemaVersion": "1.0", "relatedArtifacts": related}
        if related
        else None
    )


@contextmanager
def _report_output_lock(output_dir: Path):
    with _REPORT_OUTPUT_LOCKS_GUARD:
        keyed = _REPORT_OUTPUT_LOCKS.get(output_dir)
        if keyed is None:
            keyed = _KeyedLock(threading.Lock())
            _REPORT_OUTPUT_LOCKS[output_dir] = keyed
        keyed.references += 1
    keyed.lock.acquire()
    try:
        yield
    finally:
        keyed.lock.release()
        with _REPORT_OUTPUT_LOCKS_GUARD:
            keyed.references -= 1
            if keyed.references == 0 and _REPORT_OUTPUT_LOCKS.get(output_dir) is keyed:
                _REPORT_OUTPUT_LOCKS.pop(output_dir, None)


def _has_completed_final_pipeline_node(state: RouterState) -> bool:
    return any(
        isinstance(node_state, dict)
        and node_state.get("agent_name") in FINAL_REPORT_NODES
        and node_state.get("done") is True
        for node_state in state.active_nodes.values()
    )


def _claim_interrupted_prompt(
    node: str, marker: Mapping[str, Any], query: str
) -> str:
    """合成 interrupted outcome 的 prompt：按 node 取交互正文，缺省回落 query。"""
    if node == "outline_interaction" and isinstance(marker.get("outline"), dict):
        formatted = _format_outline_card_markdown(marker["outline"])
        return formatted or json.dumps(marker["outline"], ensure_ascii=False)
    questions = marker.get("questions")
    if isinstance(questions, list) and questions:
        return "\n\n".join(str(question) for question in questions)
    return str(query or "").strip() or "请输入反馈"


async def _claim_start_snapshot(query: str) -> dict[str, Any] | None:
    """stream action='start' 的扫库认领兜底入口。

    同步 SQLite 扫描经 to_thread 卸载避免阻塞事件循环（contextvar 经
    copy_context 随线程传播，_get_route 归属校验线程内安全）；同时作为
    测试桩点——execute 侧用例默认把本函数打桩为未命中，防止真实扫库
    读开发机共享 checkpointer.db 造成机器相关行为（扫库逻辑本身由
    _scan_resumable_checkpoint_by_query 的专项用例覆盖）。
    """
    if not str(query or "").strip():
        return None
    return await asyncio.to_thread(_scan_resumable_checkpoint_by_query, query)


def _synthesize_claim_interrupted_outcome(
    claim: Mapping[str, Any], route: Mapping[str, Any], query: str
) -> dict[str, Any]:
    """扫库认领交互中断快照时本地合成 interrupted 终局（不经子进程）。

    停止可能落在交互卡待答复期间（快照为 __interrupt__ 而非停止态）：
    交互内容已由扫库从 workflow_state 带出，直接合成标准 interrupted
    outcome 交外层模型重弹卡，避免空反馈拉起子进程再中断一圈。
    """
    cid = str(claim.get("conversation_id") or "")
    node = str(claim.get("node") or "")
    marker: dict[str, Any] = {}
    questions = claim.get("questions")
    if isinstance(questions, list) and questions:
        marker["questions"] = questions
    outline_json = claim.get("outline_json")
    if isinstance(outline_json, dict):
        marker["outline"] = outline_json
        # 与流式 interrupt 路径同款缓存：outline 编辑回传与 execute 认领
        # 兜底都依赖该缓存（缺 sections 时下游 fail-closed）。
        _cache_outline_json(route, cid, outline_json)
    return {
        "status": "interrupted",
        "conversation_id": cid,
        "node_id": node,
        "marker": marker,
        "prompt": _claim_interrupted_prompt(node, marker, query),
        "resume_hint": _build_interrupted_resume_hint(node, cid),
        "skill_execution_ms": 0,
    }




async def _push_materials_progress(content: str) -> None:
    """Use the existing trusted route for bounded preparation status only."""
    route = _get_route()
    if not route.get("session_id") or not route.get("channel_id"):
        return
    try:
        await WebSocketGatewayPushTransport().send_push({
            "request_id": route.get("request_id", ""), "channel_id": route["channel_id"],
            "session_id": route["session_id"], "is_complete": False,
            "payload": {"event_type": "chat.reasoning", "content": content},
        })
    except Exception:  # Progress transport cannot change material failure semantics.
        logger.warning("[deepresearch] material progress push unavailable")


_materials_reference_ctx: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "deepresearch_materials_reference", default=None)
_mcp_servers_ctx: contextvars.ContextVar[list[dict] | None] = contextvars.ContextVar(
    "deepresearch_mcp_servers", default=None)


# Both dedicated entry points must hand new research back to the same Skill.
MODEL_RESEARCH_FALLBACK_ERRORS = frozenset({
    "runner_missing", "runtime_python_missing", "runtime_python_invalid", "search_config_missing",
})
MODEL_RESEARCH_FALLBACK_HINT = (
    "专用研究能力在启动前不可用，尚未启动研究或交付报告。"
    '下一步必须重新调用 skill_tool(skill_name="deepresearch", relative_file_path="SKILL.md")，'
    "从最新版 SKILL.md 的‘模型研究路径’第 1 步‘明确问题与范围’重新开始，"
    "由当前主模型完整执行各步，最后保存并交付 Markdown。"
    "不要用历史经验中的‘多子代理并行检索后汇总’替代该流程，"
    "不要因降级自动派发子代理；历史经验与当前 Skill 冲突时以当前 Skill 为准。"
    "不要重试 deepresearch_execute 或 deepresearch_stream，"
    "不要把内部错误、接手通知或执行过程写进最终报告。"
)


def build_model_research_fallback(query: str, file_name: str) -> dict[str, str]:
    """Point the main model at Skill step 1 rather than prescribing another plan."""
    return {
        "mode": "model_research", "query": query, "file_name": file_name,
        "restart_from": "明确问题与范围",
    }


def _startup_error_outcome(
    action: str, error_code: str, error: str, query: str, file_name: str,
) -> str:
    outcome = {"status": "error", "error_code": error_code, "error": error}
    if action == "start" and query.strip() and error_code in MODEL_RESEARCH_FALLBACK_ERRORS:
        outcome["fallback"] = build_model_research_fallback(query, file_name)
        outcome["content"] = MODEL_RESEARCH_FALLBACK_HINT
    return json.dumps(outcome, ensure_ascii=False)


async def _call_deepresearch_stream_impl(
    *, _router_state: object | None = None, _materials_reference: dict | None = None,
    _mcp_servers: list[dict] | None = None, **kwargs: Any
) -> str:
    token = _deepresearch_router_state_ctx.set(_router_state)
    materials_token = _materials_reference_ctx.set(_materials_reference)
    try:
        async with AsyncExitStack() as stack:
            from .mcp_search import search_mcp_gateway
            try:
                native_servers = await stack.enter_async_context(search_mcp_gateway(_mcp_servers))
            except Exception:
                return json.dumps({"status": "error", "error_code": "mcp_gateway_unavailable",
                                   "error": "DeepResearch MCP authorization endpoint is unavailable"})
            mcp_token = _mcp_servers_ctx.set(native_servers)
            try:
                stream_impl = getattr(deepresearch_stream, "_func")
                return await stream_impl(**kwargs)
            finally:
                _mcp_servers_ctx.reset(mcp_token)
    finally:
        _deepresearch_router_state_ctx.reset(token)
        _materials_reference_ctx.reset(materials_token)


@tool(
    name="deepresearch_stream",
    description=(
        "Low-level compatibility interface, not a substitute for loading the skill. "
        "For requests to use the deepresearch skill, including the DeepResearch "
        "capsule, first call skill_tool(skill_name=\"deepresearch\") and follow "
        "the skill's deepresearch_execute entry instead of calling this tool. "
        "Run or resume DeepResearch through the isolated external skill runner. "
        "Progress is pushed to the current Gateway route; only a terminal "
        "interrupted, completed, or structured error outcome is returned. "
        "action='start' launches a new research run and requires query "
        "(if a stopped or interaction-paused snapshot for the same topic "
        "exists in this session, it is claimed automatically: a stopped "
        "snapshot resumes from where it stopped without re-running "
        "completed stages, and an interaction-paused snapshot returns the "
        "pending interaction); "
        "action='resume' continues an interrupted conversation and requires "
        "the conversation_id and node from that interrupted outcome. When the "
        "outcome status is 'interrupted', the workflow is paused waiting for "
        "the USER's answer at an interaction node: present the marker content "
        "to the user, collect the user's reply, then resume with it (the "
        "user's answer text as feedback for feedback_handler; the user's "
        "answer encoded as interaction_result for outline_interaction). "
        "Follow outcome.resume_hint for the exact resume arguments and "
        "accepted values. Never answer the interaction on the user's behalf "
        "or resume without the user's input. When the outcome status is "
        "'stopped', the run was gracefully stopped by the user and "
        "checkpointed: to continue it from where it stopped, resume with "
        "action='resume', the same conversation_id, and node='stopped' "
        "(no feedback needed); completed stages are not re-executed."
    ),
)
async def deepresearch_stream(  # pylint: disable=huawei-too-many-arguments
    action: str,
    query: str = "",
    conversation_id: str = "",
    feedback: str = "",
    node: str = "",
    file_name: str = "",
    interaction_result: str = "",
    report_type: DeepResearchReportType | None = None,
) -> str:
    skill_started_ns = time.monotonic_ns()
    route = _get_route()
    if report_type is not None and report_type not in {"professional", "brief"}:
        return json.dumps(
            {
                "status": "error",
                "error_code": "report_type_invalid",
                "error": "report_type must be one of: professional, brief",
            },
            ensure_ascii=False,
        )
    try:
        python_bin = resolve_python_executable()
    except DeepResearchRuntimeError as exc:
        code = str(exc)
        return _startup_error_outcome(action, code, code, query, file_name)
    script = _resolve_run_script()
    if not script:
        return _startup_error_outcome(
            action, "runner_missing", "run_deepsearch.py not found", query, file_name,
        )

    # 认领停止快照时按 claim node 推导的阶段提示(消费点 set/reset);
    # 其余路径保持 None。
    claim_stage_hint: int | None = None
    if action == "start":
        # 停止恢复兜底（2026-10-15 19:52 回归实证）：停止后新请求中 skill
        # 激活态随请求重建而重置，deepresearch_execute 被
        # ProgressiveToolRail 过滤（stopped 文案指引的唯一入口成为死信），
        # 外层模型经 invoke_tool 摸到本工具 action='start' → 绕过认领全新
        # 起跑，丢弃停止快照从头重跑。此处与 execute 同口径兜底：按 query
        # 扫库认领（含归属守卫与主题一致校验），命中停止快照直通改写为
        # resume node='stopped'（已完成节点不重跑）；命中交互中断快照
        # （停止落在卡片待答复期间）本地合成 interrupted outcome 重弹卡。
        claim = await _claim_start_snapshot(query)
        if claim is not None:
            claim_cid = str(claim.get("conversation_id") or "")
            if claim.get("stopped") and claim_cid:
                logger.info(
                    "[deepresearch_stream] start claimed stopped conversation "
                    "conversation_id=%s node=%s",
                    claim_cid,
                    claim.get("node"),
                )
                action = "resume"
                conversation_id = claim_cid
                node = "stopped"
                # 与 execute 认领路径(execution.py resuming_stopped)同口径
                # 推导阶段提示:否则 _consume_stream 的 resuming 分支取默认
                # stage=1,前端会显示"回到 stage1 从头重跑"(2026-10-16 日志
                # 实证:brief_html_reporter 暂停续跑后 stage1 转圈 2 分 45
                # 秒直跳全部完成)。暂存到局部变量,消费点再 set/reset。
                claim_stage_hint = stage_for_resume_node(
                    str(claim.get("node") or "")
                )
            elif claim_cid:
                logger.info(
                    "[deepresearch_stream] start claimed interrupted "
                    "conversation conversation_id=%s node=%s",
                    claim_cid,
                    claim.get("node"),
                )
                return json.dumps(
                    _synthesize_claim_interrupted_outcome(claim, route, query),
                    ensure_ascii=False,
                )

    if action == "start":
        argv = [
            str(python_bin),
            script,
            "run",
            "--config-stdin",
            "--query",
            query,
        ]
        if conversation_id:
            argv.extend(("--conversation-id", conversation_id))
    elif action == "resume":
        if not conversation_id or not node:
            return json.dumps(
                {
                    "status": "error",
                    "error_code": "resume_invalid",
                    "error": "resume requires conversation_id and node",
                },
                ensure_ascii=False,
            )
        if node == "feedback_handler" and interaction_result:
            try:
                feedback = _normalize_feedback_handler_resume_feedback(
                    feedback, interaction_result
                )
            except ValueError:
                return json.dumps(
                    {
                        "status": "error",
                        "error_code": "interaction_result_invalid",
                        "error": "DeepResearch interaction result is invalid",
                    },
                    ensure_ascii=False,
                )
        elif node == "outline_interaction":
            if not interaction_result:
                return json.dumps(
                    {
                        "status": "error",
                        "error_code": "outline_interaction_invalid_result",
                        "error": "outline_interaction resume requires interaction_result",
                    },
                    ensure_ascii=False,
                )
            try:
                feedback = _resolve_outline_interaction_feedback(
                    interaction_result,
                    conversation_id=conversation_id,
                    route=route,
                )
            except ValueError as exc:
                return json.dumps(
                    {
                        "status": "error",
                        "error_code": "outline_interaction_invalid_result",
                        "error": f"outline_interaction interaction_result invalid: {exc}",
                    },
                    ensure_ascii=False,
                )
        argv = [
            str(python_bin),
            script,
            "resume",
            "--config-stdin",
            "--conversation-id",
            conversation_id,
            "--feedback",
            feedback or "",
            "--node",
            node,
            "--interrupt-feedback",
            "",
        ]
    else:
        return json.dumps(
            {
                "status": "error",
                "error_code": "action_invalid",
                "error": "Unknown DeepResearch action",
            },
            ensure_ascii=False,
        )

    materials_reference = _materials_reference_ctx.get()
    if materials_reference:
        if materials_reference.get("conversation_id") != conversation_id:
            return json.dumps({"status": "error", "error_code": "materials_scope_invalid"})
        argv.extend(("--materials-file", materials_reference["path"],
                     "--materials-sha256", materials_reference["sha256"]))

    try:
        config = _build_deepresearch_request_config(
            interactive_ask=True,
            service_id=str(route.get("service_id") or "default"),
            agent_id=str(route.get("agent_id") or "default"),
        )
        if report_type is not None:
            config["REPORT_TYPE"] = report_type
        mcp_servers = _mcp_servers_ctx.get()
        if mcp_servers:
            config["MCP_SERVERS_JSON"] = json.dumps(mcp_servers, ensure_ascii=False)
        child_env = _build_deepresearch_child_env(
            interactive_ask=True,
            service_id=str(route.get("service_id") or "default"),
            agent_id=str(route.get("agent_id") or "default"),
            runtime_config=config,
            executable=python_bin,
        )
        if mcp_servers:
            # Only the private loopback endpoint bypasses configured HTTP proxies.
            bypass = child_env.get("NO_PROXY") or child_env.get("no_proxy") or ""
            child_env["NO_PROXY"] = ",".join(filter(None, (bypass, "127.0.0.1")))
            child_env["no_proxy"] = child_env["NO_PROXY"]
        config_frame = _encode_deepresearch_config(config)
    except DeepResearchRuntimeError as exc:
        code = str(exc)
        return _startup_error_outcome(action, code, code, query, file_name)
    except Exception as exc:  # pylint: disable=broad-exception-caught
        return json.dumps(
            {
                "status": "error",
                "error_code": "child_config_invalid",
                "error": f"child config build failed: {type(exc).__name__}",
            },
            ensure_ascii=False,
        )
    search_error = _validate_deepresearch_search_config(config)
    if search_error:
        return _startup_error_outcome(
            action, "search_config_missing", search_error, query, file_name,
        )

    try:
        progress_artifact = _create_progress_artifact()
    except OSError as exc:
        return json.dumps(
            {
                "status": "error",
                "error_code": "progress_file_invalid",
                "error": f"progress file setup failed: {type(exc).__name__}",
            },
            ensure_ascii=False,
        )
    progress_file = progress_artifact.path
    argv.extend(("--progress-file", str(progress_file)))

    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=child_env,
        )
    except Exception as exc:  # pylint: disable=broad-exception-caught
        _remove_progress_artifact(progress_artifact)
        return json.dumps(
            {
                "status": "error",
                "error_code": "spawn_failed",
                "error": f"spawn failed: {type(exc).__name__}",
            },
            ensure_ascii=False,
        )

    manager = get_deepresearch_manager(_route_scope())
    session_id = str(route.get("session_id") or "")
    tracked = False
    stderr_tail = bytearray()

    async def _drain_stderr() -> None:
        stream = proc.stderr
        if stream is None:
            return
        while True:
            chunk = await stream.read(8192)
            if not chunk:
                return
            stderr_tail.extend(chunk)
            if len(stderr_tail) > DEEPRESEARCH_STDERR_TAIL_MAX_BYTES:
                del stderr_tail[:-DEEPRESEARCH_STDERR_TAIL_MAX_BYTES]

    stderr_task = asyncio.create_task(_drain_stderr())
    try:
        try:
            manager.track_process(session_id, proc)
            tracked = True
        except BaseException:
            cleanup = asyncio.create_task(_stop_deepresearch_process(proc))
            await _await_cleanup_task(cleanup)
            raise
        await _write_deepresearch_config(proc, config_frame)
        # 认领停止快照的续跑消费段设置阶段提示(仅在消费窗口内生效,
        # finally 复位避免向同上下文后续调用泄漏陈旧 hint)。
        stage_hint_token = (
            _deepresearch_stage_hint_ctx.set(claim_stage_hint)
            if claim_stage_hint is not None
            else None
        )
        try:
            outcome = await _consume_stream(
                proc=proc,
                route=route,
                action=action,
                node=node,
                query=query,
                conversation_id=conversation_id,
                file_name=file_name,
                secrets=_config_secret_values(config),
                skill_started_ns=skill_started_ns,
            )
        finally:
            if stage_hint_token is not None:
                _deepresearch_stage_hint_ctx.reset(stage_hint_token)
    except asyncio.CancelledError:
        # 用户停止:先优雅停止(发帧→等落盘→超时兜底),保留 checkpoint
        # 供 resume --node stopped 恢复,不回退到并行调研阶段重跑。
        captured: dict[str, Any] = {}
        cleanup = asyncio.create_task(
            _request_graceful_stop(proc, captured=captured)
        )
        await _await_cleanup_task(cleanup)
        if captured:
            # 修复A:停止-完成竞速——子进程在停止信号生效前已自然完成,
            # completed 帧被帧感知排空捕获。快照已按 workflow_completed
            # 清理,续跑无可恢复状态,必须在此把报告交付给用户。
            try:
                await asyncio.wait_for(
                    _deliver_race_completed_report(
                        route=route,
                        chunk=captured,
                        file_name=file_name,
                        conversation_id=conversation_id,
                        secrets=_config_secret_values(config),
                    ),
                    timeout=DEEPRESEARCH_RACE_DELIVERY_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "[deepresearch_stream] race-completed delivery timed "
                    "out after %ss",
                    DEEPRESEARCH_RACE_DELIVERY_TIMEOUT_SECONDS,
                )
            # CancelledError 是 BaseException 子类，不会落入下方
            # except Exception，自动向上传播保持取消语义，无需显式
            # except 分支（G.ERR.13）。
            except Exception as exc:  # pylint: disable=broad-exception-caught
                logger.warning(
                    "[deepresearch_stream] race-completed delivery failed "
                    "type=%s",
                    type(exc).__name__,
                )
        raise
    except _StreamProtocolInvalid:
        outcome = _stream_protocol_error()
    except Exception as exc:  # pylint: disable=broad-exception-caught
        safe_reason = "unclassified"
        if isinstance(exc, ValueError) and str(exc) in {
            "deepresearch_stdout_limit_exceeded",
            ROUTER_LIMIT_ERROR,
        }:
            safe_reason = str(exc)
        logger.error(
            "[deepresearch_stream] stream consume failed type=%s reason=%s "
            "request_id=%s session_id=%s conversation_id=%s",
            type(exc).__name__,
            safe_reason,
            _safe_log_correlation_id(route.get("request_id")),
            _safe_log_correlation_id(session_id),
            _safe_log_correlation_id(conversation_id),
            exc_info=safe_reason != "unclassified",
        )
        outcome = {
            "status": "error",
            "error_code": "stream_failed",
            "error": f"DeepResearch stream failed: {type(exc).__name__}",
        }
    finally:
        cleanup = asyncio.create_task(_stop_deepresearch_process(proc))
        await _await_cleanup_task(cleanup)
        stderr_cleanup = asyncio.create_task(_finish_stderr_task(stderr_task))
        await _await_cleanup_task(stderr_cleanup)
        try:
            if tracked:
                try:
                    manager.untrack_process(session_id, proc)
                except BaseException as exc:  # cleanup must not mask outcome
                    logger.warning(
                        "[deepresearch_stream] untrack failed type=%s",
                        type(exc).__name__,
                    )
        finally:
            _remove_progress_artifact(progress_artifact)

    if outcome.get("status") == "error":
        outcome["returncode"] = proc.returncode
        stderr_text = stderr_tail.decode("utf-8", errors="replace").strip()
        if stderr_text:
            outcome["stderr_tail"] = stderr_text
    if (
        outcome.get("status") in {"completed", "interrupted", "error", "stopped"}
        and isinstance(outcome.get("timing"), dict)
    ):
        outcome["skill_execution_ms"] = max(
            0,
            (time.monotonic_ns() - skill_started_ns) // 1_000_000,
        )
    if outcome.get("status") == "stopped":
        outcome["resume_hint"] = {
            "action": "resume",
            "node": "stopped",
            "conversation_id": str(
                outcome.get("conversation_id") or conversation_id
            ),
            "guidance": (
                "The research run was gracefully stopped by the user and its "
                "progress is checkpointed. To continue the same run from "
                "where it stopped, use action='resume', this conversation_id, "
                "and node='stopped' (no feedback needed); completed stages "
                "will not be re-executed. Otherwise, start a new run."
            ),
        }
    if outcome.get("status") in {"completed", "error", "cancelled"}:
        _clear_outline_title_cache(
            route, outcome.get("conversation_id", conversation_id)
        )
        _clear_outline_json_cache(
            route, outcome.get("conversation_id", conversation_id)
        )
    if (
        outcome.get("status") == "interrupted"
        and outcome.get("node_id") in {"feedback_handler", "outline_interaction"}
    ):
        outcome["resume_hint"] = _build_interrupted_resume_hint(
            str(outcome.get("node_id") or ""),
            str(outcome.get("conversation_id") or conversation_id),
        )
    if (
        outcome.get("status") == "interrupted"
        and outcome.get("node_id") == "outline_interaction"
    ):
        if action == "resume" and node == "outline_interaction":
            return json.dumps(
                {
                    "status": "error",
                    "conversation_id": outcome.get(
                        "conversation_id", conversation_id
                    ),
                    "error_code": "outline_auto_resume_loop",
                    "error": "outline_interaction repeated after automatic acceptance",
                },
                ensure_ascii=False,
            )
    outcome.pop("_router_state", None)
    outcome = _sanitize_terminal_outcome(outcome, config)
    return json.dumps(outcome, ensure_ascii=False)


# DeepResearch owns subprocess cancellation and cleanup, while a real research
# turn can legitimately outlive agent-core's default 300-second tool deadline.
deepresearch_stream.card.properties["resilience"] = {"timeout_s": None}


async def _finish_stderr_task(task: asyncio.Task[Any]) -> None:
    try:
        await asyncio.wait_for(task, timeout=2.0)
    except asyncio.TimeoutError:
        task.cancel()
        with suppress(BaseException):
            await task


_DEEPRESEARCH_SALVAGE_MIN_REPORT_CHARS = 64


def _locate_salvage_report(conversation_id: str) -> dict[str, Any] | None:
    """修复7：定位子进程落盘的 raw_report.md 止损产物。

    子进程在报告生成后、发出 completed 终局帧之前，会把
    ``final_result.response_content`` 原文写入
    ``<skill_root>/data/<cid>.raw_report.md``（保留 [checked_citation:N]
    锚点）。子进程在交付前崩溃/被停时，该文件是唯一存活的报告载体。
    返回 {"response_content", "citation_artifacts"}；不可止损返回 None。
    """
    cid = str(conversation_id or "").strip()
    if not cid:
        return None
    root = _resolve_skill_root()
    if not root:
        return None
    data_dir = Path(root) / "data"
    raw_path = data_dir / f"{cid}.raw_report.md"
    try:
        if raw_path.stat().st_size > DEEPRESEARCH_STREAM_JSON_MAX_TEXT_CHARS:
            return None
        content = raw_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    if len(content.strip()) < _DEEPRESEARCH_SALVAGE_MIN_REPORT_CHARS:
        return None
    citation_artifacts: dict[str, str] = {
        "raw_report_path": str(raw_path).replace("\\", "/")
    }
    preview_path = data_dir / f"{cid}.citations.preview.json"
    try:
        if preview_path.is_file():
            citation_artifacts["citations_preview_path"] = str(
                preview_path
            ).replace("\\", "/")
    except OSError:
        pass
    return {
        "response_content": content,
        "citation_artifacts": citation_artifacts,
    }


async def _deliver_salvaged_report(
    *,
    route: dict[str, object],
    conversation_id: str,
    file_name: str,
    send: Callable[[dict[str, Any]], Awaitable[bool]],
    reason: str,
) -> dict[str, Any] | None:
    """修复7：从 raw_report.md 止损交付报告。

    子进程异常终局（error / completed 缺 response_content）但报告已落盘
    时，复用正常完成路径的产物管线（清理引文锚点 + HTML 样式/离线兜底）
    把报告交付给用户，返回 completed outcome；不可止损（无文件/产物写
    失败/推送失败）返回 None，调用方回退原始错误。
    """
    located = _locate_salvage_report(conversation_id)
    if located is None:
        return None
    if not route.get("session_id") or not route.get("channel_id"):
        return None
    final_result = {
        "response_content": located["response_content"],
        "response_content_type": "text/markdown",
    }
    try:
        (
            artifacts,
            html_style_status,
            html_style_phase,
            html_style_reason_code,
        ) = await _write_report_artifacts_stream(
            final_result,
            file_name,
            conversation_id,
            located["citation_artifacts"],
        )
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logger.warning(
            "[deepresearch_stream] salvage artifact write failed type=%s",
            type(exc).__name__,
        )
        return None
    artifact_mime_types = {"md": "text/markdown", "html": "text/html"}
    files = [
        {
            "path": value,
            "name": Path(value).name,
            "mime_type": artifact_mime_types.get(key),
        }
        for key, value in artifacts.items()
        if key in artifact_mime_types
    ]
    if not files:
        return None
    file_payload: dict[str, Any] = {"event_type": "chat.file", "files": files}
    markdown_index = list(artifacts).index("md") if "md" in artifacts else 0
    bundle = _build_related_artifact_bundle(
        located["citation_artifacts"], markdown_index
    )
    if bundle:
        file_payload["metadata"] = {"artifactBundle": bundle}
    if not await send(file_payload):
        return None
    logger.warning(
        "[deepresearch_stream] salvaged report from raw_report.md "
        "conversation_id=%s reason=%s",
        conversation_id,
        reason,
    )
    outcome: dict[str, Any] = {
        "status": "completed",
        "conversation_id": conversation_id,
        "report_delivered": True,
        "salvaged_report": True,
    }
    if html_style_status in {"applied", "fallback"}:
        outcome["html_style_status"] = html_style_status
        if (
            html_style_status == "fallback"
            and isinstance(html_style_phase, str)
            and isinstance(html_style_reason_code, str)
        ):
            outcome["html_style_phase"] = html_style_phase
            outcome["html_style_reason_code"] = html_style_reason_code
    return outcome


async def _deliver_race_completed_report(
    *,
    route: dict[str, object],
    chunk: dict[str, Any],
    file_name: str,
    conversation_id: str,
    secrets: tuple[str, ...],
) -> dict[str, Any] | None:
    """修复A：交付停止-完成竞速窗口内捕获的 completed 终局帧。

    用户按停止时消费循环已被取消,但子进程可能在停止信号生效前按自然
    完成路径输出 completed 终局帧——快照同时按 workflow_completed 清理,
    "继续"时无可恢复状态、brief 成品又无止损产物可捞,报告将彻底丢失。
    本函数复用正常完成路径的产物管线(html 直写 / markdown 引文管线)
    与进度收尾推送,把已生成的报告交付给用户。返回 completed outcome;
    不可交付(帧无效/写盘失败/推送失败)返回 None,调用方保持停止语义。
    """
    try:
        safe_chunk = _redact_json_value(chunk, secrets)
    except _StreamProtocolInvalid:
        return None
    if not isinstance(safe_chunk, dict):
        return None
    final_result = safe_chunk.get("final_result")
    if not isinstance(final_result, dict):
        return None
    response_content = final_result.get("response_content")
    if not isinstance(response_content, str) or not response_content:
        return None
    response_content_type = _normalize_response_content_type(final_result)
    if response_content_type is None:
        return None
    if not route.get("session_id") or not route.get("channel_id"):
        return None
    resolved_cid = str(safe_chunk.get("conversation_id") or conversation_id)

    push = WebSocketGatewayPushTransport()
    todo_path: Path | None = None
    try:
        todo_path = deepresearch_todo_path(
            session_id=str(route["session_id"]),
            workspace_key=str(route.get("workspace_key") or "default"),
        )
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logger.warning(
            "[deepresearch_stream] resolve todo path failed type=%s",
            type(exc).__name__,
        )

    async def send(payload: dict[str, Any]) -> bool:
        try:
            safe_payload = _redact_json_value(payload, secrets)
        except _StreamProtocolInvalid:
            return False
        if not isinstance(safe_payload, dict):
            return False
        if todo_path is not None and safe_payload.get("event_type") == "task.update":
            try:
                persist_deepresearch_task_update(safe_payload, todo_path=todo_path)
            except Exception as exc:  # pylint: disable=broad-exception-caught
                logger.warning(
                    "[deepresearch_stream] persist todo failed type=%s",
                    type(exc).__name__,
                )
        try:
            await push.send_push(
                {
                    "request_id": route.get("request_id", ""),
                    "channel_id": route["channel_id"],
                    "session_id": route["session_id"],
                    "payload": safe_payload,
                    "is_complete": False,
                }
            )
            return True
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logger.warning(
                "[deepresearch_stream] race-completed push failed type=%s",
                type(exc).__name__,
            )
            return False

    # 进度收尾:消费循环已被取消,原 RouterState 不可达;合成路由状态
    # 关闭"最终报告处理"子任务并把全阶段标记完成,防止前端进度列表
    # 停留在"报告交付"转圈。
    state = RouterState(current_stage=4, final_report_started=True)
    for payload in complete_final_report_processing(state):
        await send(payload)
    for payload in advance_stage(state, 4, complete=True):
        await send(payload)

    html_style_status = None
    html_style_phase = None
    html_style_reason_code = None
    try:
        if response_content_type == "text/html":
            artifacts = {
                "html": await _write_report_html(response_content, file_name)
            }
        else:
            artifact_result = await _write_report_artifacts_stream(
                final_result,
                file_name,
                resolved_cid,
                _normalize_citation_artifacts(safe_chunk),
            )
            if isinstance(artifact_result, tuple) and len(artifact_result) == 4:
                (
                    artifacts,
                    html_style_status,
                    html_style_phase,
                    html_style_reason_code,
                ) = artifact_result
            elif isinstance(artifact_result, tuple):
                artifacts, html_style_status = artifact_result
                html_style_phase = None
                html_style_reason_code = None
            else:
                # Preserve compatibility with private test doubles and older callers.
                artifacts = artifact_result
                html_style_phase = None
                html_style_reason_code = None
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logger.warning(
            "[deepresearch_stream] race-completed artifact write failed type=%s",
            type(exc).__name__,
        )
        return None
    artifact_mime_types = {"md": "text/markdown", "html": "text/html"}
    files = [
        {
            "path": value,
            "name": Path(value).name,
            "mime_type": artifact_mime_types.get(key),
        }
        for key, value in artifacts.items()
        if key in artifact_mime_types
    ]
    if not files:
        return None
    file_payload: dict[str, Any] = {"event_type": "chat.file", "files": files}
    file_metadata: dict[str, Any] = {}
    if response_content_type == "text/markdown" and "md" in artifacts:
        markdown_index = list(artifacts).index("md")
        bundle = _build_related_artifact_bundle(safe_chunk, markdown_index)
        if bundle:
            file_metadata["artifactBundle"] = bundle
    if html_style_status in {"applied", "fallback"}:
        file_metadata["htmlStyleStatus"] = html_style_status
        if (
            html_style_status == "fallback"
            and isinstance(html_style_phase, str)
            and isinstance(html_style_reason_code, str)
        ):
            file_metadata["htmlStylePhase"] = html_style_phase
            file_metadata["htmlStyleReasonCode"] = html_style_reason_code
    if file_metadata:
        file_payload["metadata"] = file_metadata
    if not await send(file_payload):
        return None
    logger.warning(
        "[deepresearch_stream] race-completed report delivered "
        "conversation_id=%s",
        resolved_cid,
    )
    outcome: dict[str, Any] = {
        "status": "completed",
        "conversation_id": resolved_cid,
        "report_delivered": True,
        "race_completed": True,
    }
    if html_style_status in {"applied", "fallback"}:
        outcome["html_style_status"] = html_style_status
    return outcome


async def salvage_deepresearch_report(
    *, conversation_id: str, file_name: str
) -> dict[str, Any] | None:
    """修复7（execute 认领侧入口）：无快照可恢复但报告已落盘时止损交付。

    供 deepresearch_execute 在认领失败（probe 无可恢复快照）时调用：
    报告在收尾阶段被停或交付前崩溃的场景下，直接交付已生成的报告，
    不重跑研究。返回 completed outcome 或 None（不可止损）。
    """
    route = _get_route()
    push = WebSocketGatewayPushTransport()

    # 与 stream 交付路径同口径脱敏（_deliver_race_completed_report 使用
    # _config_secret_values(config)）：止损交付的报告全文同样经 WebSocket
    # 推送,若报告内容（如搜索结果引用）携带与 config secret 匹配的子串,
    # 不脱敏会直接外泄。secrets 计算失败回退空表——止损交付本身不因此
    # 中断（与改前行为一致），仅失去脱敏。
    try:
        salvage_config = _build_deepresearch_request_config(
            interactive_ask=True,
            service_id=str(route.get("service_id") or "default"),
            agent_id=str(route.get("agent_id") or "default"),
        )
        salvage_secrets = _config_secret_values(salvage_config)
    except Exception:  # pylint: disable=broad-exception-caught
        salvage_secrets = ()

    async def send(payload: dict[str, Any]) -> bool:
        if not route.get("session_id") or not route.get("channel_id"):
            return False
        try:
            safe_payload = _redact_json_value(payload, salvage_secrets)
        except _StreamProtocolInvalid:
            return False
        if not isinstance(safe_payload, dict):
            return False
        try:
            await push.send_push(
                {
                    "request_id": route.get("request_id", ""),
                    "channel_id": route["channel_id"],
                    "session_id": route["session_id"],
                    "payload": safe_payload,
                    "is_complete": False,
                }
            )
            return True
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logger.warning(
                "[deepresearch_stream] salvage push failed type=%s",
                type(exc).__name__,
            )
            return False

    return await _deliver_salvaged_report(
        route=route,
        conversation_id=conversation_id,
        file_name=file_name,
        send=send,
        reason="claim_without_checkpoint",
    )


async def _consume_stream(
    *,
    proc: Any,
    route: dict[str, object],
    action: str,
    node: str,
    query: str,
    conversation_id: str,
    file_name: str,
    secrets: tuple[str, ...],
    skill_started_ns: int | None = None,
) -> dict[str, Any]:
    push = WebSocketGatewayPushTransport()
    cached_titles = (
        _get_cached_outline_titles(route, conversation_id)
        if action == "resume"
        else {}
    )
    existing_state = _deepresearch_router_state_ctx.get()
    # 停止续跑:阶段提示来自认领侧按被停止节点的推导,种子取 hint-1,
    # 使首个 resuming 帧恰好推进到 hint(之前的阶段已完成、hint 进行中)。
    stage_hint = (
        _deepresearch_stage_hint_ctx.get()
        if action == "resume" and node == "stopped"
        else None
    )
    if existing_state is not None and isinstance(existing_state, RouterState):
        state = existing_state
    elif stage_hint:
        state = RouterState(
            section_titles=dict(cached_titles),
            current_stage=stage_hint - 1,
        )
    else:
        state = RouterState(
            section_titles=dict(cached_titles),
            current_stage=(
                _RESUME_NODE_CURRENT_STAGE.get(node, 0)
                if action == "resume"
                else 0
            ),
            final_report_started=(
                action == "resume" and node == "user_feedback_processor"
            ),
        )
    outcome_cid = conversation_id
    todo_path: Path | None = None
    if route.get("session_id"):
        try:
            todo_path = deepresearch_todo_path(
                session_id=str(route["session_id"]),
                workspace_key=str(route.get("workspace_key") or "default"),
            )
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logger.warning(
                "[deepresearch_stream] resolve todo path failed type=%s",
                type(exc).__name__,
            )

    async def send(payload: dict[str, Any]) -> bool:
        safe_payload = _redact_json_value(payload, secrets)
        if not isinstance(safe_payload, dict):
            raise _StreamProtocolInvalid("Gateway payload is not an object")
        if todo_path is not None and safe_payload.get("event_type") == "task.update":
            try:
                persist_deepresearch_task_update(
                    safe_payload, todo_path=todo_path
                )
            except Exception as exc:  # pylint: disable=broad-exception-caught
                logger.warning(
                    "[deepresearch_stream] persist todo failed type=%s",
                    type(exc).__name__,
                )
        if not route.get("session_id") or not route.get("channel_id"):
            return False
        try:
            await push.send_push(
                {
                    "request_id": route.get("request_id", ""),
                    "channel_id": route["channel_id"],
                    "session_id": route["session_id"],
                    "payload": safe_payload,
                    "is_complete": False,
                }
            )
            return True
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logger.warning(
                "[deepresearch_stream] Gateway push failed type=%s",
                type(exc).__name__,
            )
            return False

    materials_reference = _materials_reference_ctx.get()
    if materials_reference and action == "start":
        from .materials import material_summary
        await send({"event_type": "chat.reasoning", "content": material_summary(materials_reference)})

    outcome: dict[str, Any] = {
        "status": "error",
        "error_code": "terminal_marker_missing",
        "error": "no terminal marker",
    }
    first_sdk_node_ns: int | None = None
    report_delivery_settled = False
    seen_started = False

    async def send_processing_progress() -> None:
        if not seen_started or report_delivery_settled:
            return
        await send(
            {
                "event_type": "chat.processing_status",
                "is_processing": True,
                "current_task": "in_progress",
            }
        )

    def terminal_timing(chunk: dict[str, Any]) -> dict[str, Any] | None:
        timing = chunk.get("timing")
        if not isinstance(timing, dict):
            return None
        enriched = dict(timing)
        if first_sdk_node_ns is not None and skill_started_ns is not None:
            enriched["skill_to_sdk_first_node_ms"] = max(
                0,
                (first_sdk_node_ns - skill_started_ns) // 1_000_000,
            )
        return enriched

    def attach_terminal_timing(
        result: dict[str, Any], chunk: dict[str, Any]
    ) -> dict[str, Any]:
        timing = terminal_timing(chunk)
        if timing is not None:
            result["timing"] = timing
        return result

    async for raw in _iter_with_periodic_progress(
        _iter_ndjson_lines(
            proc.stdout,
            request_id=str(route.get("request_id") or ""),
            session_id=str(route.get("session_id") or ""),
            conversation_id=conversation_id,
        ),
        send_processing_progress,
    ):
        try:
            line = raw.decode("utf-8").strip()
        except UnicodeDecodeError:
            return _stream_protocol_error()
        if not line:
            continue
        try:
            chunk = json.loads(line)
        except (json.JSONDecodeError, RecursionError):
            return _stream_protocol_error()
        if not _validate_stream_json_shape(chunk):
            return _stream_protocol_error()
        try:
            chunk = _redact_json_value(chunk, secrets)
        except _StreamProtocolInvalid:
            return _stream_protocol_error()
        if not isinstance(chunk, dict):
            continue
        status_value = chunk.get("__deepsearch_status__") or chunk.get("status")
        is_terminal = status_value in {
            "completed",
            "interrupted",
            "error",
            "cancelled",
            "stopped",
        }
        if first_sdk_node_ns is None and not is_terminal:
            is_timed_node = str(chunk.get("agent") or "") in _TIMED_SDK_NODES
            if is_timed_node and chunk.get("event") != "done":
                first_sdk_node_ns = time.monotonic_ns()
        if status_value in {"started", "resuming"}:
            seen_started = True
            outcome_cid = str(chunk.get("conversation_id") or outcome_cid)
            stage = 1
            if action == "resume" and node == "outline_interaction":
                stage = 2
            elif action == "resume" and node == "user_feedback_processor":
                stage = 4
            elif action == "resume" and node == "stopped" and stage_hint:
                # 停止续跑:直接推进到被停止阶段,避免前端进度列表回退到
                # 第一步"研究主题澄清"转圈误导用户以为从头重跑。
                stage = stage_hint
            for payload in advance_stage(state, stage):
                await send(payload)
            await send(
                {
                    "event_type": "chat.processing_status",
                    "is_processing": True,
                    "current_task": status_value,
                }
            )
            continue
        if status_value == "interrupted":
            node_id = str(chunk.get("agent") or state.interrupt_node_id)
            marker = {
                key: value
                for key, value in chunk.items()
                if key not in {"__deepsearch_status__", "status"}
            }
            if node_id == "feedback_handler" and not marker.get("questions"):
                questions = collected_questions(state)
                if questions:
                    marker["questions"] = questions
            outline_json_dict: dict[str, Any] | None = None
            missing_outline = (
                node_id == "outline_interaction"
                and not marker.get("outline")
                and (
                    not marker.get("content")
                    or is_outline_status_placeholder(marker.get("content"))
                )
                and state.outline_parts
            )
            if missing_outline:
                card_markdown = ""
                try:
                    outline_json_dict = json.loads("".join(state.outline_parts))
                    formatted = _format_outline_card_markdown(outline_json_dict)
                    if formatted:
                        card_markdown = formatted
                    else:
                        outline_json_dict = {}
                except (json.JSONDecodeError, TypeError):
                    outline_json_dict = {}
                marker["outline"] = card_markdown
                marker["preview"] = {"text": card_markdown}
            resolved_cid = str(
                chunk.get("conversation_id")
                or outcome_cid
                or state.interrupt_conversation_id
            )
            if node_id == "outline_interaction":
                outline = marker.get("outline") or marker.get("content")
                if outline:
                    titles = extract_deepresearch_section_titles(
                        outline
                        if isinstance(outline, str)
                        else json.dumps(outline, ensure_ascii=False)
                    )
                    if titles and resolved_cid:
                        _cache_outline_titles(route, resolved_cid, titles)
                if resolved_cid:
                    if outline_json_dict is not None:
                        _cache_outline_json(route, resolved_cid, outline_json_dict)
                    elif isinstance(marker.get("outline"), dict):
                        # marker 自带完整 outline JSON 时同样写缓存：execute 认领
                        # 旧会话与 outline_use_edited 编辑回传都依赖该缓存
                        #（缺 sections 时下游 fail-closed）。
                        _cache_outline_json(route, resolved_cid, marker["outline"])
            if node_id == "user_feedback_processor" and state.report_parts:
                report = "".join(state.report_parts)
                marker["report"] = report[:6000] + (
                    "…\n(完整报告见最终产物)" if len(report) > 6000 else ""
                )
            outcome = {
                "status": "interrupted",
                "conversation_id": resolved_cid,
                "node_id": node_id,
                "marker": marker,
                "prompt": build_interrupt_prompt(node_id, state, marker, query),
                "_router_state": state,
            }
            timing = terminal_timing(chunk)
            if timing is not None:
                outcome["timing"] = timing
            continue
        if status_value == "completed":
            final_result = chunk.get("final_result")
            response_content = (
                final_result.get("response_content", "")
                if isinstance(final_result, dict)
                else ""
            )
            if not response_content:
                # 修复7：completed 帧缺报告正文时先尝试止损交付已落盘的
                # raw_report.md（同一会话此前运行可能已产出报告）。
                salvage = await _deliver_salvaged_report(
                    route=route,
                    conversation_id=str(
                        chunk.get("conversation_id") or outcome_cid
                    ),
                    file_name=file_name,
                    send=send,
                    reason="completed_missing_response_content",
                )
                if salvage is not None:
                    return attach_terminal_timing(salvage, chunk)
                return attach_terminal_timing({
                    "status": "error",
                    "conversation_id": chunk.get("conversation_id", outcome_cid),
                    "error_code": "empty_report",
                    "error": "completed marker missing final_result.response_content",
                }, chunk)
            response_content_type = _normalize_response_content_type(final_result)
            if response_content_type is None:
                return attach_terminal_timing(_stream_protocol_error(), chunk)
            if not state.final_report_started:
                # dev-stable may deliberately finish with a degraded section and
                # still run the final report pipeline.  Flush that observed
                # pipeline before delivery; a bare terminal marker remains an
                # invalid/incomplete stream.
                if (
                    not state.pending_final_report_frames
                    or not _has_completed_final_pipeline_node(state)
                ):
                    return attach_terminal_timing({
                        "status": "error",
                        "conversation_id": chunk.get("conversation_id", outcome_cid),
                        "error_code": "incomplete_section_progress",
                        "error": "completed marker arrived before section completion",
                    }, chunk)
                for payload in start_final_report_processing(state):
                    await send(payload)
            for payload in complete_final_report_processing(state):
                await send(payload)
            for payload in advance_stage(state, 4):
                await send(payload)
            if not route.get("session_id") or not route.get("channel_id"):
                return attach_terminal_timing({
                    "status": "error",
                    "conversation_id": chunk.get("conversation_id", outcome_cid),
                    "error_code": "report_file_delivery_failed",
                    "error": "Report file route is unavailable",
                }, chunk)
            html_style_status = None
            html_style_phase = None
            html_style_reason_code = None
            try:
                if response_content_type == "text/html":
                    primary_path = await _await_with_periodic_progress(
                        _write_report_html(response_content, file_name),
                        send_processing_progress,
                    )
                    artifacts = {"html": primary_path}
                else:
                    artifact_result = await _await_with_periodic_progress(
                        _write_report_artifacts_stream(
                            final_result,
                            file_name,
                            str(chunk.get("conversation_id") or outcome_cid),
                            _normalize_citation_artifacts(chunk),
                        ),
                        send_processing_progress,
                    )
                    if isinstance(artifact_result, tuple) and len(artifact_result) == 4:
                        (
                            artifacts,
                            html_style_status,
                            html_style_phase,
                            html_style_reason_code,
                        ) = artifact_result
                    elif isinstance(artifact_result, tuple):
                        artifacts, html_style_status = artifact_result
                    else:
                        # Preserve compatibility with private test doubles and older callers.
                        artifacts = artifact_result
                    primary_path = artifacts.get("md", "")
            except Exception as exc:  # pylint: disable=broad-exception-caught
                return attach_terminal_timing({
                    "status": "error",
                    "conversation_id": chunk.get("conversation_id", outcome_cid),
                    "error_code": "report_file_write_failed",
                    "error": type(exc).__name__,
                }, chunk)
            artifact_mime_types = {
                "md": "text/markdown",
                "html": "text/html",
            }
            files = [
                {
                    "path": value,
                    "name": Path(value).name,
                    "mime_type": artifact_mime_types.get(key),
                }
                for key, value in artifacts.items()
                if key in artifact_mime_types
            ]
            file_payload: dict[str, Any] = {
                "event_type": "chat.file",
                "files": files,
            }
            file_metadata: dict[str, Any] = {}
            if response_content_type == "text/markdown":
                markdown_index = list(artifacts).index("md")
                bundle = _build_related_artifact_bundle(chunk, markdown_index)
                if bundle:
                    file_metadata["artifactBundle"] = bundle
            if html_style_status in {"applied", "fallback"}:
                file_metadata["htmlStyleStatus"] = html_style_status
            if (
                html_style_status == "fallback"
                and isinstance(html_style_phase, str)
                and isinstance(html_style_reason_code, str)
            ):
                file_metadata["htmlStylePhase"] = html_style_phase
                file_metadata["htmlStyleReasonCode"] = html_style_reason_code
            if file_metadata:
                file_payload["metadata"] = file_metadata
            if not await send(file_payload):
                return attach_terminal_timing({
                    "status": "error",
                    "conversation_id": chunk.get("conversation_id", outcome_cid),
                    "error_code": "report_file_delivery_failed",
                    "error": "Report files could not be delivered",
                    "report_path": primary_path,
                }, chunk)
            report_delivery_settled = True
            for payload in advance_stage(state, 4, complete=True):
                await send(payload)
            completed_outcome: dict[str, Any] = {
                "status": "completed",
                "conversation_id": chunk.get("conversation_id", outcome_cid),
                "report_delivered": True,
            }
            if html_style_status in {"applied", "fallback"}:
                completed_outcome["html_style_status"] = html_style_status
                if html_style_status == "fallback":
                    if (
                        isinstance(html_style_phase, str)
                        and isinstance(html_style_reason_code, str)
                    ):
                        completed_outcome["html_style_phase"] = html_style_phase
                        completed_outcome["html_style_reason_code"] = (
                            html_style_reason_code
                        )
                    logger.warning(
                        "[deepresearch_stream] HTML style fallback "
                        "request_id=%s session_id=%s conversation_id=%s "
                        "style_phase=%s style_reason_code=%s",
                        _safe_log_correlation_id(route.get("request_id")),
                        _safe_log_correlation_id(route.get("session_id")),
                        _safe_log_correlation_id(
                            chunk.get("conversation_id") or outcome_cid
                        ),
                        html_style_phase,
                        html_style_reason_code,
                    )
            workflow_usage = normalize_workflow_llm_token_usage(
                final_result.get("workflow_llm_token_usage")
            )
            if workflow_usage is not None:
                completed_outcome["workflow_llm_token_usage"] = workflow_usage
            return attach_terminal_timing(completed_outcome, chunk)
        if status_value == "stopped":
            # 优雅停止终态(正常路径由 CancelledError 分支处理;此处覆盖
            # 取消信号晚于子进程优雅退出的竞态)。checkpoint 已落盘,
            # 可用 resume --node stopped 从被停止节点继续,已完成节点不重跑。
            return attach_terminal_timing(
                {
                    "status": "stopped",
                    "conversation_id": str(
                        chunk.get("conversation_id") or outcome_cid
                    ),
                },
                chunk,
            )
        if status_value == "error":
            # 修复7：子进程异常终局但报告已落盘时止损交付——本类事故中
            # 20 分钟研究报告已写入 raw_report.md，resume 崩溃却向用户返
            # 回 empty_report，报告与用户双重损失。
            salvage = await _deliver_salvaged_report(
                route=route,
                conversation_id=str(
                    chunk.get("conversation_id") or outcome_cid
                ),
                file_name=file_name,
                send=send,
                reason=str(chunk.get("error_code") or "workflow_error"),
            )
            if salvage is not None:
                return attach_terminal_timing(salvage, chunk)
            return attach_terminal_timing({
                "status": "error",
                "conversation_id": chunk.get("conversation_id", outcome_cid),
                "error_code": chunk.get("error_code", "workflow_error"),
                "error": chunk.get("error", "deepresearch workflow failed"),
            }, chunk)
        if chunk.get("agent") == "outline" and chunk.get("content"):
            outline = chunk["content"]
            titles = extract_deepresearch_section_titles(
                outline
                if isinstance(outline, str)
                else json.dumps(outline, ensure_ascii=False)
            )
            if titles:
                state.section_titles.update(titles)
                state.authoritative_section_indices.update(titles)
        for payload in route_chunk(chunk, state):
            await send(payload)
    return outcome


async def _write_report_html(response_content: str, file_name: str) -> str:
    return await asyncio.to_thread(
        _write_report_html_sync,
        response_content,
        file_name,
    )


def _write_report_html_sync(response_content: str, file_name: str) -> str:
    """Publish one immutable native HTML artifact with bounded collision retries."""
    from jiuwenswarm.agents.harness.common.tools.deepresearch_plugin.artifact_naming import (
        allocate_initial_html_path,
    )

    if not isinstance(response_content, str) or not response_content:
        raise ValueError("native HTML report content is empty")
    output_dir = _get_effective_request_output_dir()
    output_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    payload = response_content.encode("utf-8")
    for _ in range(_REPORT_PUBLICATION_ATTEMPTS):
        with _report_output_lock(output_dir):
            root_fd, root_identity = _open_output_root(output_dir)
            path: Path | None = None
            published_identity: tuple[int, int] | None = None
            try:
                path = allocate_initial_html_path(output_dir, file_name)
                _verify_output_root(output_dir, root_identity)
                try:
                    published_identity = _exclusive_write_at(
                        root_fd,
                        path.name,
                        payload,
                        output_dir=output_dir,
                    )
                except FileExistsError as exc:
                    raise _ReportPublicationCollision() from exc
                if root_fd is not None:
                    os.fsync(root_fd)
                _verify_output_root(output_dir, root_identity)
                return str(path)
            except _ReportPublicationCollision:
                continue
            except BaseException:
                if path is not None and published_identity is not None:
                    _cleanup_owned_output(
                        root_fd,
                        path.name,
                        published_identity,
                        False,
                        output_dir=output_dir,
                    )
                raise
            finally:
                if root_fd is not None:
                    os.close(root_fd)
    raise FileExistsError("report publication attempts exhausted")


def _write_report_markdown(
    final_result: dict[str, Any],
    file_name: str,
    conversation_id: str,
    citation_artifacts: object = None,
) -> str:
    """Retry bounded initial allocation collisions without overwriting."""
    reservations: list[tuple[Path, tuple[int, int]]] = []
    try:
        for _ in range(_REPORT_PUBLICATION_ATTEMPTS):
            try:
                return _write_report_markdown_once(
                    final_result,
                    file_name,
                    conversation_id,
                    citation_artifacts,
                    reservations,
                )
            except _ReportPublicationCollision:
                continue
    finally:
        for path, identity in reversed(reservations):
            _cleanup_owned_output_path(path, identity, False)
    raise FileExistsError("report publication attempts exhausted")


def _write_report_markdown_once(
    final_result: dict[str, Any],
    file_name: str,
    conversation_id: str,
    citation_artifacts: object,
    reservations: list[tuple[Path, tuple[int, int]]],
) -> str:
    """Publish the primary immutable report and its hidden sidecars."""
    from jiuwenswarm.agents.harness.common.tools.deepresearch_plugin.artifact_naming import (
        allocate_initial_paths,
    )
    from jiuwenswarm.agents.harness.common.tools.deepresearch_plugin.report_bundle import (
        build_report_bundle,
        serialize_final_result_snapshot,
    )

    output_dir = _get_effective_request_output_dir()
    output_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    with _report_output_lock(output_dir):
        root_fd, root_identity = _open_output_root(output_dir)
        try:
            paths = allocate_initial_paths(output_dir, file_name)
            with tempfile.TemporaryDirectory(
                prefix="deepresearch-report-staging-"
            ) as staging_name:
                bundle = build_report_bundle(
                    final_result,
                    Path(staging_name) / paths.markdown_path.stem,
                )
                markdown_bytes = bundle.markdown_text.encode("utf-8")
                snapshot_bytes = serialize_final_result_snapshot(
                    bundle.final_result_snapshot
                )
                provenance: dict[str, Any] = {
                    "schema_version": 2,
                    "document_id": f"doc_{uuid.uuid4().hex}",
                    "revision_id": f"rev_{uuid.uuid4().hex}",
                    "parent_revision_id": None,
                    "conversation_id": conversation_id,
                    "markdown_path": str(paths.markdown_path),
                    "content_sha256": hashlib.sha256(markdown_bytes).hexdigest(),
                    "final_result_path": paths.final_result_path.name,
                    "final_result_sha256": hashlib.sha256(snapshot_bytes).hexdigest(),
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "operation": {"action": "deepresearch_generate"},
                    "version_number": paths.version.version_number,
                    "version_base_stem": paths.version.base_stem,
                    "citations": bundle.citations,
                    "inference_manifest": bundle.inference_manifest,
                    "inference_graph_path": bundle.inference_graph_path,
                    "inference_graph_sha256": (
                        hashlib.sha256(bundle.inference_graph_bytes).hexdigest()
                        if bundle.inference_graph_bytes is not None
                        else ""
                    ),
                    "chart_manifest": bundle.chart_manifest,
                    "rewrite_history": [],
                }
                normalized = _normalize_citation_artifacts(citation_artifacts)
                if normalized:
                    provenance["citation_artifacts"] = normalized
                provenance_bytes = json.dumps(
                    provenance, ensure_ascii=False, indent=2
                ).encode("utf-8")
                published: list[tuple[str, tuple[int, int], bool]] = []
                try:
                    _verify_output_root(output_dir, root_identity)
                    # Publish resources before the hidden sidecars and visible
                    # Markdown commit marker.
                    for staged_dir in (bundle.infer_dir, bundle.chart_dir):
                        if staged_dir:
                            source = Path(staged_dir)
                            identity = _publish_asset_directory_at(
                                source,
                                source.name,
                                root_fd,
                                output_dir=output_dir,
                            )
                            published.append((source.name, identity, True))
                    for path, payload in (
                        (paths.final_result_path, snapshot_bytes),
                        (paths.provenance_path, provenance_bytes),
                        (paths.markdown_path, markdown_bytes),
                    ):
                        identity = _exclusive_write_at(
                            root_fd,
                            path.name,
                            payload,
                            output_dir=output_dir,
                        )
                        published.append((path.name, identity, False))
                    if root_fd is not None:
                        os.fsync(root_fd)
                    _verify_output_root(output_dir, root_identity)
                except FileExistsError as exc:
                    for name, identity, is_directory in reversed(published):
                        _cleanup_owned_output(
                            root_fd,
                            name,
                            identity,
                            is_directory,
                            output_dir=output_dir,
                        )
                    try:
                        reservation_identity = _exclusive_write_at(
                            root_fd,
                            paths.markdown_path.name,
                            b"",
                            output_dir=output_dir,
                        )
                    except FileExistsError:
                        pass
                    else:
                        reservations.append(
                            (paths.markdown_path, reservation_identity)
                        )
                    raise _ReportPublicationCollision() from exc
                except BaseException:
                    for name, identity, is_directory in reversed(published):
                        _cleanup_owned_output(
                            root_fd,
                            name,
                            identity,
                            is_directory,
                            output_dir=output_dir,
                        )
                    raise
            return str(paths.markdown_path)
        finally:
            if root_fd is not None:
                os.close(root_fd)


def _uses_windows_path_publication() -> bool:
    return os.name == "nt"


def _open_output_root(output_dir: Path) -> tuple[int | None, tuple[int, int]]:
    if _uses_windows_path_publication():
        try:
            metadata = output_dir.lstat()
        except OSError as exc:
            raise OSError("DeepResearch output root is unsafe") from exc
        if not is_direct_directory(metadata):
            raise OSError("DeepResearch output root is unsafe")
        return None, (metadata.st_dev, metadata.st_ino)
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(output_dir, flags, mode=0o700)
    except OSError as exc:
        raise OSError("DeepResearch output root is unsafe") from exc
    metadata = os.fstat(descriptor)
    if not stat.S_ISDIR(metadata.st_mode):
        os.close(descriptor)
        raise OSError("DeepResearch output root is unsafe")
    return descriptor, (metadata.st_dev, metadata.st_ino)


def _verify_output_root(
    output_dir: Path, expected_identity: tuple[int, int]
) -> None:
    if _uses_windows_path_publication():
        try:
            metadata = output_dir.lstat()
        except OSError as exc:
            raise OSError("DeepResearch output root changed") from exc
        if (
            not is_direct_directory(metadata)
            or (metadata.st_dev, metadata.st_ino) != expected_identity
        ):
            raise OSError("DeepResearch output root changed")
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(output_dir, flags, mode=0o700)
    except OSError as exc:
        raise OSError("DeepResearch output root changed") from exc
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or (metadata.st_dev, metadata.st_ino) != expected_identity
        ):
            raise OSError("DeepResearch output root changed")
    finally:
        os.close(descriptor)


def _exclusive_write_at(
    root_fd: int | None,
    name: str,
    payload: bytes,
    *,
    output_dir: Path | None = None,
) -> tuple[int, int]:
    if Path(name).name != name:
        raise OSError("unsafe report artifact name")
    if root_fd is None:
        if output_dir is None:
            raise OSError("DeepResearch output root is unavailable")
        return _exclusive_write(output_dir / name, payload)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(name, flags, 0o600, dir_fd=root_fd)
    identity: tuple[int, int] | None = None
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise OSError("unsafe report artifact")
        identity = (metadata.st_dev, metadata.st_ino)
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        return metadata.st_dev, metadata.st_ino
    except BaseException:
        # Close before unlinking: on Windows an open handle blocks unlink.
        with suppress(OSError):
            os.close(descriptor)
        if identity is not None:
            try:
                current = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
            except OSError:
                pass
            else:
                if (
                    stat.S_ISREG(current.st_mode)
                    and (current.st_dev, current.st_ino) == identity
                ):
                    with suppress(OSError):
                        os.unlink(name, dir_fd=root_fd)
        raise
    finally:
        with suppress(OSError):
            os.close(descriptor)


def _publish_asset_directory_at(
    source: Path,
    name: str,
    root_fd: int | None,
    *,
    output_dir: Path | None = None,
) -> tuple[int, int]:
    if Path(name).name != name:
        raise OSError("unsafe report asset name")
    if root_fd is None:
        if output_dir is None:
            raise OSError("DeepResearch output root is unavailable")
        return _publish_asset_directory_path(source, output_dir / name)
    os.mkdir(name, 0o700, dir_fd=root_fd)
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    directory_fd: int | None = None
    try:
        directory_fd = os.open(name, flags, dir_fd=root_fd)
        directory_metadata = os.fstat(directory_fd)
        if not stat.S_ISDIR(directory_metadata.st_mode):
            raise OSError("unsafe report asset directory")
        for entry_name in os.listdir(source):
            if Path(entry_name).name != entry_name:
                raise OSError("unsafe report asset")
            entry = source / entry_name
            before = entry.lstat()
            if (
                not stat.S_ISREG(before.st_mode)
                or stat.S_ISLNK(before.st_mode)
                or before.st_nlink != 1
            ):
                raise OSError("unsafe report asset")
            source_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            source_fd = os.open(entry, source_flags, mode=0o600)
            try:
                opened = os.fstat(source_fd)
                if (
                    not stat.S_ISREG(opened.st_mode)
                    or opened.st_nlink != 1
                    or (opened.st_dev, opened.st_ino)
                    != (before.st_dev, before.st_ino)
                ):
                    raise OSError("unsafe report asset")
                target_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
                target_flags |= getattr(os, "O_NOFOLLOW", 0)
                target_fd = os.open(
                    entry_name, target_flags, 0o600, dir_fd=directory_fd
                )
                try:
                    while True:
                        chunk = os.read(source_fd, 64 * 1024)
                        if not chunk:
                            break
                        os.write(target_fd, chunk)
                    os.fsync(target_fd)
                finally:
                    os.close(target_fd)
            finally:
                os.close(source_fd)
        os.fsync(directory_fd)
        return directory_metadata.st_dev, directory_metadata.st_ino
    except BaseException:
        if directory_fd is not None:
            for entry_name in os.listdir(directory_fd):
                with suppress(OSError):
                    os.unlink(entry_name, dir_fd=directory_fd)
        with suppress(OSError):
            os.rmdir(name, dir_fd=root_fd)
        raise
    finally:
        if directory_fd is not None:
            with suppress(OSError):
                os.close(directory_fd)


def _cleanup_owned_output(
    root_fd: int | None,
    name: str,
    identity: tuple[int, int],
    is_directory: bool,
    *,
    output_dir: Path | None = None,
) -> None:
    if root_fd is None:
        if output_dir is not None:
            _cleanup_owned_output_path(output_dir / name, identity, is_directory)
        return
    try:
        metadata = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
    except OSError:
        return
    wrong_output_type = (
        (metadata.st_dev, metadata.st_ino) != identity
        or (is_directory and not stat.S_ISDIR(metadata.st_mode))
        or (not is_directory and not stat.S_ISREG(metadata.st_mode))
    )
    if wrong_output_type:
        return
    if not is_directory:
        with suppress(OSError):
            os.unlink(name, dir_fd=root_fd)
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        directory_fd = os.open(name, flags, dir_fd=root_fd)
    except OSError:
        return
    try:
        for entry_name in os.listdir(directory_fd):
            with suppress(OSError):
                os.unlink(entry_name, dir_fd=directory_fd)
    finally:
        os.close(directory_fd)
    with suppress(OSError):
        os.rmdir(name, dir_fd=root_fd)


def _publish_asset_directory_path(
    source: Path, target: Path
) -> tuple[int, int]:
    target.mkdir(mode=0o700)
    metadata = target.lstat()
    identity = (metadata.st_dev, metadata.st_ino)
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise OSError("unsafe report asset directory")
    try:
        for entry_name in os.listdir(source):
            if Path(entry_name).name != entry_name:
                raise OSError("unsafe report asset")
            source_path = source / entry_name
            payload = _read_regular_file(
                source_path,
                limit=DEEPRESEARCH_ZIP_MEMBER_MAX_BYTES,
                label="report asset",
            )
            _exclusive_write(target / entry_name, payload)
        current = target.lstat()
        if (
            not stat.S_ISDIR(current.st_mode)
            or stat.S_ISLNK(current.st_mode)
            or (current.st_dev, current.st_ino) != identity
        ):
            raise OSError("unsafe report asset directory")
        return identity
    except BaseException:
        _cleanup_owned_output_path(target, identity, True)
        raise


def _cleanup_owned_output_path(
    path: Path, identity: tuple[int, int], is_directory: bool
) -> None:
    try:
        metadata = path.lstat()
    except OSError:
        return
    wrong_output_type = (
        (metadata.st_dev, metadata.st_ino) != identity
        or (is_directory and not stat.S_ISDIR(metadata.st_mode))
        or (not is_directory and not stat.S_ISREG(metadata.st_mode))
        or stat.S_ISLNK(metadata.st_mode)
    )
    if wrong_output_type:
        return
    if not is_directory:
        with suppress(OSError):
            path.unlink()
        return
    try:
        entries = list(path.iterdir())
    except OSError:
        return
    for entry in entries:
        try:
            child = entry.lstat()
        except OSError:
            continue
        if stat.S_ISREG(child.st_mode) and not stat.S_ISLNK(child.st_mode):
            with suppress(OSError):
                entry.unlink()
    with suppress(OSError):
        path.rmdir()


def _read_regular_file(path: Path, *, limit: int, label: str) -> bytes:
    before = path.lstat()
    invalid_named_file = (
        not stat.S_ISREG(before.st_mode)
        or stat.S_ISLNK(before.st_mode)
        or before.st_nlink != 1
        or before.st_size > limit
    )
    if invalid_named_file:
        raise OSError(f"unsafe {label}")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, mode=0o600)
    try:
        opened = os.fstat(descriptor)
        invalid_open_file = (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or opened.st_size > limit
            or (opened.st_dev, opened.st_ino)
            != (before.st_dev, before.st_ino)
        )
        if invalid_open_file:
            raise OSError(f"unsafe {label}")
        data = bytearray()
        while True:
            chunk = os.read(descriptor, min(64 * 1024, limit + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
            if len(data) > limit:
                raise OSError(f"unsafe {label}")
        after = os.fstat(descriptor)
        if (after.st_dev, after.st_ino) != (opened.st_dev, opened.st_ino):
            raise OSError(f"unsafe {label}")
        return bytes(data)
    finally:
        os.close(descriptor)


def _install_styled_bundle(bundle_root: Path, html_path: Path) -> None:
    """Install a validated styled bundle without mutating provenance assets."""
    bundle_root = Path(bundle_root)
    html_path = Path(html_path)
    output_dir = html_path.parent
    output_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    report_base = html_path.with_suffix("")
    infer_name = f"{report_base.name}_html_infer"
    chart_name = f"{report_base.name}_html_charts"
    html_bytes = _read_regular_file(
        bundle_root / "report.html",
        limit=DEEPRESEARCH_ZIP_MEMBER_MAX_BYTES,
        label="styled report HTML",
    )
    try:
        rendered = html_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise OSError("unsafe styled report HTML") from exc
    rendered = rendered.replace('href="infer/', f'href="{infer_name}/')
    rendered = rendered.replace("href='infer/", f"href='{infer_name}/")
    rendered = rendered.replace('src="charts/', f'src="{chart_name}/')
    rendered = rendered.replace("src='charts/", f"src='{chart_name}/")

    with _report_output_lock(output_dir):
        root_fd, root_identity = _open_output_root(output_dir)
        published: list[tuple[str, tuple[int, int], bool]] = []
        try:
            _verify_output_root(output_dir, root_identity)
            for source, target_name in (
                (bundle_root / "infer", infer_name),
                (bundle_root / "charts", chart_name),
            ):
                try:
                    source_metadata = source.lstat()
                except FileNotFoundError:
                    continue
                if (
                    not stat.S_ISDIR(source_metadata.st_mode)
                    or stat.S_ISLNK(source_metadata.st_mode)
                ):
                    raise OSError("unsafe report asset directory")
                if not any(source.iterdir()):
                    continue
                identity = _publish_asset_directory_at(
                    source,
                    target_name,
                    root_fd,
                    output_dir=output_dir,
                )
                published.append((target_name, identity, True))
            html_identity = _exclusive_write_at(
                root_fd,
                html_path.name,
                rendered.replace("\r\n", "\n").encode("utf-8"),
                output_dir=output_dir,
            )
            published.append((html_path.name, html_identity, False))
            if root_fd is not None:
                os.fsync(root_fd)
            _verify_output_root(output_dir, root_identity)
        except BaseException:
            for name, identity, is_directory in reversed(published):
                _cleanup_owned_output(
                    root_fd,
                    name,
                    identity,
                    is_directory,
                    output_dir=output_dir,
                )
            raise
        finally:
            if root_fd is not None:
                os.close(root_fd)


def _exclusive_write(path: Path, payload: bytes) -> tuple[int, int]:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    identity: tuple[int, int] | None = None
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise OSError("unsafe report artifact")
        identity = (metadata.st_dev, metadata.st_ino)
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        return metadata.st_dev, metadata.st_ino
    except BaseException:
        # Close before unlinking: on Windows an open handle blocks unlink.
        with suppress(OSError):
            os.close(descriptor)
        if identity is not None:
            try:
                current = path.lstat()
            except OSError:
                pass
            else:
                if (
                    stat.S_ISREG(current.st_mode)
                    and not stat.S_ISLNK(current.st_mode)
                    and (current.st_dev, current.st_ino) == identity
                ):
                    with suppress(OSError):
                        path.unlink()
        raise
    finally:
        with suppress(OSError):
            os.close(descriptor)


async def _generate_report_html(
    final_result: dict[str, Any],
    report_path_md: Path,
    fallback_markdown: str | None,
) -> tuple[Path, str, str | None, str | None] | None:
    """Prefer isolated SDK styling, then fall back to safe offline HTML."""
    report_path_html = report_path_md.with_suffix(".html")
    try:
        route = _get_route()
        runtime_config = _build_deepresearch_request_config(
            interactive_ask=False,
            service_id=str(route.get("service_id") or "default"),
            agent_id=str(route.get("agent_id") or "default"),
        )
        tls = normalize_child_tls_config({
            "LLM_SSL_VERIFY": runtime_config.get("LLM_SSL_VERIFY", "false"),
            "TOOL_SSL_VERIFY": runtime_config.get("TOOL_SSL_VERIFY", "false"),
        })
        manager = get_deepresearch_manager(_route_scope())
        llm_config = _build_styled_export_llm_config(runtime_config)
        llm_auth = _build_styled_export_llm_auth(runtime_config, llm_config)
        async with stylize_report_archive(
            final_result=final_result,
            llm_config=llm_config,
            llm_auth=llm_auth,
            tls=tls,
            manager=manager,
            session_id=str(route.get("session_id") or ""),
        ) as styled_archive:
            with tempfile.TemporaryDirectory(
                prefix="deepresearch-report-styled-"
            ) as temporary_dir:
                destination = Path(temporary_dir) / "extracted"
                bundle_root = _extract_styled_archive(styled_archive.path, destination)
                _install_styled_bundle(bundle_root, report_path_html)
        return (
            report_path_html,
            styled_archive.style_status,
            getattr(styled_archive, "style_phase", None),
            getattr(styled_archive, "style_reason_code", None),
        )
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logger.warning(
            "SDK styled HTML export failed; using offline conversion. type=%s",
            type(exc).__name__,
        )

    if not isinstance(fallback_markdown, str):
        return None
    try:
        from jiuwenswarm.agents.harness.common.tools.deepresearch_plugin.convert_html_offline import (
            convert_md_to_html,
        )

        with tempfile.TemporaryDirectory(
            prefix="deepresearch-report-fallback-"
        ) as temporary_dir:
            root = Path(temporary_dir)
            source = root / "report.md"
            target = root / "report.html"
            source.write_text(fallback_markdown, encoding="utf-8", newline="\n")
            convert_md_to_html(source, target)
            html_bytes = target.read_bytes()
        _exclusive_write(report_path_html, html_bytes)
        return report_path_html, "fallback", None, None
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logger.warning(
            "Offline HTML conversion failed type=%s", type(exc).__name__
        )
        return None


async def _write_report_artifacts_stream(
    final_result: dict[str, Any],
    file_name: str,
    conversation_id: str,
    citation_artifacts: object = None,
) -> tuple[dict[str, str], str | None, str | None, str | None]:
    report_path_md = Path(
        await asyncio.to_thread(
            _write_report_markdown,
            final_result,
            file_name,
            conversation_id,
            citation_artifacts,
        )
    )
    artifacts = {"md": str(report_path_md)}
    try:
        fallback_markdown = report_path_md.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        fallback_markdown = None
    generated_html = await _generate_report_html(
        final_result, report_path_md, fallback_markdown
    )
    html_style_status = None
    html_style_phase = None
    html_style_reason_code = None
    if generated_html is not None:
        (
            report_path_html,
            html_style_status,
            html_style_phase,
            html_style_reason_code,
        ) = generated_html
        artifacts["html"] = str(report_path_html)
    return (
        artifacts,
        html_style_status,
        html_style_phase,
        html_style_reason_code,
    )


def enable_deepresearch() -> bool:
    try:
        configured = get_config().get("enable_deepresearch", True)
    except Exception:
        return False
    return configured if isinstance(configured, bool) else False


__all__ = [
    "deepresearch_stream",
    "push_deepresearch_route",
    "reset_deepresearch_route",
]

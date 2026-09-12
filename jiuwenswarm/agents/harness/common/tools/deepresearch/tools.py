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
import re
import stat
import tempfile
import threading
import time
import unicodedata
import uuid
import zipfile
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

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
    ROUTER_LIMIT_ERROR,
    RouterState,
    _format_outline_card_markdown,
    advance_stage,
    build_interrupt_prompt,
    collected_questions,
    complete_final_report_processing,
    is_outline_status_placeholder,
    route_chunk,
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
from jiuwenswarm.server.gateway_push import WebSocketGatewayPushTransport
from jiuwenswarm.server.runtime.runtime_scope import RuntimeScopeKey
from jiuwenswarm.server.runtime.session.session_history import append_history_record

logger = logging.getLogger(__name__)

DEEPRESEARCH_CONFIG_PROTOCOL_VERSION = 1
DEEPRESEARCH_CONFIG_MAX_BYTES = 64 * 1024
DEEPRESEARCH_STDOUT_PENDING_MAX_BYTES = 16 * 1024 * 1024
DEEPRESEARCH_STDERR_TAIL_MAX_BYTES = 20_000
DEEPRESEARCH_ERROR_TEXT_MAX_CHARS = 2048
DEEPRESEARCH_STDERR_OUTCOME_MAX_CHARS = 2048
_HUAWEI_MAAS_PLACEHOLDER_API_KEY = "huawei-maas-session"
DEEPRESEARCH_STREAM_JSON_MAX_DEPTH = 64
DEEPRESEARCH_STREAM_JSON_MAX_NODES = 100_000
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
        "name",
        "node_id",
        "outline",
        "path",
        "pending_tasks",
        "prompt",
        "questions",
        "reasoning_content",
        "report",
        "report_chars",
        "report_delivered",
        "response_content",
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
_FINAL_PIPELINE_NODES = frozenset({
    "reporter",
    "vlm_chart_generator",
    "source_tracer",
    "source_tracer_infer",
    "brief_reporter",
    "brief_mermaid_generator",
    "brief_source_tracer",
})
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
    return config


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
    return {
        "general": {
            "model_name": model_name,
            "model_type": model_type,
            "base_url": base_url,
            "api_key": api_key,
            "extension": {
                "extra_body": {"thinking": {"type": "disabled"}},
            },
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
        return bool(_CHILD_ERROR_CODE_PATTERN.fullmatch(value))
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
            if text_chars > DEEPRESEARCH_STREAM_JSON_MAX_TEXT_CHARS:
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
    if not isinstance(error_code, str) or not _CHILD_ERROR_CODE_PATTERN.fullmatch(
        error_code
    ):
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
        if text_chars > DEEPRESEARCH_STREAM_JSON_MAX_TEXT_CHARS:
            return False
    return True


async def _write_deepresearch_config(proc: Any, frame: bytes) -> None:
    if proc.stdin is None:
        raise RuntimeError("DeepResearch config stdin is unavailable")
    try:
        proc.stdin.write(frame)
        await proc.stdin.drain()
    finally:
        proc.stdin.close()
        wait_closed = getattr(proc.stdin, "wait_closed", None)
        if callable(wait_closed):
            with suppress(BrokenPipeError, ConnectionResetError):
                await wait_closed()


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

    read = getattr(stream, "read", None)
    if not callable(read):
        async for line in stream:
            yield line
        return
    pending = bytearray()
    while True:
        chunk = await read(read_size)
        if not chunk:
            break
        pending.extend(chunk)
        if len(pending) > DEEPRESEARCH_STDOUT_PENDING_MAX_BYTES:
            logger.error(
                "[deepresearch_stream] stdout frame limit exceeded "
                "pending_bytes=%s limit_bytes=%s request_id=%s session_id=%s "
                "conversation_id=%s",
                len(pending),
                DEEPRESEARCH_STDOUT_PENDING_MAX_BYTES,
                *correlation_ids,
            )
            raise ValueError("deepresearch_stdout_limit_exceeded")
        while True:
            newline = pending.find(b"\n")
            if newline < 0:
                break
            yield bytes(pending[:newline])
            del pending[: newline + 1]
    if pending:
        yield bytes(pending)


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

    raise ValueError(f"unknown selected_options[0]={choice!r}")


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
        and node_state.get("agent_name") in _FINAL_PIPELINE_NODES
        and node_state.get("done") is True
        for node_state in state.active_nodes.values()
    )


async def _call_deepresearch_stream_impl(
    *, _router_state: object | None = None, **kwargs: Any
) -> str:
    token = _deepresearch_router_state_ctx.set(_router_state)
    try:
        stream_impl = getattr(deepresearch_stream, "_func")
        return await stream_impl(**kwargs)
    finally:
        _deepresearch_router_state_ctx.reset(token)


@tool(
    name="deepresearch_stream",
    description=(
        "Run or resume DeepResearch through the isolated external skill runner. "
        "Progress is pushed to the current Gateway route; only a terminal "
        "interrupted, completed, or structured error outcome is returned."
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
) -> str:
    skill_started_ns = time.monotonic_ns()
    route = _get_route()
    try:
        python_bin = resolve_python_executable()
    except DeepResearchRuntimeError as exc:
        code = str(exc)
        return json.dumps(
            {"status": "error", "error_code": code, "error": code},
            ensure_ascii=False,
        )
    script = _resolve_run_script()
    if not script:
        return json.dumps(
            {
                "status": "error",
                "error_code": "runner_missing",
                "error": "run_deepsearch.py not found",
            },
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

    try:
        config = _build_deepresearch_request_config(
            interactive_ask=True,
            service_id=str(route.get("service_id") or "default"),
            agent_id=str(route.get("agent_id") or "default"),
        )
        child_env = _build_deepresearch_child_env(
            interactive_ask=True,
            service_id=str(route.get("service_id") or "default"),
            agent_id=str(route.get("agent_id") or "default"),
            runtime_config=config,
            executable=python_bin,
        )
        config_frame = _encode_deepresearch_config(config)
    except DeepResearchRuntimeError as exc:
        code = str(exc)
        return json.dumps(
            {"status": "error", "error_code": code, "error": code},
            ensure_ascii=False,
        )
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
        return json.dumps(
            {
                "status": "error",
                "error_code": "search_config_missing",
                "error": search_error,
            },
            ensure_ascii=False,
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
    except asyncio.CancelledError:
        cleanup = asyncio.create_task(_stop_deepresearch_process(proc))
        await _await_cleanup_task(cleanup)
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
        outcome.get("status") in {"completed", "interrupted", "error"}
        and isinstance(outcome.get("timing"), dict)
    ):
        outcome["skill_execution_ms"] = max(
            0,
            (time.monotonic_ns() - skill_started_ns) // 1_000_000,
        )
    if outcome.get("status") in {"completed", "error", "cancelled"}:
        _clear_outline_title_cache(
            route, outcome.get("conversation_id", conversation_id)
        )
        _clear_outline_json_cache(
            route, outcome.get("conversation_id", conversation_id)
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
    state = (
        existing_state
        if isinstance(existing_state, RouterState)
        else RouterState(
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

    outcome: dict[str, Any] = {
        "status": "error",
        "error_code": "terminal_marker_missing",
        "error": "no terminal marker",
    }
    first_sdk_node_ns: int | None = None
    report_delivery_settled = False

    async def send_final_report_progress() -> None:
        if not state.final_report_started or report_delivery_settled:
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
        send_final_report_progress,
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
        }
        if first_sdk_node_ns is None and not is_terminal:
            is_timed_node = str(chunk.get("agent") or "") in _TIMED_SDK_NODES
            if is_timed_node and chunk.get("event") != "done":
                first_sdk_node_ns = time.monotonic_ns()
        if status_value in {"started", "resuming"}:
            outcome_cid = str(chunk.get("conversation_id") or outcome_cid)
            stage = 1
            if action == "resume" and node == "outline_interaction":
                stage = 2
            elif action == "resume" and node == "user_feedback_processor":
                stage = 4
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
                if outline_json_dict is not None and resolved_cid:
                    _cache_outline_json(route, resolved_cid, outline_json_dict)
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
                return attach_terminal_timing({
                    "status": "error",
                    "conversation_id": chunk.get("conversation_id", outcome_cid),
                    "error_code": "empty_report",
                    "error": "completed marker missing final_result.response_content",
                }, chunk)
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
                    "error": "Markdown report file route is unavailable",
                }, chunk)
            try:
                artifact_result = await _await_with_periodic_progress(
                    _write_report_artifacts_stream(
                        final_result,
                        file_name,
                        str(chunk.get("conversation_id") or outcome_cid),
                        _normalize_citation_artifacts(chunk),
                    ),
                    send_final_report_progress,
                )
            except Exception as exc:  # pylint: disable=broad-exception-caught
                return attach_terminal_timing({
                    "status": "error",
                    "conversation_id": chunk.get("conversation_id", outcome_cid),
                    "error_code": "report_file_write_failed",
                    "error": type(exc).__name__,
                }, chunk)
            html_style_phase = None
            html_style_reason_code = None
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
                html_style_status = None
            files = [
                {"path": value, "name": Path(value).name}
                for value in artifacts.values()
            ]
            file_payload: dict[str, Any] = {
                "event_type": "chat.file",
                "files": files,
            }
            markdown_index = list(artifacts).index("md")
            bundle = _build_related_artifact_bundle(chunk, markdown_index)
            file_metadata: dict[str, Any] = {}
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
                    "report_path": artifacts.get("md", ""),
                }, chunk)
            try:
                history_extra: dict[str, Any] = {"files": files}
                if file_metadata:
                    history_extra["metadata"] = file_metadata
                append_history_record(
                    session_id=str(route["session_id"]),
                    request_id=str(route.get("request_id") or ""),
                    channel_id=str(route["channel_id"]),
                    role="assistant",
                    event_type="chat.file",
                    content="",
                    timestamp=time.time(),
                    extra=history_extra,
                )
            except Exception:  # pylint: disable=broad-exception-caught
                logger.warning(
                    "[deepresearch_stream] persist chat.file history failed "
                    "request_id=%s session_id=%s",
                    _safe_log_correlation_id(route.get("request_id")),
                    _safe_log_correlation_id(route.get("session_id")),
                    exc_info=True,
                )
            report_delivery_settled = True
            for payload in advance_stage(state, 4, complete=True):
                await send(payload)
            completed_outcome: dict[str, Any] = {
                "status": "completed",
                "conversation_id": chunk.get("conversation_id", outcome_cid),
                "report_delivered": True,
                "report_chars": len(response_content),
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
        if status_value == "error":
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

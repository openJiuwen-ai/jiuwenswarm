# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Deterministic, user-compatible orchestration for DeepResearch."""

from __future__ import annotations

import asyncio
import weakref
import json
import inspect
import logging
import re
import time
import uuid
from contextvars import ContextVar, Token
from dataclasses import dataclass, field, replace
from typing import Any, Awaitable, Callable, Mapping

from openjiuwen.core.foundation.tool import tool

from jiuwenswarm.common.schema.ask_user import (
    AskUserResponse,
    AskUserResponseError,
    decode_user_input,
    parse_ask_user_response,
)
from jiuwenswarm.perf.context import DeepResearchReportType

from .brief_refresh import build_brief_refresh_query
from .stream_router import _format_outline_card_markdown, stage_for_resume_node
from .tools import (
    MODEL_RESEARCH_FALLBACK_ERRORS,
    MODEL_RESEARCH_FALLBACK_HINT,
    build_model_research_fallback,
    _call_deepresearch_stream_impl,
    _deepresearch_stage_hint_ctx,
    _get_brief_upgrade_record,
    _get_cached_outline_json,
    _get_route,
    _probe_resumable_checkpoint,
    _report_title_from_file_name,
    _scan_resumable_checkpoint_by_query,
    salvage_deepresearch_report,
)
from .usage import normalize_workflow_llm_token_usage

logger = logging.getLogger(__name__)

EXECUTION_SCHEMA = "openjiuwen.deepresearch.execute.v1"
_STATE_SCHEMA_VERSION = 1
MODEL_RESEARCH_HANDOFF_ERRORS = MODEL_RESEARCH_FALLBACK_ERRORS | {"materials_preparation_failed"}
_IN_FLIGHT_PHASES = frozenset(
    {"preparing_materials", "starting", "resuming_feedback", "resuming_outline", "resuming_stopped"}
)
_QUESTION_NUMBER_PREFIX = re.compile(r"^\s*\d+[.、)]\s*")
_OUTLINE_SECTION = re.compile(r"^\s*###\s+P\d+\s*:\s*(.+?)\s*$", re.MULTILINE)
_JSON_FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL | re.IGNORECASE)
_FINISH_KEYWORDS = re.compile(r"(?:结束|完成|finish|\bend\b)", re.IGNORECASE)
_OTHER_LABELS = frozenset({"其他", "Other"})
_OPTION_DESCRIPTION_MAX_CHARS = 50
_OPTIONS_MAX_TOKENS = 2048
_OPTIONS_GENERATION_TIMEOUT_SECONDS = 60.0
_MAX_TIMING_WINDOWS = 8
_MAX_TIMING_SPANS = 128


@dataclass(frozen=True)
class DeepResearchExecutionContext:
    """Per-tool-call runtime data supplied by DeepResearchExecutionRail."""

    tool_call_id: str
    state: dict[str, Any] | None
    user_input: Any
    model: Any
    agent_id: str
    save_state: Callable[[dict[str, Any]], None]
    request_id: str = ""
    requested_report_type: DeepResearchReportType | None = None
    # 结构化升级动作（relay 校验后经请求上下文传入）：phase=new 启动 SDK 前
    # 解析并强制 strict 绑定最新来源记录；非 Mapping 一律按未携带处理。
    deepresearch_followup: Mapping[str, Any] | None = None
    materials_context: dict[str, Any] | None = None
    mcp_servers: list[dict[str, Any]] | None = field(default=None, repr=False)
    mcp_context_provider: Callable[[], Any] | None = field(default=None, repr=False)
    load_state: Callable[[], dict[str, Any] | None] | None = None


_execution_context: ContextVar[DeepResearchExecutionContext | None] = ContextVar(
    "jiuwenswarm_deepresearch_execution", default=None
)


def bind_deepresearch_execution_context(
    *,
    tool_call_id: str,
    state: dict[str, Any] | None,
    user_input: Any,
    model: Any,
    save_state: Callable[[dict[str, Any]], None],
    agent_id: str = "jiuwenswarm",
    request_id: str = "",
    requested_report_type: DeepResearchReportType | None = None,
    deepresearch_followup: Mapping[str, Any] | None = None,
    materials_context: dict[str, Any] | None = None,
    mcp_servers: list[dict[str, Any]] | None = None,
    mcp_context_provider: Callable[[], Any] | None = None,
    load_state: Callable[[], dict[str, Any] | None] | None = None,
) -> Token:
    """Bind state and resume input for one outer tool execution."""
    return _execution_context.set(
        DeepResearchExecutionContext(
            tool_call_id=tool_call_id,
            state=dict(state) if isinstance(state, dict) else None,
            user_input=user_input,
            model=model,
            agent_id=agent_id.strip() or "jiuwenswarm",
            save_state=save_state,
            request_id=request_id.strip(),
            requested_report_type=requested_report_type,
            deepresearch_followup=(
                dict(deepresearch_followup)
                if isinstance(deepresearch_followup, Mapping)
                else None
            ),
            materials_context=materials_context,
            mcp_servers=mcp_servers,
            mcp_context_provider=mcp_context_provider,
            load_state=load_state,
        )
    )


def reset_deepresearch_execution_context(token: Token | None) -> None:
    """Restore the previous execution context."""
    if token is not None:
        _execution_context.reset(token)


def _persist(
    context: DeepResearchExecutionContext,
    state: dict[str, Any],
    phase: str,
    **updates: Any,
) -> dict[str, Any]:
    next_state = dict(state)
    next_state.update(updates)
    next_state["schema_version"] = _STATE_SCHEMA_VERSION
    next_state["phase"] = phase
    next_state["revision"] = int(next_state.get("revision") or 0) + 1
    context.save_state(next_state)
    logger.info(
        "[deepresearch_execute] state transition tool_call_id=%s phase=%s revision=%d",
        context.tool_call_id,
        phase,
        next_state["revision"],
    )
    return next_state


def _persist_delivered_state(
    context: Any,
    *,
    workflow_id: str,
    conversation_id: str,
    query: str = "",
    file_name: str = "",
    requested_report_type: str = "brief",
) -> dict[str, Any]:
    """构造一个 phase=completed 的只读 state 供幂等短路返回，不写 session（rail 负责）。

    context/workflow_id 仅用于与 _persist 对齐调用形态，实际未使用——短路返回的
    state 不落盘。query/file_name/requested_report_type 由调用方从交付索引与工具
    入参取真实值传入，使 _completion_content 能输出真实报告名而非默认「研究报告」。
    """
    return {
        "schema_version": _STATE_SCHEMA_VERSION,
        "phase": "completed",
        "query": query,
        "file_name": file_name,
        "conversation_id": conversation_id,
        "requested_report_type": requested_report_type,
        "revision": 0,
    }


def _result(kind: str, state: dict[str, Any], **fields: Any) -> dict[str, Any]:
    result = {
        "schema_version": EXECUTION_SCHEMA,
        "kind": kind,
        **fields,
        "state": state,
    }
    windows = state.get("timing_windows")
    if isinstance(windows, list) and windows:
        result["timing_windows"] = windows
        last = windows[-1]
        if isinstance(last, Mapping):
            for key in (
                "timing",
                "skill_execution_ms",
                "report_delivery_ms",
                "conversation_id",
                "status",
            ):
                if key in last:
                    result[key] = last[key]
    return result


def _nonnegative_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _sanitize_timing(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping) or value.get("schema_version") not in {1, 2}:
        return None
    required = {
        key: _nonnegative_int(value.get(key))
        for key in ("runner_total_ms", "runner_bootstrap_ms", "sdk_execution_ms")
    }
    if any(item is None for item in required.values()):
        return None
    timing: dict[str, Any] = {
        "schema_version": value["schema_version"],
        **required,
    }
    for key in ("sdk_first_node_ms", "skill_to_sdk_first_node_ms"):
        duration = _nonnegative_int(value.get(key))
        if duration is not None:
            timing[key] = duration
    spans = value.get("sdk_node_spans")
    if isinstance(spans, list) and len(spans) <= _MAX_TIMING_SPANS:
        safe_spans: list[dict[str, Any]] = []
        for span in spans:
            if not isinstance(span, Mapping):
                safe_spans = []
                break
            agent = str(span.get("agent") or "").strip()
            section_idx = str(span.get("section_idx") or "").strip()
            started_ms = _nonnegative_int(span.get("started_ms"))
            ended_ms = _nonnegative_int(span.get("ended_ms"))
            duration_ms = _nonnegative_int(span.get("duration_ms"))
            completed = span.get("completed")
            invalid_agent = not agent or len(agent) > 128
            invalid_section = not section_idx.isdecimal() or len(section_idx) > 10
            invalid_duration = (
                started_ms is None or ended_ms is None or duration_ms is None
            )
            if invalid_agent or invalid_section or invalid_duration:
                safe_spans = []
                break
            if not isinstance(completed, bool):
                safe_spans = []
                break
            safe_spans.append(
                {
                    "agent": agent,
                    "section_idx": section_idx,
                    "started_ms": started_ms,
                    "ended_ms": ended_ms,
                    "duration_ms": duration_ms,
                    "completed": completed,
                }
            )
        if safe_spans or not spans:
            timing["sdk_node_spans"] = safe_spans
    return timing


def _sanitize_timing_window(
    value: Any,
    *,
    action: str = "",
    node: str = "",
) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    timing = _sanitize_timing(value.get("timing"))
    if timing is None:
        return None
    safe_action = str(value.get("action") or action).strip()
    safe_node = str(value.get("node") or node).strip()
    status = str(value.get("status") or "").strip()
    conversation_id = str(value.get("conversation_id") or "").strip()
    window: dict[str, Any] = {
        "action": safe_action if safe_action in {"start", "resume"} else "",
        "node": safe_node[:128],
        "status": status[:128],
        "conversation_id": conversation_id[:128],
        "timing": timing,
    }
    for key in ("skill_execution_ms", "report_delivery_ms"):
        duration = _nonnegative_int(value.get(key))
        if duration is not None:
            window[key] = duration
    return window


def _append_timing_window(
    state: dict[str, Any],
    outcome: Mapping[str, Any],
    *,
    action: str,
    node: str,
) -> dict[str, Any]:
    current = _sanitize_timing_window(outcome, action=action, node=node)
    if current is None:
        return state
    existing = state.get("timing_windows")
    windows = []
    for item in existing if isinstance(existing, list) else []:
        sanitized = _sanitize_timing_window(item)
        if sanitized is not None:
            windows.append(sanitized)
    next_state = dict(state)
    next_state["timing_windows"] = [*windows, current][-_MAX_TIMING_WINDOWS:]
    return next_state


def _parse_response(value: Any) -> AskUserResponse:
    decoded = decode_user_input(value)
    if not isinstance(decoded, Mapping):
        raise AskUserResponseError("AskUser response must be an object")
    if decoded.get("status") in {"error", "cancelled"}:
        raise AskUserResponseError(str(decoded.get("status")))
    return parse_ask_user_response(decoded)


def _terminal_interaction_status(value: Any) -> str:
    decoded = decode_user_input(value)
    if not isinstance(decoded, Mapping):
        return ""
    status = str(decoded.get("status") or "").strip().lower()
    return status if status in {"error", "cancelled"} else ""


def _split_questions(raw: Any) -> list[str]:
    candidates: list[Any]
    if isinstance(raw, list):
        candidates = raw
    elif isinstance(raw, str):
        candidates = raw.splitlines()
    else:
        candidates = []
    questions: list[str] = []
    for item in candidates:
        if isinstance(item, Mapping):
            item = item.get("question") or item.get("content") or ""
        if not isinstance(item, str):
            continue
        question = _QUESTION_NUMBER_PREFIX.sub("", item).strip()
        if question:
            questions.append(question)
    return questions


def _response_text(response: Any) -> str:
    content = getattr(response, "content", response)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, Mapping) and isinstance(item.get("text"), str):
                parts.append(item["text"])
            elif isinstance(getattr(item, "text", None), str):
                parts.append(item.text)
        return "".join(parts)
    return str(content or "")


def _options_model_name(model: Any) -> str:
    for source in (
        model,
        getattr(model, "model_config", None),
        getattr(model, "model_client_config", None),
    ):
        if source is None:
            continue
        for key in ("model_name", "model"):
            value = (
                source.get(key)
                if isinstance(source, Mapping)
                else getattr(source, key, None)
            )
            name = str(value or "").strip()
            if name:
                return name
    return ""


def _record_options_llm_perf(
    context: DeepResearchExecutionContext,
    *,
    response: Any,
    duration_ms: float,
    status: str,
    error_message: str | None = None,
) -> None:
    """Count the direct Options call in the same request summary as Main Agent LLMs."""
    try:
        from jiuwenswarm.perf.collector import get_perf_collector
        from jiuwenswarm.perf.context import (
            get_react_iteration,
            get_request_context,
            resolve_task_id,
        )
        from jiuwenswarm.perf.events import LlmPerfEvent
        from jiuwenswarm.perf.extract import extract_usage_tokens

        request_context = get_request_context()
        request_id = str(
            (request_context or {}).get("request_id") or context.request_id or ""
        ).strip()
        if not request_id:
            return
        input_tokens, output_tokens, cache_read, reasoning_tokens = extract_usage_tokens(
            response
        )
        collector = get_perf_collector()
        accumulator = collector.get_accumulator(request_id)
        if accumulator is not None and cache_read:
            accumulator.cache_read_tokens += cache_read
        collector.record_llm(
            request_id,
            LlmPerfEvent(
                llm_call_id=f"deepresearch_options_{time.monotonic_ns()}",
                duration_ms=max(0.0, duration_ms),
                model=_options_model_name(context.model),
                iteration=get_react_iteration(),
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                status=status,
                agent_id=context.agent_id,
                task_id=resolve_task_id(request_ctx=request_context),
                stream_source_id="deepresearch_options",
                error_message=error_message,
                reasoning_tokens=reasoning_tokens,
            ),
        )
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logger.debug(
            "[deepresearch_execute] option perf recording skipped type=%s",
            type(exc).__name__,
        )


def _parse_option_payload(raw: str, count: int) -> list[list[dict[str, str]]] | None:
    fenced = _JSON_FENCE.match(raw)
    if fenced:
        raw = fenced.group(1)
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return None
    items = payload.get("items") if isinstance(payload, Mapping) else None
    if not isinstance(items, list) or len(items) != count:
        return None
    parsed: list[list[dict[str, str]] | None] = [None] * count
    for item in items:
        if not isinstance(item, Mapping):
            return None
        index = item.get("question_index")
        options = item.get("options")
        if not isinstance(index, int) or not 0 <= index < count:
            return None
        if parsed[index] is not None or not isinstance(options, list):
            return None
        if not 2 <= len(options) <= 3:
            return None
        normalized = []
        labels = set()
        for option in options:
            if not isinstance(option, Mapping):
                return None
            label = option.get("label")
            if not isinstance(label, str):
                return None
            label = label.strip()
            if not label or len(label) > 50:
                return None
            if label in _OTHER_LABELS or label in labels:
                return None
            labels.add(label)
            normalized_option = {"label": label}
            description = option.get("description")
            if isinstance(description, str) and description.strip():
                normalized_option["description"] = description.strip()[
                    :_OPTION_DESCRIPTION_MAX_CHARS
                ]
            normalized.append(normalized_option)
        parsed[index] = normalized
    return parsed if all(item is not None for item in parsed) else None  # type: ignore[return-value]


async def _generate_options(
    context: DeepResearchExecutionContext,
    query: str,
    questions: list[str],
) -> list[list[dict[str, str]]]:
    """Generate only answer options; SDK question text remains authoritative."""
    model = context.model
    if model is None or not callable(getattr(model, "invoke", None)):
        return [[] for _ in questions]
    prompt = (
        "你只负责为 DeepResearch 已生成的问题提供便于点击的候选答案，不得改写问题。"
        "每题生成 2-3 个具体、互斥、符合问题语境的选项；label 不超过 50 字；"
        "description 不超过 50 字；不得输出分析或推理过程；"
        "不要生成‘其他’或 Other，前端会自动提供自由输入。只输出严格 JSON："
        '{"items":[{"question_index":0,"options":[{"label":"...",'
        '"description":"可选"}]}]}。\n'
        f"研究主题：{query}\n问题：{json.dumps(questions, ensure_ascii=False)}"
    )
    started = time.monotonic()
    attempt_started = started
    try:
        async with asyncio.timeout(_OPTIONS_GENERATION_TIMEOUT_SECONDS):
            for attempt in range(2):
                attempt_started = time.monotonic()
                try:
                    response = await model.invoke(
                        [{"role": "user", "content": prompt}],
                        max_tokens=_OPTIONS_MAX_TOKENS,
                    )
                    _record_options_llm_perf(
                        context,
                        response=response,
                        duration_ms=(time.monotonic() - attempt_started) * 1000,
                        status="ok",
                    )
                    parsed = _parse_option_payload(_response_text(response), len(questions))
                    if parsed is not None:
                        logger.info(
                            "[deepresearch_execute] option generation completed attempts=%d "
                            "questions=%d duration_ms=%.1f",
                            attempt + 1,
                            len(questions),
                            (time.monotonic() - started) * 1000,
                        )
                        return parsed
                except Exception as exc:  # pylint: disable=broad-exception-caught
                    _record_options_llm_perf(
                        context,
                        response=None,
                        duration_ms=(time.monotonic() - attempt_started) * 1000,
                        status="error",
                        error_message=type(exc).__name__,
                    )
                    logger.warning(
                        "[deepresearch_execute] option generation failed attempt=%d type=%s",
                        attempt + 1,
                        type(exc).__name__,
                    )
    except TimeoutError:
        _record_options_llm_perf(
            context,
            response=None,
            duration_ms=(time.monotonic() - attempt_started) * 1000,
            status="error",
            error_message="TimeoutError",
        )
        logger.warning(
            "[deepresearch_execute] option generation timed out "
            "questions=%d timeout_s=%.1f",
            len(questions),
            _OPTIONS_GENERATION_TIMEOUT_SECONDS,
        )
    logger.warning(
        "[deepresearch_execute] option generation fell back to free text "
        "questions=%d duration_ms=%.1f",
        len(questions),
        (time.monotonic() - started) * 1000,
    )
    return [[] for _ in questions]


def _current_mcp_servers(context: DeepResearchExecutionContext, state: dict) -> list[dict] | None:
    """Resume with current credentials for original sources only (never persist secrets)."""
    from .mcp_search import source_identity
    identities = state.get("mcp_source_identities") or {}
    return [server for server in context.mcp_servers or []
            if identities.get(server["server_name"]) == source_identity(server)] or None


async def _call_sdk(
    context: DeepResearchExecutionContext,
    **kwargs: Any,
) -> str:
    """Measure one low-level SDK window without logging business content."""
    if kwargs.get("_materials_reference") is None:
        kwargs.pop("_materials_reference", None)
    provider = getattr(context, "mcp_context_provider", None)
    if kwargs.get("_mcp_servers") and callable(provider):
        from .mcp_search import source_identity
        identities = {source_identity(server) for server in kwargs["_mcp_servers"]}
        current = provider()
        if inspect.isawaitable(current):
            current = await current
        kwargs["_mcp_servers"] = [server for server in current or [] if source_identity(server) in identities] or None
    if kwargs.get("_mcp_servers") is None:
        kwargs.pop("_mcp_servers", None)
    started = time.monotonic()
    outcome_status = "exception"
    try:
        result = await _call_deepresearch_stream_impl(**kwargs)
        try:
            decoded = json.loads(result) if isinstance(result, str) else result
        except (TypeError, ValueError):
            decoded = None
        if isinstance(decoded, Mapping):
            outcome_status = str(decoded.get("status") or "unknown")
        else:
            outcome_status = "invalid"
        return result
    finally:
        logger.info(
            "[deepresearch_execute] sdk window tool_call_id=%s action=%s node=%s "
            "status=%s duration_ms=%.1f",
            context.tool_call_id,
            str(kwargs.get("action") or ""),
            str(kwargs.get("node") or ""),
            outcome_status,
            (time.monotonic() - started) * 1000,
        )


def _marker_prompt(outcome: Mapping[str, Any]) -> str:
    marker = outcome.get("marker")
    if not isinstance(marker, Mapping):
        marker = {}
    for value in (
        marker.get("prompt"),
        marker.get("content"),
        outcome.get("prompt"),
    ):
        if isinstance(value, str) and value.strip():
            return value.strip()
    return "研究进行中，请输入您对当前研究方向的反馈意见："


def _interaction_json(value: Any) -> str:
    decoded = decode_user_input(value)
    if isinstance(decoded, Mapping):
        return json.dumps(dict(decoded), ensure_ascii=False, separators=(",", ":"))
    return ""


def _feedback_payload(user_input: Any, questions: list[str]) -> tuple[str, str]:
    response = _parse_response(user_input)
    interaction_result = _interaction_json(user_input)
    if response.status == "skipped" or not response.answers:
        return (
            json.dumps(
                {"feedback": "", "interaction_status": "skipped"},
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            interaction_result,
        )
    answers = list(response.answers)
    ordered: list[tuple[str, Any | None]] = []
    if questions:
        used: set[int] = set()
        for question_index, question in enumerate(questions):
            match_index = next(
                (
                    index
                    for index, answer in enumerate(answers)
                    if index not in used and answer.question == question
                ),
                None,
            )
            if match_index is None and question_index < len(answers):
                candidate = answers[question_index]
                if not candidate.question and question_index not in used:
                    match_index = question_index
            if match_index is None:
                ordered.append((question, None))
            else:
                used.add(match_index)
                ordered.append((question, answers[match_index]))
        ordered.extend(
            (answer.question, answer)
            for index, answer in enumerate(answers)
            if index not in used
        )
    else:
        ordered = [(answer.question, answer) for answer in answers]

    blocks = []
    answer_texts = []
    for index, (question, answer) in enumerate(ordered):
        if answer is None:
            readable = "未回答"
            answer_texts.append(readable)
            blocks.append(f"问题{index + 1}: {question}\n回答: {readable}")
            continue
        values = [*answer.selected_options]
        if answer.custom_input:
            values.append(answer.custom_input)
        readable = "、".join(values) or "未回答"
        answer_texts.append(readable)
        blocks.append(f"问题{index + 1}: {question}\n回答: {readable}")
    merged = "\n\n".join(blocks)
    feedback = (
        "finish"
        if _FINISH_KEYWORDS.search("\n".join(answer_texts))
        else merged
    )
    return (
        json.dumps({"feedback": feedback}, ensure_ascii=False, separators=(",", ":")),
        interaction_result,
    )


def _outline_markdown(marker: Mapping[str, Any]) -> str:
    outline = marker.get("outline")
    if isinstance(outline, str) and outline.strip():
        return outline.strip()
    if isinstance(outline, dict):
        rendered = _format_outline_card_markdown(outline)
        if rendered:
            return rendered
    preview = marker.get("preview")
    if isinstance(preview, Mapping) and isinstance(preview.get("text"), str):
        return preview["text"].strip()
    return "大纲已生成，请确认是否继续研究。"


def _outline_sections(outline_text: str) -> list[str]:
    return [
        re.sub(r"\s*[（(]重点[）)]\s*$", "", match).strip()
        for match in _OUTLINE_SECTION.findall(outline_text)[:20]
    ]


def _validated_outline_sections(value: object) -> list[str]:
    """校验流式回传的 outline_sections；不可用返回空列表，调用方不覆盖原值。"""
    if not isinstance(value, list) or not value:
        return []
    titles: list[str] = []
    for item in value:
        if not isinstance(item, str):
            return []
        title = item.strip()
        if not title:
            return []
        titles.append(title)
    return titles


def _upgrade_file_name(original_file_name: str) -> str:
    """升级执行的文件名从来源记录派生（同源约束），已带「完整版」则保留。"""
    stem = str(original_file_name or "").strip()
    if not stem:
        return "完整版"
    stem = _report_title_from_file_name(stem)
    if "完整版" in stem:
        return stem
    return f"{stem}完整版"


def _validated_brief_report(value: object) -> dict[str, Any]:
    """校验流式回传的 brief_report 小描述；不可用返回空 dict，调用方不附带。"""
    if not isinstance(value, Mapping):
        return {}
    report_id = value.get("report_id")
    title = value.get("title")
    upgrade_available = value.get("upgrade_available")
    if (
        value.get("v") != 1
        or value.get("report_type") != "brief"
        or value.get("report_delivered") is not True
    ):
        return {}
    if not isinstance(report_id, str) or not report_id.strip():
        return {}
    if not isinstance(title, str) or not isinstance(upgrade_available, bool):
        return {}
    return {
        "v": 1,
        "report_id": report_id.strip(),
        "report_type": "brief",
        "report_delivered": True,
        "title": title.strip() or "研究报告",
        "upgrade_available": upgrade_available,
    }


def _outline_review_interaction(outline_text: str) -> dict[str, Any]:
    """The outline review ask-user card shared by all entry paths."""
    return {
        "query": "请审阅生成的研究报告大纲",
        "return_json": True,
        "questions": [
            {
                "header": "研究报告大纲审阅",
                "question": "请审阅生成的研究报告大纲，确认后将继续执行深度研究。",
                "options": [
                    {"label": "确认大纲，继续研究"},
                    {"label": "需要修改"},
                ],
                "preview": {
                    "title": "研究报告大纲",
                    "text": outline_text,
                    "format": "markdown",
                    "editable": True,
                },
            }
        ],
    }


def _completion_content(
    state: Mapping[str, Any],
    html_style_status: Any = None,
    brief_upgrade_available: bool = False,
) -> str:
    title = _report_title_from_file_name(str(state.get("file_name") or ""))
    lines = [
        "✅ **深度研究已完成！**",
        "",
        f"{title or '研究报告'}已成功生成并交付。",
    ]
    sections = state.get("outline_sections")
    if isinstance(sections, list):
        lines.extend(["", "**报告概览**："])
    normalized_sections = [
        str(section).strip()
        for section in (sections if isinstance(sections, list) else [])
        if str(section).strip()
    ]
    if normalized_sections:
        lines.append(
            f"- **报告结构**：按您确认的大纲，包含 {len(normalized_sections)} 个核心章节"
        )
        lines.extend(
            f"  {index}. {section}"
            for index, section in enumerate(normalized_sections, start=1)
        )
    if html_style_status == "fallback":
        lines.extend(
            [
                "",
                "⚠️ HTML 已交付内置基础视觉模板，但 AI 生成的增强样式未应用；"
                "Markdown 报告内容不受影响。",
            ]
        )
    lines.extend(
        [
            "",
            "报告已通过系统自动交付，您可以直接查看完整内容。",
        ]
    )
    # brief→professional 升级追问：仅精简报告且 metadata 缓存成功时附带的
    # 话术（缓存缺失不承诺"沿用大纲"，直接不问）。rail 以本文案强收尾，
    # Main Agent 在工具完成后没有下一轮 LLM，追问只能由工具侧拼装。
    if (
        str(state.get("requested_report_type") or "") == "brief"
        and brief_upgrade_available
    ):
        lines.extend(
            [
                "",
                "📄 以上为精简版报告。如需基于此报告大纲生成完整的专业版深度研究报告，"
                "请回复「生成完整报告」。",
            ]
        )
    return "\n".join(lines)


def _outline_feedback(user_input: Any) -> tuple[str, str]:
    response = _parse_response(user_input)
    interaction_result = _interaction_json(user_input)
    if response.status == "skipped" or not response.answers:
        return '{"interrupt_feedback":"accepted","feedback":""}', interaction_result
    answer = response.answers[0]
    selected = " ".join(answer.selected_options)
    if "确认大纲" in selected:
        return '{"interrupt_feedback":"accepted","feedback":""}', interaction_result
    # The low-level tool resolves edited outline feedback from the canonical result.
    return "", interaction_result


def _model_research_fallback_result(state: dict[str, Any]) -> dict[str, Any]:
    """Return an unavailable-engine result for the main model, not a report."""
    query = str(state.get("original_query") or state.get("query") or "")
    content = MODEL_RESEARCH_FALLBACK_HINT
    if state.get("error_code") == "materials_preparation_failed":
        content = "本次材料准备失败，交由模型研究继续处理。" + content + (
            "不得绕过已有文件权限或网络限制；使用正常授权工具处理可读取的材料，"
            "不可用的核心材料不得声称已读取。保留原始研究目标，必要时请求用户补充材料，"
            "并在报告中客观说明证据缺口，不编造材料内容。"
        )
    return _result(
        "error", state,
        error_code=str(state.get("error_code") or ""),
        fallback=build_model_research_fallback(query, str(state.get("file_name") or "")),
        content=content,
    )


def _terminal_error(
    context: DeepResearchExecutionContext,
    state: dict[str, Any],
    *,
    error_code: str,
    content: str,
    **diagnostics: Any,
) -> dict[str, Any]:
    state = _persist(context, state, "error", error_code=error_code)
    return _result(
        "error",
        state,
        error_code=error_code,
        content=content,
        **diagnostics,
    )


def _stopped_result(
    context: DeepResearchExecutionContext,
    state: Mapping[str, Any],
    conversation_id: str,
) -> dict[str, Any]:
    """优雅停止终态：phase 回落 new 并保留 conversation_id。

    下一次调用可凭 probe/扫库认领停止快照直通 resume node='stopped'，
    已完成节点不重跑。修复11：文案禁止模型在用户停止后自主重启
    （仅用户明确要求继续时才重调，凭原主题 query 即可被修复9' 的
    扫库认领接住）；content 不含 conversation_id（与 rail 改写口径
    对齐——cid 是模型直通重启的最短路径，rail 挂载与否不应影响
    该防线；认领与恢复靠 query + 归属守卫，cid 的功能载体是
    state 而非 content 文本）。恢复入口给双指引：execute 优先，
    其不可见时（停止后新请求 skill 激活态重置，execute 被
    ProgressiveToolRail 过滤）回落 deepresearch_stream action='start'
    ——stream 侧已同口径扫库认领（2026-10-15 19:52 回归实证：单
    execute 指引是死信，模型绕行 stream 全新起跑丢快照）。
    """
    state = _persist(context, state, "new", conversation_id=conversation_id)
    return _result(
        "stopped",
        state,
        content=(
            "DeepResearch 任务已被用户停止，执行进度已保存。"
            "请勿代替用户决定继续，也不要自动重新调用本工具；仅当用户"
            "明确要求继续该研究时才恢复执行：优先再次调用 "
            "deepresearch_execute（传入原研究主题即可自动认领停止快照），"
            "若该工具当前不可见则调用 deepresearch_stream（action='start'，"
            "传入原研究主题，同样会自动认领）。届时将从停止处恢复，已完成"
            "阶段不会重跑。"
        ),
    )


async def _handle_outcome(
    context: DeepResearchExecutionContext,
    state: dict[str, Any],
    raw_outcome: Any,
    *,
    action: str,
    node: str = "",
) -> dict[str, Any]:
    try:
        outcome = json.loads(raw_outcome) if isinstance(raw_outcome, str) else raw_outcome
    except (TypeError, ValueError):
        outcome = None
    if not isinstance(outcome, Mapping):
        return _terminal_error(
            context,
            state,
            error_code="outcome_invalid",
            content="DeepResearch 返回了无法识别的结果，任务已停止。",
        )
    state = _append_timing_window(state, outcome, action=action, node=node)
    conversation_id = str(outcome.get("conversation_id") or state.get("conversation_id") or "")
    status = str(outcome.get("status") or "")
    if status == "interrupted":
        marker = outcome.get("marker") if isinstance(outcome.get("marker"), Mapping) else {}
        node = str(outcome.get("node_id") or "")
        if node == "feedback_handler":
            questions = _split_questions(marker.get("questions"))
            options = await _generate_options(
                context,
                str(state.get("query") or ""),
                questions,
            )
            if questions:
                cards = [
                    {
                        "header": "研究方向反馈",
                        "question": question,
                        "options": options[index],
                    }
                    for index, question in enumerate(questions)
                ]
            else:
                cards = [
                    {
                        "header": "研究方向反馈",
                        "question": _marker_prompt(outcome),
                        "multi_select": False,
                        "options": [],
                    }
                ]
            state = _persist(
                context,
                state,
                "wait_feedback",
                conversation_id=conversation_id,
                questions=questions,
            )
            return _result(
                "interaction",
                state,
                interaction={
                    "query": (
                        "请回答以下研究主题澄清问题"
                        if questions
                        else "请补充研究方向反馈"
                    ),
                    "return_json": True,
                    "questions": cards,
                },
            )
        if node == "outline_interaction":
            if state.get("outline_presented"):
                return _terminal_error(
                    context,
                    state,
                    error_code="outline_auto_resume_loop",
                    content="DeepResearch 大纲确认重复出现，任务已停止以避免恢复循环。",
                )
            outline_text = _outline_markdown(marker)
            state = _persist(
                context,
                state,
                "wait_outline",
                conversation_id=conversation_id,
                outline_presented=True,
                outline_sections=_outline_sections(outline_text),
            )
            return _result(
                "interaction",
                state,
                interaction=_outline_review_interaction(outline_text),
            )
        return _terminal_error(
            context,
            state,
            error_code="interrupt_node_unknown",
            content="DeepResearch 返回了不支持的交互节点，任务已停止。",
        )
    if status == "completed" and outcome.get("report_delivered") is True:
        html_style_status = outcome.get("html_style_status")
        if html_style_status not in {"applied", "fallback"}:
            html_style_status = None
        html_style_phase = outcome.get("html_style_phase")
        html_style_reason_code = outcome.get("html_style_reason_code")
        if (
            html_style_status != "fallback"
            or not isinstance(html_style_phase, str)
            or not isinstance(html_style_reason_code, str)
        ):
            html_style_phase = None
            html_style_reason_code = None
        # 大纲编辑后 SDK 重新生成（max-rounds 自动确认，无中断标记，JSON 缓存
        # 停留旧版）：出现过大纲卡时，把流式回传的最后生效章节标题先同步进
        # state 再生成完成摘要，「报告概览」与持久化/返回的 state 因此用同一
        # 份章节；字段缺失或无效时保持原值。
        latest_outline_sections = _validated_outline_sections(
            outcome.get("outline_sections")
        )
        if latest_outline_sections and state.get("outline_presented") is True:
            state = {**state, "outline_sections": latest_outline_sections}
        content = _completion_content(
            state,
            html_style_status,
            brief_upgrade_available=outcome.get("brief_upgrade_available") is True,
        )
        completed_updates: dict[str, Any] = {"conversation_id": conversation_id}
        if html_style_status is not None:
            completed_updates["html_style_status"] = html_style_status
        if html_style_phase is not None:
            completed_updates["html_style_phase"] = html_style_phase
            completed_updates["html_style_reason_code"] = html_style_reason_code
        state = _persist(context, state, "completed", **completed_updates)
        result_fields: dict[str, Any] = {"content": content}
        if html_style_status is not None:
            result_fields["html_style_status"] = html_style_status
        if html_style_phase is not None:
            result_fields["html_style_phase"] = html_style_phase
            result_fields["html_style_reason_code"] = html_style_reason_code
        workflow_usage = normalize_workflow_llm_token_usage(
            outcome.get("workflow_llm_token_usage")
        )
        if workflow_usage is not None:
            result_fields["workflow_llm_token_usage"] = workflow_usage
        # brief 完成小描述（tools.py 回传，经校验才透传）：上层据此登记
        # "最新完成的精简报告"；无效时静默省略，不影响既有完成处理。
        brief_report = _validated_brief_report(outcome.get("brief_report"))
        if brief_report:
            result_fields["brief_report"] = brief_report
        return _result("completed", state, **result_fields)
    if status in {"cancelled", "stopped"}:
        # 两种来源、同一处置——均按优雅停止口径收尾（phase 回落 new 并保留
        # conversation_id，下一次调用可凭 probe 认领停止快照直通 resume
        # node='stopped'，已完成节点不重跑）：
        # - stopped：子进程优雅停止终态，checkpoint 已落盘；
        # - cancelled：取消信号晚于子进程优雅退出的竞态变体（防御分支，
        #   现网 runner 已不下发 cancelled 终态帧），同样意味着 checkpoint
        #   已落盘，按可续跑处理可消除竞速路径产生歧义 cancelled 死终态的
        #   源头。此后存续的 cancelled 终态只剩用户在交互卡上显式放弃一种
        #   含义（走 phase 路径，不经过本分支）。
        return _stopped_result(context, state, conversation_id)
    error_code = str(outcome.get("error_code") or "deepresearch_failed")
    error = str(outcome.get("error") or "DeepResearch 执行失败。")
    if action == "start" and status == "error" and error_code in MODEL_RESEARCH_FALLBACK_ERRORS:
        state = _persist(
            context, state, "error", error_code=error_code, model_research_fallback=True,
        )
        return _model_research_fallback_result(state)
    diagnostics: dict[str, Any] = {}
    returncode = outcome.get("returncode")
    if isinstance(returncode, int) and not isinstance(returncode, bool):
        diagnostics["returncode"] = returncode
    stderr_tail = outcome.get("stderr_tail")
    if isinstance(stderr_tail, str) and stderr_tail:
        diagnostics["stderr_tail"] = stderr_tail
    return _terminal_error(
        context,
        state,
        error_code=error_code,
        content=f"DeepResearch 执行失败：{error}",
        **diagnostics,
    )


async def _resume_stopped_conversation(
    context: DeepResearchExecutionContext,
    state: dict[str, Any],
    claimed_cid: str,
    probe_node: str = "",
) -> dict[str, Any]:
    """认领优雅停止快照后直通续跑：不弹任何交互卡。

    旧会话被用户停止时 checkpoint 已保留执行进度，resume node='stopped'
    从被停止的节点继续，已完成的节点不重跑。probe_node 为被停止节点
    (停止快照的 pending_node),据此推导前端进度条起始阶段,避免续跑时
    进度列表回退到第一步误导用户以为从头重跑。
    """
    state = _persist(context, state, "resuming_stopped", conversation_id=claimed_cid)
    stage_hint = stage_for_resume_node(probe_node) if probe_node else None
    token = _deepresearch_stage_hint_ctx.set(stage_hint)
    try:
        outcome = await _call_sdk(
            context,
            action="resume",
            _materials_reference=state.get("materials_reference"),
            _mcp_servers=_current_mcp_servers(context, state),
            conversation_id=claimed_cid,
            node="stopped",
            file_name=str(state.get("file_name") or ""),
            report_type=state.get("requested_report_type"),
        )
    finally:
        _deepresearch_stage_hint_ctx.reset(token)
    return await _handle_outcome(
        context,
        state,
        outcome,
        action="resume",
        node="stopped",
    )


async def _guard_user_stop(
    context: DeepResearchExecutionContext,
    state: Mapping[str, Any],
    awaitable: Awaitable[Any],
) -> Any:
    """工具级用户停止守卫（修复点1b）。

    外部 task.cancel() 在工具 await 点注入 CancelledError 时，转译为
    stopped 业务终态（phase 回落 new 并保留 conversation_id）：历史中
    呈现可续跑口径并保留 checkpoint 认领能力，而不是落入 ability_manager
    的通用 [Interrupted] 分支（拿不到 conversation_id，模型无从引导用户
    续跑）。CancelledError 被吞掉后 ReAct 循环正常收敛；会话级取消仍由
    react_agent 的外部取消处理器兜底。
    """
    try:
        return await awaitable
    except asyncio.CancelledError:
        conversation_id = str(state.get("conversation_id") or "")
        logger.info(
            "[deepresearch_execute] cancelled by user, translating to "
            "stopped tool_call_id=%s conversation_id=%s",
            context.tool_call_id,
            conversation_id,
        )
        return _stopped_result(context, state, conversation_id)


def _query_matches_stopped_claim(query: str, state: Mapping[str, Any]) -> bool:
    """停止快照认领守卫：入参 query 为空或与原研究主题一致才续跑。

    用户已换研究主题时不认领旧停止会话，由调用方走全新会话。
    """
    incoming = str(query or "").strip()
    if not incoming:
        return True
    original = str(state.get("original_query") or state.get("query") or "").strip()
    if not original:
        return True
    return incoming == original


def _is_claimable_stopped_probe(
    probe: Mapping[str, Any] | None,
    query: str,
    state: Mapping[str, Any],
) -> bool:
    """停止快照可认领判定：probe 命中、处于 stopped、且研究主题未变。

    in-flight 与 error 两个相位入口共用（G.CTL.03 收敛四条件表达式）。
    """
    return bool(
        probe
        and probe.get("stopped")
        and _query_matches_stopped_claim(query, state)
    )


async def _run_sdk_step(
    context: DeepResearchExecutionContext,
    state: dict[str, Any],
    *,
    action: str,
    node: str = "",
    **sdk_kwargs: Any,
) -> dict[str, Any]:
    """SDK 调用 + 结果处理的组合协程（修复点3.1：单守卫结构）。

    调用方只在外层包一层 _guard_user_stop：取消无论落在 _call_sdk 窗口
    还是 _handle_outcome 内部的选项生成段（唯一 await 点），都由同一
    守卫转译为 stopped 终态，转译结果直接作为工具返回值返回。旧的双
    守卫串联结构中，守卫#1 转译出的 stopped dict 不带 status 字段，
    会被守卫#2 内的 _handle_outcome 误判为无效 outcome 落 error 相位，
    与同一 revision 上的 new 写入形成双写竞争（生产日志同毫秒出现
    phase=new rev=4 与 phase=error rev=4 双写的根因）。
    """
    if node:
        sdk_kwargs = dict(sdk_kwargs)
        sdk_kwargs["node"] = node
    if state.get("brief_refresh_original_query"):
        sdk_kwargs["_brief_refresh_original_query"] = state["brief_refresh_original_query"]
    if state.get("materials_reference") is not None:
        sdk_kwargs["_materials_reference"] = state["materials_reference"]
    sdk_kwargs["_mcp_servers"] = _current_mcp_servers(context, state)
    outcome = await _call_sdk(context, action=action, **sdk_kwargs)
    return await _handle_outcome(context, state, outcome, action=action, node=node)


_material_execution_locks = weakref.WeakValueDictionary()


@tool(
    name="deepresearch_execute",
    description=(
        "Execute one complete interactive DeepResearch workflow. It preserves "
        "the standard detail, research-question, and outline review cards, "
        "resumes the same SDK conversation, and directly delivers the terminal result. "
        "report_type: 'professional' for in-depth multi-section reports, 'brief' for "
        "single-page concise reports; infer from the user's original wording. "
        "file_name: the delivered report file name and part of the idempotency key; "
        "the same query plus file_name as an already-delivered report returns the "
        "delivered result without re-running, so pass a different file_name when the "
        "user explicitly asks to regenerate a report for the same topic. "
        "upgrade_from_brief: set true only when the user confirms expanding the "
        "just-delivered brief report in the same session into a full professional "
        "report; it reuses the cached brief outline and skips intent recognition. "
        "Optional conversation_id claims a previous DeepResearch conversation "
        "that was interrupted while waiting for outline review: when its "
        "generated outline is still cached, the outline review card is shown "
        "immediately and the original conversation is resumed after the user "
        "answers, instead of restarting the research from scratch. It also "
        "claims a conversation the user gracefully stopped: the run resumes "
        "from where it stopped without re-executing completed stages. After "
        "a user stop, never restart or resume the research on your own "
        "initiative; only call this tool again when the user explicitly "
        "asks to continue that stopped research, passing the original "
        "research query so the stopped snapshot is claimed automatically."
    ),
)
async def deepresearch_execute(
    query: str,
    file_name: str = "",
    report_type: DeepResearchReportType | None = None,
    upgrade_from_brief: bool = False,
    conversation_id: str = "",
) -> dict[str, Any]:
    context = _execution_context.get()
    if context is None or not (context.materials_context or (context.state or {}).get("materials_prepared")):
        return await _execute_workflow(
            query, file_name, report_type, conversation_id,
            upgrade_from_brief=upgrade_from_brief,
        )
    key = (str((context.materials_context or {}).get("scope") or ""), context.tool_call_id)
    lock = _material_execution_locks.setdefault(key, asyncio.Lock())
    if lock.locked():
        latest = context.load_state() if context.load_state else context.state
        return _result("in_progress", dict(latest or {}), error_code="execution_uncertain")
    async with lock:
        latest = context.load_state() if context.load_state else context.state
        token = _execution_context.set(replace(context, state=latest))
        try:
            return await _execute_workflow(
                query, file_name, report_type, conversation_id,
                upgrade_from_brief=upgrade_from_brief,
            )
        finally:
            _execution_context.reset(token)


async def _execute_workflow(
    query: str, file_name: str = "", report_type: DeepResearchReportType | None = None,
    conversation_id: str = "",
    *, upgrade_from_brief: bool = False,
) -> dict[str, Any]:
    """Run the interactive workflow without Main Agent resume choreography."""
    context = _execution_context.get()
    if context is None:
        return {
            "schema_version": EXECUTION_SCHEMA,
            "kind": "error",
            "error_code": "execution_context_missing",
            "content": "DeepResearch 执行上下文不可用。",
            "state": {},
        }
    state = dict(context.state or {})
    new_workflow = not state
    if not state:
        state = {
            "schema_version": _STATE_SCHEMA_VERSION,
            "phase": "new",
            "query": query.strip(),
            "file_name": file_name.strip(),
            # 传入 conversation_id 表示认领一个此前中断的旧会话（研究问题
            # 确认或大纲确认阶段）；认领失败（checkpointer.db 与缓存均无可
            # 恢复状态）时下面 start 分支会换成全新 id 重跑。
            "conversation_id": conversation_id.strip() or str(uuid.uuid4()),
            # report_type 优先取 Main Agent 工具入参（从用户原话识别）；未传入时
            # 回退执行上下文（params 通道），最终兜底 brief。归一后写入 state，
            # resume 路径读此值不重新识别。deepresearch_stream:1918 据此写
            # config REPORT_TYPE -> run_deepsearch.py -> SDK config.report_type，
            # 触发 IntentRecognitionNode 锁死 report_type 字段，LLM 无决定权。
            "requested_report_type": report_type or context.requested_report_type or "brief",
            "revision": 0,
        }
    if not str(state.get("query") or "").strip():
        return _terminal_error(
            context,
            state,
            error_code="query_missing",
            content="请先提供明确的研究主题，再开始 DeepResearch。",
        )
    phase = str(state.get("phase") or "new")
    if phase in _IN_FLIGHT_PHASES:
        # 用户手动停止会使上次执行没有终态返回（CancelledError 直接冒泡，
        # state 停留在 in-flight 相位）。checkpointer 存在优雅停止快照且本次
        # 仍是同一研究主题时，认领直通续跑而不是报 execution_uncertain 拒绝。
        in_flight_cid = str(state.get("conversation_id") or "").strip()
        in_flight_probe = (
            _probe_resumable_checkpoint(in_flight_cid)
            if in_flight_cid
            else None
        )
        if in_flight_cid and _is_claimable_stopped_probe(
            in_flight_probe, query, state
        ):
            logger.info(
                "[deepresearch_execute] claimed stopped conversation from "
                "in-flight phase tool_call_id=%s phase=%s conversation_id=%s",
                context.tool_call_id,
                phase,
                in_flight_cid,
            )
            return await _guard_user_stop(
                context,
                state,
                _resume_stopped_conversation(
                    context,
                    state,
                    in_flight_cid,
                    probe_node=str(in_flight_probe.get("node") or ""),
                ),
            )
        return _terminal_error(
            context,
            state,
            error_code="execution_uncertain",
            content=(
                "DeepResearch 上一次执行的最终状态无法确认。为避免重复启动或恢复，"
                "本次未再次调用研究引擎。"
            ),
        )
    if phase in {"completed", "cancelled"}:
        return _result(phase, state, content="DeepResearch 任务已经结束。")
    if phase == "error":
        if (state.get("model_research_fallback") is True
                and state.get("error_code") in MODEL_RESEARCH_HANDOFF_ERRORS):
            return _model_research_fallback_result(state)
        # 修复点3.2：error 相位 claim-or-fresh。3.1 修复前取消转译的
        # stopped 写入（phase=new）会被同一 revision 上的 error 覆盖写
        # 吞掉，error 相位下可能实际存有可恢复的停止快照。带
        # conversation_id 先 probe：有停止快照且研究主题未变则认领直通
        # 续跑（已完成节点不重跑）；无快照才判死。completed/cancelled
        # 保持死终态不认领。
        error_cid = str(state.get("conversation_id") or "").strip()
        error_probe = (
            _probe_resumable_checkpoint(error_cid) if error_cid else None
        )
        if error_cid and _is_claimable_stopped_probe(error_probe, query, state):
            logger.info(
                "[deepresearch_execute] claimed stopped conversation from "
                "error phase tool_call_id=%s conversation_id=%s",
                context.tool_call_id,
                error_cid,
            )
            return await _guard_user_stop(
                context,
                state,
                _resume_stopped_conversation(
                    context,
                    state,
                    error_cid,
                    probe_node=str(error_probe.get("node") or ""),
                ),
            )
        # 修复7：error 相位死路（无可恢复快照）同样先尝试止损交付已落盘
        # 的报告（同主题追问场景），失败才判死。
        if error_cid and _query_matches_stopped_claim(query, state):
            salvage = await salvage_deepresearch_report(
                conversation_id=error_cid,
                file_name=str(state.get("file_name") or file_name),
            )
            if salvage is not None:
                state = _persist(
                    context, state, "completed", conversation_id=error_cid
                )
                logger.info(
                    "[deepresearch_execute] salvaged completed report from "
                    "error phase tool_call_id=%s conversation_id=%s",
                    context.tool_call_id,
                    error_cid,
                )
                return _result(
                    "completed",
                    state,
                    content=_completion_content(
                        state, salvage.get("html_style_status")
                    ),
                )
        return _result(
            "error",
            state,
            content="DeepResearch 任务已经结束，请重新发起。",
        )

    if phase == "new":
        followup = context.deepresearch_followup
        is_followup = followup is not None or upgrade_from_brief
        if new_workflow and context.mcp_servers and is_followup:
            # 追问同样启动新研究：记录本次信源身份，续跑时再读取当前凭证。
            from .mcp_search import source_identity
            state["mcp_server_names"] = [server["server_name"] for server in context.mcp_servers]
            state["mcp_source_identities"] = {
                server["server_name"]: source_identity(server) for server in context.mcp_servers
            }
        if followup is not None and followup.get("action") == "refresh_latest_brief":
            record = _get_brief_upgrade_record(_get_route())
            if record is None or not str(record.get("original_query") or "").strip():
                return _terminal_error(
                    context, state, error_code="brief_refresh_unavailable",
                    content="最新精简报告的来源不可用，请重新生成精简报告后再补充。",
                )
            if str(record.get("report_id") or "") != str(followup.get("source_report_id") or ""):
                return _terminal_error(
                    context, state, error_code="brief_refresh_target_changed",
                    content="最新精简报告已变化，本次补充未执行；请刷新后基于最新报告重新发起。",
                )
            source_query = str(record["original_query"])
            try:
                source_path = str(record.get("report_path") or "").strip()
                if not source_path:
                    raise ValueError("brief report path missing")
                refresh_query = await asyncio.to_thread(
                    build_brief_refresh_query, source_query, source_path, query
                )
            except (OSError, ValueError):
                return _terminal_error(
                    context, state, error_code="brief_refresh_unavailable",
                    content="无法读取最新精简报告的正文，本次未生成补充报告；请重新生成精简报告后再试。",
                )
            # A fresh identity and filename preserve the source and avoid claiming
            # an unrelated stopped workflow. Resume uses this bound source state.
            title = _report_title_from_file_name(str(record.get("original_file_name") or ""))
            title = re.sub(r"（补充版-[0-9a-f]+）$", "", title) or "研究报告"
            state = _persist(
                context, state, "starting",
                conversation_id=str(uuid.uuid4()),
                query=refresh_query,
                file_name=f"{title}（补充版-{uuid.uuid4().hex[:8]}）",
                requested_report_type="brief",
                source_brief_report_id=str(record["report_id"]),
                brief_refresh_original_query=source_query,
            )
            return await _guard_user_stop(
                context, state,
                _run_sdk_step(
                    context, state, action="start", query=refresh_query,
                    conversation_id=state["conversation_id"], file_name=state["file_name"],
                    report_type="brief", upgrade_from_brief=False,
                ),
            )
        # A new brief upgrade must not claim a stopped run with the same topic.
        # Existing upgrade workflows still use the normal resume branches below.
        if context.deepresearch_followup is not None or upgrade_from_brief:
            # 升级来源统一解析（strict 结构化上下文与手动 upgrade_from_brief 共用）：
            # query、文件名、metadata 必须出自同一份来源记录，避免模型自选 query
            # 与缓存注入的 metadata 混源；结构化动作强制 professional。
            followup = context.deepresearch_followup
            upgrade_record: dict[str, Any] | None = None
            if followup is not None or upgrade_from_brief:
                upgrade_record = _get_brief_upgrade_record(_get_route())
            if followup is not None:
                # strict：来源必须与该会话最新登记的可用记录完全一致；不一致明确
                # 失败，不静默降级为普通 professional（承诺复用大纲就必须复用）。
                source_report_id = str(followup.get("source_report_id") or "").strip()
                if (
                    upgrade_record is None
                    or upgrade_record.get("upgrade_available") is not True
                    or not str(upgrade_record.get("original_query") or "").strip()
                ):
                    return _terminal_error(
                        context,
                        state,
                        error_code="brief_upgrade_unavailable",
                        content=(
                            "最新精简报告的升级上下文不完整或不可用，"
                            "请重新生成精简报告后再发起升级。"
                        ),
                    )
                if str(upgrade_record.get("report_id") or "") != source_report_id:
                    return _terminal_error(
                        context,
                        state,
                        error_code="brief_upgrade_target_changed",
                        content=(
                            "最新精简报告已变化，本次升级未执行；"
                            "请刷新后基于最新报告重新发起。"
                        ),
                    )
            if (
                upgrade_record is not None
                and upgrade_record.get("upgrade_available") is True
                and str(upgrade_record.get("original_query") or "").strip()
            ):
                # 快照同源覆盖（strict 与手动共用）：升级来源一旦在启动时确定，
                # 本次任务持续使用同一份快照；附加要求由专业版大纲审阅编辑承载。
                state = {
                    **state,
                    "query": str(upgrade_record["original_query"]),
                    "file_name": _upgrade_file_name(
                        str(upgrade_record.get("original_file_name") or "")
                    ),
                    "requested_report_type": "professional",
                    "source_brief_report_id": str(
                        upgrade_record.get("report_id") or ""
                    ),
                }
                upgrade_from_brief = True
            state = _persist(
                context, state, "starting", conversation_id=str(uuid.uuid4())
            )
            upgrade_metadata: dict[str, Any] | None = None
            if upgrade_from_brief and upgrade_record is not None:
                candidate = upgrade_record.get("metadata")
                upgrade_metadata = candidate if isinstance(candidate, dict) else None
            return await _guard_user_stop(
                context,
                state,
                _run_sdk_step(
                    context,
                    state,
                    action="start",
                    query=str(state.get("query") or query),
                    conversation_id=str(state.get("conversation_id") or ""),
                    file_name=str(state.get("file_name") or file_name),
                    report_type=state.get("requested_report_type"),
                    upgrade_from_brief=upgrade_from_brief,
                    _upgrade_brief_metadata=upgrade_metadata,
                ),
            )
        claimed_cid = str(state.get("conversation_id") or "").strip()
        probe = (
            _probe_resumable_checkpoint(claimed_cid) if claimed_cid else None
        )
        if probe is None:
            # 修复9'：LLM 传参 conversation_id 不可信（实机事故：两次幻觉
            # id 均在认领入口失效，probe 落空后直接全新起跑，丢弃已停止
            # 会话的全部研究成果）。cid 落空时不放弃认领——按本次研究
            # 主题扫库兜底：以快照 search_context.original_query 与本次
            # query 完全一致为认领条件（换主题不认领旧会话），rowid 倒序
            # 最新会话优先。命中后以扫出的 cid 重走下方 probe 认领分支
            # （含 stopped 直通续跑与交互卡重弹），认领仍失败时下方
            # salvage 止损也改用扫出的 cid 定位报告。
            scan_query = str(query or state.get("query") or "").strip()
            # 同步 SQLite 扫库经 to_thread 卸载，避免阻塞事件循环
            # （contextvar 经 copy_context 随线程传播，扫库内 _get_route
            # 归属校验线程内安全）。
            scan = (
                await asyncio.to_thread(
                    _scan_resumable_checkpoint_by_query, scan_query
                )
                if scan_query
                else None
            )
            if scan is not None:
                scanned_cid = str(scan.get("conversation_id") or "")
                if scanned_cid and scanned_cid != claimed_cid:
                    claimed_cid = scanned_cid
                    probe = {
                        key: value
                        for key, value in scan.items()
                        if key != "conversation_id"
                    }
                    incoming_query = str(query or "").strip()
                    if incoming_query:
                        # 扫库已确认快照主题与本次 query 完全一致；同步
                        # 刷新 state.query，使下方认领分支的主题守卫
                        # （_query_matches_stopped_claim）通过。
                        state = dict(state, query=incoming_query)
                    logger.info(
                        "[deepresearch_execute] claimed conversation by "
                        "query scan (llm conversation_id untrusted) "
                        "tool_call_id=%s conversation_id=%s node=%s "
                        "stopped=%s",
                        context.tool_call_id,
                        claimed_cid,
                        probe.get("node"),
                        bool(probe.get("stopped")),
                    )
        probe_node = str(probe.get("node") or "") if probe else ""
        stopped_probe = bool(probe and probe.get("stopped"))
        if (
            claimed_cid
            and stopped_probe
            and _query_matches_stopped_claim(query, state)
        ):
            # 认领成功：旧会话被用户优雅停止（checkpoint 保留进度），不弹
            # 交互卡直通 resume node='stopped'，从被停止节点继续，已完成
            # 节点不重跑。
            logger.info(
                "[deepresearch_execute] claimed stopped conversation "
                "tool_call_id=%s conversation_id=%s node=%s",
                context.tool_call_id,
                claimed_cid,
                probe_node,
            )
            return await _guard_user_stop(
                context,
                state,
                _resume_stopped_conversation(
                    context,
                    state,
                    claimed_cid,
                    probe_node=probe_node,
                ),
            )
        if (
            claimed_cid
            and not stopped_probe
            and probe_node == "feedback_handler"
        ):
            # 认领成功：旧会话停在研究问题确认阶段（含跨进程重启），从
            # checkpointer.db 恢复问题清单出反馈卡；用户答复后从原会话 resume。
            questions = _split_questions(probe.get("questions"))
            if questions:
                options = await _guard_user_stop(
                    context,
                    state,
                    _generate_options(
                        context,
                        str(state.get("query") or ""),
                        questions,
                    ),
                )
                cards = [
                    {
                        "header": "研究方向反馈",
                        "question": question,
                        "options": options[index],
                    }
                    for index, question in enumerate(questions)
                ]
                state = _persist(
                    context,
                    state,
                    "wait_feedback",
                    conversation_id=claimed_cid,
                    questions=questions,
                )
                logger.info(
                    "[deepresearch_execute] claimed interrupted conversation "
                    "tool_call_id=%s conversation_id=%s node=feedback_handler",
                    context.tool_call_id,
                    claimed_cid,
                )
                return _result(
                    "interaction",
                    state,
                    interaction={
                        "query": "请回答以下研究主题澄清问题",
                        "return_json": True,
                        "questions": cards,
                    },
                )
        claimed_outline = (
            probe.get("outline_json")
            if probe_node == "outline_interaction"
            and not stopped_probe
            else None
        )
        claim_source = "db" if isinstance(claimed_outline, dict) else ""
        if not isinstance(claimed_outline, dict):
            # 修复5：缓存兜底仅在 DB 确认存在 outline_interaction 可恢复中断
            # 时启用（probe 命中但快照缺 outline_json 的场景）。probe 未命中
            # （会话已正常完结被清理/快照损坏/不存在）时一律不信内存缓存：
            # 事故中 probe=None 却用残留缓存大纲弹卡，用户确认后 resume 无
            # 快照可恢复而全新起跑，6 秒崩溃并删除该会话全部数据。
            if (
                claimed_cid
                and probe_node == "outline_interaction"
                and not stopped_probe
            ):
                cached_outline = _get_cached_outline_json(
                    _get_route(), claimed_cid
                )
                if isinstance(cached_outline, dict):
                    claimed_outline = cached_outline
                    claim_source = "cache"
            else:
                # 停止快照未被认领（含换主题）时,缓存兜底同样不得复用旧会话
                # 大纲,否则会把旧主题的大纲弹给新研究。
                claimed_outline = {}
        claimed_sections = claimed_outline.get("sections")
        if claimed_cid and isinstance(claimed_sections, list) and claimed_sections:
            # 认领成功：旧会话已生成大纲且停在大纲确认（DB 快照或其缓存
            # 兜底），复用大纲出审阅卡片，跳过 start 重跑；用户答复后从原
            # 会话 resume。
            outline_text = (
                _format_outline_card_markdown(claimed_outline)
                or "大纲已生成，请确认是否继续研究。"
            )
            state = _persist(
                context,
                state,
                "wait_outline",
                conversation_id=claimed_cid,
                outline_presented=True,
                outline_sections=_outline_sections(outline_text),
            )
            logger.info(
                "[deepresearch_execute] claimed interrupted conversation "
                "tool_call_id=%s conversation_id=%s node=outline_interaction "
                "source=%s",
                context.tool_call_id,
                claimed_cid,
                claim_source or "db",
            )
            return _result(
                "interaction",
                state,
                interaction=_outline_review_interaction(outline_text),
            )
        # A claimed checkpoint returned above. Its original source identities
        # cannot be reconstructed from today's connections, so keep them empty.
        if new_workflow and context.mcp_servers:
            from .mcp_search import source_identity
            state["mcp_server_names"] = [server["server_name"] for server in context.mcp_servers]
            state["mcp_source_identities"] = {
                server["server_name"]: source_identity(server) for server in context.mcp_servers
            }
        if claimed_cid:
            # 修复7：认领失败先止损——报告若已随此前运行落盘（收尾阶段被
            # 停/交付前崩溃后快照已清理），且用户仍在追问同一主题，直接
            # 交付已有报告，不重跑研究。
            if _query_matches_stopped_claim(query, state):
                salvage = await salvage_deepresearch_report(
                    conversation_id=claimed_cid,
                    file_name=str(state.get("file_name") or file_name),
                )
                if salvage is not None:
                    state = _persist(
                        context, state, "completed", conversation_id=claimed_cid
                    )
                    logger.info(
                        "[deepresearch_execute] salvaged completed report "
                        "tool_call_id=%s conversation_id=%s",
                        context.tool_call_id,
                        claimed_cid,
                    )
                    return _result(
                        "completed",
                        state,
                        content=_completion_content(
                            state, salvage.get("html_style_status")
                        ),
                    )
        if claimed_cid and not state.get("materials_reference"):
            # 认领失败：checkpointer.db 与内存缓存均无该会话可恢复的中断状态
            # （已完结/已清理/不存在），或旧会话为停止快照但用户已更换主题；
            # 绝不复用旧会话的 id 发起新跑，换成全新 id 从头执行。换主题时
            # 同步刷新 state.query，确保新跑研究的是新主题而非旧主题。
            if stopped_probe and not _query_matches_stopped_claim(query, state):
                logger.info(
                    "[deepresearch_execute] stopped snapshot not claimed: "
                    "query changed, starting fresh conversation "
                    "tool_call_id=%s conversation_id=%s",
                    context.tool_call_id,
                    claimed_cid,
                )
            else:
                logger.warning(
                    "[deepresearch_execute] claim failed, starting fresh "
                    "conversation: no resumable checkpoint in checkpointer.db "
                    "tool_call_id=%s conversation_id=%s probe=%s",
                    context.tool_call_id,
                    claimed_cid,
                    f"node={probe_node}" if probe else "none",
                )
            state = _persist(
                context,
                state,
                "starting",
                conversation_id=str(uuid.uuid4()),
                query=str(query or state.get("query") or "").strip(),
            )
        else:
            state = _persist(context, state, "starting")
        from .materials import MaterialsError, MaterialsPolicyError, MATERIALS_POLICY, prepare_materials
        material_context = context.materials_context
        if material_context and not state.get("materials_prepared"):
            state = _persist(context, state, "preparing_materials",
                             original_query=str(material_context.get("original_query") or state["query"]))
            from .tools import _push_materials_progress
            try:
                await _push_materials_progress("正在准备本次研究材料。")
                reference = await prepare_materials(material_context, str(state["conversation_id"]))
            except asyncio.CancelledError:
                _persist(context, state, "cancelled")
                raise
            except MaterialsPolicyError as exc:
                return _terminal_error(context, state, error_code="materials_preparation_failed", content=str(exc))
            except (MaterialsError, OSError, ValueError):
                state = _persist(context, state, "error", error_code="materials_preparation_failed",
                                 model_research_fallback=True)
                return _model_research_fallback_result(state)
            original_query = str(material_context.get("original_query") or state["query"])
            sdk_query = original_query
            if reference and any(r["status"] == "ready" for r in reference["results"]):
                sdk_query = MATERIALS_POLICY + "\n\n<原始研究请求>\n" + original_query + "\n</原始研究请求>"
            state = _persist(context, state, "starting", materials_prepared=True,
                             materials_reference=reference, original_query=original_query, query=sdk_query)
        return await _guard_user_stop(
            context,
            state,
            _run_sdk_step(
                context,
                state,
                action="start",
                query=str(state.get("query") or query),
                conversation_id=str(state.get("conversation_id") or ""),
                file_name=str(state.get("file_name") or file_name),
                report_type=state.get("requested_report_type"),
                upgrade_from_brief=False,
            ),
        )
    if phase == "wait_feedback":
        terminal_status = _terminal_interaction_status(context.user_input)
        if terminal_status:
            state = _persist(context, state, terminal_status)
            content = (
                "DeepResearch 任务已取消。"
                if terminal_status == "cancelled"
                else "研究方向反馈交互失败，DeepResearch 任务已停止。"
            )
            return _result(terminal_status, state, content=content)
        try:
            feedback, interaction_result = _feedback_payload(
                context.user_input,
                list(state.get("questions") or []),
            )
        except AskUserResponseError as exc:
            return _terminal_error(
                context,
                state,
                error_code="interaction_invalid",
                content=f"研究方向反馈无效：{exc}",
            )
        state = _persist(context, state, "resuming_feedback")
        return await _guard_user_stop(
            context,
            state,
            _run_sdk_step(
                context,
                state,
                action="resume",
                query=str(state.get("query") or query),
                node="feedback_handler",
                conversation_id=str(state.get("conversation_id") or ""),
                feedback=feedback,
                interaction_result=interaction_result,
                file_name=str(state.get("file_name") or file_name),
                report_type=state.get("requested_report_type"),
            ),
        )
    if phase == "wait_outline":
        terminal_status = _terminal_interaction_status(context.user_input)
        if terminal_status:
            state = _persist(context, state, terminal_status)
            content = (
                "DeepResearch 任务已取消。"
                if terminal_status == "cancelled"
                else "大纲审阅交互失败，DeepResearch 任务已停止。"
            )
            return _result(terminal_status, state, content=content)
        try:
            feedback, interaction_result = _outline_feedback(context.user_input)
        except AskUserResponseError as exc:
            return _terminal_error(
                context,
                state,
                error_code="interaction_invalid",
                content=f"大纲审阅结果无效：{exc}",
            )
        state = _persist(context, state, "resuming_outline")
        return await _guard_user_stop(
            context,
            state,
            _run_sdk_step(
                context,
                state,
                action="resume",
                query=str(state.get("query") or query),
                node="outline_interaction",
                conversation_id=str(state.get("conversation_id") or ""),
                feedback=feedback,
                interaction_result=interaction_result,
                file_name=str(state.get("file_name") or file_name),
                report_type=state.get("requested_report_type"),
            ),
        )
    return _terminal_error(
        context,
        state,
        error_code="execution_phase_invalid",
        content="DeepResearch 执行状态无效，任务已停止。",
    )


deepresearch_execute.card.properties["resilience"] = {"timeout_s": None}
deepresearch_execute.card.properties["required_skill"] = "deepresearch"
deepresearch_execute.card.parallel_safe = False


__all__ = [
    "EXECUTION_SCHEMA",
    "bind_deepresearch_execution_context",
    "deepresearch_execute",
    "reset_deepresearch_execution_context",
]

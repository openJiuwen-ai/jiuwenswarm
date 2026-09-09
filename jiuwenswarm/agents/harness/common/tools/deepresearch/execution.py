# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Deterministic, user-compatible orchestration for DeepResearch."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from openjiuwen.core.foundation.tool import tool

from jiuwenswarm.common.schema.ask_user import (
    AskUserResponse,
    AskUserResponseError,
    parse_ask_user_response,
)
from jiuwenswarm.perf.context import DeepResearchReportType

from .stream_router import _format_outline_card_markdown
from .tools import _call_deepresearch_stream_impl
from .usage import normalize_workflow_llm_token_usage

logger = logging.getLogger(__name__)

EXECUTION_SCHEMA = "openjiuwen.deepresearch.execute.v1"
_STATE_SCHEMA_VERSION = 1
_IN_FLIGHT_PHASES = frozenset({"starting", "resuming_feedback", "resuming_outline"})
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


def _decode_user_input(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return value
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        try:
            return model_dump(mode="json")
        except TypeError:
            return model_dump()
    return value


def _parse_response(value: Any) -> AskUserResponse:
    decoded = _decode_user_input(value)
    if not isinstance(decoded, Mapping):
        raise AskUserResponseError("AskUser response must be an object")
    if decoded.get("status") in {"error", "cancelled"}:
        raise AskUserResponseError(str(decoded.get("status")))
    return parse_ask_user_response(decoded)


def _terminal_interaction_status(value: Any) -> str:
    decoded = _decode_user_input(value)
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
        input_tokens, output_tokens, cache_read = extract_usage_tokens(response)
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


async def _call_sdk(
    context: DeepResearchExecutionContext,
    **kwargs: Any,
) -> str:
    """Measure one low-level SDK window without logging business content."""
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
    decoded = _decode_user_input(value)
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


def _completion_content(
    state: Mapping[str, Any], report_chars: Any, html_style_status: Any = None
) -> str:
    file_name = str(state.get("file_name") or "研究报告").strip()
    title = re.sub(r"\.(?:md|markdown)$", "", file_name, flags=re.IGNORECASE)
    lines = [
        "✅ **深度研究已完成！**",
        "",
        f"{title or '研究报告'}已成功生成并交付。",
    ]
    sections = state.get("outline_sections")
    if isinstance(report_chars, int) or isinstance(sections, list):
        lines.extend(["", "**报告概览**："])
    if isinstance(report_chars, int):
        lines.append(f"- **报告字符数**：{report_chars:,} 字")
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
                interaction={
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
                },
            )
        return _terminal_error(
            context,
            state,
            error_code="interrupt_node_unknown",
            content="DeepResearch 返回了不支持的交互节点，任务已停止。",
        )
    if status == "completed" and outcome.get("report_delivered") is True:
        report_chars = outcome.get("report_chars")
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
        content = _completion_content(state, report_chars, html_style_status)
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
        return _result("completed", state, **result_fields)
    if status == "cancelled":
        state = _persist(context, state, "cancelled", conversation_id=conversation_id)
        return _result("cancelled", state, content="DeepResearch 任务已取消。")
    error_code = str(outcome.get("error_code") or "deepresearch_failed")
    error = str(outcome.get("error") or "DeepResearch 执行失败。")
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


@tool(
    name="deepresearch_execute",
    description=(
        "Execute one complete interactive DeepResearch workflow. It preserves "
        "the standard detail, research-question, and outline review cards, "
        "resumes the same SDK conversation, and directly delivers the terminal result."
    ),
)
async def deepresearch_execute(query: str, file_name: str = "") -> dict[str, Any]:
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
    if not state:
        state = {
            "schema_version": _STATE_SCHEMA_VERSION,
            "phase": "new",
            "query": query.strip(),
            "file_name": file_name.strip(),
            "conversation_id": str(uuid.uuid4()),
            "requested_report_type": context.requested_report_type,
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
        return _terminal_error(
            context,
            state,
            error_code="execution_uncertain",
            content=(
                "DeepResearch 上一次执行的最终状态无法确认。为避免重复启动或恢复，"
                "本次未再次调用研究引擎。"
            ),
        )
    if phase in {"completed", "cancelled", "error"}:
        kind = phase if phase != "error" else "error"
        return _result(kind, state, content="DeepResearch 任务已经结束。")

    if phase == "new":
        state = _persist(context, state, "starting")
        outcome = await _call_sdk(
            context,
            action="start",
            query=str(state.get("query") or query),
            conversation_id=str(state.get("conversation_id") or ""),
            file_name=str(state.get("file_name") or file_name),
            report_type=state.get("requested_report_type"),
        )
        return await _handle_outcome(context, state, outcome, action="start")
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
        outcome = await _call_sdk(
            context,
            action="resume",
            conversation_id=str(state.get("conversation_id") or ""),
            node="feedback_handler",
            feedback=feedback,
            interaction_result=interaction_result,
            file_name=str(state.get("file_name") or file_name),
            report_type=state.get("requested_report_type"),
        )
        return await _handle_outcome(
            context,
            state,
            outcome,
            action="resume",
            node="feedback_handler",
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
        outcome = await _call_sdk(
            context,
            action="resume",
            conversation_id=str(state.get("conversation_id") or ""),
            node="outline_interaction",
            feedback=feedback,
            interaction_result=interaction_result,
            file_name=str(state.get("file_name") or file_name),
            report_type=state.get("requested_report_type"),
        )
        return await _handle_outcome(
            context,
            state,
            outcome,
            action="resume",
            node="outline_interaction",
        )
    return _terminal_error(
        context,
        state,
        error_code="execution_phase_invalid",
        content="DeepResearch 执行状态无效，任务已停止。",
    )


deepresearch_execute.card.properties["resilience"] = {"timeout_s": None}
deepresearch_execute.card.parallel_safe = False


__all__ = [
    "EXECUTION_SCHEMA",
    "bind_deepresearch_execution_context",
    "deepresearch_execute",
    "reset_deepresearch_execution_context",
]

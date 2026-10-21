# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Span normalization and rule scanning used by the stub engine.

The real engine rebuilds its own message and tool nodes from the trajectory
snapshot. The stub engine only needs a flat view of each span, which this module
derives from the OTLP payload of one :class:`~jiuwenswarm.diagnosis.snapshot.TrajectoryRecord`.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from jiuwenswarm.diagnosis.models import StepRef

logger = logging.getLogger(__name__)

_EXCERPT_CHARS = 500
_INFERENCE_OPERATIONS = frozenset({"chat", "generate_content", "text_completion"})
_STATUS_CODE_ERROR = 2


@dataclass(frozen=True, slots=True)
class SpanFact:
    """Normalized view of one span, anchored by its span reference."""

    ref: StepRef
    name: str
    kind: str
    start_nano: int
    end_nano: int
    lifecycle: str
    has_error: bool = False
    status_message: str | None = None
    tool_name: str | None = None
    tool_arguments: str | None = None
    tool_result: str | None = None
    output_text: str | None = None
    has_tool_calls: bool = False
    turn_number: int | None = None
    input_user_text: str | None = None
    output_tool_calls: tuple[ModelToolCall, ...] = ()
    tool_call_id: str | None = None


@dataclass(frozen=True, slots=True)
class ModelToolCall:
    """One tool call requested by a model reply."""

    call_id: str
    name: str
    arguments: str


@dataclass(frozen=True, slots=True)
class RuleFinding:
    """One deterministic rule alert raised over a span."""

    code: str
    message: str
    ref: StepRef


def _attribute_value(value: Mapping[str, Any]) -> Any:
    for key in ("stringValue", "boolValue", "doubleValue"):
        if key in value:
            return value[key]
    if "intValue" in value:
        try:
            return int(value["intValue"])
        except (TypeError, ValueError):
            return None
    return None


def _attribute_map(span: Mapping[str, Any]) -> dict[str, Any]:
    attributes = span.get("attributes")
    result: dict[str, Any] = {}
    if not isinstance(attributes, list):
        return result
    for attribute in attributes:
        if not isinstance(attribute, Mapping):
            continue
        key = attribute.get("key")
        value = attribute.get("value")
        if isinstance(key, str) and isinstance(value, Mapping):
            result[key] = _attribute_value(value)
    return result


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _excerpt(text: str | None, limit: int = _EXCERPT_CHARS) -> str:
    if not text:
        return ""
    normalized = text.strip()
    if len(normalized) <= limit:
        return normalized
    return normalized[:limit] + "..."


def _message_text(raw: Any) -> tuple[str, bool]:
    """Extract assistant text and tool-call presence from ``gen_ai.output.messages``."""
    if not isinstance(raw, str) or not raw.strip():
        return "", False
    try:
        messages = json.loads(raw)
    except ValueError:
        return raw, False
    if not isinstance(messages, list):
        return "", False
    texts: list[str] = []
    has_tool_calls = False
    for message in messages:
        if not isinstance(message, Mapping):
            continue
        parts = message.get("parts")
        if not isinstance(parts, list):
            continue
        for part in parts:
            if not isinstance(part, Mapping):
                continue
            part_type = str(part.get("type") or "")
            if part_type in {"tool_call", "tool-call"}:
                has_tool_calls = True
            elif part_type == "text" and isinstance(part.get("content"), str):
                texts.append(part["content"])
    return "\n".join(texts), has_tool_calls


def _model_tool_calls(raw: Any, span_id: str) -> tuple[ModelToolCall, ...]:
    """Extract the tool calls requested in ``gen_ai.output.messages``."""
    if not isinstance(raw, str) or not raw.strip():
        return ()
    try:
        messages = json.loads(raw)
    except ValueError:
        return ()
    calls: list[ModelToolCall] = []
    for message in messages if isinstance(messages, list) else []:
        parts = message.get("parts") if isinstance(message, Mapping) else None
        for part in parts if isinstance(parts, list) else []:
            if not isinstance(part, Mapping) or str(part.get("type") or "") not in {"tool_call", "tool-call"}:
                continue
            arguments = part.get("arguments")
            calls.append(
                ModelToolCall(
                    call_id=str(part.get("id") or f"call_{span_id}_{len(calls)}"),
                    name=str(part.get("name") or "tool"),
                    arguments=arguments if isinstance(arguments, str) else _flexible_text(arguments) or "{}",
                ),
            )
    return tuple(calls)


def _last_user_text(raw: Any) -> str | None:
    """Return the text of the last user message in ``gen_ai.input.messages``."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        messages = json.loads(raw)
    except ValueError:
        return None
    for message in reversed(messages if isinstance(messages, list) else []):
        if not isinstance(message, Mapping) or str(message.get("role") or "") != "user":
            continue
        parts = message.get("parts")
        texts = [
            str(part["content"])
            for part in (parts if isinstance(parts, list) else [])
            if isinstance(part, Mapping) and part.get("type") == "text" and isinstance(part.get("content"), str)
        ]
        if texts:
            return "\n".join(texts)
    return None


def _flexible_text(raw: Any) -> str | None:
    if raw is None:
        return None
    if isinstance(raw, str):
        return raw
    try:
        return json.dumps(raw, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(raw)


def _classify(span: Mapping[str, Any], attributes: Mapping[str, Any]) -> str:
    operation = attributes.get("gen_ai.operation.name")
    if operation in _INFERENCE_OPERATIONS:
        return "inference"
    if operation == "execute_tool" or attributes.get("gen_ai.tool.name"):
        return "tool"
    record_kind = attributes.get("openjiuwen.trajectory.record.kind")
    if record_kind == "inference":
        return "inference"
    name = str(span.get("name") or "")
    if name.startswith("llm"):
        return "inference"
    if name.startswith("tool"):
        return "tool"
    return "other"


def _tool_result_has_error(value: str | None) -> bool:
    if not value:
        return False
    try:
        from jiuwenswarm.agents.harness.common.rails.execution_guard.circuit_breaker_rail import (
            ToolResultErrorDetector,
        )

        return bool(ToolResultErrorDetector.has_error(value))
    except Exception:  # noqa: BLE001
        logger.debug("[diagnosis] ToolResultErrorDetector unavailable", exc_info=True)
        return value.lstrip().upper().startswith("[ERROR]:")


def spans_from_record(
    record: Mapping[str, Any],
    otlp: Mapping[str, Any],
    subject_id: str,
) -> list[SpanFact]:
    """Normalize every span in one OTLP payload.

    Args:
        record: Record envelope (lifecycle and fallback identifiers).
        otlp: OTLP payload whose ``@oj-seq`` attributes are already restored.
        subject_id: Execution subject owning the record.

    Returns:
        Normalized spans; spans with malformed identifiers are skipped.
    """
    lifecycle = str(record.get("lifecycle") or "")
    facts: list[SpanFact] = []
    for resource in otlp.get("resourceSpans") or []:
        if not isinstance(resource, Mapping):
            continue
        for scope in resource.get("scopeSpans") or []:
            if not isinstance(scope, Mapping):
                continue
            for span in scope.get("spans") or []:
                if not isinstance(span, Mapping):
                    continue
                fact = _span_fact(span, record, lifecycle, subject_id)
                if fact is not None:
                    facts.append(fact)
    return facts


def _span_fact(
    span: Mapping[str, Any],
    record: Mapping[str, Any],
    lifecycle: str,
    subject_id: str,
) -> SpanFact | None:
    trace_id = str(span.get("traceId") or record.get("trace_id") or "")
    span_id = str(span.get("spanId") or record.get("span_id") or "")
    try:
        ref = StepRef(trace_id=trace_id, span_id=span_id, subject_id=subject_id)
    except ValueError:
        return None
    attributes = _attribute_map(span)
    kind = _classify(span, attributes)
    status = span.get("status")
    status_code = _to_int(status.get("code")) if isinstance(status, Mapping) else 0
    status_message = (
        str(status.get("message")) if isinstance(status, Mapping) and status.get("message") else None
    )
    tool_name = attributes.get("gen_ai.tool.name")
    tool_arguments = _flexible_text(attributes.get("gen_ai.tool.call.arguments"))
    tool_result = _flexible_text(attributes.get("gen_ai.tool.call.result"))
    output_text = None
    has_tool_calls = False
    input_user_text = None
    output_tool_calls: tuple[ModelToolCall, ...] = ()
    if kind == "inference":
        text, has_tool_calls = _message_text(attributes.get("gen_ai.output.messages"))
        output_text = text or None
        input_user_text = _last_user_text(attributes.get("gen_ai.input.messages"))
        output_tool_calls = _model_tool_calls(attributes.get("gen_ai.output.messages"), span_id)
    call_id = attributes.get("gen_ai.tool.call.id")
    has_error = status_code == _STATUS_CODE_ERROR
    if kind == "tool" and _tool_result_has_error(tool_result):
        has_error = True
    turn_value = attributes.get("openjiuwen.turn.number")
    return SpanFact(
        ref=ref,
        name=str(span.get("name") or ""),
        kind=kind,
        start_nano=_to_int(span.get("startTimeUnixNano")),
        end_nano=_to_int(span.get("endTimeUnixNano")),
        lifecycle=lifecycle,
        has_error=has_error,
        status_message=status_message,
        tool_name=str(tool_name) if tool_name else None,
        tool_arguments=tool_arguments,
        tool_result=tool_result,
        output_text=output_text,
        has_tool_calls=has_tool_calls,
        turn_number=_to_int(turn_value) if turn_value is not None else None,
        input_user_text=input_user_text,
        output_tool_calls=output_tool_calls,
        tool_call_id=str(call_id) if call_id else None,
    )


def scan_rules(spans: Sequence[SpanFact]) -> list[RuleFinding]:
    """Run deterministic rules over the spans inside the scope."""
    findings: list[RuleFinding] = []
    for span in spans:
        if not span.has_error:
            continue
        if span.kind == "tool":
            code = "tool_error"
            label = span.tool_name or span.name or "tool"
            message = f"Tool {label} reported an error: {_excerpt(span.tool_result or span.status_message, 200)}"
        else:
            code = "span_error"
            message = f"Span {span.name or span.ref.span_id} ended with an error: {_excerpt(span.status_message, 200)}"
        findings.append(RuleFinding(code=code, message=message.strip(), ref=span.ref))
    return findings


def span_interval(spans: Sequence[SpanFact]) -> tuple[int, int]:
    """Return the minimum start and maximum end of ``spans``."""
    start = min(span.start_nano for span in spans)
    end = max(max(span.end_nano, span.start_nano) for span in spans)
    return start, end


__all__ = [
    "ModelToolCall",
    "RuleFinding",
    "SpanFact",
    "scan_rules",
    "span_interval",
    "spans_from_record",
]

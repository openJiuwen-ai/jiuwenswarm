# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Turn a trajectory stretch into the conversation TTSE induces experience from.

TTSE learns from OpenAI-style chat messages and a task query, while the
diagnosis keeps OTLP records. This module projects the finished spans of one
subject inside a time window into that shape: the user task, the assistant
replies with their tool calls, and the tool results.
"""

from __future__ import annotations

from typing import Any

from jiuwenswarm.diagnosis.snapshot import TrajectorySnapshot
from jiuwenswarm.diagnosis.spans import SpanFact, spans_from_record


def build_ttse_conversation(
    snapshot: TrajectorySnapshot,
    subject_id: str,
    start_nano: int,
    end_nano: int,
) -> tuple[str, list[dict[str, Any]]]:
    """Project one subject's spans inside a closed window into chat messages.

    Args:
        snapshot: Trajectory snapshot of the session.
        subject_id: Subject whose trajectory is projected.
        start_nano: Window start (inclusive), matched against span start times.
        end_nano: Window end (inclusive).

    Returns:
        The task query (the user text seen by the first model call in the
        window) and the messages. Both are empty when the window holds no
        usable span.
    """
    facts: list[SpanFact] = []
    for subject in snapshot.subjects:
        if subject.subject_id != subject_id:
            continue
        for record in subject.records:
            if not start_nano <= record.start_nano <= end_nano:
                continue
            envelope = {"trace_id": record.trace_id, "span_id": record.span_id, "lifecycle": "final"}
            facts.extend(spans_from_record(envelope, record.otlp, subject_id))
    facts.sort(key=lambda fact: (fact.start_nano, fact.ref.span_id))
    task_query = next(
        (fact.input_user_text for fact in facts if fact.kind == "inference" and fact.input_user_text),
        "",
    )
    messages: list[dict[str, Any]] = []
    if task_query:
        messages.append({"role": "user", "content": task_query})
    for fact in facts:
        if fact.kind == "inference":
            if not fact.output_text and not fact.output_tool_calls:
                continue
            message: dict[str, Any] = {"role": "assistant", "content": fact.output_text or ""}
            if fact.output_tool_calls:
                message["tool_calls"] = [
                    {
                        "id": call.call_id,
                        "type": "function",
                        "function": {"name": call.name, "arguments": call.arguments},
                    }
                    for call in fact.output_tool_calls
                ]
            messages.append(message)
        elif fact.kind == "tool":
            messages.append(
                {
                    "role": "tool",
                    "name": fact.tool_name or fact.name,
                    "tool_call_id": fact.tool_call_id or "",
                    "content": fact.tool_result or fact.status_message or "",
                },
            )
    return task_query, messages


__all__ = ["build_ttse_conversation"]

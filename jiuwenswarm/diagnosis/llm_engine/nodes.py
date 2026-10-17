# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Trajectory nodes and the transcript the LLM engine reads.

Every span of a subject is numbered in start-time order and becomes one node
named ``<subject_id>:<n>``. The transcript prints one line per message or tool
call, each led by its node id, so the model can cite steps by id and the host
can validate them.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace

from jiuwenswarm.diagnosis.models import StepRef
from jiuwenswarm.diagnosis.snapshot import SubjectRange, TrajectorySnapshot
from jiuwenswarm.diagnosis.spans import SpanFact, spans_from_record

#: Character budget of the whole transcript handed to the model.
MAX_TRANSCRIPT_CHARS = 60_000


@dataclass(frozen=True, slots=True)
class Node:
    """One citable step of the trajectory.

    Attributes:
        node_id: Identifier ``<subject_id>:<n>`` the model cites.
        ref: Step anchor carrying ``node_id``.
        fact: Normalized span behind the node.
        subject_name: Human-readable subject name.
        subject_kind: Subject kind reported by the trajectory store.
    """

    node_id: str
    ref: StepRef
    fact: SpanFact
    subject_name: str
    subject_kind: str


@dataclass(frozen=True, slots=True)
class _Limits:
    text: int
    arguments: int
    result: int


# Per-field clip limits, tried from the most to the least generous.
_LIMIT_STEPS = (
    _Limits(text=1200, arguments=400, result=1200),
    _Limits(text=500, arguments=200, result=500),
    _Limits(text=200, arguments=100, result=200),
)


def select_nodes(snapshot: TrajectorySnapshot, ranges: Sequence[SubjectRange]) -> list[Node]:
    """Return the nodes whose start lies inside their subject's range.

    Numbering covers every span of the subject, not only the ones in range, so
    a node keeps its id however the range is chosen.

    Args:
        snapshot: Finished records of the session.
        ranges: At most one range per diagnosed subject.

    Returns:
        Nodes ordered by subject, then start time.
    """
    windows = {item.subject_id: item for item in ranges}
    nodes: list[Node] = []
    for subject in snapshot.subjects:
        window = windows.get(subject.subject_id)
        if window is None:
            continue
        facts: list[SpanFact] = []
        for record in subject.records:
            envelope = {"trace_id": record.trace_id, "span_id": record.span_id, "lifecycle": "final"}
            facts.extend(spans_from_record(envelope, record.otlp, subject.subject_id))
        facts.sort(key=lambda fact: (fact.start_nano, fact.ref.span_id))
        for index, fact in enumerate(facts):
            if not window.start_nano <= fact.start_nano <= window.end_nano:
                continue
            node_id = f"{subject.subject_id}:{index}"
            ref = fact.ref.model_copy(update={"node_id": node_id})
            nodes.append(
                Node(
                    node_id=node_id,
                    ref=ref,
                    fact=replace(fact, ref=ref),
                    subject_name=subject.display_name or subject.subject_id,
                    subject_kind=subject.kind,
                ),
            )
    return nodes


def _clip(text: str | None, limit: int) -> str:
    collapsed = " ".join((text or "").split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[:limit] + "...[truncated]"


def _node_line(node: Node, limits: _Limits) -> str | None:
    fact = node.fact
    if fact.kind == "inference":
        parts: list[str] = []
        text = _clip(fact.output_text, limits.text)
        if text:
            parts.append(text)
        for call in fact.output_tool_calls:
            parts.append(f"calls {call.name}({_clip(call.arguments, limits.arguments)})")
        if fact.has_error:
            parts.append(f"ERROR: {_clip(fact.status_message, limits.arguments) or 'model call failed'}")
        if not parts:
            return None
        return f"[{node.node_id}] ASSISTANT: " + " | ".join(parts)
    if fact.kind == "tool":
        name = fact.tool_name or fact.name or "tool"
        result = _clip(fact.tool_result or fact.status_message, limits.result)
        flag = " [ERROR]" if fact.has_error else ""
        return f"[{node.node_id}] TOOL {name}({_clip(fact.tool_arguments, limits.arguments)}) =>{flag} {result}"
    return None


def _lines(nodes: Sequence[Node], limits: _Limits) -> list[str]:
    lines: list[str] = []
    current_subject: str | None = None
    last_user: str | None = None
    for node in nodes:
        subject_id = node.ref.subject_id
        if subject_id != current_subject:
            current_subject = subject_id
            last_user = None
            lines.append(f"=== Subject {subject_id} ({node.subject_name}, {node.subject_kind or 'agent'}) ===")
        fact = node.fact
        if fact.kind == "inference" and fact.input_user_text and fact.input_user_text != last_user:
            last_user = fact.input_user_text
            lines.append(f"USER: {_clip(fact.input_user_text, limits.text)}")
        line = _node_line(node, limits)
        if line is not None:
            lines.append(line)
    return lines


def render_transcript(nodes: Sequence[Node], *, max_chars: int = MAX_TRANSCRIPT_CHARS) -> str:
    """Render the nodes as the transcript the model reads.

    Fields are clipped harder until the transcript fits ``max_chars``. When even
    the tightest clipping is too long, the middle is dropped and replaced by a
    marker, keeping the start of the task and the most recent steps.

    Args:
        nodes: Nodes from :func:`select_nodes`.
        max_chars: Character budget.

    Returns:
        The transcript text; empty when there is nothing to show.
    """
    lines: list[str] = []
    for limits in _LIMIT_STEPS:
        lines = _lines(nodes, limits)
        if sum(len(line) + 1 for line in lines) <= max_chars:
            return "\n".join(lines)
    head_budget = max_chars // 4
    tail_budget = max_chars - head_budget
    head: list[str] = []
    used = 0
    for line in lines:
        if used + len(line) + 1 > head_budget:
            break
        head.append(line)
        used += len(line) + 1
    tail: list[str] = []
    used = 0
    for line in reversed(lines[len(head) :]):
        if used + len(line) + 1 > tail_budget:
            break
        tail.append(line)
        used += len(line) + 1
    tail.reverse()
    omitted = len(lines) - len(head) - len(tail)
    return "\n".join([*head, f"... {omitted} lines omitted to fit the size limit ...", *tail])


__all__ = ["MAX_TRANSCRIPT_CHARS", "Node", "render_transcript", "select_nodes"]

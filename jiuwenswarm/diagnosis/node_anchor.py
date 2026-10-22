# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Anchor assistant nodes on the model call that produced them.

The core node builder anchors an assistant message that reached a context
window on the ``context.window.commit`` span of the *next* model call (the one
that first carried the message in its prompt), and a turn's final reply on the
turn root span. Neither is the row a reader expects when a root cause names
the message: the trajectory view shows the reply on the model call that wrote
it. The diagnosis therefore re-anchors those nodes here, without touching what
the core agent reads (ids, text and timestamps stay as they are).
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping, Sequence
from typing import Any

from openjiuwen.agent_evolving.attribution.nodes import TrajectoryNode
from openjiuwen.agent_evolving.trajectory.messages import tool_call_id
from openjiuwen.agent_evolving.trajectory.spans import is_compaction_span, read_llm_exchange, span_identity
from openjiuwen.agent_evolving.trajectory.team import span_category

#: Node sources whose span is a context commit or a turn root, not a model call.
_REANCHORED_SOURCES = frozenset({"msg", "turn"})

SpanKey = tuple[str, str]


@dataclasses.dataclass(frozen=True, slots=True)
class _Reply:
    """Text one model call wrote, with the call's span."""

    text: str
    key: SpanKey


@dataclasses.dataclass(frozen=True, slots=True)
class _ModelCalls:
    """What the model calls of one subject wrote."""

    by_call_id: dict[str, SpanKey]
    replies: list[_Reply]


def _index_model_calls(records: Sequence[Mapping[str, Any]]) -> _ModelCalls:
    """Index the tool-call ids and the reply texts by the model call that wrote them.

    Args:
        records: Spans of one subject in start order.

    Returns:
        The first call that wrote each tool-call id, and every text reply in order.
    """
    by_call_id: dict[str, SpanKey] = {}
    replies: list[_Reply] = []
    for span in records:
        if not isinstance(span, Mapping) or span_category(span) != "llm" or is_compaction_span(span):
            continue
        identity = span_identity(span)
        if identity is None or identity[0] is None or identity[1] is None:
            continue
        key = (str(identity[0]), str(identity[1]))
        _prompts, completions = read_llm_exchange(span)
        for message in completions:
            if message.get("role") != "assistant":
                continue
            for call in message.get("tool_calls") or ():
                call_id = tool_call_id(call)
                if call_id is not None:
                    by_call_id.setdefault(str(call_id), key)
            content = message.get("content")
            if content is not None and str(content).strip():
                replies.append(_Reply(text=str(content).strip(), key=key))
    return _ModelCalls(by_call_id=by_call_id, replies=replies)


def _producing_call(node: TrajectoryNode, calls: _ModelCalls) -> SpanKey | None:
    """Return the model call that wrote an assistant node, or ``None`` when unknown."""
    for call in node.tool_calls:
        call_id = tool_call_id(call)
        if call_id is not None and str(call_id) in calls.by_call_id:
            return calls.by_call_id[str(call_id)]
    text = (node.content or "").strip()
    if not text:
        return None
    for reply in calls.replies:
        if reply.text == text:
            return reply.key
    if node.source_kind == "turn":
        # A turn's output joins every reply of the turn; the last one closes it.
        contained = [reply for reply in calls.replies if reply.text in text]
        if contained:
            return contained[-1].key
    return None


def anchor_assistant_nodes(
    nodes_by_subagent: Mapping[str, list[TrajectoryNode]],
    core_snapshot: Mapping[str, Any],
) -> dict[str, list[TrajectoryNode]]:
    """Re-anchor assistant messages on the model call that wrote them.

    Args:
        nodes_by_subagent: Numbered nodes per subject.
        core_snapshot: The core snapshot the nodes were built from.

    Returns:
        The nodes per subject; an assistant node whose writer cannot be found
        keeps its original span.
    """
    records_by_subject: dict[str, list[Mapping[str, Any]]] = {}
    for subject in core_snapshot.get("subjects") or ():
        if isinstance(subject, Mapping):
            records_by_subject[str(subject.get("subject_id") or "")] = list(subject.get("records") or [])
    anchored: dict[str, list[TrajectoryNode]] = {}
    for subject_id, nodes in nodes_by_subagent.items():
        calls: _ModelCalls | None = None
        result: list[TrajectoryNode] = []
        for node in nodes:
            if node.role != "assistant" or node.source_kind not in _REANCHORED_SOURCES:
                result.append(node)
                continue
            if calls is None:
                calls = _index_model_calls(records_by_subject.get(subject_id, []))
            key = _producing_call(node, calls)
            if key is None or key == (node.trace_id, node.span_id):
                result.append(node)
                continue
            result.append(dataclasses.replace(node, trace_id=key[0], span_id=key[1]))
        anchored[subject_id] = result
    return anchored


__all__ = ["anchor_assistant_nodes"]

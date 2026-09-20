# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Project consumed Symphony retrieval results out of model context windows."""

from __future__ import annotations

import ast
import json
from typing import Any

from pydantic import BaseModel

from openjiuwen.core.context_engine.base import ContextWindow, ModelContext
from openjiuwen.core.context_engine.context.context_utils import ContextUtils
from openjiuwen.core.context_engine.context_engine import ContextEngine
from openjiuwen.core.context_engine.processor.base import ContextEvent, ContextProcessor
from openjiuwen.core.foundation.llm import BaseMessage, ToolMessage, UserMessage


_RETRIEVAL_TOOL_NAMES = frozenset({"skill_branch_explore", "skill_branch_peek"})
_COMPOSE_TOOL_NAME = "symphony_compose_graph"
_COMPACT_REASON = "retrieval_consumed_by_executable_plan"
_MAX_SUMMARY_BYTES = 1024


class SymphonyRetrievalCompactProcessorConfig(BaseModel):
    """Configuration placeholder for the always-on Symphony projection."""


class _ReadyPlan:
    def __init__(self, selected_skill_ids: tuple[str, ...]) -> None:
        self.selected_skill_ids = selected_skill_ids


@ContextEngine.register_processor()
class SymphonyRetrievalCompactProcessor(ContextProcessor):
    """Compact retrieval tool results after a valid executable plan is returned.

    The processor only changes the outgoing ``ContextWindow``. The canonical
    ``ModelContext`` and persisted history retain the original tool results.
    """

    @property
    def config(self) -> SymphonyRetrievalCompactProcessorConfig:
        return self._config

    async def trigger_get_context_window(
        self,
        context: ModelContext,
        context_window: ContextWindow,
        **kwargs: Any,
    ) -> bool:
        _ = context, kwargs
        return bool(self._find_replacements(context_window.context_messages))

    async def on_get_context_window(
        self,
        context: ModelContext,
        context_window: ContextWindow,
        **kwargs: Any,
    ) -> tuple[ContextEvent | None, ContextWindow]:
        _ = context, kwargs
        replacements = self._find_replacements(context_window.context_messages)
        if not replacements:
            return None, context_window

        messages = list(context_window.context_messages)
        for index, content in replacements.items():
            message = messages[index]
            if isinstance(message, ToolMessage):
                messages[index] = message.model_copy(update={"content": content})

        window = context_window.model_copy(update={"context_messages": messages})
        event = ContextEvent(
            event_type=self.processor_type(),
            messages_to_modify=sorted(replacements),
            compact_summary=f"Compacted {len(replacements)} consumed Symphony retrieval result(s)",
        )
        return event, window

    @classmethod
    def _find_replacements(cls, messages: list[BaseMessage]) -> dict[int, str]:
        pending_retrievals: list[int] = []
        replacements: dict[int, str] = {}
        in_user_round = False

        for index, message in enumerate(messages):
            if isinstance(message, UserMessage):
                pending_retrievals.clear()
                in_user_round = True
                continue
            if not in_user_round or not isinstance(message, ToolMessage):
                continue

            tool_name = ContextUtils.resolve_tool_name_from_message(message, messages)
            if tool_name in _RETRIEVAL_TOOL_NAMES:
                pending_retrievals.append(index)
                continue
            if tool_name != _COMPOSE_TOOL_NAME:
                continue

            ready_plan = cls._parse_ready_plan(message.content)
            if ready_plan is None:
                continue

            for retrieval_index in pending_retrievals:
                summary = cls._build_summary(
                    messages[retrieval_index].content,
                    ready_plan,
                )
                if summary is not None:
                    replacements[retrieval_index] = summary
            pending_retrievals.clear()

        return replacements

    @classmethod
    def _build_summary(
        cls,
        content: Any,
        ready_plan: _ReadyPlan,
    ) -> str | None:
        if not isinstance(content, str):
            return None
        payload = cls._parse_payload(content)
        if isinstance(payload, dict) and payload.get("compacted") is True:
            return None

        summary: dict[str, Any] = {
            "success": True,
            "compacted": True,
            "reason": _COMPACT_REASON,
            "selected_skill_ids": list(ready_plan.selected_skill_ids),
        }
        candidate_count = cls._candidate_count(payload)
        if candidate_count is not None:
            summary["candidate_count"] = candidate_count

        compacted = json.dumps(summary, ensure_ascii=False, separators=(",", ":"))
        compacted_bytes = compacted.encode("utf-8")
        if len(compacted_bytes) > _MAX_SUMMARY_BYTES:
            return None
        if len(compacted_bytes) >= len(content.encode("utf-8")):
            return None
        return compacted

    @classmethod
    def _parse_ready_plan(cls, content: Any) -> _ReadyPlan | None:
        payload = cls._parse_payload(content)
        if not isinstance(payload, dict) or payload.get("success") is not True:
            return None

        planned_graph = payload.get("planned_graph")
        graph = planned_graph.get("graph") if isinstance(planned_graph, dict) else None
        if not isinstance(graph, dict) or graph.get("type") != "planned_graph":
            return None
        metadata = graph.get("metadata")
        if not isinstance(metadata, dict) or metadata.get("status") != "ready":
            return None
        if metadata.get("missing_inputs"):
            return None

        nodes = graph.get("nodes")
        edges = graph.get("edges")
        if not isinstance(nodes, dict) or not nodes or not isinstance(edges, list):
            return None

        selected_skill_ids: list[str] = []
        for node_id, node in nodes.items():
            if not isinstance(node_id, str) or not node or not isinstance(node, dict):
                return None
            node_metadata = node.get("metadata")
            if not isinstance(node_metadata, dict):
                return None
            if node_metadata.get("type") == "skill":
                selected_skill_ids.append(node_id)

        if not selected_skill_ids:
            return None

        for edge in edges:
            if not isinstance(edge, dict):
                return None
            if edge.get("source") not in nodes or edge.get("target") not in nodes:
                return None

        return _ReadyPlan(tuple(selected_skill_ids))

    @staticmethod
    def _parse_payload(content: Any) -> dict[str, Any] | None:
        if isinstance(content, dict):
            return content
        if not isinstance(content, str):
            return None
        text = content.strip()
        if not text:
            return None

        for parser in (json.loads, ast.literal_eval):
            try:
                payload = parser(text)
            except (ValueError, SyntaxError, TypeError):
                continue
            if isinstance(payload, dict):
                return payload
        return None

    @classmethod
    def _candidate_count(cls, payload: dict[str, Any] | None) -> int | None:
        if not isinstance(payload, dict):
            return None
        skill_tree = payload.get("skill_tree")
        if not isinstance(skill_tree, dict):
            return None
        explicit_count = skill_tree.get("candidate_count")
        if isinstance(explicit_count, int) and not isinstance(explicit_count, bool) and explicit_count >= 0:
            return explicit_count
        candidates = skill_tree.get("candidates")
        if isinstance(candidates, (list, dict)):
            return len(candidates)
        return None


def symphony_retrieval_compact_processor_spec() -> tuple[
    str,
    SymphonyRetrievalCompactProcessorConfig,
]:
    """Return the processor registration consumed by ContextProcessorRail."""

    return (
        SymphonyRetrievalCompactProcessor.processor_type(),
        SymphonyRetrievalCompactProcessorConfig(),
    )


__all__ = [
    "SymphonyRetrievalCompactProcessor",
    "SymphonyRetrievalCompactProcessorConfig",
    "symphony_retrieval_compact_processor_spec",
]

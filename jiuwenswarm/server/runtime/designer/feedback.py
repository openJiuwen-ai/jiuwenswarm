# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Per-node notes read from Designer pipeline feedback (self/director ratings)."""

from __future__ import annotations

from typing import Any


def director_node_suggestions(feedback: dict[str, Any] | None) -> dict[str, Any]:
    """Per-node rerun notes. Trajectory stores them on director.suggestions.

    Reports written before that key existed nest the same map under
    director.final.suggestions.
    """
    if not isinstance(feedback, dict):
        return {}
    director = feedback.get("director") if isinstance(feedback.get("director"), dict) else {}
    top = director.get("suggestions")
    if isinstance(top, dict) and top:
        return top
    final = director.get("final") if isinstance(director.get("final"), dict) else {}
    nested = final.get("suggestions")
    if isinstance(nested, dict) and nested:
        return nested
    return top if isinstance(top, dict) else {}


def suggestion_for_node(feedback: dict[str, Any] | None, node_id: str) -> str:
    if not feedback:
        return ""
    suggestions = director_node_suggestions(feedback)
    if suggestions.get(node_id):
        return str(suggestions[node_id])
    agents = feedback.get("agents") or {}
    agent = agents.get(node_id) or {}
    if agent.get("suggestion_for_next"):
        return str(agent["suggestion_for_next"])
    final = feedback.get("final") or {}
    return str(final.get("improvement_plan") or "")

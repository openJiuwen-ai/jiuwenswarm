# -*- coding: utf-8 -*-
"""Utility helpers for orchestration planning."""

from __future__ import annotations

from typing import Any, Sequence


def skill_id(node_id: Any) -> str:
    text = str(node_id or "")
    return text.removeprefix("skill:")


def clamp(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def skill_payload(skill: dict[str, Any]) -> dict[str, Any]:
    current_skill_id = str(skill.get("id") or "")
    return {
        "id": current_skill_id,
        "name": str(skill.get("name") or current_skill_id),
        "description": str(skill.get("description") or "")[:800],
    }


def eligible_can_feed_edges(
    edges: Sequence[dict[str, Any]],
    *,
    known_skill_ids: set[str],
    min_confidence: float,
) -> list[dict[str, Any]]:
    eligible = []
    for edge in edges:
        source_id = skill_id(edge.get("source"))
        target_id = skill_id(edge.get("target"))
        if edge.get("type") != "can_feed":
            continue
        if float(edge.get("confidence") or 0.0) < min_confidence:
            continue
        if source_id not in known_skill_ids or target_id not in known_skill_ids:
            continue
        eligible.append(edge)
    return sorted(
        eligible,
        key=lambda item: (
            -float(item.get("confidence") or 0.0),
            skill_id(item.get("source")),
            skill_id(item.get("target")),
        ),
    )


def normalize_known_skill_ids(
    values: Sequence[str] | None,
    *,
    known_skill_ids: set[str],
) -> tuple[str, ...] | None:
    if values is None:
        return None
    output = []
    seen = set()
    for value in values:
        current_skill_id = str(value or "").strip()
        if (
            current_skill_id
            and current_skill_id not in seen
            and current_skill_id in known_skill_ids
        ):
            seen.add(current_skill_id)
            output.append(current_skill_id)
    return tuple(output) if output else None

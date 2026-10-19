"""Online orchestration planning service APIs."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Sequence
from uuid import uuid4

from jiuwenswarm.symphony.config import (
    DEFAULT_ORCHESTRATION_MIN_EDGE_CONFIDENCE,
    SymphonyOrchestrationConfig,
)
from jiuwenswarm.symphony.evolution.service import load_dynamic_overlay
from jiuwenswarm.symphony.llm import LLMConfig
from jiuwenswarm.symphony.orchestration.artifacts import (
    filter_disabled_score_artifacts,
    load_score_artifacts,
)
from jiuwenswarm.symphony.orchestration.execution_graph import build_execution_graph
from jiuwenswarm.symphony.orchestration.planning.beam import BidirectionalBeamPlanner
from jiuwenswarm.symphony.orchestration.planning.fast import FastOneShotPlanner
from jiuwenswarm.symphony.orchestration.planning.utils import clamp


async def plan_from_score(
    score_dir: str | Path,
    query: str,
    llm_config: LLMConfig | None = None,
    *,
    top_k: int = 3,
    max_depth: int = 4,
    min_edge_confidence: float = DEFAULT_ORCHESTRATION_MIN_EDGE_CONFIDENCE,
    llm_client: Any | None = None,
    orchestration_config: SymphonyOrchestrationConfig | None = None,
    dynamic_graph_enabled: bool = False,
    candidate_skill_ids: Sequence[str] | None = None,
    disabled_skill_names: Sequence[str] | None = None,
    progress_callback: Any | None = None,
    language: str = "cn",
) -> dict[str, Any]:
    """Run online planning from an existing Symphony score."""

    if orchestration_config is not None:
        top_k = orchestration_config.top_k
        max_depth = orchestration_config.max_depth
        min_edge_confidence = orchestration_config.min_edge_confidence
    mode = orchestration_config.mode if orchestration_config is not None else "fast"

    artifacts = filter_disabled_score_artifacts(
        load_score_artifacts(score_dir),
        disabled_skill_names,
    )
    if mode not in {"fast", "beam"}:
        raise ValueError(f"Unsupported orchestration mode: {mode}")

    plan_id = str(uuid4())
    dynamic_overlay = (
        load_dynamic_overlay(artifacts.score_dir) if dynamic_graph_enabled else {}
    )
    selected_candidate_skill_ids, skill_retrieval = _input_candidate_summary(
        candidate_skill_ids,
        skills=artifacts.skills,
    )
    if mode == "beam":
        result = await BidirectionalBeamPlanner(
            artifacts,
            llm_config=llm_config,
            llm_client=llm_client,
            min_edge_confidence=clamp(min_edge_confidence),
            top_k=max(1, int(top_k)),
            max_depth=max(1, int(max_depth)),
            candidate_skill_ids=selected_candidate_skill_ids,
            progress_callback=progress_callback,
            language=language,
        ).plan(query)
    else:
        result = await FastOneShotPlanner(
            artifacts,
            llm_config=llm_config,
            llm_client=llm_client,
            min_edge_confidence=clamp(min_edge_confidence),
            max_depth=max(1, int(max_depth)),
            candidate_skill_ids=selected_candidate_skill_ids,
            language=language,
            dynamic_overlay=dynamic_overlay,
        ).plan(query)
    result["language"] = language
    result["plan_id"] = plan_id
    result["score_dir"] = str(artifacts.score_dir)
    result["dynamic_graph_enabled"] = bool(dynamic_graph_enabled)
    result["skill_retrieval"] = skill_retrieval
    result["execution_graph"] = build_execution_graph(result, artifacts)
    return result


def _input_candidate_summary(
    values: Sequence[str] | None,
    *,
    skills: Sequence[dict[str, Any]],
) -> tuple[tuple[str, ...] | None, dict[str, Any]]:
    normalized = _normalize_candidate_skill_ids(values)
    selected = _resolve_candidate_skill_ids(normalized, skills)
    fallback_reason = ""
    if values is None:
        fallback_reason = "candidate_skill_ids not provided"
    elif not normalized:
        fallback_reason = "candidate_skill_ids is empty"
    elif not selected:
        fallback_reason = "candidate_skill_ids did not match current score"

    return selected or None, {
        "source": "input",
        "used": bool(selected),
        "candidate_skill_ids": list(selected),
        "candidate_count": len(selected),
        "fallback_reason": fallback_reason,
    }


def _resolve_candidate_skill_ids(
    values: Sequence[str],
    skills: Sequence[dict[str, Any]],
) -> tuple[str, ...]:
    known_skill_ids = {
        str(skill.get("id") or "").strip()
        for skill in skills
        if str(skill.get("id") or "").strip()
    }
    skill_ids_by_name: dict[str, set[str]] = {}
    for skill in skills:
        current_skill_id = str(skill.get("id") or "").strip()
        name = str(skill.get("name") or "").strip()
        if current_skill_id and name:
            skill_ids_by_name.setdefault(name, set()).add(current_skill_id)

    selected: list[str] = []
    seen: set[str] = set()
    for value in values:
        current_skill_id = value if value in known_skill_ids else ""
        if not current_skill_id:
            matching_ids = skill_ids_by_name.get(value, set())
            if len(matching_ids) == 1:
                current_skill_id = next(iter(matching_ids))
        if not current_skill_id or current_skill_id in seen:
            continue
        seen.add(current_skill_id)
        selected.append(current_skill_id)
    return tuple(selected)


def _normalize_candidate_skill_ids(values: Sequence[str] | None) -> tuple[str, ...]:
    if values is None:
        return ()
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        current_skill_id = str(value or "").strip()
        if not current_skill_id or current_skill_id in seen:
            continue
        seen.add(current_skill_id)
        output.append(current_skill_id)
    return tuple(output)

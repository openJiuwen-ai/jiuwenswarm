"""Bidirectional beam planner for Symphony score orchestration."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import mimetypes
import re
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Iterable, Sequence

from jiuwenswarm.symphony.llm import LLMConfig, create_llm_client, llm_usage_context
from jiuwenswarm.symphony.orchestration.artifacts import ScoreArtifacts
from jiuwenswarm.symphony.orchestration.language import (
    default_beam_plan_title,
    planner_language_instruction,
)
from jiuwenswarm.symphony.orchestration.planning.models import (
    OrchestrationPlan,
    SearchState,
)
from jiuwenswarm.symphony.orchestration.planning.plan_builder import (
    build_incoming_edges,
    build_outgoing_edges,
    edge_plan_item,
    state_to_plan,
)
from jiuwenswarm.symphony.orchestration.planning.utils import (
    eligible_can_feed_edges,
    normalize_known_skill_ids,
    skill_id,
    skill_payload,
)

SEMANTIC_SCORE_THRESHOLD = 0.5
SEED_BASELINE_SCORE = 0.5
DEFAULT_MAX_CONCURRENT_JUDGES = 3
BEAM_DEFAULT_MAX_SKILLS = 40
BeamProgressCallback = Callable[[dict[str, Any]], Awaitable[None] | None]
logger = logging.getLogger(__name__)
_EXPLICIT_FILE_EXTENSION_RE = re.compile(
    r"(?<![A-Za-z0-9])[\w.-]+\.([A-Za-z0-9]{1,12})\b"
)
_MIME_TO_OUTPUT_TYPE = {
    "application/json": "json",
    "application/msword": "docx",
    "application/pdf": "pdf",
    "application/vnd.ms-excel": "xlsx",
    "application/vnd.ms-powerpoint": "pptx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": (
        "pptx"
    ),
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "text/csv": "csv",
    "text/html": "html",
}
for _extension, _mime_type in {
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".ppt": "application/vnd.ms-powerpoint",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".xls": "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}.items():
    mimetypes.add_type(_mime_type, _extension)

BEAM_JUDGE_SYSTEM_PROMPT = """You are Symphony's bidirectional beam expansion judge.
Return strict JSON only.

You receive:
- The user's query.
- The current Skill being expanded.
- A search direction: forward or backward.
- Candidate Skills already validated by existing can_feed edges.

Task:
- Score each candidate from 0.0 to 1.0 for usefulness to the user's query and
  the local Skill chain represented by the current Skill and search direction.
- Treat data dependency as already validated; do not judge edge feasibility.
- Do not invent Skills, inputs, outputs, or edge relationships.

Schema:
{
  "judgements": [
    {"candidate_id": "candidate-id", "score": 0.0, "reason": "short reason"}
  ]
}
"""

BEAM_FINAL_RERANK_SYSTEM_PROMPT = """You are a final task orchestration reranker.
Return strict JSON only.

You receive the user's query, candidate plans produced by bidirectional beam
search, a candidate Skill pool, and the existing can_feed edges available among
those Skills.

Task:
- Select the best plan for completing the user's task.
- Candidate plans are references, not hard constraints. You may combine Skills
  from different plans or remove duplicate-function Skills.
- Use only Skill IDs from candidate_skill_pool.
- Use only edges from candidate_can_feed_edges. Skills without a dependency edge
  may remain parallel steps in the same plan.
- Preserve distinct Skills needed for multi-stage or multi-deliverable tasks.
- Prefer the shortest plan that covers the user's intent.
- Do not invent Skills, inputs, outputs, dependencies, or artifacts.
- Keep each reason <= 50 Chinese characters or <= 12 English words.

Schema:
{
  "selected_plan_index": 1,
  "reason": "short reason",
  "steps": [
    {"skill_id": "skill-a", "reason": "short reason"}
  ],
  "can_feed_edges": [
    {"source_id": "skill-a", "target_id": "skill-b"}
  ]
}
"""


@dataclass(frozen=True)
class BeamState:
    """One bidirectional beam search state."""

    skill_ids: tuple[str, ...]
    edge_indices: tuple[int, ...]
    semantic_scores: tuple[float, ...]
    score_reasons: tuple[str, ...]
    seed_skill_ids: tuple[str, ...]
    last_direction: str

    @property
    def depth(self) -> int:
        return len(self.skill_ids)

    @property
    def head(self) -> str:
        return self.skill_ids[0]

    @property
    def tail(self) -> str:
        return self.skill_ids[-1]

    def to_search_state(self) -> SearchState:
        return SearchState(
            skill_ids=self.skill_ids,
            edges=self.edge_indices,
            score_reasons=self.score_reasons,
        )


@dataclass(frozen=True)
class NeighborExpansion:
    """A single adjacent Skill candidate to be judged."""

    candidate_id: str
    state: BeamState
    direction: str
    current_skill_id: str
    candidate_skill_id: str
    edge_index: int


@dataclass(frozen=True)
class SemanticJudgement:
    """LLM judgement for one neighbor expansion."""

    candidate_id: str
    score: float
    reason: str


_GRAPH_STATUS_PRIORITY = {
    "pending": 0,
    "rejected": 1,
    "seed": 2,
    "selected": 3,
    "final": 4,
}


class BeamSearchGraph:
    """Lightweight search graph for user-facing beam progress."""

    def __init__(
        self,
        skill_by_id: dict[str, dict[str, Any]],
    ) -> None:
        self.skill_by_id = skill_by_id
        self.nodes: dict[str, dict[str, Any]] = {}
        self.graph_edges: dict[str, dict[str, Any]] = {}

    def add_seed(self, current_skill_id: str) -> None:
        self._ensure_node(current_skill_id, status="seed", seed=True)

    def add_candidate(self, expansion: NeighborExpansion) -> None:
        self._ensure_node(
            expansion.current_skill_id,
            status="seed" if self._is_seed(expansion.current_skill_id) else "selected",
        )
        self._ensure_node(
            expansion.candidate_skill_id,
            status="pending",
        )
        self._ensure_edge(expansion, status="pending")

    def apply_judgement(
        self,
        expansion: NeighborExpansion,
        judgement: SemanticJudgement,
    ) -> None:
        status = (
            "selected" if judgement.score >= SEMANTIC_SCORE_THRESHOLD else "rejected"
        )
        self._ensure_node(
            expansion.candidate_skill_id,
            status=status,
        )
        self._ensure_edge(expansion, status=status)

    def mark_final_plan(self, plan: dict[str, Any]) -> None:
        for step in plan.get("steps") or []:
            if not isinstance(step, dict):
                continue
            current_skill_id = str(step.get("skill_id") or "").strip()
            if not current_skill_id:
                continue
            self._ensure_node(current_skill_id, status="final")
        for edge in plan.get("can_feed_edges") or []:
            if not isinstance(edge, dict):
                continue
            source_id = str(edge.get("source_id") or edge.get("source") or "").strip()
            target_id = str(edge.get("target_id") or edge.get("target") or "").strip()
            edge_id = self._existing_edge_id(source_id, target_id)
            if not edge_id:
                continue
            self.graph_edges[edge_id]["status"] = _dominant_status(
                str(self.graph_edges[edge_id].get("status") or ""),
                "final",
            )

    def snapshot(self) -> dict[str, Any]:
        nodes = []
        for node in self.nodes.values():
            nodes.append(
                {
                    "id": node["id"],
                    "label": node["label"],
                    "status": node["status"],
                    "seed": bool(node.get("seed")),
                }
            )
        edges = [_copy_public_graph_edge(edge) for edge in self.graph_edges.values()]
        return {"nodes": nodes, "edges": edges}

    def _ensure_node(
        self,
        current_skill_id: str,
        *,
        status: str,
        seed: bool = False,
    ) -> None:
        node = self.nodes.get(current_skill_id)
        if node is None:
            node = {
                "id": current_skill_id,
                "label": _skill_label(self.skill_by_id.get(current_skill_id, {})),
                "status": status,
                "seed": seed,
            }
            self.nodes[current_skill_id] = node
        else:
            node["status"] = _dominant_status(str(node.get("status") or ""), status)
            node["seed"] = bool(node.get("seed")) or seed
        return node

    def _ensure_edge(
        self,
        expansion: NeighborExpansion,
        *,
        status: str,
    ) -> dict[str, Any]:
        source_id = expansion.current_skill_id
        target_id = expansion.candidate_skill_id
        edge_id = f"{source_id}->{target_id}"
        graph_edge = self.graph_edges.get(edge_id)
        if graph_edge is None:
            graph_edge = {
                "id": edge_id,
                "source": source_id,
                "target": target_id,
                "status": status,
            }
            self.graph_edges[edge_id] = graph_edge
        else:
            graph_edge["status"] = _dominant_status(
                str(graph_edge.get("status") or ""),
                status,
            )

    def _existing_edge_id(self, source_id: str, target_id: str) -> str:
        forward_id = f"{source_id}->{target_id}"
        if forward_id in self.graph_edges:
            return forward_id
        backward_id = f"{target_id}->{source_id}"
        if backward_id in self.graph_edges:
            return backward_id
        return ""

    def _is_seed(self, current_skill_id: str) -> bool:
        node = self.nodes.get(current_skill_id)
        return bool(node and node.get("seed"))


class BidirectionalBeamPlanner:
    """Build candidate plans through semantic bidirectional beam search."""

    def __init__(
        self,
        artifacts: ScoreArtifacts,
        *,
        llm_config: LLMConfig | None,
        llm_client: Any | None,
        min_edge_confidence: float,
        top_k: int,
        max_depth: int,
        candidate_skill_ids: Sequence[str] | None = None,
        max_concurrent_judges: int = DEFAULT_MAX_CONCURRENT_JUDGES,
        progress_callback: BeamProgressCallback | None = None,
        language: str = "cn",
    ) -> None:
        self.artifacts = artifacts
        self.llm_config = llm_config
        self.llm_client = llm_client
        self.min_edge_confidence = min_edge_confidence
        self.top_k = max(1, int(top_k))
        self.max_depth = max(1, int(max_depth))
        self.max_concurrent_judges = max(1, int(max_concurrent_judges))
        self.skill_by_id = artifacts.skill_by_id
        self.candidate_skill_ids = normalize_known_skill_ids(
            candidate_skill_ids,
            known_skill_ids=set(self.skill_by_id),
        )
        self.progress_callback = progress_callback
        self.language = language
        self.eligible_edges = self._sorted_eligible_edges()
        self._beam_graph = BeamSearchGraph(self.skill_by_id)
        self.outgoing_edges = build_outgoing_edges(self.eligible_edges)
        self.incoming_edges = build_incoming_edges(self.eligible_edges)
        self._judgement_cache: dict[tuple[str, str, str, str], SemanticJudgement] = {}
        self._cache_hits = 0
        self._cache_misses = 0
        self._judge_semaphore = asyncio.Semaphore(self.max_concurrent_judges)
        self._llm_call_count = 0
        self._final_rerank_call_count = 0
        self._judge_failures: list[str] = []
        self.required_output_types: frozenset[str] = frozenset()
        self._current_expansions: dict[str, NeighborExpansion] = {}
        self._current_expansion_aliases: dict[str, list[str]] = {}

    async def plan(self, query: str) -> dict[str, Any]:
        self._reset_beam_progress()
        self._final_rerank_call_count = 0
        self.required_output_types = _required_output_types_from_query(query)
        query_signature = _query_signature(query)
        frontier = self._initial_states()
        all_states = list(frontier)
        seed_skill_ids = tuple(state.skill_ids[0] for state in frontier)
        for current_skill_id in seed_skill_ids:
            self._beam_graph.add_seed(current_skill_id)
        await self._emit_beam_event(
            "started",
            round_index=0,
            graph=self._beam_graph.snapshot(),
        )
        round_index = 0

        for _round in range(max(0, self.max_depth - 1)):
            groups, cached = self._expansion_groups(query_signature, frontier)
            found = self._record_candidates_found()
            if found:
                await self._emit_beam_event(
                    "candidates_found",
                    round_index=_round + 1,
                    graph=self._beam_graph.snapshot(),
                )
            if not groups and not cached:
                break
            fresh = await self._judge_groups(query, groups)
            fresh = self._apply_judgement_aliases(fresh)
            for candidate_id, judgement in fresh.items():
                expansion = self._expansion_by_id(candidate_id)
                if expansion is not None:
                    self._cache_set(query_signature, expansion, judgement)
            judgements = {**cached, **fresh}
            judged = self._record_candidates_judged(judgements)
            if judged:
                await self._emit_beam_event(
                    "candidates_judged",
                    round_index=_round + 1,
                    graph=self._beam_graph.snapshot(),
                )
            next_states = self._apply_judgements(judgements)
            if not next_states:
                await self._emit_beam_event(
                    "graph_merged",
                    round_index=_round + 1,
                    graph=self._beam_graph.snapshot(),
                )
                round_index = _round + 1
                break
            retained = self._rank_and_trim(next_states, limit=self.top_k)
            all_states = retained
            frontier = retained
            round_index = _round + 1
            await self._emit_beam_event(
                "graph_merged",
                round_index=_round + 1,
                graph=self._beam_graph.snapshot(),
            )

        final_states = self._merge_converged_states(all_states)
        plans = self._plans_from_states(final_states)[: self.top_k]
        recommended_plans = [self._plan_payload(plan, query=query) for plan in plans]
        final_rerank = await self._final_rerank_plans(
            query=query,
            recommended_plans=recommended_plans,
            seed_skill_ids=seed_skill_ids,
        )
        recommended_plans = final_rerank["recommended_plans"]
        final_plan = recommended_plans[0] if recommended_plans else {}
        self._beam_graph.mark_final_plan(final_plan)
        await self._emit_beam_event(
            "completed",
            round_index=round_index,
            graph=self._beam_graph.snapshot(),
        )
        status = recommended_plans[0]["status"] if recommended_plans else "no_plan"
        reason = recommended_plans[0].get("reason", "") if recommended_plans else ""
        unique_skill_ids = {
            current_skill_id
            for state in all_states
            for current_skill_id in state.skill_ids
        }
        return {
            "query": query,
            "language": self.language,
            "score_dir": str(self.artifacts.score_dir),
            "planning_mode": "bidirectional_beam",
            "llm_call_count": self._llm_call_count + self._final_rerank_call_count,
            "candidate_skill_count": len(unique_skill_ids),
            "candidate_edge_count": len(self.eligible_edges),
            "plans": recommended_plans,
            "recommended_plans": recommended_plans,
            "ranking_mode": "bidirectional_beam",
            "beam_search": {
                "mode": "bidirectional_beam",
                "language": self.language,
                "round_index": round_index,
                "graph": self._beam_graph.snapshot(),
            },
            "decision": {
                "mode": "bidirectional_beam",
                "strategy": "semantic_bidirectional_beam",
                "validated_count": len(recommended_plans),
                "candidate_count": len(unique_skill_ids),
                "judge_llm_call_count": self._llm_call_count,
                "judge_cache_hits": self._cache_hits,
                "judge_cache_misses": self._cache_misses,
                "final_rerank": {
                    "applied": final_rerank["applied"],
                    "llm_call_count": self._final_rerank_call_count,
                    "cross_plan_skill_merge": final_rerank["cross_plan_skill_merge"],
                    "error": final_rerank["error"],
                },
            },
            "validation": {
                "valid": True,
                "details": self._judge_failures[:10],
            },
            "status": status,
            "reason": reason,
        }

    async def judge_expansions(
        self,
        query: str,
        expansions: list[NeighborExpansion],
    ) -> dict[str, SemanticJudgement]:
        if not expansions:
            return {}

        first = expansions[0]
        current_skill = self.skill_by_id.get(first.current_skill_id, {})
        payload = {
            "query": query,
            "direction": first.direction,
            "current_skill": skill_payload(current_skill),
            "candidates": [
                self._candidate_payload(expansion) for expansion in expansions
            ],
        }
        with llm_usage_context("orchestration", "bidirectional_beam_judgement"):
            raw = await self._client().complete_json_async(
                system_prompt=(
                    f"{BEAM_JUDGE_SYSTEM_PROMPT}\n"
                    f"{planner_language_instruction(self.language)}"
                ),
                user_content=json.dumps(payload, ensure_ascii=False),
                error_context="Symphony bidirectional beam judgement",
            )
        data = json.loads(raw)
        items = data.get("judgements") if isinstance(data, dict) else data
        if not isinstance(items, list):
            return {}

        known_ids = {expansion.candidate_id for expansion in expansions}
        judgements: dict[str, SemanticJudgement] = {}
        for item in items:
            if not isinstance(item, dict):
                continue
            candidate_id = str(item.get("candidate_id") or "").strip()
            if candidate_id not in known_ids:
                continue
            score = _clamp_score(item.get("score"))
            judgements[candidate_id] = SemanticJudgement(
                candidate_id=candidate_id,
                score=score,
                reason=str(item.get("reason") or "").strip(),
            )
        return judgements

    async def _final_rerank_plans(
        self,
        *,
        query: str,
        recommended_plans: list[dict[str, Any]],
        seed_skill_ids: tuple[str, ...],
    ) -> dict[str, Any]:
        base = {
            "applied": False,
            "error": "",
            "cross_plan_skill_merge": False,
            "recommended_plans": recommended_plans[: self.top_k],
        }
        if not recommended_plans:
            return base

        payload = self._final_rerank_payload(
            query=query,
            recommended_plans=recommended_plans,
            seed_skill_ids=seed_skill_ids,
        )
        self._final_rerank_call_count += 1
        try:
            with llm_usage_context("orchestration", "beam_final_rerank"):
                raw = await self._client().complete_json_async(
                    system_prompt=(
                        f"{BEAM_FINAL_RERANK_SYSTEM_PROMPT}\n"
                        f"{planner_language_instruction(self.language)}"
                    ),
                    user_content=json.dumps(payload, ensure_ascii=False),
                    error_context="Symphony beam final rerank",
                    request_overrides={
                        "extra_body": {"thinking": {"type": "disabled"}}
                    },
                )
            selection = json.loads(raw)
        except Exception as exc:
            return {**base, "error": f"{type(exc).__name__}: {exc}"}
        if not isinstance(selection, dict):
            return {**base, "error": "Final rerank returned non-object JSON."}

        materialized = self._materialize_final_rerank(
            selection,
            query=query,
            recommended_plans=recommended_plans,
        )
        if not materialized["valid"]:
            return {**base, "error": materialized["detail"]}

        primary_plan = materialized["plan"]
        output = [primary_plan]
        seen = {_dict_plan_signature(primary_plan)}
        for plan in recommended_plans:
            signature = _dict_plan_signature(plan)
            if signature in seen:
                continue
            output.append(plan)
            seen.add(signature)
            if len(output) >= self.top_k:
                break
        return {
            **base,
            "applied": True,
            "cross_plan_skill_merge": materialized["cross_plan_skill_merge"],
            "recommended_plans": output,
        }

    def _final_rerank_payload(
        self,
        *,
        query: str,
        recommended_plans: list[dict[str, Any]],
        seed_skill_ids: tuple[str, ...],
    ) -> dict[str, Any]:
        candidate_skill_ids = _candidate_plan_skill_ids(recommended_plans)
        return {
            "query": query,
            "required_output_types": sorted(self.required_output_types),
            "candidate_skill_pool": self._candidate_skill_pool_for_rerank(
                candidate_skill_ids,
                seed_skill_ids=seed_skill_ids,
            ),
            "candidate_can_feed_edges": self._candidate_edges_for_rerank(
                candidate_skill_ids
            ),
            "candidate_plans": [
                self._compact_plan_for_rerank(plan, plan_index=index)
                for index, plan in enumerate(recommended_plans, start=1)
            ],
        }

    def _candidate_skill_pool_for_rerank(
        self,
        candidate_skill_ids: set[str],
        *,
        seed_skill_ids: tuple[str, ...],
    ) -> list[dict[str, Any]]:
        seed_skill_id_set = set(seed_skill_ids)
        output = []
        for current_skill_id in sorted(candidate_skill_ids):
            skill = self.skill_by_id.get(current_skill_id)
            if not skill:
                continue
            output.append(
                {
                    **skill_payload(skill),
                    "is_seed": current_skill_id in seed_skill_id_set,
                    "outputs": [
                        {
                            "name": str(item.get("name") or ""),
                            "type": str(item.get("type") or "unknown"),
                        }
                        for item in skill.get("outputs") or []
                        if isinstance(item, dict) and item.get("name")
                    ],
                }
            )
        return output

    def _candidate_edges_for_rerank(
        self,
        candidate_skill_ids: set[str],
    ) -> list[dict[str, Any]]:
        output = []
        for edge in self.eligible_edges:
            source_id = skill_id(edge.get("source"))
            target_id = skill_id(edge.get("target"))
            if source_id not in candidate_skill_ids:
                continue
            if target_id not in candidate_skill_ids:
                continue
            output.append(
                {
                    "source_id": source_id,
                    "target_id": target_id,
                    "confidence": edge.get("confidence"),
                }
            )
        return output

    @staticmethod
    def _compact_plan_for_rerank(
        plan: dict[str, Any],
        *,
        plan_index: int,
    ) -> dict[str, Any]:
        return {
            "plan_index": plan_index,
            "skills": list(_dict_plan_signature(plan)),
            "missing_input_count": len(plan.get("missing_inputs") or []),
            "status": plan.get("status"),
        }

    def _materialize_final_rerank(
        self,
        selection: dict[str, Any],
        *,
        query: str,
        recommended_plans: list[dict[str, Any]],
    ) -> dict[str, Any]:
        selected_index = _positive_int(selection.get("selected_plan_index")) or 1
        selected_index = min(selected_index, len(recommended_plans))

        selected_plan = recommended_plans[selected_index - 1]
        raw_steps = selection.get("steps")
        if raw_steps is None:
            return {
                "valid": True,
                "detail": "",
                "cross_plan_skill_merge": False,
                "plan": {
                    **selected_plan,
                    "reason": _short_text(str(selection.get("reason") or ""))
                    or selected_plan.get("reason", ""),
                    "source": "bidirectional_beam_final_rerank",
                },
            }

        allowed_skill_ids = _candidate_plan_skill_ids(recommended_plans)
        step_ids = []
        for current_skill_id in _normalize_rerank_step_ids(raw_steps):
            if current_skill_id not in allowed_skill_ids:
                continue
            if current_skill_id not in self.skill_by_id:
                continue
            step_ids.append(current_skill_id)
        if not step_ids:
            return {
                "valid": True,
                "detail": "",
                "cross_plan_skill_merge": False,
                "plan": selected_plan,
            }

        edge_result = _normalize_rerank_edges(selection.get("can_feed_edges"))
        selected_edges = edge_result["edges"]
        if selection.get("can_feed_edges") is None:
            selected_edges = self._default_rerank_edges(step_ids)
        step_set = set(step_ids)
        eligible_edge_keys = {
            (skill_id(edge.get("source")), skill_id(edge.get("target")))
            for edge in self.eligible_edges
        }
        order = {
            current_skill_id: index for index, current_skill_id in enumerate(step_ids)
        }
        filtered_edges = []
        for edge in selected_edges:
            if edge[0] not in step_set or edge[1] not in step_set:
                continue
            if edge not in eligible_edge_keys:
                continue
            if order[edge[0]] >= order[edge[1]]:
                continue
            filtered_edges.append(edge)
        selected_edges = filtered_edges

        return {
            "valid": True,
            "detail": "",
            "cross_plan_skill_merge": not set(step_ids)
            <= set(_dict_plan_signature(selected_plan)),
            "plan": self._final_rerank_plan_payload(
                selection,
                query=query,
                step_ids=step_ids,
                selected_edges=selected_edges,
                recommended_plans=recommended_plans,
            ),
        }

    def _default_rerank_edges(
        self,
        step_ids: list[str],
    ) -> list[tuple[str, str]]:
        order = {
            current_skill_id: index for index, current_skill_id in enumerate(step_ids)
        }
        output = []
        for item in self.eligible_edges:
            edge = (skill_id(item.get("source")), skill_id(item.get("target")))
            if (
                edge[0] in order
                and edge[1] in order
                and order[edge[0]] < order[edge[1]]
            ):
                output.append(edge)
        return output

    def _final_rerank_plan_payload(
        self,
        selection: dict[str, Any],
        *,
        query: str,
        step_ids: list[str],
        selected_edges: list[tuple[str, str]],
        recommended_plans: list[dict[str, Any]],
    ) -> dict[str, Any]:
        source_steps = _plan_steps_by_skill(recommended_plans)
        step_reasons = _step_reasons_by_skill(selection.get("steps"))
        steps = []
        produced_artifacts = []
        for index, current_skill_id in enumerate(step_ids, start=1):
            skill = self.skill_by_id[current_skill_id]
            source_step = source_steps.get(current_skill_id, {})
            outputs = list(skill.get("outputs") or [])
            produced_artifacts.extend(
                {
                    "name": item.get("name"),
                    "type": item.get("type") or "unknown",
                    "source": "skill_output",
                }
                for item in outputs
                if isinstance(item, dict) and item.get("name")
            )
            steps.append(
                {
                    "step": index,
                    "skill_id": current_skill_id,
                    "name": str(skill.get("name") or current_skill_id),
                    "inputs": list(skill.get("inputs") or []),
                    "outputs": outputs,
                    "missing_inputs": list(source_step.get("missing_inputs") or []),
                    "filled_inputs": list(source_step.get("filled_inputs") or []),
                    "reason": step_reasons.get(current_skill_id, ""),
                }
            )
        raw_missing_inputs = []
        for step in steps:
            for item in step.get("missing_inputs") or []:
                if isinstance(item, dict):
                    raw_missing_inputs.append(item)
        missing_inputs = _dedupe_dict_items(raw_missing_inputs)
        edge_by_key = {
            (skill_id(edge.get("source")), skill_id(edge.get("target"))): edge
            for edge in self.eligible_edges
        }
        edge_items = [edge_plan_item(edge_by_key[edge]) for edge in selected_edges]
        status = "needs_input" if missing_inputs else "ready"
        reason = _short_text(str(selection.get("reason") or ""))
        return {
            "title": _query_plan_title(query, language=self.language),
            "status": status,
            "steps": steps,
            "stages": _rerank_plan_stages(steps, edge_items),
            "produced_artifacts": _dedupe_dict_items(produced_artifacts),
            "missing_inputs": missing_inputs,
            "can_feed_edges": edge_items,
            "reason": reason,
            "reasons": [reason] if reason else [],
            "plan_classification": (
                "executable"
                if status == "ready"
                else "structurally_valid_but_incomplete"
            ),
            "connectivity_trace": ["can_feed"] if edge_items else [],
            "source": "bidirectional_beam_final_rerank",
        }

    def _client(self) -> Any:
        if self.llm_client is not None:
            return self.llm_client
        if self.llm_config is None:
            raise ValueError(
                "beam Symphony planning requires llm_config or llm_client."
            )
        self.llm_client = create_llm_client(self.llm_config)
        return self.llm_client

    async def _judge_groups(
        self,
        query: str,
        groups: list[list[NeighborExpansion]],
    ) -> dict[str, SemanticJudgement]:
        tasks = [self._judge_group(query, group) for group in groups if group]
        if not tasks:
            return {}
        output: dict[str, SemanticJudgement] = {}
        for result in await asyncio.gather(*tasks):
            output.update(result)
        return output

    async def _judge_group(
        self,
        query: str,
        group: list[NeighborExpansion],
    ) -> dict[str, SemanticJudgement]:
        async with self._judge_semaphore:
            self._llm_call_count += 1
            try:
                return await self.judge_expansions(query, group)
            except Exception as exc:
                candidates = ", ".join(item.candidate_id for item in group[:5])
                self._judge_failures.append(
                    f"beam judge failed for candidates [{candidates}]: {exc}"
                )
                return {}

    @staticmethod
    def _cache_key(
        query_signature: str,
        expansion: NeighborExpansion,
    ) -> tuple[str, str, str, str]:
        return (
            query_signature,
            expansion.direction,
            expansion.current_skill_id,
            expansion.candidate_skill_id,
        )

    def _cache_get(
        self,
        query_signature: str,
        expansion: NeighborExpansion,
    ) -> SemanticJudgement | None:
        cached = self._judgement_cache.get(self._cache_key(query_signature, expansion))
        if cached is None:
            self._cache_misses += 1
        else:
            self._cache_hits += 1
        return cached

    def _cache_set(
        self,
        query_signature: str,
        expansion: NeighborExpansion,
        judgement: SemanticJudgement,
    ) -> None:
        self._judgement_cache[self._cache_key(query_signature, expansion)] = judgement

    def _initial_states(self) -> list[BeamState]:
        seeds = self.candidate_skill_ids or self._default_seed_skill_ids()
        states = [
            BeamState(
                skill_ids=(current_skill_id,),
                edge_indices=(),
                semantic_scores=(),
                score_reasons=(),
                seed_skill_ids=tuple(seeds),
                last_direction="seed",
            )
            for current_skill_id in seeds
            if current_skill_id in self.skill_by_id
        ]
        return self._rank_and_trim(states, limit=self.top_k)

    def _default_seed_skill_ids(self) -> tuple[str, ...]:
        output: list[str] = []
        seen: set[str] = set()
        for edge in self.eligible_edges:
            for key in ("source", "target"):
                current_skill_id = skill_id(edge.get(key))
                if (
                    current_skill_id
                    and current_skill_id in self.skill_by_id
                    and current_skill_id not in seen
                ):
                    seen.add(current_skill_id)
                    output.append(current_skill_id)
                if len(output) >= BEAM_DEFAULT_MAX_SKILLS:
                    return tuple(output)
        if output:
            return tuple(output)
        for skill in self.artifacts.skills[:BEAM_DEFAULT_MAX_SKILLS]:
            current_skill_id = str(skill.get("id") or "").strip()
            if current_skill_id:
                output.append(current_skill_id)
        return tuple(output)

    def _expansion_groups(
        self,
        query_signature: str,
        states: list[BeamState],
    ) -> tuple[list[list[NeighborExpansion]], dict[str, SemanticJudgement]]:
        groups: list[list[NeighborExpansion]] = []
        cached: dict[str, SemanticJudgement] = {}
        current_expansions: dict[str, NeighborExpansion] = {}
        pending_keys: dict[tuple[str, str, str], str] = {}
        aliases: dict[str, list[str]] = defaultdict(list)
        for state in states:
            for direction, current_skill_id in (
                ("forward", state.tail),
                ("backward", state.head),
            ):
                expansions = self._neighbor_expansions(
                    state=state,
                    direction=direction,
                    current_skill_id=current_skill_id,
                )
                misses = []
                for expansion in expansions:
                    current_expansions[expansion.candidate_id] = expansion
                    judgement = self._cache_get(query_signature, expansion)
                    if judgement is None:
                        misses.append(expansion)
                    else:
                        cached[expansion.candidate_id] = judgement
                if misses:
                    deduped_misses = []
                    for expansion in misses:
                        key = self._cache_key(query_signature, expansion)
                        existing_candidate_id = pending_keys.get(key)
                        if existing_candidate_id is not None:
                            aliases[existing_candidate_id].append(
                                expansion.candidate_id
                            )
                            continue
                        pending_keys[key] = expansion.candidate_id
                        deduped_misses.append(expansion)
                    if deduped_misses:
                        groups.append(deduped_misses)
        self._current_expansions = current_expansions
        self._current_expansion_aliases = aliases
        return groups, cached

    def _apply_judgement_aliases(
        self,
        judgements: dict[str, SemanticJudgement],
    ) -> dict[str, SemanticJudgement]:
        output = dict(judgements)
        for source_id, alias_ids in self._current_expansion_aliases.items():
            judgement = judgements.get(source_id)
            if judgement is None:
                continue
            for alias_id in alias_ids:
                output[alias_id] = SemanticJudgement(
                    candidate_id=alias_id,
                    score=judgement.score,
                    reason=judgement.reason,
                )
        return output

    def _neighbor_expansions(
        self,
        *,
        state: BeamState,
        direction: str,
        current_skill_id: str,
    ) -> list[NeighborExpansion]:
        if state.depth >= self.max_depth:
            return []
        edge_indices = (
            self.outgoing_edges.get(current_skill_id, [])
            if direction == "forward"
            else self.incoming_edges.get(current_skill_id, [])
        )
        expansions = []
        for edge_index in edge_indices:
            edge = self.eligible_edges[edge_index]
            candidate_skill_id = (
                skill_id(edge.get("target"))
                if direction == "forward"
                else skill_id(edge.get("source"))
            )
            if (
                not candidate_skill_id
                or candidate_skill_id not in self.skill_by_id
                or candidate_skill_id in state.skill_ids
            ):
                continue
            expansions.append(
                NeighborExpansion(
                    candidate_id=(
                        f"{direction}:{edge_index}:"
                        f"{_state_signature(state)}:{candidate_skill_id}"
                    ),
                    state=state,
                    direction=direction,
                    current_skill_id=current_skill_id,
                    candidate_skill_id=candidate_skill_id,
                    edge_index=edge_index,
                )
            )
        return expansions

    def _apply_judgements(
        self,
        judgements: dict[str, SemanticJudgement],
    ) -> list[BeamState]:
        output = []
        for candidate_id, judgement in judgements.items():
            if judgement.score < SEMANTIC_SCORE_THRESHOLD:
                continue
            expansion = self._expansion_by_id(candidate_id)
            if expansion is None:
                continue
            state = expansion.state
            if expansion.direction == "forward":
                skill_ids = (*state.skill_ids, expansion.candidate_skill_id)
                edge_indices = (*state.edge_indices, expansion.edge_index)
            else:
                skill_ids = (expansion.candidate_skill_id, *state.skill_ids)
                edge_indices = (expansion.edge_index, *state.edge_indices)
            output.append(
                BeamState(
                    skill_ids=skill_ids,
                    edge_indices=edge_indices,
                    semantic_scores=(*state.semantic_scores, judgement.score),
                    score_reasons=_append_unique(
                        state.score_reasons,
                        judgement.reason,
                    ),
                    seed_skill_ids=state.seed_skill_ids,
                    last_direction=expansion.direction,
                )
            )
        return output

    def _expansion_by_id(self, candidate_id: str) -> NeighborExpansion | None:
        return self._current_expansions.get(candidate_id)

    def _reset_beam_progress(self) -> None:
        self._beam_graph = BeamSearchGraph(self.skill_by_id)

    async def _emit_beam_event(
        self,
        event: str,
        *,
        round_index: int,
        graph: dict[str, Any],
    ) -> None:
        item = {
            "event": event,
            "language": self.language,
            "round_index": round_index,
            "graph": graph,
        }
        if self.progress_callback is None:
            return
        try:
            result = self.progress_callback(item)
            if asyncio.iscoroutine(result):
                await result
        except Exception as exc:
            logger.debug("beam progress callback failed: %s", exc)

    def _record_candidates_found(self) -> bool:
        expansions = _sorted_expansions(self._current_expansions.values())
        for expansion in expansions:
            self._beam_graph.add_candidate(expansion)
        return bool(expansions)

    def _record_candidates_judged(
        self,
        judgements: dict[str, SemanticJudgement],
    ) -> bool:
        found = False
        for candidate_id in sorted(judgements):
            expansion = self._expansion_by_id(candidate_id)
            if expansion is None:
                continue
            self._beam_graph.apply_judgement(
                expansion,
                judgements[candidate_id],
            )
            found = True
        return found

    def _plans_from_states(self, states: list[BeamState]) -> list[OrchestrationPlan]:
        state_plans = [
            (
                state,
                state_to_plan(
                    state=state.to_search_state(),
                    seed_skill_ids=state.seed_skill_ids,
                    skill_by_id=self.skill_by_id,
                    can_feed_edges=self.eligible_edges,
                ),
            )
            for state in states
            if state.skill_ids
        ]
        state_plans.sort(
            key=lambda item: (
                -self._state_score(item[0], plan=item[1]),
                len(item[1].missing_inputs),
                len(item[1].steps),
                tuple(step.skill_id for step in item[1].steps),
            )
        )
        return [plan for _state, plan in state_plans]

    def _merge_converged_states(self, states: list[BeamState]) -> list[BeamState]:
        deduped = self._dedupe_states(states)
        merged = list(deduped)
        directional_groups = (
            _group_states(
                [state for state in deduped if state.last_direction == "forward"],
                lambda state: state.tail,
            ),
            _group_states(
                [state for state in deduped if state.last_direction == "backward"],
                lambda state: state.head,
            ),
        )
        for groups in directional_groups:
            for group in groups:
                if len(group) < 2:
                    continue
                merged_state = self._merge_state_group(group)
                if merged_state is not None:
                    merged.append(merged_state)
        return self._dedupe_states(merged)

    def _merge_state_group(self, states: list[BeamState]) -> BeamState | None:
        skill_ids = {current for state in states for current in state.skill_ids}
        edge_indices = tuple(
            sorted(
                {edge_index for state in states for edge_index in state.edge_indices}
            )
        )
        ordered = self._topological_order(skill_ids, edge_indices)
        if ordered is None:
            return None
        seed_skill_ids = tuple(
            dict.fromkeys(
                current for state in states for current in state.seed_skill_ids
            )
        )
        semantic_scores = tuple(
            score for state in states for score in state.semantic_scores
        )
        unique_reasons = []
        for state in states:
            for reason in state.score_reasons:
                if reason and reason not in unique_reasons:
                    unique_reasons.append(reason)
        reasons = tuple(unique_reasons)
        return BeamState(
            skill_ids=tuple(ordered),
            edge_indices=edge_indices,
            semantic_scores=semantic_scores,
            score_reasons=reasons[:8],
            seed_skill_ids=seed_skill_ids,
            last_direction=states[0].last_direction,
        )

    def _topological_order(
        self,
        skill_ids: set[str],
        edge_indices: tuple[int, ...],
    ) -> list[str] | None:
        incoming = {current_skill_id: 0 for current_skill_id in skill_ids}
        outgoing: dict[str, list[str]] = defaultdict(list)
        for edge_index in edge_indices:
            edge = self.eligible_edges[edge_index]
            source_id = skill_id(edge.get("source"))
            target_id = skill_id(edge.get("target"))
            if source_id not in skill_ids or target_id not in skill_ids:
                continue
            outgoing[source_id].append(target_id)
            incoming[target_id] += 1

        ready = sorted(
            current_skill_id
            for current_skill_id, count in incoming.items()
            if count == 0
        )
        ordered = []
        while ready:
            current_skill_id = ready.pop(0)
            ordered.append(current_skill_id)
            for target_id in sorted(outgoing.get(current_skill_id, [])):
                incoming[target_id] -= 1
                if incoming[target_id] == 0:
                    ready.append(target_id)
                    ready.sort()
        if len(ordered) != len(skill_ids):
            return None
        return ordered

    def _dedupe_states(self, states: list[BeamState]) -> list[BeamState]:
        deduped: dict[tuple[tuple[str, ...], tuple[int, ...]], BeamState] = {}
        for state in states:
            key = (state.skill_ids, tuple(sorted(state.edge_indices)))
            existing = deduped.get(key)
            if existing is None or self._state_score(state) > self._state_score(
                existing
            ):
                deduped[key] = state
        return list(deduped.values())

    def _rank_and_trim(
        self,
        states: list[BeamState],
        *,
        limit: int,
    ) -> list[BeamState]:
        ranked = []
        for state in self._dedupe_states(states):
            plan = self._state_plan(state)
            ranked.append(
                (state, self._state_score(state, plan=plan), len(plan.missing_inputs))
            )
        ranked.sort(
            key=lambda item: (
                -item[1],
                item[2],
                len(item[0].skill_ids),
                item[0].skill_ids,
                item[0].edge_indices,
            )
        )
        return [state for state, _score, _missing in ranked[:limit]]

    def _state_score(
        self,
        state: BeamState,
        *,
        plan: OrchestrationPlan | None = None,
    ) -> float:
        plan = plan or self._state_plan(state)
        semantic_score = (
            sum(state.semantic_scores) / len(state.semantic_scores)
            if state.semantic_scores
            else SEED_BASELINE_SCORE
        )
        edge_confidence = self._state_edge_confidence(state)
        input_coverage = self._input_coverage(plan)
        depth_penalty = max(0, len(state.skill_ids) - 1)
        return (
            0.45 * semantic_score
            + 0.25 * edge_confidence
            + 0.20 * input_coverage
            - 0.03 * depth_penalty
        )

    def _state_plan(self, state: BeamState) -> OrchestrationPlan:
        return state_to_plan(
            state=state.to_search_state(),
            seed_skill_ids=state.seed_skill_ids,
            skill_by_id=self.skill_by_id,
            can_feed_edges=self.eligible_edges,
        )

    def _state_edge_confidence(self, state: BeamState) -> float:
        if not state.edge_indices:
            return 1.0
        values = [
            float(self.eligible_edges[index].get("confidence") or 0.0)
            for index in state.edge_indices
        ]
        return sum(values) / len(values)

    @staticmethod
    def _input_coverage(plan: OrchestrationPlan) -> float:
        required = 0
        for step in plan.steps:
            required += sum(1 for item in step.inputs if item.get("required", True))
        if required <= 0:
            return 1.0
        missing = len(plan.missing_inputs)
        return max(0.0, min(1.0, (required - missing) / required))

    def _candidate_payload(self, expansion: NeighborExpansion) -> dict[str, Any]:
        return {
            "candidate_id": expansion.candidate_id,
            "skill": skill_payload(self.skill_by_id[expansion.candidate_skill_id]),
        }

    def _sorted_eligible_edges(self) -> list[dict[str, Any]]:
        return eligible_can_feed_edges(
            self.artifacts.graph.get("edges", []),
            known_skill_ids=set(self.skill_by_id),
            min_confidence=self.min_edge_confidence,
        )

    def _plan_payload(self, plan: OrchestrationPlan, *, query: str) -> dict[str, Any]:
        payload = plan.to_dict()
        title = _query_plan_title(query, language=self.language)
        reason = "; ".join(plan.reasons[:3])
        payload.update(
            {
                "title": title,
                "reason": reason,
                "plan_classification": (
                    "executable"
                    if plan.status == "ready"
                    else "structurally_valid_but_incomplete"
                ),
                "connectivity_trace": (["can_feed"] if plan.can_feed_edges else []),
                "source": "bidirectional_beam",
            }
        )
        return payload


def _query_signature(query: str) -> str:
    normalized = " ".join(str(query or "").strip().lower().split())
    return hashlib.sha1(normalized.encode("utf-8")).hexdigest()


def _state_signature(state: BeamState) -> str:
    payload = {
        "skills": state.skill_ids,
        "edges": state.edge_indices,
        "last_direction": state.last_direction,
    }
    return hashlib.sha1(
        json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:10]


def _dict_plan_signature(plan: dict[str, Any]) -> tuple[str, ...]:
    return tuple(
        str(step.get("skill_id") or "").strip()
        for step in plan.get("steps") or []
        if isinstance(step, dict) and str(step.get("skill_id") or "").strip()
    )


def _candidate_plan_skill_ids(plans: list[dict[str, Any]]) -> set[str]:
    return {
        current_skill_id
        for plan in plans
        for current_skill_id in _dict_plan_signature(plan)
    }


def _normalize_rerank_step_ids(raw_steps: Any) -> list[str]:
    if not isinstance(raw_steps, list):
        return []
    output = []
    for item in raw_steps:
        current_skill_id = skill_id(
            item.get("skill_id") if isinstance(item, dict) else item
        ).strip()
        if current_skill_id and current_skill_id not in output:
            output.append(current_skill_id)
    return output


def _normalize_rerank_edges(raw_edges: Any) -> dict[str, Any]:
    edges: list[tuple[str, str]] = []
    if not isinstance(raw_edges, list):
        return {"edges": edges}
    seen = set()
    for item in raw_edges:
        if not isinstance(item, dict):
            continue
        source_id = skill_id(item.get("source_id") or item.get("source")).strip()
        target_id = skill_id(item.get("target_id") or item.get("target")).strip()
        edge = (source_id, target_id)
        if not source_id or not target_id:
            continue
        if source_id == target_id or edge in seen:
            continue
        seen.add(edge)
        edges.append(edge)
    return {"edges": edges}


def _positive_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _short_text(value: str, *, max_chars: int = 80) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip()


def _plan_steps_by_skill(
    plans: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    output = {}
    for plan in plans:
        for step in plan.get("steps") or []:
            if not isinstance(step, dict):
                continue
            current_skill_id = str(step.get("skill_id") or "").strip()
            if current_skill_id:
                output.setdefault(current_skill_id, step)
    return output


def _step_reasons_by_skill(raw_steps: Any) -> dict[str, str]:
    if not isinstance(raw_steps, list):
        return {}
    output = {}
    for item in raw_steps:
        if not isinstance(item, dict):
            continue
        current_skill_id = skill_id(item.get("skill_id")).strip()
        if current_skill_id:
            output[current_skill_id] = _short_text(str(item.get("reason") or ""))
    return output


def _dedupe_dict_items(items: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    output = []
    seen = set()
    for item in items:
        key = json.dumps(item, ensure_ascii=False, sort_keys=True, default=str)
        if key in seen:
            continue
        seen.add(key)
        output.append(item)
    return output


def _rerank_plan_stages(
    steps: list[dict[str, Any]],
    edges: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    step_by_id = {str(step.get("skill_id") or ""): step for step in steps}
    incoming = {current_skill_id: set() for current_skill_id in step_by_id}
    for edge in edges:
        source_id = str(edge.get("source_id") or "")
        target_id = str(edge.get("target_id") or "")
        if source_id in step_by_id and target_id in incoming:
            incoming[target_id].add(source_id)
    remaining = list(step_by_id)
    completed = set()
    stages = []
    while remaining:
        ready = [
            current_skill_id
            for current_skill_id in remaining
            if incoming[current_skill_id] <= completed
        ]
        if not ready:
            ready = [remaining[0]]
        stages.append(
            {
                "stage": len(stages) + 1,
                "skills": [step_by_id[current_skill_id] for current_skill_id in ready],
            }
        )
        completed.update(ready)
        remaining = [item for item in remaining if item not in completed]
    return stages


def _required_output_types_from_query(query: str) -> frozenset[str]:
    output_types = set()
    for match in _EXPLICIT_FILE_EXTENSION_RE.finditer(str(query or "")):
        normalized = str(match.group(1) or "").strip().lower().lstrip(".")
        mime_type, _encoding = mimetypes.guess_type(f"file.{normalized}")
        if mime_type in _MIME_TO_OUTPUT_TYPE:
            output_types.add(_MIME_TO_OUTPUT_TYPE[mime_type])
        elif mime_type and mime_type.startswith("image/"):
            output_types.add("image")
    return frozenset(output_types)


def _clamp_score(value: Any) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, parsed))


def _query_plan_title(query: str, *, language: str) -> str:
    title = " ".join(str(query or "").split())
    if not title:
        return default_beam_plan_title(language)
    if len(title) <= 80:
        return title
    return f"{title[:77].rstrip()}..."


def _append_unique(values: tuple[str, ...], value: str) -> tuple[str, ...]:
    text = str(value or "").strip()
    if not text or text in values:
        return values
    return (*values, text)


def _group_states(
    states: list[BeamState],
    key_fn: Callable[[BeamState], str],
) -> list[list[BeamState]]:
    grouped: dict[str, list[BeamState]] = defaultdict(list)
    for state in states:
        grouped[str(key_fn(state))].append(state)
    return list(grouped.values())


def _dominant_status(current: str, incoming: str) -> str:
    current_priority = _GRAPH_STATUS_PRIORITY.get(current, -1)
    incoming_priority = _GRAPH_STATUS_PRIORITY.get(incoming, -1)
    return incoming if incoming_priority >= current_priority else current


def _copy_public_graph_edge(edge: dict[str, Any]) -> dict[str, Any]:
    return {
        "source": edge.get("source"),
        "target": edge.get("target"),
        "status": edge.get("status"),
    }


def _skill_label(skill: dict[str, Any]) -> str:
    current_skill_id = str(skill.get("id") or "").strip()
    return str(skill.get("name") or current_skill_id).strip() or current_skill_id


def _sorted_expansions(
    expansions: Sequence[NeighborExpansion],
) -> list[NeighborExpansion]:
    return sorted(
        expansions,
        key=lambda item: (
            item.direction,
            item.current_skill_id,
            item.candidate_skill_id,
            item.edge_index,
            item.candidate_id,
        ),
    )

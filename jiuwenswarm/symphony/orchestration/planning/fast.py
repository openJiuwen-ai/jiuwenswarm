"""One-shot fast Symphony score planner."""

from __future__ import annotations

import heapq
import json
import re
from collections import defaultdict
from typing import Any, Sequence

from jiuwenswarm.symphony.evolution.models import (
    CAN_FEED,
    edge_key as evolution_edge_key,
)
from jiuwenswarm.symphony.llm import LLMConfig, create_llm_client, llm_usage_context
from jiuwenswarm.symphony.orchestration.artifacts import ScoreArtifacts
from jiuwenswarm.symphony.orchestration.language import (
    default_fast_no_plan_title,
    planner_language_instruction,
)
from jiuwenswarm.symphony.orchestration.planning.plan_builder import edge_plan_item
from jiuwenswarm.symphony.orchestration.planning.utils import (
    eligible_can_feed_edges,
    normalize_known_skill_ids,
    skill_id,
    skill_payload,
)

FAST_PLANNER_SYSTEM_PROMPT = """You are Symphony's fast Skill planner.
Return strict JSON only.

You receive:
- The user's query.
- Candidate Skills with id, name, and description only.
- Candidate can_feed relationships between those Skills.

Task:
- Select the best Skill execution plan for the query.
- Return the single best plan using the schema below.
- Use only provided skill IDs.
- Prefer provided can_feed edges. Infer a missing relationship only when needed to form
  the plan.
- Give a short reason when explicitly including an inferred edge in can_feed_edges.
- When can_feed_edges is omitted, Symphony infers adjacent step edges during validation.
- Do not judge compatibility from Skill I/O because it is not provided; Symphony handles
  Skill I/O after validating the plan.
- missing_inputs does not describe a Skill I/O schema. Use it only for information the
  user must provide when that need can be determined from the query and Skill
  descriptions.
- Do not invent Skills, inputs, or outputs.
- Prefer the shortest path that satisfies the user's intent.
- When runtime_weight and planner_score are present, use them only as tie-breakers
  between otherwise relevant relationships. planner_score already combines static and
  scoped runtime evidence.
- If required information is missing, set status to "needs_input" and list it.
- If no useful plan exists from the candidates, set status to "no_plan".

Schema:
{
  "title": "short plan title",
  "status": "ready | needs_input | no_plan",
  "reason": "why this plan is best",
  "steps": [
    {"skill_id": "skill-a", "reason": "why this step is used"}
  ],
  "can_feed_edges": [
    {"source_id": "skill-a", "target_id": "skill-b", "reason": "why the relationship is needed"}
  ],
  "missing_inputs": [
    {"skill_id": "skill-a", "name": "input name", "type": "unknown", "reason": "why it is needed"}
  ]
}
"""


class FastOneShotPlanner:
    """Ask the LLM for one validated plan from a compact score subgraph."""

    def __init__(
        self,
        artifacts: ScoreArtifacts,
        *,
        llm_config: LLMConfig | None,
        llm_client: Any | None,
        min_edge_confidence: float,
        max_depth: int = 4,
        candidate_skill_ids: Sequence[str] | None = None,
        language: str = "cn",
        dynamic_overlay: dict[str, Any] | None = None,
    ) -> None:
        self.artifacts = artifacts
        self.llm_config = llm_config
        self.llm_client = llm_client
        self.min_edge_confidence = min_edge_confidence
        self.max_depth = max(1, int(max_depth))
        self.language = language
        self.dynamic_overlay = (
            dynamic_overlay if isinstance(dynamic_overlay, dict) else {}
        )
        self.candidate_skill_ids = normalize_known_skill_ids(
            candidate_skill_ids,
            known_skill_ids=set(artifacts.skill_by_id),
        )

    async def plan(self, query: str) -> dict[str, Any]:
        client = self._client()
        subgraph = self._candidate_subgraph(query)
        prompt_payload = {
            "query": query,
            "skills": subgraph["skills"],
            "can_feed_edges": subgraph["edges"],
        }
        with llm_usage_context("orchestration", "one_shot_fast_planning"):
            raw = await client.complete_json_async(
                system_prompt=(
                    f"{FAST_PLANNER_SYSTEM_PROMPT}\n"
                    f"{planner_language_instruction(self.language)}"
                ),
                user_content=json.dumps(prompt_payload, ensure_ascii=False),
                error_context="Symphony one-shot fast planning",
                request_overrides={
                    "extra_body": {"thinking": {"type": "disabled"}},
                },
            )

        base = {
            "query": query,
            "language": self.language,
            "score_dir": str(self.artifacts.score_dir),
            "planning_mode": "one_shot_fast",
            "llm_call_count": 1,
            "candidate_skill_count": len(subgraph["skills"]),
            "candidate_edge_count": len(subgraph["edges"]),
            "dynamic_overlay_used": bool(subgraph["runtime_summary"]["applied_edges"]),
            "runtime_scoring_summary": subgraph["runtime_summary"],
        }
        try:
            selection = json.loads(raw)
        except json.JSONDecodeError as exc:
            return self._failure(base, f"Invalid fast planner JSON: {raw[:500]}", exc)
        if not isinstance(selection, dict):
            return self._failure(base, "Fast planner returned a non-object JSON payload.")

        materialized = self._materialize_selection(
            selection,
            candidate_skill_ids=set(subgraph["skill_ids"]),
            candidate_edges=subgraph["edge_by_key"],
        )
        if not materialized["valid"]:
            return self._failure(base, materialized["detail"], validation=materialized)

        plan = materialized["plan"]
        return {
            **base,
            "plans": [plan] if plan.get("steps") else [],
            "recommended_plans": [plan] if plan.get("steps") else [],
            "ranking_mode": "one_shot_fast",
            "decision": {
                "mode": "one_shot_fast",
                "strategy": "single_llm_selection",
                "validated_count": 1 if plan.get("steps") else 0,
                "candidate_count": len(subgraph["skills"]),
            },
            "validation": materialized,
            "status": plan.get("status", "no_plan") if plan else "no_plan",
            "reason": plan.get("reason", "") if plan else "",
        }

    def _client(self) -> Any:
        if self.llm_client is not None:
            return self.llm_client
        if self.llm_config is None:
            raise ValueError("fast Symphony planning requires llm_config or llm_client.")
        return create_llm_client(self.llm_config)

    def _candidate_subgraph(self, query: str) -> dict[str, Any]:
        if self.candidate_skill_ids:
            return self._retrieval_candidate_subgraph(query)
        return self._default_candidate_subgraph(query)

    def _default_candidate_subgraph(self, query: str) -> dict[str, Any]:
        candidate_edges = self._known_edges(self._sorted_eligible_edges(query))
        selected = []
        selected_set = set()
        for edge in candidate_edges:
            self._append_skill(selected, selected_set, skill_id(edge.get("source")))
            self._append_skill(selected, selected_set, skill_id(edge.get("target")))
        if not selected:
            for skill in self.artifacts.skills:
                current_skill_id = str(skill.get("id") or "")
                self._append_skill(selected, selected_set, current_skill_id)
        return self._subgraph_payload(selected, candidate_edges)

    def _retrieval_candidate_subgraph(self, query: str) -> dict[str, Any]:
        sorted_edges = self._known_edges(self._sorted_eligible_edges(query))
        edge_by_key = {
            (skill_id(edge.get("source")), skill_id(edge.get("target"))): edge
            for edge in sorted_edges
        }
        seeds = list(self.candidate_skill_ids)
        adjacency: dict[str, list[str]] = defaultdict(list)
        for source_id, target_id in edge_by_key:
            adjacency[source_id].append(target_id)
        for source_id in adjacency:
            adjacency[source_id].sort()

        selected = list(seeds)
        selected_set = set(seeds)
        candidate_edges = []
        candidate_edge_keys = set()
        parent = {seed_id: seed_id for seed_id in seeds}

        def find(current_skill_id: str) -> str:
            while parent[current_skill_id] != current_skill_id:
                parent[current_skill_id] = parent[parent[current_skill_id]]
                current_skill_id = parent[current_skill_id]
            return current_skill_id

        def union(left: str, right: str) -> None:
            left_root = find(left)
            right_root = find(right)
            if left_root != right_root:
                parent[right_root] = left_root

        connections = []
        for left_index, left_seed in enumerate(seeds):
            for right_index in range(left_index + 1, len(seeds)):
                right_seed = seeds[right_index]
                paths = [
                    self._best_directed_path(
                        left_seed,
                        right_seed,
                        adjacency=adjacency,
                        edge_by_key=edge_by_key,
                    ),
                    self._best_directed_path(
                        right_seed,
                        left_seed,
                        adjacency=adjacency,
                        edge_by_key=edge_by_key,
                    ),
                ]
                ranked_paths = [
                    (self._path_rank(path), path) for path in paths if path
                ]
                if ranked_paths:
                    path_rank, path = min(ranked_paths, key=lambda item: item[0])
                    connections.append((path_rank, left_index, right_index, path))

        seed_set = set(seeds)
        for _, left_index, right_index, path in sorted(connections):
            left_seed = seeds[left_index]
            right_seed = seeds[right_index]
            if find(left_seed) == find(right_seed):
                continue
            for edge in path:
                self._append_candidate_edge(
                    candidate_edges,
                    candidate_edge_keys,
                    edge,
                )
                self._append_skill(
                    selected,
                    selected_set,
                    skill_id(edge.get("source")),
                )
                self._append_skill(
                    selected,
                    selected_set,
                    skill_id(edge.get("target")),
                )
            path_seeds = [skill for skill in self._path_skill_ids(path) if skill in seed_set]
            for path_seed in path_seeds[1:]:
                union(path_seeds[0], path_seed)

        components: dict[str, list[str]] = defaultdict(list)
        for seed_id in seeds:
            components[find(seed_id)].append(seed_id)
        neighbor_owners: dict[str, str] = {}
        for component_seeds in components.values():
            if len(component_seeds) != 1:
                continue
            seed_id = component_seeds[0]
            incoming = next(
                (edge for edge in sorted_edges if skill_id(edge.get("target")) == seed_id),
                None,
            )
            outgoing = next(
                (edge for edge in sorted_edges if skill_id(edge.get("source")) == seed_id),
                None,
            )
            for edge in (incoming, outgoing):
                if edge is None:
                    continue
                source_id = skill_id(edge.get("source"))
                target_id = skill_id(edge.get("target"))
                neighbor_id = source_id if target_id == seed_id else target_id
                owner = neighbor_owners.get(neighbor_id)
                if neighbor_id in seed_set or (owner is not None and owner != seed_id):
                    continue
                neighbor_owners[neighbor_id] = seed_id
                self._append_candidate_edge(candidate_edges, candidate_edge_keys, edge)
                self._append_skill(selected, selected_set, source_id)
                self._append_skill(selected, selected_set, target_id)
        return self._subgraph_payload(selected, candidate_edges)

    def _best_directed_path(
        self,
        source_id: str,
        target_id: str,
        *,
        adjacency: dict[str, list[str]],
        edge_by_key: dict[tuple[str, str], dict[str, Any]],
    ) -> list[dict[str, Any]]:
        queue = [(0, -1.0, 0.0, (source_id,))]
        while queue:
            hops, negative_minimum, negative_total, nodes = heapq.heappop(queue)
            current_id = nodes[-1]
            if current_id == target_id and hops:
                return [
                    edge_by_key[(nodes[index], nodes[index + 1])]
                    for index in range(len(nodes) - 1)
                ]
            if hops >= self.max_depth:
                continue
            minimum_confidence = -negative_minimum
            total_confidence = -negative_total
            for next_id in adjacency.get(current_id, []):
                if next_id in nodes:
                    continue
                edge = edge_by_key[(current_id, next_id)]
                confidence = self._edge_rank_score(edge)
                heapq.heappush(
                    queue,
                    (
                        hops + 1,
                        -min(minimum_confidence, confidence),
                        -(total_confidence + confidence),
                        (*nodes, next_id),
                    ),
                )
        return []

    def _sorted_eligible_edges(self, query: str) -> list[dict[str, Any]]:
        graph_edges = self.artifacts.graph.get("edges", [])
        static_keys = {
            evolution_edge_key(
                skill_id(edge.get("source")),
                skill_id(edge.get("target")),
                CAN_FEED,
            )
            for edge in graph_edges
            if edge.get("type") == CAN_FEED
        }
        eligible_edges = eligible_can_feed_edges(
            graph_edges,
            known_skill_ids=set(self.artifacts.skill_by_id),
            min_confidence=self.min_edge_confidence,
        )
        filtered_edges = [
            self._with_runtime_score(
                edge,
                self._overlay_edges().get(
                    evolution_edge_key(
                        skill_id(edge.get("source")),
                        skill_id(edge.get("target")),
                        CAN_FEED,
                    )
                ),
                query=query,
            )
            for edge in eligible_edges
        ]
        filtered_edges.extend(self._runtime_only_edges(query, static_keys=static_keys))
        return sorted(
            filtered_edges,
            key=lambda item: (
                -self._edge_rank_score(item),
                str(item.get("source") or ""),
                str(item.get("target") or ""),
            ),
        )

    def _runtime_only_edges(
        self,
        query: str,
        *,
        static_keys: set[str],
    ) -> list[dict[str, Any]]:
        known_skill_ids = set(self.artifacts.skill_by_id)
        output = []
        for current_key, stats in self._overlay_edges().items():
            if current_key in static_keys or not isinstance(stats, dict):
                continue
            source_id = str(stats.get("source_id") or "").strip()
            target_id = str(stats.get("target_id") or "").strip()
            relation_type = str(stats.get("relation_type") or CAN_FEED)
            success_count = int(stats.get("success_count") or 0)
            failure_count = int(stats.get("failure_count") or 0)
            attempt_count = max(int(stats.get("attempt_count") or 0), success_count)
            if relation_type != CAN_FEED:
                continue
            if source_id not in known_skill_ids or target_id not in known_skill_ids:
                continue
            if success_count < 2 or failure_count > 0:
                continue
            if not self._runtime_is_query_relevant(stats, query, require_seed=True):
                continue
            runtime_confidence = (success_count + 1) / (attempt_count + 2)
            runtime_weight = self._runtime_weight(stats)
            output.append(
                {
                    "type": CAN_FEED,
                    "source": source_id,
                    "target": target_id,
                    "confidence": runtime_confidence,
                    "method": "runtime_observed",
                    "evidence": {
                        "reasons": ["Repeated successful Session executions"],
                    },
                    "runtime_only": True,
                    "runtime_weight": runtime_weight,
                    "effective_score": runtime_confidence * runtime_weight,
                    "runtime_evidence": self._runtime_evidence(stats),
                }
            )
        return output

    def _with_runtime_score(
        self,
        edge: dict[str, Any],
        stats: dict[str, Any] | None,
        *,
        query: str,
    ) -> dict[str, Any]:
        if not isinstance(stats, dict) or not self._runtime_is_query_relevant(stats, query):
            return edge
        runtime_weight = self._runtime_weight(stats)
        confidence = float(edge.get("confidence") or 0.0)
        return {
            **edge,
            "runtime_weight": runtime_weight,
            "effective_score": confidence * runtime_weight,
            "runtime_evidence": self._runtime_evidence(stats),
        }

    def _runtime_is_query_relevant(
        self,
        stats: dict[str, Any],
        query: str,
        *,
        require_seed: bool = False,
    ) -> bool:
        endpoints = {
            str(stats.get("source_id") or "").strip(),
            str(stats.get("target_id") or "").strip(),
        }
        seeds = set(self.candidate_skill_ids or ())
        touches_seed = bool(seeds & endpoints)
        representative_queries = [
            str(value).strip()
            for value in stats.get("representative_queries") or []
            if str(value).strip()
        ]
        query_matches = any(
            self._text_overlap(query, example) >= 0.2
            for example in representative_queries
        )
        if require_seed:
            return (
                touches_seed and (query_matches or not representative_queries)
            ) or (not seeds and query_matches)
        return touches_seed or query_matches

    def _overlay_edges(self) -> dict[str, dict[str, Any]]:
        edges = self.dynamic_overlay.get("edges")
        return edges if isinstance(edges, dict) else {}

    @staticmethod
    def _runtime_weight(stats: dict[str, Any]) -> float:
        try:
            value = float(stats.get("runtime_weight") or 1.0)
        except (TypeError, ValueError):
            value = 1.0
        return max(0.2, min(2.0, value))

    @staticmethod
    def _runtime_evidence(stats: dict[str, Any]) -> dict[str, Any]:
        return {
            "success_count": int(stats.get("success_count") or 0),
            "failure_count": int(stats.get("failure_count") or 0),
            "needs_input_count": int(stats.get("needs_input_count") or 0),
            "attempt_count": int(stats.get("attempt_count") or 0),
        }

    @staticmethod
    def _edge_rank_score(edge: dict[str, Any]) -> float:
        return float(edge.get("effective_score") or edge.get("confidence") or 0.0)

    @classmethod
    def _text_overlap(cls, left: str, right: str) -> float:
        left_tokens = cls._text_tokens(left)
        right_tokens = cls._text_tokens(right)
        if not left_tokens or not right_tokens:
            return 0.0
        return len(left_tokens & right_tokens) / min(len(left_tokens), len(right_tokens))

    @staticmethod
    def _text_tokens(value: str) -> set[str]:
        normalized = str(value or "").lower()
        tokens = set(re.findall(r"[a-z0-9][a-z0-9_-]+", normalized))
        cjk_runs = re.findall(r"[\u3400-\u9fff]+", normalized)
        for run in cjk_runs:
            if len(run) == 1:
                tokens.add(run)
            else:
                tokens.update(run[index:index + 2] for index in range(len(run) - 1))
        return tokens

    def _subgraph_payload(
        self,
        selected: Sequence[str],
        candidate_edges: list[dict[str, Any]],
    ) -> dict[str, Any]:
        skill_by_id = self.artifacts.skill_by_id
        skill_payloads = [
            skill_payload(skill_by_id[current_skill_id])
            for current_skill_id in selected
            if current_skill_id in skill_by_id
        ]
        edge_payloads = [self._edge_payload(edge) for edge in candidate_edges]
        runtime_edges = [edge for edge in candidate_edges if edge.get("runtime_evidence")]
        return {
            "skills": skill_payloads,
            "edges": edge_payloads,
            "skill_ids": [item["id"] for item in skill_payloads],
            "edge_by_key": {
                (skill_id(edge.get("source")), skill_id(edge.get("target"))): edge
                for edge in candidate_edges
            },
            "runtime_summary": {
                "available": bool(self._overlay_edges()),
                "applied_edges": len(runtime_edges),
                "runtime_only_edges": sum(
                    1 for edge in runtime_edges if edge.get("runtime_only")
                ),
            },
        }

    def _known_edges(self, edges: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
        known_skill_ids = set(self.artifacts.skill_by_id)
        output = []
        seen = set()
        for edge in edges:
            key = (skill_id(edge.get("source")), skill_id(edge.get("target")))
            if key in seen or key[0] not in known_skill_ids or key[1] not in known_skill_ids:
                continue
            seen.add(key)
            output.append(edge)
        return output

    @staticmethod
    def _append_skill(output: list[str], seen: set[str], current_skill_id: str) -> None:
        current_skill_id = str(current_skill_id or "").strip()
        if current_skill_id and current_skill_id not in seen:
            seen.add(current_skill_id)
            output.append(current_skill_id)

    @staticmethod
    def _append_candidate_edge(
        output: list[dict[str, Any]],
        seen: set[tuple[str, str]],
        edge: dict[str, Any],
    ) -> None:
        key = (skill_id(edge.get("source")), skill_id(edge.get("target")))
        if key not in seen:
            seen.add(key)
            output.append(edge)

    @staticmethod
    def _path_skill_ids(path: Sequence[dict[str, Any]]) -> list[str]:
        if not path:
            return []
        return [
            skill_id(path[0].get("source")),
            *(skill_id(edge.get("target")) for edge in path),
        ]

    @classmethod
    def _path_rank(cls, path: Sequence[dict[str, Any]]) -> tuple[Any, ...]:
        confidences = [cls._edge_rank_score(edge) for edge in path]
        edge_ids = tuple(
            (skill_id(edge.get("source")), skill_id(edge.get("target")))
            for edge in path
        )
        return (
            len(path),
            -min(confidences),
            -sum(confidences),
            edge_ids,
        )

    def _materialize_selection(
        self,
        selection: dict[str, Any],
        *,
        candidate_skill_ids: set[str],
        candidate_edges: dict[tuple[str, str], dict[str, Any]],
    ) -> dict[str, Any]:
        status = str(selection.get("status") or "ready").strip().lower()
        if status not in {"ready", "needs_input", "no_plan"}:
            status = "ready"
        raw_steps = selection.get("steps") or selection.get("skill_order") or []
        step_ids = self._normalize_step_ids(raw_steps)
        if status == "no_plan" or not step_ids:
            return {
                "valid": True,
                "detail": "",
                "plan": {
                    "title": str(
                        selection.get("title")
                        or default_fast_no_plan_title(self.language)
                    ).strip(),
                    "status": "no_plan",
                    "steps": [],
                    "stages": [],
                    "produced_artifacts": [],
                    "missing_inputs": [],
                    "can_feed_edges": [],
                    "reason": str(selection.get("reason") or "").strip(),
                    "plan_classification": "invalid",
                    "connectivity_trace": [],
                },
            }

        unknown_ids = [
            current_skill_id
            for current_skill_id in step_ids
            if current_skill_id not in candidate_skill_ids
        ]
        if unknown_ids:
            return {
                "valid": False,
                "detail": f"Fast planner selected unknown skill IDs: {unknown_ids}",
            }
        skill_by_id = self.artifacts.skill_by_id

        selected_edges_result = self._normalize_selected_edges(
            selection.get("can_feed_edges") or []
        )
        selected_edges = selected_edges_result["edges"]
        if selected_edges_result["invalid"]:
            return {
                "valid": False,
                "detail": (
                    "Fast planner returned malformed can_feed edges: "
                    f"{selected_edges_result['invalid']}"
                ),
            }
        if selected_edges_result["duplicates"]:
            return {
                "valid": False,
                "detail": (
                    "Fast planner returned duplicate can_feed edges: "
                    f"{selected_edges_result['duplicates']}"
                ),
            }
        if len(step_ids) > 1 and not selected_edges:
            selected_edges = [
                (step_ids[index], step_ids[index + 1])
                for index in range(len(step_ids) - 1)
            ]

        step_id_set = set(step_ids)
        unknown_edge_endpoints = [
            edge
            for edge in selected_edges
            if edge[0] not in step_id_set or edge[1] not in step_id_set
        ]
        if unknown_edge_endpoints:
            return {
                "valid": False,
                "detail": (
                    "Fast planner selected edges outside plan steps: "
                    f"{unknown_edge_endpoints}"
                ),
            }
        order = {current_skill_id: index for index, current_skill_id in enumerate(step_ids)}
        backward_edges = [
            (source, target)
            for source, target in selected_edges
            if order.get(source, -1) >= order.get(target, -1)
        ]
        if backward_edges:
            return {
                "valid": False,
                "detail": (
                    "Fast planner selected edges that violate step order: "
                    f"{backward_edges}"
                ),
            }

        missing_inputs = self._normalize_missing_inputs(
            selection.get("missing_inputs") or [],
            set(step_ids),
        )
        missing_by_skill: dict[str, list[dict[str, Any]]] = {}
        for item in missing_inputs:
            missing_by_skill.setdefault(str(item.get("skill_id") or ""), []).append(item)

        steps = []
        produced_artifacts = []
        for index, current_skill_id in enumerate(step_ids, start=1):
            skill = skill_by_id[current_skill_id]
            outputs = list(skill.get("outputs") or [])
            produced_artifacts.extend(
                {
                    "name": item.get("name"),
                    "type": item.get("type") or "unknown",
                    "source": "skill_output",
                }
                for item in outputs
                if item.get("name")
            )
            steps.append(
                {
                    "step": index,
                    "skill_id": current_skill_id,
                    "name": str(skill.get("name") or current_skill_id),
                    "inputs": list(skill.get("inputs") or []),
                    "outputs": outputs,
                    "missing_inputs": missing_by_skill.get(current_skill_id, []),
                    "filled_inputs": [],
                    "reason": self._step_reason(
                        selection.get("steps") or [],
                        current_skill_id,
                    ),
                }
            )

        edge_items = []
        has_inferred_edges = False
        for edge in selected_edges:
            if edge in candidate_edges:
                edge_items.append(edge_plan_item(candidate_edges[edge]))
                continue
            has_inferred_edges = True
            edge_items.append(
                self._inferred_edge_plan_item(
                    edge,
                    selected_edges_result["reasons"].get(edge, ""),
                )
            )
        if missing_inputs:
            status = "needs_input"
        return {
            "valid": True,
            "detail": "",
            "plan": {
                "title": str(selection.get("title") or "Symphony fast plan").strip(),
                "status": status,
                "steps": steps,
                "stages": [
                    {"stage": index, "skills": [step]}
                    for index, step in enumerate(steps, start=1)
                ],
                "produced_artifacts": produced_artifacts,
                "missing_inputs": missing_inputs,
                "can_feed_edges": edge_items,
                "reason": str(selection.get("reason") or "").strip(),
                "plan_classification": (
                    "executable"
                    if status == "ready"
                    else "structurally_valid_but_incomplete"
                ),
                "connectivity_trace": (
                    ["can_feed", "fast_llm_inferred"]
                    if has_inferred_edges
                    else (["can_feed"] if edge_items else [])
                ),
                "source": "one_shot_fast",
            },
        }

    @staticmethod
    def _failure(
        base: dict[str, Any],
        detail: str,
        exc: Exception | None = None,
        *,
        validation: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload = {
            **base,
            "success": False,
            "detail": detail,
            "plans": [],
            "recommended_plans": [],
            "ranking_mode": "one_shot_fast_failed",
            "validation": validation or {"valid": False, "detail": detail},
        }
        if exc is not None:
            payload["error"] = str(exc)
        return payload

    @staticmethod
    def _edge_payload(edge: dict[str, Any]) -> dict[str, Any]:
        payload = {
            "source_id": skill_id(edge.get("source")),
            "target_id": skill_id(edge.get("target")),
        }
        if isinstance(edge.get("runtime_evidence"), dict):
            payload.update(
                {
                    "runtime_weight": edge.get("runtime_weight"),
                    "planner_score": edge.get("effective_score"),
                    "runtime_only": bool(edge.get("runtime_only")),
                }
            )
        return payload

    @staticmethod
    def _inferred_edge_plan_item(
        edge: tuple[str, str],
        reason: str,
    ) -> dict[str, Any]:
        return {
            "source_id": edge[0],
            "target_id": edge[1],
            "confidence": None,
            "method": "fast_llm_inferred",
            "port_mappings": [],
            "source_outputs": [],
            "target_inputs": [],
            "reasons": [reason] if reason else [],
            "reason": reason,
        }

    @staticmethod
    def _normalize_step_ids(raw_steps: Any) -> list[str]:
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

    @staticmethod
    def _normalize_selected_edges(raw_edges: Any) -> dict[str, Any]:
        if not isinstance(raw_edges, list):
            return {
                "edges": [],
                "invalid": [raw_edges],
                "duplicates": [],
                "reasons": {},
            }
        edges = []
        invalid = []
        duplicates = []
        reasons = {}
        for item in raw_edges:
            if not isinstance(item, dict):
                invalid.append(item)
                continue
            source = skill_id(item.get("source_id") or item.get("source")).strip()
            target = skill_id(item.get("target_id") or item.get("target")).strip()
            if not source or not target:
                invalid.append(item)
                continue
            if source == target:
                continue
            edge = (source, target)
            if edge in edges:
                duplicates.append(edge)
                continue
            edges.append(edge)
            reasons[edge] = str(item.get("reason") or "").strip()
        return {
            "edges": edges,
            "invalid": invalid,
            "duplicates": duplicates,
            "reasons": reasons,
        }

    @staticmethod
    def _normalize_missing_inputs(
        raw_items: Any,
        step_ids: set[str],
    ) -> list[dict[str, Any]]:
        if not isinstance(raw_items, list):
            return []
        output = []
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            current_skill_id = skill_id(item.get("skill_id")).strip()
            if current_skill_id and current_skill_id not in step_ids:
                continue
            normalized = {
                "skill_id": current_skill_id,
                "name": str(item.get("name") or "unknown"),
                "type": str(item.get("type") or "unknown"),
            }
            reason = str(item.get("reason") or "").strip()
            if reason:
                normalized["reason"] = reason
            output.append(normalized)
        return output

    @staticmethod
    def _step_reason(raw_steps: Any, target_skill_id: str) -> str:
        if not isinstance(raw_steps, list):
            return ""
        for item in raw_steps:
            if (
                isinstance(item, dict)
                and skill_id(item.get("skill_id")) == target_skill_id
            ):
                return str(item.get("reason") or "").strip()
        return ""

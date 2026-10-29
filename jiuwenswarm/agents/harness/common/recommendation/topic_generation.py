# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Replaceable ranking, generation, and selection contracts for topic recommendation."""

from __future__ import annotations

import json
import math
from collections import Counter
from dataclasses import asdict, dataclass
from typing import Any, Awaitable, Callable

from jiuwenswarm.agents.harness.common.recommendation import topic_history as history

I2_PROMPT_VERSION = "topic-starter-v5-i2-rank-v1"
I4_PROMPT_VERSION = "topic-starter-v5-i4-generate-v1"
I5_PROMPT_VERSION = "topic-starter-v5-i5-select-v1"

OVERSAMPLE_SIZE = 12
RETRIEVED_INTENT_SLOTS = 4
GENERATION_CONFIG_VERSION = "topic-starter-v5-generation-v1"

_I2_SYSTEM_PROMPT = f"""\
You are the independent Topic Starter I2 interest ranker ({I2_PROMPT_VERSION}).
Rank every supplied snapshot interest exactly once. Use only
interest_summary_id values from snapshot and assign contiguous 1-based ranks.
api_score_nullable must be a finite number or null and is observational only.
Return exactly
{{"ranked_interests":[{{"interest_summary_id":"...","rank":1,
"api_score_nullable":null}}]}}. Do not add, omit, merge, rewrite, or explain
interests, and do not return extra fields.
"""

_I4_SYSTEM_PROMPT = f"""\
You are the independent Topic Starter I4 candidate generator
({I4_PROMPT_VERSION}). Return exactly twelve candidates using the supplied
generation contract. Warm requests must contain 8 personalized-intent,
2 perspective-expansion, and 2 fresh-trend candidates. Cold requests must
contain 8 generic-intent, 2 generic-perspective, and 2 fresh-trend candidates.
The supplied retrieved_fixed_hits are an immutable leading prefix: copy each
hit into one retrieved-fixed candidate in the same order. Its source_starter_id
and title must be copied verbatim, without rewriting even one character; copy
its interest and provenance bindings too. Never invent another retrieved-fixed
candidate. Generated candidates must use origin generated and a null
source_starter_id. Every candidate must require web retrieval. Return only JSON
{{"candidates":[...]}} with the frozen candidate fields supplied by the
contract, with no explanations or extra fields.
"""

_I5_SYSTEM_PROMPT = f"""\
You are the independent Topic Starter I5 grounded-slate selector
({I5_PROMPT_VERSION}). Select at most target_count items from the supplied
grounded candidates under selection_constraints. Use candidate IDs only;
never invent an ID. If any qualified retrieved-fixed candidate is supplied,
select at least one. Return contiguous 1-based selector ranks and a finite
observational score or null. Return exactly
{{"selections":[{{"candidate_id":"...","selector_rank":1,
"api_score_nullable":null}}]}}. Return candidate IDs only; Do not rewrite or
return titles, intents, evidence, queries, candidate content, explanations, or
extra fields.
"""

_WARM_LANE_COUNTS = {
    "personalized-intent": 8,
    "perspective-expansion": 2,
    "fresh-trend": 2,
}
_COLD_LANE_COUNTS = {
    "generic-intent": 8,
    "generic-perspective": 2,
    "fresh-trend": 2,
}
_CANDIDATE_FIELDS = frozenset(
    {
        "candidate_id",
        "lane",
        "intent",
        "persona",
        "provenance_relation",
        "source_memory_unit_ids",
        "source_locators",
        "identity_anchor",
        "context_anchors",
        "bridge",
        "primary_query",
        "fallback_query",
        "freshness_class",
        "source_requirement",
        "title",
        "origin",
        "source_starter_id",
        "interest_summary_id",
    }
)
_GENERIC_LANES = frozenset(
    {"generic-intent", "generic-perspective", "fresh-trend"}
)
_FRESHNESS_CLASSES = frozenset(
    {"current_event", "recent_development", "evergreen_corpus"}
)

ApiAdapter = Callable[[dict[str, Any]], Awaitable[Any]]
StageInvoker = Callable[[str, str, str], Awaitable[Any]]


def _plain_int(value: Any) -> bool:
    # bool is an int subclass; a JSON boolean is never a version or a count.
    return isinstance(value, int) and not isinstance(value, bool)


class ArkInterestRankingAdapter:
    """I2-specific prompt adapter over the shared Ark stage invocation seam."""

    def __init__(self, invoke_stage: StageInvoker) -> None:
        self._invoke_stage = invoke_stage

    async def __call__(self, payload: dict[str, Any]) -> Any:
        return await self._invoke_stage(
            "interest_ranker",
            _I2_SYSTEM_PROMPT,
            json.dumps(payload, ensure_ascii=False),
        )


class ArkCandidateGenerationAdapter:
    """I4-specific prompt adapter over the shared Ark stage invocation seam."""

    def __init__(self, invoke_stage: StageInvoker) -> None:
        self._invoke_stage = invoke_stage

    async def __call__(self, payload: dict[str, Any]) -> Any:
        return await self._invoke_stage(
            "starter_candidate_generator",
            _I4_SYSTEM_PROMPT,
            json.dumps(payload, ensure_ascii=False),
        )


class ArkSlateSelectionAdapter:
    """I5-specific prompt adapter over the shared Ark stage invocation seam."""

    def __init__(self, invoke_stage: StageInvoker) -> None:
        self._invoke_stage = invoke_stage

    async def __call__(self, payload: dict[str, Any]) -> Any:
        return await self._invoke_stage(
            "starter_slate_selector",
            _I5_SYSTEM_PROMPT,
            json.dumps(payload, ensure_ascii=False),
        )


@dataclass(frozen=True)
class RankedInterest:
    interest_summary_id: str
    rank: int
    api_score_nullable: float | None


@dataclass(frozen=True)
class OrderedSelection:
    candidate_id: str
    selector_rank: int
    api_score_nullable: float | None


def _finite_nullable_score(value: Any) -> tuple[bool, float | None]:
    if value is None:
        return True, None
    if type(value) not in (int, float) or not math.isfinite(float(value)):
        return False, None
    return True, float(value)


async def rank_interest_summaries(
    snapshot: list[dict[str, Any]],
    delta: list[dict[str, Any]],
    feedback_context: dict[str, Any],
    *,
    invoke_ranker: ApiAdapter,
) -> tuple[list[RankedInterest], str]:
    """I2: rank every frozen interest through one replaceable API adapter."""

    interest_ids = [
        item.get("interest_summary_id")
        for item in snapshot
        if isinstance(item, dict)
    ]
    if len(interest_ids) != len(snapshot):
        return [], "interest_ranking_contract_failed"
    if (
        any(
            not isinstance(interest_id, str) or not interest_id
            for interest_id in interest_ids
        )
    ):
        return [], "interest_ranking_contract_failed"
    if len(set(interest_ids)) != len(interest_ids):
        return [], "interest_ranking_contract_failed"

    try:
        response = await invoke_ranker(
            {
                "prompt_version": I2_PROMPT_VERSION,
                "snapshot": snapshot,
                "delta": delta,
                "feedback_context": feedback_context,
            }
        )
    except Exception:  # noqa: BLE001 - adapter failure is a closed contract result
        return [], "interest_ranking_failed"

    if not isinstance(response, dict):
        return [], "interest_ranking_contract_failed"
    if set(response) != {"ranked_interests"}:
        return [], "interest_ranking_contract_failed"
    if not isinstance(response.get("ranked_interests"), list):
        return [], "interest_ranking_contract_failed"

    ranked: list[RankedInterest] = []
    for expected_rank, raw in enumerate(response["ranked_interests"], start=1):
        if not isinstance(raw, dict) or set(raw) != {
            "interest_summary_id",
            "rank",
            "api_score_nullable",
        }:
            return [], "interest_ranking_contract_failed"
        interest_id = raw.get("interest_summary_id")
        rank = raw.get("rank")
        score_valid, score = _finite_nullable_score(raw.get("api_score_nullable"))
        if not isinstance(interest_id, str):
            return [], "interest_ranking_contract_failed"
        if not interest_id:
            return [], "interest_ranking_contract_failed"
        if not _plain_int(rank):
            return [], "interest_ranking_contract_failed"
        if rank != expected_rank:
            return [], "interest_ranking_contract_failed"
        if not score_valid:
            return [], "interest_ranking_contract_failed"
        ranked.append(RankedInterest(interest_id, rank, score))

    if {item.interest_summary_id for item in ranked} != set(interest_ids) or len(
        ranked
    ) != len(interest_ids):
        return [], "interest_ranking_contract_failed"
    return ranked, ""


def _valid_candidate_shape(candidate: Any) -> bool:
    if not isinstance(candidate, dict) or set(candidate) != _CANDIDATE_FIELDS:
        return False
    for field_name in (
        "candidate_id",
        "lane",
        "intent",
        "persona",
        "provenance_relation",
        "identity_anchor",
        "primary_query",
        "fallback_query",
        "freshness_class",
        "source_requirement",
        "title",
        "origin",
    ):
        if not isinstance(candidate[field_name], str):
            return False
    for field_name in (
        "source_memory_unit_ids",
        "source_locators",
        "context_anchors",
    ):
        if not isinstance(candidate[field_name], list):
            return False
    if not isinstance(candidate["bridge"], dict):
        return False
    if any(
        not isinstance(value, str) or not value
        for value in candidate["source_memory_unit_ids"]
    ):
        return False
    if any(
        not isinstance(locator, list)
        or len(locator) != 2
        or not isinstance(locator[0], str)
        or not locator[0]
        or not _plain_int(locator[1])
        or locator[1] < 0
        for locator in candidate["source_locators"]
    ):
        return False
    if any(
        not isinstance(anchor, str) or not anchor
        for anchor in candidate["context_anchors"]
    ):
        return False
    for nullable in ("source_starter_id", "interest_summary_id"):
        if candidate[nullable] is not None and not isinstance(candidate[nullable], str):
            return False
    return bool(
        candidate["candidate_id"]
        and candidate["intent"]
        and candidate["persona"]
        and candidate["primary_query"]
        and candidate["title"]
        and candidate["source_requirement"] == "web_required"
        and candidate["freshness_class"] in _FRESHNESS_CLASSES
    )


def _valid_candidate_semantics(
    candidate: dict[str, Any],
    *,
    cold_start: bool,
    ranked_interest_ids: set[str],
    interest_context_by_id: dict[str, dict[str, Any]] | None,
) -> bool:
    lane = candidate["lane"]
    generic = lane in _GENERIC_LANES
    non_generic_semantics_invalid = (
        cold_start
        or candidate["interest_summary_id"] not in ranked_interest_ids
        or not candidate["source_memory_unit_ids"]
        or not candidate["source_locators"]
        or not candidate["identity_anchor"]
        or candidate["provenance_relation"] not in {"stable_progress", "recent_follow_up"}
    )
    if generic:
        if not (
            candidate["interest_summary_id"] is None
            and candidate["source_memory_unit_ids"] == []
            and candidate["source_locators"] == []
            and candidate["identity_anchor"] == ""
            and candidate["context_anchors"] == []
            and candidate["provenance_relation"] == "generic"
        ):
            return False
    elif non_generic_semantics_invalid:
        return False

    if not generic and interest_context_by_id is not None:
        context = interest_context_by_id.get(candidate["interest_summary_id"])
        if not isinstance(context, dict):
            return False
        allowed_locators = {
            (locator[0], locator[1])
            for locator in context.get("source_locators") or []
            if isinstance(locator, list) and len(locator) == 2
        }
        candidate_locators = {
            (locator[0], locator[1]) for locator in candidate["source_locators"]
        }
        allowed_anchors = set(context.get("allowed_anchors") or [])
        if (
            candidate["source_memory_unit_ids"]
            != context.get("source_memory_unit_ids")
        ):
            return False
        if not candidate_locators.issubset(allowed_locators):
            return False
        if candidate["identity_anchor"] not in allowed_anchors:
            return False
        if not set(candidate["context_anchors"]).issubset(allowed_anchors):
            return False
        if candidate["identity_anchor"] in candidate["context_anchors"]:
            return False
        if (
            candidate["provenance_relation"]
            != context.get("provenance_relation")
        ):
            return False

    if lane == "perspective-expansion":
        if set(candidate["bridge"]) != {"source", "shared_property", "target"}:
            return False
        if any(not str(candidate["bridge"][field] or "") for field in candidate["bridge"]):
            return False
    elif candidate["bridge"] != {}:
        return False

    fixed = candidate["origin"] == "retrieved-fixed"
    return (
        fixed
        and isinstance(candidate["source_starter_id"], str)
        and bool(candidate["source_starter_id"])
    ) or (
        candidate["origin"] == "generated"
        and candidate["source_starter_id"] is None
    )


def _fixed_candidate_matches_hit(
    candidate: dict[str, Any],
    hit: dict[str, Any],
    *,
    cold_start: bool,
) -> bool:
    expected_lane = "generic-intent" if cold_start else "personalized-intent"
    expected_interest = None if cold_start else hit.get("interest_summary_id")
    expected_buckets = [] if cold_start else hit.get("source_memory_unit_ids")
    expected_locators = [] if cold_start else hit.get("source_locators")
    expected_identity = "" if cold_start else hit.get("identity_anchor")
    expected_context = [] if cold_start else hit.get("context_anchors")
    return (
        candidate["lane"] == expected_lane
        and candidate["origin"] == "retrieved-fixed"
        and candidate["source_starter_id"] == hit.get("starter_id")
        and candidate["title"] == hit.get("text")
        and candidate["interest_summary_id"] == expected_interest
        and candidate["source_memory_unit_ids"] == expected_buckets
        and candidate["source_locators"] == expected_locators
        and candidate["identity_anchor"] == expected_identity
        and candidate["context_anchors"] == expected_context
        and candidate["freshness_class"] == hit.get("freshness_class")
    )


async def generate_starter_candidates(
    ranked_interests: list[RankedInterest],
    starter_hits: list[dict[str, Any]],
    feedback_context: dict[str, Any],
    *,
    cold_start: bool,
    invoke_generator: ApiAdapter,
    target_count: int = OVERSAMPLE_SIZE,
    interest_context_by_id: dict[str, dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], str]:
    """I4: generate the frozen twelve-candidate oversampling pool."""

    if target_count != OVERSAMPLE_SIZE:
        return [], "candidate_generation_contract_failed"
    fixed_hits = starter_hits[:RETRIEVED_INTENT_SLOTS]
    ranked_ids = {item.interest_summary_id for item in ranked_interests}
    if interest_context_by_id is not None and set(interest_context_by_id) != ranked_ids:
        return [], "candidate_generation_contract_failed"

    payload = {
        "prompt_version": I4_PROMPT_VERSION,
        "generation_config_version": GENERATION_CONFIG_VERSION,
        "target_count": target_count,
        "cold_start": cold_start,
        "ranked_interests": [asdict(item) for item in ranked_interests],
        "retrieved_fixed_hits": fixed_hits,
        "feedback_context": feedback_context,
    }
    if interest_context_by_id is not None:
        payload["interest_context_by_id"] = interest_context_by_id
    try:
        response = await invoke_generator(
            payload
        )
    except Exception:  # noqa: BLE001 - adapter failure is a closed contract result
        return [], "candidate_generation_failed"

    if not isinstance(response, dict):
        return [], "candidate_generation_contract_failed"
    if set(response) != {"candidates"}:
        return [], "candidate_generation_contract_failed"
    if not isinstance(response.get("candidates"), list):
        return [], "candidate_generation_contract_failed"
    if len(response["candidates"]) != OVERSAMPLE_SIZE:
        return [], "candidate_generation_contract_failed"
    candidates = response["candidates"]
    if any(not _valid_candidate_shape(candidate) for candidate in candidates):
        return [], "candidate_generation_contract_failed"
    if len({candidate["candidate_id"] for candidate in candidates}) != len(candidates):
        return [], "candidate_generation_contract_failed"

    expected_counts = _COLD_LANE_COUNTS if cold_start else _WARM_LANE_COUNTS
    if Counter(candidate["lane"] for candidate in candidates) != expected_counts:
        return [], "candidate_generation_contract_failed"
    if any(
        not _valid_candidate_semantics(
            candidate,
            cold_start=cold_start,
            ranked_interest_ids=ranked_ids,
            interest_context_by_id=interest_context_by_id,
        )
        for candidate in candidates
    ):
        return [], "candidate_generation_contract_failed"

    fixed_candidates = [
        candidate
        for candidate in candidates
        if candidate["origin"] == "retrieved-fixed"
    ]
    if len(fixed_candidates) != len(fixed_hits):
        return [], "candidate_generation_contract_failed"

    if any(
        not _fixed_candidate_matches_hit(candidate, hit, cold_start=cold_start)
        for candidate, hit in zip(candidates, fixed_hits, strict=False)
    ):
        return [], "candidate_generation_contract_failed"
    return list(candidates), ""


async def select_starter_slate(
    grounded_candidates: list[dict[str, Any]],
    selection_constraints: dict[str, Any],
    *,
    invoke_selector: ApiAdapter,
    target_count: int = 4,
) -> tuple[list[OrderedSelection], str]:
    """I5: select candidate IDs while keeping ranking authority replaceable."""

    candidate_ids = [
        candidate.get("candidate_id")
        for candidate in grounded_candidates
        if isinstance(candidate, dict)
    ]
    if target_count != 4:
        return [], "slate_selection_contract_failed"
    if len(candidate_ids) != len(grounded_candidates):
        return [], "slate_selection_contract_failed"
    if any(not isinstance(candidate_id, str) or not candidate_id for candidate_id in candidate_ids):
        return [], "slate_selection_contract_failed"
    if len(set(candidate_ids)) != len(candidate_ids):
        return [], "slate_selection_contract_failed"

    try:
        response = await invoke_selector(
            {
                "prompt_version": I5_PROMPT_VERSION,
                "target_count": target_count,
                "grounded_candidates": grounded_candidates,
                "selection_constraints": selection_constraints,
            }
        )
    except Exception:  # noqa: BLE001 - adapter failure is a closed contract result
        return [], "slate_selection_failed"

    if not isinstance(response, dict):
        return [], "slate_selection_contract_failed"
    if set(response) != {"selections"}:
        return [], "slate_selection_contract_failed"
    if not isinstance(response.get("selections"), list):
        return [], "slate_selection_contract_failed"
    if len(response["selections"]) > target_count:
        return [], "slate_selection_contract_failed"

    candidate_id_set = set(candidate_ids)
    selected_ids: set[str] = set()
    selections: list[OrderedSelection] = []
    for expected_rank, raw in enumerate(response["selections"], start=1):
        if not isinstance(raw, dict) or set(raw) != {
            "candidate_id",
            "selector_rank",
            "api_score_nullable",
        }:
            return [], "slate_selection_contract_failed"
        candidate_id = raw.get("candidate_id")
        selector_rank = raw.get("selector_rank")
        score_valid, score = _finite_nullable_score(raw.get("api_score_nullable"))
        if not isinstance(candidate_id, str):
            return [], "slate_selection_contract_failed"
        if candidate_id not in candidate_id_set:
            return [], "slate_selection_contract_failed"
        if candidate_id in selected_ids:
            return [], "slate_selection_contract_failed"
        if not _plain_int(selector_rank):
            return [], "slate_selection_contract_failed"
        if selector_rank != expected_rank:
            return [], "slate_selection_contract_failed"
        if not score_valid:
            return [], "slate_selection_contract_failed"
        selected_ids.add(candidate_id)
        selections.append(OrderedSelection(candidate_id, selector_rank, score))

    candidates_by_id = {
        candidate["candidate_id"]: candidate for candidate in grounded_candidates
    }
    trend_seen = False
    eligible_candidates: list[dict[str, Any]] = []
    for selection in selections:
        candidate = candidates_by_id[selection.candidate_id]
        if candidate.get("lane") == "fresh-trend":
            if trend_seen:
                continue
            trend_seen = True
        eligible_candidates.append(candidate)
    kept_candidates = history.filter_distinct_personalized_candidates(
        eligible_candidates,
        target_count=target_count,
    )
    if any(
        candidate.get("origin") == "retrieved-fixed"
        for candidate in grounded_candidates
    ) and not any(
        candidate.get("origin") == "retrieved-fixed"
        for candidate in kept_candidates
    ):
        return [], "slate_selection_contract_failed"
    selections_by_id = {selection.candidate_id: selection for selection in selections}
    return [
        selections_by_id[candidate["candidate_id"]]
        for candidate in kept_candidates
    ], ""

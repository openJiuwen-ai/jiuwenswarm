# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Utility/diversity scoring and deterministic list search for topic cards."""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from itertools import combinations
from typing import Any, cast

from jiuwenswarm.agents.harness.common.recommendation.topic_model import (
    provider_validation,
)
from jiuwenswarm.agents.harness.common.recommendation.topic_terminal import (
    LearnedComponentReceipt,
)

UTILITY_COMPONENT_ID = "interaction_utility_scorer"
# Where a fixed condition on a card comes from, as the utility scorer lists it.
GIVEN_CONDITION_SOURCES = (
    "user",
    "assistant_estimate",
    "source_page",
    "stated_assumption",
    "unsupported",
)
DIVERSITY_COMPONENT_ID = "diversity_scorer"
LIST_SEARCH_VERSION = "topic-starter-v6-host-beam-v4"
PREFERENCE_PAIR_VERSION = "topic-starter-v6-preference-pair-v1"

FIRST_SCREEN_CARD_TARGET = 4
"""Cards one screen shows at once."""
GLOBAL_POOL_TARGET_SIZE = 7
"""Cards a global round prepares: one screen, then the cards that replace opened or dismissed ones."""

_GLOBAL_SCOPE = "global_new_chat"
_GLOBAL_QUOTAS = {
    "profile_recent_progress": 3,
    "adjacent_explanation": 2,
    "broad_timely_practical": 2,
    "profile_interest_continuation": GLOBAL_POOL_TARGET_SIZE,
}
_PROJECT_QUOTAS = {
    "project_continuation": 3,
    "project_domain_progress": 1,
}


def _plain_int(value: Any) -> bool:
    # bool is an int subclass; a JSON boolean is never a version or a count.
    return isinstance(value, int) and not isinstance(value, bool)


def _nonempty(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _nullable_score(value: Any, *, field_name: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field_name} must be a finite number or null")
    score = float(value)
    if not math.isfinite(score):
        raise ValueError(f"{field_name} must be a finite number or null")
    return score


@dataclass(frozen=True)
class SelectionCandidate:
    """Frozen host projection used by both scorers and list search."""

    candidate_id: str
    context_scope: str
    category: str
    stable_rank: int
    evidence_eligible: bool
    freshness_eligible: bool
    negative_preference_hit: bool
    interest_summary_id: str | None
    identity_anchor: str | None
    source_memory_unit_ids: tuple[str, ...]
    scoring_features: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not all(
            _nonempty(value)
            for value in (self.candidate_id, self.context_scope, self.category)
        ):
            raise ValueError("candidate identity, scope, and category are required")
        if not _plain_int(self.stable_rank) or self.stable_rank <= 0:
            raise ValueError("stable_rank must be a positive integer")
        _value_items = (
            self.evidence_eligible,
            self.freshness_eligible,
            self.negative_preference_hit,
        )
        if any(
            not isinstance(value, bool)
            for value in _value_items
        ):
            raise ValueError("candidate eligibility flags must be booleans")
        if not isinstance(self.scoring_features, Mapping):
            raise ValueError("scoring_features must be a mapping")
        if not isinstance(self.source_memory_unit_ids, tuple) or any(
            not isinstance(value, str) for value in self.source_memory_unit_ids
        ):
            raise ValueError("source_memory_unit_ids must be a tuple of strings")
        object.__setattr__(self, "scoring_features", dict(self.scoring_features))

    def as_scoring_payload(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "context_scope": self.context_scope,
            "category": self.category,
            "stable_rank": self.stable_rank,
            "scoring_features": dict(self.scoring_features),
        }


@dataclass(frozen=True)
class UtilityScore:
    candidate_id: str
    click_score_nullable: float | None
    depth_score_nullable: float | None
    combined_utility_nullable: float | None
    admitted: bool
    reason: str
    # Audit only: the conditions the scorer read as fixed, with their sources.
    given_conditions: tuple[dict[str, str], ...]


@dataclass(frozen=True)
class UtilityScoringResult:
    scores: tuple[UtilityScore, ...]
    scorer_version: str
    receipt: LearnedComponentReceipt


@dataclass(frozen=True)
class PairwiseDiversityScore:
    left_candidate_id: str
    right_candidate_id: str
    diversity_score_nullable: float | None
    compatible: bool
    reason: str


@dataclass(frozen=True)
class MarginalDiversityScore:
    candidate_id: str
    marginal_diversity_nullable: float | None


@dataclass(frozen=True)
class DiversityScoringResult:
    pairwise_scores: tuple[PairwiseDiversityScore, ...]
    marginal_scores: tuple[MarginalDiversityScore, ...]
    scorer_version: str
    receipt: LearnedComponentReceipt


@dataclass(frozen=True)
class ListSearchConstraints:
    context_scope: str
    beam_width: int = 64
    max_expansions: int = 4_096
    max_candidates: int = 64
    utility_weight: float = 1.0
    diversity_weight: float = 1.0
    required_first_window_interest_summary_ids: tuple[str, ...] = ()
    # The global news card, placed ahead of the searched list.
    leading_cards: int = 0

    def __post_init__(self) -> None:
        valid_scope = self.context_scope == _GLOBAL_SCOPE or (
            self.context_scope.startswith("project:")
            and bool(self.context_scope.removeprefix("project:"))
        )
        if not valid_scope:
            raise ValueError("invalid context_scope")
        _rejected2 = (
            self.required_first_window_interest_summary_ids and (
                self.context_scope != _GLOBAL_SCOPE
                or not isinstance(self.required_first_window_interest_summary_ids, tuple)
                or any(not _nonempty(value) for value in self.required_first_window_interest_summary_ids)
            )
        )
        if _rejected2:
            raise ValueError("required interests must be a global ID tuple")
        if self.leading_cards not in ((0, 1) if self.context_scope == _GLOBAL_SCOPE else (0,)):
            raise ValueError("only a global list has a leading card, and at most one")
        _value_items2 = (
            self.beam_width,
            self.max_expansions,
            self.max_candidates,
        )
        if any(
            not _plain_int(value) or value <= 0
            for value in _value_items2
        ):
            raise ValueError("search bounds must be positive integers")
        for name, value in (
            ("utility_weight", self.utility_weight),
            ("diversity_weight", self.diversity_weight),
        ):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{name} must be finite")
            if not math.isfinite(float(value)):
                raise ValueError(f"{name} must be finite")
        if self.utility_weight < 0 or self.diversity_weight < 0:
            raise ValueError("search weights must be non-negative")

    @property
    def quotas(self) -> dict[str, int]:
        return dict(
            _GLOBAL_QUOTAS if self.context_scope == _GLOBAL_SCOPE else _PROJECT_QUOTAS
        )

    @property
    def target_size(self) -> int:
        """D10: the global pool holds up to seven cards whatever the mix, a leading card included."""

        return (
            GLOBAL_POOL_TARGET_SIZE - self.leading_cards
            if self.context_scope == _GLOBAL_SCOPE
            else sum(self.quotas.values())
        )

    @property
    def first_window_size(self) -> int:
        """The searched cards the first screen shows after a leading card."""

        return FIRST_SCREEN_CARD_TARGET - self.leading_cards


@dataclass(frozen=True)
class CandidateHostDecision:
    candidate_id: str
    selected: bool
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class BeamTraceEntry:
    trace_index: int
    candidate_ids: tuple[str, ...]
    depth: int
    status: str
    retained_in_beam: bool
    hard_constraints_pass: bool
    utility_pass: bool | None
    diversity_pass: bool | None
    mean_utility_nullable: float | None
    mean_diversity_nullable: float | None
    minimum_pairwise_diversity_nullable: float | None
    objective_nullable: float | None
    failure_reasons: tuple[str, ...]


@dataclass(frozen=True)
class BoundedBeamTrace:
    entries: tuple[BeamTraceEntry, ...]
    preferred_candidate_ids: tuple[str, ...]
    target_size: int
    expansion_count: int
    beam_width: int
    max_expansions: int
    utility_scorer_version: str
    diversity_scorer_version: str
    search_version: str = LIST_SEARCH_VERSION


@dataclass(frozen=True)
class ListSearchResult:
    preferred_list: tuple[str, ...]
    decisions: tuple[CandidateHostDecision, ...]
    bounded_beam_trace: BoundedBeamTrace
    failure_reason: str
    utility_scorer_version: str
    diversity_scorer_version: str
    utility_receipt: LearnedComponentReceipt
    # None when the pool is the news card alone and no diversity judgment ran.
    diversity_receipt: LearnedComponentReceipt | None
    search_version: str = LIST_SEARCH_VERSION

    def as_manifest_metadata(self) -> dict[str, Any]:
        return {
            "utility_scorer_version": self.utility_scorer_version,
            "diversity_scorer_version": self.diversity_scorer_version,
            "list_search_version": self.search_version,
            "component_receipts": {
                UTILITY_COMPONENT_ID: self.utility_receipt.as_payload(),
                **(
                    {DIVERSITY_COMPONENT_ID: self.diversity_receipt.as_payload()}
                    if self.diversity_receipt is not None
                    else {}
                ),
            },
        }


@dataclass(frozen=True)
class HierarchicalPreferencePair:
    pair_id: str
    preferred_candidate_ids: tuple[str, ...]
    negative_candidate_ids: tuple[str, ...]
    negative_type: str
    source_trace_index: int
    failure_reasons: tuple[str, ...]
    utility_scorer_version: str
    diversity_scorer_version: str
    search_version: str
    pair_version: str = PREFERENCE_PAIR_VERSION

    def as_payload(self) -> dict[str, Any]:
        return {
            "pair_id": self.pair_id,
            "preferred_candidate_ids": list(self.preferred_candidate_ids),
            "negative_candidate_ids": list(self.negative_candidate_ids),
            "negative_type": self.negative_type,
            "source_trace_index": self.source_trace_index,
            "failure_reasons": list(self.failure_reasons),
            "utility_scorer_version": self.utility_scorer_version,
            "diversity_scorer_version": self.diversity_scorer_version,
            "search_version": self.search_version,
            "pair_version": self.pair_version,
        }


ScoreInvoker = Callable[[dict[str, Any]], Awaitable[Any]]


def _require_successful_receipt(
    receipt: LearnedComponentReceipt,
    *,
    component_id: str,
) -> None:
    if not isinstance(receipt, LearnedComponentReceipt):
        raise TypeError("scoring invocation requires LearnedComponentReceipt")
    if receipt.binding.component_id != component_id:
        raise ValueError("receipt component_id does not match scorer")
    if receipt.output_status != "success":
        raise ValueError("scoring receipt must be successful")


async def score_interaction_utility(
    user_state: Mapping[str, Any],
    candidates: Sequence[SelectionCandidate],
    *,
    invoke_scorer: ScoreInvoker,
    receipt: LearnedComponentReceipt,
) -> UtilityScoringResult:
    """Invoke one replaceable utility scorer and validate exact ID coverage."""

    _require_successful_receipt(receipt, component_id=UTILITY_COMPONENT_ID)
    candidate_ids = [candidate.candidate_id for candidate in candidates]
    if len(set(candidate_ids)) != len(candidate_ids):
        raise ValueError("candidate IDs must be unique")
    raw = await invoke_scorer(
        {
            "input_snapshot_id": receipt.input_snapshot_id,
            "request_id": receipt.request_id,
            "user_state": dict(user_state),
            "candidates": [candidate.as_scoring_payload() for candidate in candidates],
        }
    )
    with provider_validation(invoke_scorer, raw):
        by_id = utility_scores_by_id(raw, candidate_ids)
        return UtilityScoringResult(
            scores=tuple(by_id[candidate_id] for candidate_id in candidate_ids),
            scorer_version=receipt.binding.config_version,
            receipt=receipt,
        )


def utility_scores_by_id(raw: Any, candidate_ids: Sequence[str]) -> dict[str, UtilityScore]:
    """Validate one utility response covering exactly these candidates, once each."""

    if (
        not isinstance(raw, dict)
        or set(raw) != {"scores"}
        or not isinstance(raw["scores"], list)
    ):
        raise ValueError("utility scorer output schema invalid")

    by_id: dict[str, UtilityScore] = {}
    expected_fields = {
        "candidate_id",
        "question_focus",
        "assertion_spans",
        "given_conditions",
        "click_score_nullable",
        "depth_score_nullable",
        "combined_utility_nullable",
        "admitted",
        "reason",
    }
    for item in raw["scores"]:
        _rejected3 = (
            not isinstance(item, dict)
            or set(item) != expected_fields
            or not _nonempty(item["question_focus"])
            or not isinstance(item["assertion_spans"], list)
            or not all(_nonempty(span) for span in item["assertion_spans"])
            or not _valid_given_conditions(item["given_conditions"])
        )
        if (
            _rejected3
        ):
            raise ValueError("utility scorer output schema invalid")
        candidate_id = item["candidate_id"]
        if (
            not _nonempty(candidate_id)
            or candidate_id not in candidate_ids
            or candidate_id in by_id
        ):
            raise ValueError("utility scorer candidate membership invalid")
        if not isinstance(item["admitted"], bool) or not _nonempty(item["reason"]):
            raise ValueError("utility scorer decision invalid")
        by_id[candidate_id] = UtilityScore(
            admitted=item["admitted"],
            reason=item["reason"],
            given_conditions=tuple(
                dict(condition) for condition in item["given_conditions"]
            ),
            candidate_id=candidate_id,
            click_score_nullable=_nullable_score(
                item["click_score_nullable"], field_name="click_score_nullable"
            ),
            depth_score_nullable=_nullable_score(
                item["depth_score_nullable"], field_name="depth_score_nullable"
            ),
            combined_utility_nullable=_nullable_score(
                item["combined_utility_nullable"],
                field_name="combined_utility_nullable",
            ),
        )
    if set(by_id) != set(candidate_ids):
        raise ValueError("utility scorer must cover every candidate exactly once")
    return by_id


def _valid_given_conditions(value: Any) -> bool:
    return isinstance(value, list) and all(
        isinstance(condition, dict)
        and set(condition) == {"card_quote", "condition", "source", "source_quote"}
        and _nonempty(condition["card_quote"])
        and _nonempty(condition["condition"])
        and _nonempty(condition["source_quote"])
        and condition["source"] in GIVEN_CONDITION_SOURCES
        for condition in value
    )


def _pair_key(left: str, right: str) -> tuple[str, str]:
    return (left, right) if left < right else (right, left)


async def score_diversity(
    candidates: Sequence[SelectionCandidate],
    *,
    invoke_scorer: ScoreInvoker,
    receipt: LearnedComponentReceipt,
) -> DiversityScoringResult:
    """Invoke one diversity scorer and require complete pair/marginal cover."""

    _require_successful_receipt(receipt, component_id=DIVERSITY_COMPONENT_ID)
    candidate_ids = [candidate.candidate_id for candidate in candidates]
    if len(set(candidate_ids)) != len(candidate_ids):
        raise ValueError("candidate IDs must be unique")
    raw = await invoke_scorer(
        {
            "input_snapshot_id": receipt.input_snapshot_id,
            "request_id": receipt.request_id,
            "candidates": [candidate.as_scoring_payload() for candidate in candidates],
        }
    )
    _rejected = (
        not isinstance(raw, dict)
        or set(raw) != {"pairwise_scores", "marginal_scores"}
        or not isinstance(raw["pairwise_scores"], list)
        or not isinstance(raw["marginal_scores"], list)
    )
    if (
        _rejected
    ):
        raise ValueError("diversity scorer output schema invalid")

    pair_fields = {
        "left_candidate_id",
        "right_candidate_id",
        "diversity_score_nullable",
        "compatible",
        "reason",
    }
    pairwise_by_key: dict[tuple[str, str], PairwiseDiversityScore] = {}
    for item in raw["pairwise_scores"]:
        if not isinstance(item, dict) or set(item) != pair_fields:
            raise ValueError("diversity scorer output schema invalid")
        left = item["left_candidate_id"]
        right = item["right_candidate_id"]
        _rejected4 = (
            not _nonempty(left)
            or not _nonempty(right)
            or left == right
            or left not in candidate_ids
            or right not in candidate_ids
        )
        if (
            _rejected4
        ):
            raise ValueError("diversity scorer pair membership invalid")
        key = _pair_key(left, right)
        if key in pairwise_by_key:
            raise ValueError("diversity scorer pair membership invalid")
        if not isinstance(item["compatible"], bool) or not _nonempty(item["reason"]):
            raise ValueError("diversity scorer decision invalid")
        pairwise_by_key[key] = PairwiseDiversityScore(
            compatible=item["compatible"],
            reason=item["reason"],
            left_candidate_id=key[0],
            right_candidate_id=key[1],
            diversity_score_nullable=_nullable_score(
                item["diversity_score_nullable"],
                field_name="diversity_score_nullable",
            ),
        )

    marginal_fields = {"candidate_id", "marginal_diversity_nullable"}
    marginal_by_id: dict[str, MarginalDiversityScore] = {}
    for item in raw["marginal_scores"]:
        if not isinstance(item, dict) or set(item) != marginal_fields:
            raise ValueError("diversity scorer output schema invalid")
        candidate_id = item["candidate_id"]
        if (
            not _nonempty(candidate_id)
            or candidate_id not in candidate_ids
            or candidate_id in marginal_by_id
        ):
            raise ValueError("diversity scorer marginal membership invalid")
        marginal_by_id[candidate_id] = MarginalDiversityScore(
            candidate_id=candidate_id,
            marginal_diversity_nullable=_nullable_score(
                item["marginal_diversity_nullable"],
                field_name="marginal_diversity_nullable",
            ),
        )

    expected_pairs = {
        _pair_key(left, right) for left, right in combinations(candidate_ids, 2)
    }
    if set(pairwise_by_key) != expected_pairs:
        raise ValueError("diversity scorer must cover every pair exactly once")
    if set(marginal_by_id) != set(candidate_ids):
        raise ValueError("diversity scorer must cover every marginal exactly once")

    pair_order = [
        _pair_key(left, right) for left, right in combinations(candidate_ids, 2)
    ]
    return DiversityScoringResult(
        pairwise_scores=tuple(pairwise_by_key[key] for key in pair_order),
        marginal_scores=tuple(
            marginal_by_id[candidate_id] for candidate_id in candidate_ids
        ),
        scorer_version=receipt.binding.config_version,
        receipt=receipt,
    )


def _candidate_order(candidate: SelectionCandidate) -> tuple[int, str]:
    return (candidate.stable_rank, candidate.candidate_id)


def _candidate_hard_reasons(
    candidate: SelectionCandidate,
    constraints: ListSearchConstraints,
) -> tuple[str, ...]:
    reasons: list[str] = []
    if candidate.context_scope != constraints.context_scope:
        reasons.append("scope_mismatch")
    if candidate.category not in constraints.quotas:
        reasons.append("category_not_allowed")
    if not candidate.evidence_eligible:
        reasons.append("evidence_ineligible")
    if not candidate.freshness_eligible:
        reasons.append("freshness_ineligible")
    if candidate.negative_preference_hit:
        reasons.append("negative_preference")
    if constraints.context_scope == _GLOBAL_SCOPE:
        if not _nonempty(candidate.interest_summary_id):
            reasons.append("missing_personalized_interest")
        if not _nonempty(candidate.identity_anchor):
            reasons.append("missing_identity_anchor")
    return tuple(reasons)


def _partial_hard_reasons(
    selected: Sequence[SelectionCandidate],
    remaining: Sequence[SelectionCandidate],
    quotas: Mapping[str, int],
    *,
    global_scope: bool,
    required_interests: tuple[str, ...],
    first_window: int,
) -> tuple[str, ...]:
    reasons: list[str] = []
    if not global_scope and _category_overflow(selected, quotas):
        reasons.append("category_quota_exceeded")
    if global_scope:
        if required_interests and not any(
            candidate.interest_summary_id in required_interests
            for candidate in selected[:first_window]
        ):
            if len(selected) >= first_window:
                reasons.append("required_first_window_interest_missing")
            elif not any(
                candidate.interest_summary_id in required_interests
                for candidate in remaining
            ):
                reasons.append("required_first_window_interest_unfillable")
    return tuple(reasons)


def _category_overflow(
    selected: Sequence[SelectionCandidate],
    quotas: Mapping[str, int],
) -> int:
    """How far a list runs past the category mix the quotas describe."""

    counts = Counter(candidate.category for candidate in selected)
    return sum(max(0, count - quotas[category]) for category, count in counts.items())


def _complete_required_reasons(
    selected: Sequence[SelectionCandidate],
    constraints: ListSearchConstraints,
) -> tuple[str, ...]:
    if constraints.context_scope != _GLOBAL_SCOPE and not any(
        candidate.category == "project_continuation" for candidate in selected
    ):
        return ("project_continuation_required",)
    required_interests = constraints.required_first_window_interest_summary_ids
    if not required_interests or any(
        candidate.interest_summary_id in required_interests
        for candidate in selected[:constraints.first_window_size]
    ):
        return ()
    return ("required_first_window_interest_missing",)


def _score_maps(
    candidates: Sequence[SelectionCandidate],
    eligible_ids: set[str],
    utility: UtilityScoringResult,
    diversity: DiversityScoringResult,
) -> tuple[
    dict[str, float | None],
    dict[tuple[str, str], float | None],
    dict[str, float | None],
]:
    _require_successful_receipt(utility.receipt, component_id=UTILITY_COMPONENT_ID)
    _require_successful_receipt(diversity.receipt, component_id=DIVERSITY_COMPONENT_ID)
    if utility.scorer_version != utility.receipt.binding.config_version:
        raise ValueError("utility scorer_version disagrees with receipt")
    if diversity.scorer_version != diversity.receipt.binding.config_version:
        raise ValueError("diversity scorer_version disagrees with receipt")
    if utility.receipt.input_snapshot_id != diversity.receipt.input_snapshot_id:
        raise ValueError("utility and diversity snapshots must match")

    input_ids = {candidate.candidate_id for candidate in candidates}
    utility_by_id: dict[str, float | None] = {}
    for utility_score in utility.scores:
        if (
            utility_score.candidate_id not in input_ids
            or utility_score.candidate_id in utility_by_id
        ):
            raise ValueError("utility scores contain invalid candidate membership")
        if not isinstance(utility_score.admitted, bool) or not _nonempty(utility_score.reason):
            raise ValueError("utility scorer decision invalid")
        utility_by_id[utility_score.candidate_id] = _nullable_score(
            utility_score.combined_utility_nullable,
            field_name="combined_utility_nullable",
        ) or 0.0
    if set(utility_by_id) != input_ids:
        raise ValueError("utility scores do not cover every eligible candidate")

    marginal_by_id: dict[str, float | None] = {}
    for marginal_score in diversity.marginal_scores:
        if (
            marginal_score.candidate_id not in input_ids
            or marginal_score.candidate_id in marginal_by_id
        ):
            raise ValueError("marginal scores contain invalid candidate membership")
        marginal_by_id[marginal_score.candidate_id] = _nullable_score(
            marginal_score.marginal_diversity_nullable,
            field_name="marginal_diversity_nullable",
        ) or 0.0
    if set(marginal_by_id) != input_ids:
        raise ValueError("marginal scores do not cover every eligible candidate")

    pairwise_by_key: dict[tuple[str, str], float | None] = {}
    for pairwise_score in diversity.pairwise_scores:
        key = _pair_key(
            pairwise_score.left_candidate_id,
            pairwise_score.right_candidate_id,
        )
        if (
            pairwise_score.left_candidate_id == pairwise_score.right_candidate_id
            or not set(key).issubset(input_ids)
            or key in pairwise_by_key
        ):
            raise ValueError("pairwise scores contain invalid candidate membership")
        if not isinstance(pairwise_score.compatible, bool) or not _nonempty(pairwise_score.reason):
            raise ValueError("diversity scorer decision invalid")
        pairwise_by_key[key] = _nullable_score(
            pairwise_score.diversity_score_nullable,
            field_name="diversity_score_nullable",
        ) or 0.0
    expected_eligible_pairs = {
        _pair_key(left, right) for left, right in combinations(input_ids, 2)
    }
    if set(pairwise_by_key) != expected_eligible_pairs:
        raise ValueError("pairwise scores do not cover every eligible pair")
    return utility_by_id, pairwise_by_key, marginal_by_id


@dataclass(frozen=True)
class _ListMetrics:
    mean_utility: float | None
    mean_diversity: float | None
    minimum_pairwise_diversity: float | None
    pairwise_count: int
    objective: float | None


def _list_metrics(
    candidate_ids: tuple[str, ...],
    utility_by_id: Mapping[str, float | None],
    pairwise_by_key: Mapping[tuple[str, str], float | None],
    marginal_by_id: Mapping[str, float | None],
    constraints: ListSearchConstraints,
) -> _ListMetrics:
    utility_values = [utility_by_id[candidate_id] for candidate_id in candidate_ids]
    mean_utility = (
        None
        if any(value is None for value in utility_values)
        else sum(cast(float, value) for value in utility_values) / len(utility_values)
    )

    pairwise_values = [
        pairwise_by_key[_pair_key(left, right)]
        for left, right in combinations(candidate_ids, 2)
    ]
    marginal_values = [marginal_by_id[candidate_id] for candidate_id in candidate_ids]
    diversity_values = pairwise_values + marginal_values
    mean_diversity = (
        None
        if any(value is None for value in diversity_values)
        else sum(cast(float, value) for value in diversity_values)
        / len(diversity_values)
    )
    minimum_pairwise = (
        None
        if not pairwise_values or any(value is None for value in pairwise_values)
        else min(cast(float, value) for value in pairwise_values)
    )
    objective = (
        None
        if mean_utility is None or mean_diversity is None
        else constraints.utility_weight * mean_utility
        + constraints.diversity_weight * mean_diversity
    )
    return _ListMetrics(
        mean_utility=mean_utility,
        mean_diversity=mean_diversity,
        minimum_pairwise_diversity=minimum_pairwise,
        pairwise_count=len(pairwise_values),
        objective=objective,
    )


def _empty_search_result(
    *,
    candidates: Sequence[SelectionCandidate],
    hard_reasons_by_id: Mapping[str, tuple[str, ...]],
    trace_entries: Sequence[BeamTraceEntry],
    expansion_count: int,
    failure_reason: str,
    utility: UtilityScoringResult,
    diversity: DiversityScoringResult,
    constraints: ListSearchConstraints,
) -> ListSearchResult:
    decisions = tuple(
        CandidateHostDecision(
            candidate_id=candidate.candidate_id,
            selected=False,
            reasons=hard_reasons_by_id.get(candidate.candidate_id) or (failure_reason,),
        )
        for candidate in sorted(candidates, key=_candidate_order)
    )
    trace = BoundedBeamTrace(
        entries=tuple(trace_entries),
        preferred_candidate_ids=(),
        target_size=constraints.target_size,
        expansion_count=expansion_count,
        beam_width=constraints.beam_width,
        max_expansions=constraints.max_expansions,
        utility_scorer_version=utility.scorer_version,
        diversity_scorer_version=diversity.scorer_version,
    )
    return ListSearchResult(
        preferred_list=(),
        decisions=decisions,
        bounded_beam_trace=trace,
        failure_reason=failure_reason,
        utility_scorer_version=utility.scorer_version,
        diversity_scorer_version=diversity.scorer_version,
        utility_receipt=utility.receipt,
        diversity_receipt=diversity.receipt,
    )


def news_only_search_result(
    utility: UtilityScoringResult,
    *,
    context_scope: str,
) -> ListSearchResult:
    """The list search of a global pool that is its news card alone.

    No regular candidate exists, so nothing is searched and no diversity is
    judged: no decisions, no trace entries, no expansions and no diversity
    scorer version. The target size is the one a news-led list would have.
    """

    constraints = ListSearchConstraints(context_scope=context_scope, leading_cards=1)
    trace = BoundedBeamTrace(
        entries=(),
        preferred_candidate_ids=(),
        target_size=constraints.target_size,
        expansion_count=0,
        beam_width=constraints.beam_width,
        max_expansions=constraints.max_expansions,
        utility_scorer_version=utility.scorer_version,
        diversity_scorer_version="",
    )
    return ListSearchResult(
        preferred_list=(),
        decisions=(),
        bounded_beam_trace=trace,
        failure_reason="",
        utility_scorer_version=utility.scorer_version,
        diversity_scorer_version="",
        utility_receipt=utility.receipt,
        diversity_receipt=None,
    )


def hybrid_list_search(
    candidates: Sequence[SelectionCandidate],
    utility: UtilityScoringResult,
    diversity: DiversityScoringResult,
    constraints: ListSearchConstraints,
) -> ListSearchResult:
    """Select one deterministic list within the category quotas' total size.

    Global quotas are preferences: they order lists of equal length and never
    reject a list for running past one category. Project quotas stay caps, and a
    project list needs at least one continuation. Prefer the largest list that
    passes every host, utility, and diversity constraint.
    """

    if not isinstance(constraints, ListSearchConstraints):
        raise TypeError("constraints must be ListSearchConstraints")
    if len(candidates) > constraints.max_candidates:
        raise ValueError("candidate set exceeds max_candidates")
    candidate_ids = [candidate.candidate_id for candidate in candidates]
    if len(set(candidate_ids)) != len(candidate_ids):
        raise ValueError("candidate IDs must be unique")

    ordered = tuple(sorted(candidates, key=_candidate_order))
    hard_reasons_by_id = {
        candidate.candidate_id: _candidate_hard_reasons(candidate, constraints)
        for candidate in ordered
    }
    utility_decisions = {score.candidate_id: score.admitted for score in utility.scores}
    for candidate in ordered:
        if utility_decisions.get(candidate.candidate_id) is False:
            hard_reasons_by_id[candidate.candidate_id] += ("utility_rejected",)
    incompatible_pairs = {
        _pair_key(score.left_candidate_id, score.right_candidate_id)
        for score in diversity.pairwise_scores if not score.compatible
    }
    eligible = tuple(
        candidate
        for candidate in ordered
        if not hard_reasons_by_id[candidate.candidate_id]
    )
    eligible_ids = {candidate.candidate_id for candidate in eligible}
    utility_by_id, pairwise_by_key, marginal_by_id = _score_maps(
        ordered, eligible_ids, utility, diversity
    )

    trace_rows: list[dict[str, Any]] = []
    for candidate in ordered:
        reasons = hard_reasons_by_id[candidate.candidate_id]
        if reasons:
            trace_rows.append(
                {
                    "candidate_ids": (candidate.candidate_id,),
                    "depth": 1,
                    "status": "hard_filter_rejected",
                    "retained_in_beam": False,
                    "hard_constraints_pass": False,
                    "utility_pass": None,
                    "diversity_pass": None,
                    "mean_utility_nullable": None,
                    "mean_diversity_nullable": None,
                    "minimum_pairwise_diversity_nullable": None,
                    "objective_nullable": None,
                    "failure_reasons": reasons,
                }
            )

    required_interests = constraints.required_first_window_interest_summary_ids
    if required_interests and not any(
        candidate.interest_summary_id in required_interests
        for candidate in eligible
    ):
        entries = tuple(
            BeamTraceEntry(trace_index=index, **row)
            for index, row in enumerate(trace_rows, start=1)
        )
        return _empty_search_result(
            candidates=ordered,
            hard_reasons_by_id=hard_reasons_by_id,
            trace_entries=entries,
            expansion_count=0,
            failure_reason="required_first_window_interest_unavailable",
            utility=utility,
            diversity=diversity,
            constraints=constraints,
        )

    quotas = constraints.quotas
    target_size = constraints.target_size
    beam: list[tuple[int, ...]] = [()]
    passing_by_depth: dict[int, list[tuple[tuple[int, ...], _ListMetrics]]] = {}
    expansion_count = 0
    budget_exhausted = False
    for depth in range(1, target_size + 1):
        attempts: list[dict[str, Any]] = []
        feasible: list[tuple[tuple[int, ...], _ListMetrics]] = []
        for positions in beam:
            start = positions[-1] + 1 if positions else 0
            for candidate_position in range(start, len(eligible)):
                if expansion_count >= constraints.max_expansions:
                    budget_exhausted = True
                    break
                expansion_count += 1
                expanded = positions + (candidate_position,)
                selected = tuple(eligible[index] for index in expanded)
                selected_ids = tuple(candidate.candidate_id for candidate in selected)
                remaining = eligible[candidate_position + 1:]
                hard_reasons = _partial_hard_reasons(
                    selected,
                    remaining,
                    quotas,
                    global_scope=constraints.context_scope == _GLOBAL_SCOPE,
                    required_interests=constraints.required_first_window_interest_summary_ids,
                    first_window=constraints.first_window_size,
                )
                if any(
                    _pair_key(left, right) in incompatible_pairs
                    for left, right in combinations(selected_ids, 2)
                ):
                    hard_reasons += ("diversity_incompatible",)
                metrics = _list_metrics(
                    selected_ids,
                    utility_by_id,
                    pairwise_by_key,
                    marginal_by_id,
                    constraints,
                )
                utility_pass: bool | None = None
                diversity_pass: bool | None = None
                failure_reasons = hard_reasons
                if not hard_reasons:
                    required_reasons = _complete_required_reasons(
                        selected,
                        constraints,
                    )
                    if required_reasons:
                        failure_reasons = required_reasons
                    else:
                        utility_pass, diversity_pass = True, True
                        failure_reasons = ()
                        if utility_pass and diversity_pass:
                            passing_by_depth.setdefault(depth, []).append(
                                (expanded, metrics)
                            )
                attempt = {
                    "positions": expanded,
                    "candidate_ids": selected_ids,
                    "depth": depth,
                    "hard_constraints_pass": not hard_reasons,
                    "utility_pass": utility_pass,
                    "diversity_pass": diversity_pass,
                    "mean_utility_nullable": metrics.mean_utility,
                    "mean_diversity_nullable": metrics.mean_diversity,
                    "minimum_pairwise_diversity_nullable": (
                        metrics.minimum_pairwise_diversity
                    ),
                    "objective_nullable": metrics.objective,
                    "failure_reasons": failure_reasons,
                }
                attempts.append(attempt)
                if not hard_reasons and metrics.objective is not None:
                    feasible.append((expanded, metrics))
            if budget_exhausted:
                break
        if budget_exhausted:
            for budget_attempt in attempts:
                budget_failure_reasons = tuple(
                    cast(Sequence[str], budget_attempt["failure_reasons"])
                )
                if "search_budget_exhausted" not in budget_failure_reasons:
                    budget_failure_reasons += ("search_budget_exhausted",)
                trace_rows.append(
                    {
                        key: value
                        for key, value in budget_attempt.items()
                        if key not in {"positions", "failure_reasons"}
                    }
                    | {
                        "status": "search_budget_exhausted",
                        "retained_in_beam": False,
                        "failure_reasons": budget_failure_reasons,
                    }
                )
            break

        feasible.sort(
            key=lambda item: (
                _category_overflow([eligible[index] for index in item[0]], quotas),
                -cast(float, item[1].objective),
                tuple(_candidate_order(eligible[index]) for index in item[0]),
            )
        )
        beam = [positions for positions, _ in feasible[: constraints.beam_width]]
        retained = set(beam)
        for attempt in attempts:
            is_retained = attempt["positions"] in retained
            if not attempt["hard_constraints_pass"]:
                status = "hard_constraint_rejected"
            elif depth == target_size:
                status = "complete_retained" if is_retained else "complete_pruned"
            else:
                status = "partial_retained" if is_retained else "partial_pruned"
            trace_rows.append(
                {key: value for key, value in attempt.items() if key != "positions"}
                | {"status": status, "retained_in_beam": is_retained}
            )
        if not beam:
            break

    preferred_positions: tuple[int, ...] | None = None
    if not budget_exhausted and passing_by_depth:
        maximum_depth = max(passing_by_depth)
        preferred_positions = min(
            passing_by_depth.get(maximum_depth, ()),
            key=lambda item: (
                _category_overflow([eligible[index] for index in item[0]], quotas),
                -cast(float, item[1].objective),
                tuple(_candidate_order(eligible[index]) for index in item[0]),
            ),
        )[0]
    preferred_ids = (
        tuple(eligible[index].candidate_id for index in preferred_positions)
        if preferred_positions is not None
        else ()
    )
    for row in trace_rows:
        if row["candidate_ids"] == preferred_ids and preferred_ids:
            row["status"] = "preferred"

    entries = tuple(
        BeamTraceEntry(trace_index=index, **row)
        for index, row in enumerate(trace_rows, start=1)
    )
    if not preferred_ids:
        return _empty_search_result(
            candidates=ordered,
            hard_reasons_by_id=hard_reasons_by_id,
            trace_entries=entries,
            expansion_count=expansion_count,
            failure_reason=(
                "search_budget_exhausted"
                if budget_exhausted
                else "quality_abstained" if utility_decisions and not any(utility_decisions.values())
                else "no_feasible_complete_list"
            ),
            utility=utility,
            diversity=diversity,
            constraints=constraints,
        )

    preferred = set(preferred_ids)

    def decision_reasons(candidate: SelectionCandidate) -> tuple[str, ...]:
        if candidate.candidate_id in preferred:
            return ("selected_by_host_list_search",)
        hard_reasons = hard_reasons_by_id[candidate.candidate_id]
        if hard_reasons:
            return hard_reasons
        return ("list_level_not_selected",)

    decisions = tuple(
        CandidateHostDecision(
            candidate_id=candidate.candidate_id,
            selected=candidate.candidate_id in preferred,
            reasons=decision_reasons(candidate),
        )
        for candidate in ordered
    )
    trace = BoundedBeamTrace(
        entries=entries,
        preferred_candidate_ids=preferred_ids,
        target_size=target_size,
        expansion_count=expansion_count,
        beam_width=constraints.beam_width,
        max_expansions=constraints.max_expansions,
        utility_scorer_version=utility.scorer_version,
        diversity_scorer_version=diversity.scorer_version,
    )
    return ListSearchResult(
        preferred_list=preferred_ids,
        decisions=decisions,
        bounded_beam_trace=trace,
        failure_reason="",
        utility_scorer_version=utility.scorer_version,
        diversity_scorer_version=diversity.scorer_version,
        utility_receipt=utility.receipt,
        diversity_receipt=diversity.receipt,
    )


def mine_hierarchical_pairs(
    beam_trace: BoundedBeamTrace,
) -> tuple[HierarchicalPreferencePair, ...]:
    """Mine only negative lists that were actually evaluated by the host beam."""

    if not isinstance(beam_trace, BoundedBeamTrace):
        raise TypeError("beam_trace must be BoundedBeamTrace")
    if not beam_trace.preferred_candidate_ids:
        return ()

    pairs: list[HierarchicalPreferencePair] = []
    seen: set[tuple[tuple[str, ...], str]] = set()
    for entry in beam_trace.entries:
        if not entry.failure_reasons:
            continue
        if not entry.candidate_ids:
            continue
        if entry.candidate_ids == beam_trace.preferred_candidate_ids:
            continue
        if (
            entry.hard_constraints_pass
            and entry.utility_pass is True
            and entry.diversity_pass is False
        ):
            negative_type = "diversity_neg"
        elif (
            entry.hard_constraints_pass
            and entry.utility_pass is False
            and entry.diversity_pass is True
        ):
            negative_type = "utility_neg"
        else:
            negative_type = "joint_failure_neg"
        key = (entry.candidate_ids, negative_type)
        if key in seen:
            continue
        seen.add(key)
        pairs.append(
            HierarchicalPreferencePair(
                pair_id=(
                    f"{PREFERENCE_PAIR_VERSION}:{entry.trace_index}:{negative_type}"
                ),
                preferred_candidate_ids=beam_trace.preferred_candidate_ids,
                negative_candidate_ids=entry.candidate_ids,
                negative_type=negative_type,
                source_trace_index=entry.trace_index,
                failure_reasons=entry.failure_reasons,
                utility_scorer_version=beam_trace.utility_scorer_version,
                diversity_scorer_version=beam_trace.diversity_scorer_version,
                search_version=beam_trace.search_version,
            )
        )
    return tuple(pairs)

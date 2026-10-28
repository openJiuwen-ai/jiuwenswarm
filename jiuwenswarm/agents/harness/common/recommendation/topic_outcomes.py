# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Outcome records, training-data plane, and new-page delivery for topic recommendation."""

from __future__ import annotations

import json
import math
import uuid
from collections.abc import Callable
from dataclasses import dataclass, fields
from typing import Any, Literal

from jiuwenswarm.agents.harness.common.recommendation.topic_history import (
    empty_history_state,
    load_history_state_at,
    load_latest_history_state,
    save_history_state,
)
from jiuwenswarm.agents.harness.common.recommendation.topic_news import news_card_event
from jiuwenswarm.agents.harness.common.recommendation.topic_product import (
    GLOBAL_NEW_CHAT_SCOPE,
    POLICY_ID,
    card_is_news,
    card_provider_response_ids,
    card_search_receipt,
    card_uses_host_evidence,
    find_exact_terminal_card,
    interest_evidence_ids,
    interest_session_ids,
    is_cold_start_card,
    memory_sessions_by_unit,
    project_id_for_scope,
    valid_search_receipt,
)
from jiuwenswarm.agents.harness.common.recommendation.topic_starter import (
    TopicStarterStore,
)
from jiuwenswarm.agents.harness.common.recommendation.topic_terminal import (
    SESSION_SUMMARY_SCHEMA_VERSION,
    SessionSummaryArtifact,
)

STARTER_OUTCOME_SCHEMA_VERSION = "topic-starter-v6-starter-outcome-v3"
UTILITY_TRAINING_SCHEMA_VERSION = "topic-starter-v6-utility-training-v2"
REVISIT_PAIR_SCHEMA_VERSION = "topic-starter-v6-revisit-pair-v1"
RID_TRAINING_SCHEMA_VERSION = "topic-starter-v6-rid-training-tuple-v3"
RID_USER_CONTEXT_SCHEMA_VERSION = "topic-starter-v6-rid-user-context-v1"
PREFERENCE_PAIR_SCHEMA_VERSION = "topic-starter-v6-preference-pair-v1"
TRAINING_SLICE_SCHEMA_VERSION = "topic-starter-v6-training-slice-v2"


def _plain_int(value: Any) -> bool:
    # bool is an int subclass; a JSON boolean is never a version or a count.
    return isinstance(value, int) and not isinstance(value, bool)


def _nonempty(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _finite_timestamp(value: Any) -> bool:
    return type(value) in (int, float) and math.isfinite(float(value)) and value >= 0


def _valid_scope(value: Any) -> bool:
    return value == "global_new_chat" or (
        isinstance(value, str)
        and value.startswith("project:")
        and _nonempty(value.removeprefix("project:"))
    )


def _from_payload(
    artifact_type: type[Any],
    payload: Any,
    *,
    schema_version: str,
    tuple_fields: tuple[str, ...] = (),
    tuple_pair_fields: tuple[str, ...] = (),
) -> Any:
    expected_fields = {field.name for field in fields(artifact_type)}
    if (
        not isinstance(payload, dict)
        or set(payload) != expected_fields
        or payload.get("schema_version") != schema_version
    ):
        raise ValueError("artifact payload contract failed")
    values = dict(payload)
    for field_name in tuple_fields:
        value = values[field_name]
        if not isinstance(value, list):
            raise ValueError("artifact tuple field contract failed")
        values[field_name] = tuple(value)
    for field_name in tuple_pair_fields:
        value = values[field_name]
        if (
            not isinstance(value, list)
            or any(not isinstance(item, list) or len(item) != 2 for item in value)
        ):
            raise ValueError("artifact tuple-pair field contract failed")
        values[field_name] = tuple(tuple(item) for item in value)
    return artifact_type(**values)


@dataclass(frozen=True, slots=True)
class RevisitPair:
    revisit_pair_id: str
    earlier_summary_id: str
    later_summary_id: str
    earlier_session_id: str
    later_session_id: str
    context_scope: str
    match_label: bool
    matcher_id: str
    matcher_version: str
    natural_eligible: bool
    exclusion_reason: str | None
    observed_at: float
    schema_version: str = REVISIT_PAIR_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _value_items3 = (
            self.revisit_pair_id,
            self.earlier_summary_id,
            self.later_summary_id,
            self.earlier_session_id,
            self.later_session_id,
            self.matcher_id,
            self.matcher_version,
            self.schema_version,
        )
        _rejected2 = (
            any(
                not _nonempty(value)
                for value in _value_items3
            )
            or not _valid_scope(self.context_scope)
            or not isinstance(self.match_label, bool)
            or not isinstance(self.natural_eligible, bool)
            or not _finite_timestamp(self.observed_at)
            or (self.natural_eligible and self.exclusion_reason is not None)
            or (not self.natural_eligible and not _nonempty(self.exclusion_reason))
        )
        if (
            _rejected2
        ):
            raise ValueError("revisit pair contract failed")

    def as_payload(self) -> dict[str, Any]:
        return {field.name: getattr(self, field.name) for field in fields(self)}

    @classmethod
    def from_payload(cls, payload: Any) -> RevisitPair:
        return _from_payload(
            cls,
            payload,
            schema_version=REVISIT_PAIR_SCHEMA_VERSION,
        )


def build_revisit_pair(
    earlier: SessionSummaryArtifact,
    later: SessionSummaryArtifact,
    *,
    match_label: bool,
    matcher_id: str,
    matcher_version: str,
) -> RevisitPair:
    if earlier.session_id == later.session_id:
        reason = "same_session_not_revisit"
    elif earlier.context_scope != later.context_scope:
        reason = "context_scope_mismatch"
    elif later.completed_at <= earlier.completed_at:
        reason = "later_summary_not_later"
    elif not earlier.natural_revisit_eligible:
        reason = "earlier_summary_ineligible"
    elif later.input_origin != "manual_free_text":
        reason = "ineligible_input_origin"
    elif later.assistance_origin != "none":
        reason = "assisted_input_excluded"
    else:
        reason = None
    return RevisitPair(
        revisit_pair_id=(
            f"revisit:{earlier.session_summary_id}:{later.session_summary_id}:"
            f"{matcher_version}"
        ),
        earlier_summary_id=earlier.session_summary_id,
        later_summary_id=later.session_summary_id,
        earlier_session_id=earlier.session_id,
        later_session_id=later.session_id,
        context_scope=earlier.context_scope,
        match_label=bool(match_label),
        matcher_id=matcher_id,
        matcher_version=matcher_version,
        natural_eligible=reason is None,
        exclusion_reason=reason,
        observed_at=later.completed_at,
    )


@dataclass(frozen=True, slots=True)
class RIDUserContextSnapshot:
    """The actual RID request before any future revisit supplies its label."""

    user_history_snapshot_id: str
    context_scope: str
    cutoff_at: float
    request_payload: dict[str, Any]
    schema_version: str = RID_USER_CONTEXT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        request = self.request_payload
        _rejected3 = (
            not _nonempty(self.user_history_snapshot_id)
            or not _valid_scope(self.context_scope)
            or not _finite_timestamp(self.cutoff_at)
            or self.schema_version != RID_USER_CONTEXT_SCHEMA_VERSION
            or not isinstance(request, dict)
            or not _nonempty(request.get("input_snapshot_id"))
            or not isinstance(request.get("user_profile"), dict)
            or not isinstance(request.get("binding"), dict)
            or not _nonempty(request.get("activity_group"))
            or not _nonempty(request.get("threshold_version"))
            or not isinstance(request.get("session_summaries"), list)
            or not isinstance(request.get("decision_context"), dict)
            or request["decision_context"].get("decision_time") != self.cutoff_at
            or not _nonempty(request["decision_context"].get("activity_group_method"))
        )
        if (
            _rejected3
        ):
            raise ValueError("RID user context contract failed")
        for payload in request["session_summaries"]:
            summary = SessionSummaryArtifact.from_payload(payload)
            if (
                summary.context_scope != self.context_scope
                or not summary.natural_revisit_eligible
                or summary.completed_at > self.cutoff_at
            ):
                raise ValueError("RID user context history after cutoff or ineligible")

    def as_payload(self) -> dict[str, Any]:
        return {field.name: getattr(self, field.name) for field in fields(self)}

    @classmethod
    def from_payload(cls, payload: Any) -> RIDUserContextSnapshot:
        return _from_payload(cls, payload, schema_version=RID_USER_CONTEXT_SCHEMA_VERSION)


@dataclass(frozen=True, slots=True)
class RIDTrainingTuple:
    rid_training_tuple_id: str
    corpus_batch_id: str
    user_history_snapshot_id: str
    user_id: str
    context_scope: str
    positive_revisit_pair_id: str
    positive_summary_id: str
    positive_witness_summary_id: str
    intra_interest_negative_summary_ids: tuple[str, ...]
    inter_interest_negative_summary_ids: tuple[str, ...]
    matcher_id: str
    matcher_version: str
    schema_version: str = RID_TRAINING_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _value_items4 = (
            self.rid_training_tuple_id,
            self.corpus_batch_id,
            self.user_history_snapshot_id,
            self.user_id,
            self.positive_revisit_pair_id,
            self.positive_summary_id,
            self.positive_witness_summary_id,
            self.matcher_id,
            self.matcher_version,
            self.schema_version,
        )
        _rejected4 = (
            any(
                not _nonempty(value)
                for value in _value_items4
            )
            or not _valid_scope(self.context_scope)
            or not self.intra_interest_negative_summary_ids
            or not self.inter_interest_negative_summary_ids
            or self.positive_summary_id == self.positive_witness_summary_id
            or self.positive_summary_id
            in {
                *self.intra_interest_negative_summary_ids,
                *self.inter_interest_negative_summary_ids,
            }
            or len(set(self.intra_interest_negative_summary_ids))
            != len(self.intra_interest_negative_summary_ids)
            or len(set(self.inter_interest_negative_summary_ids))
            != len(self.inter_interest_negative_summary_ids)
            or not set(self.intra_interest_negative_summary_ids).isdisjoint(
                self.inter_interest_negative_summary_ids
            )
        )
        if (
            _rejected4
        ):
            raise ValueError("RID training tuple contract failed")

    def as_payload(self) -> dict[str, Any]:
        return {
            "rid_training_tuple_id": self.rid_training_tuple_id,
            "corpus_batch_id": self.corpus_batch_id,
            "user_history_snapshot_id": self.user_history_snapshot_id,
            "user_id": self.user_id,
            "context_scope": self.context_scope,
            "positive_revisit_pair_id": self.positive_revisit_pair_id,
            "positive_summary_id": self.positive_summary_id,
            "positive_witness_summary_id": self.positive_witness_summary_id,
            "intra_interest_negative_summary_ids": list(
                self.intra_interest_negative_summary_ids
            ),
            "inter_interest_negative_summary_ids": list(
                self.inter_interest_negative_summary_ids
            ),
            "matcher_id": self.matcher_id,
            "matcher_version": self.matcher_version,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_payload(cls, payload: Any) -> RIDTrainingTuple:
        return _from_payload(
            cls,
            payload,
            schema_version=RID_TRAINING_SCHEMA_VERSION,
            tuple_fields=(
                "intra_interest_negative_summary_ids",
                "inter_interest_negative_summary_ids",
            ),
        )


def build_rid_training_tuple(
    *,
    rid_training_tuple_id: str,
    corpus_batch_id: str,
    user_history_snapshot_id: str,
    user_id: str,
    positive_pair: RevisitPair,
    intra_interest_negative_summary_ids: tuple[str, ...],
    inter_interest_negative_summary_ids: tuple[str, ...],
) -> RIDTrainingTuple:
    if not positive_pair.natural_eligible or not positive_pair.match_label:
        raise ValueError("RID training requires a natural positive revisit pair")
    _value_items = (
        rid_training_tuple_id,
        corpus_batch_id,
        user_history_snapshot_id,
        user_id,
    )
    _rejected = (
        any(
            not _nonempty(value)
            for value in _value_items
        )
        or not _valid_scope(positive_pair.context_scope)
        or not intra_interest_negative_summary_ids
        or not inter_interest_negative_summary_ids
    )
    if (
        _rejected
    ):
        raise ValueError("RID training tuple contract failed")
    return RIDTrainingTuple(
        rid_training_tuple_id=rid_training_tuple_id,
        corpus_batch_id=corpus_batch_id,
        user_history_snapshot_id=user_history_snapshot_id,
        user_id=user_id,
        context_scope=positive_pair.context_scope,
        positive_revisit_pair_id=positive_pair.revisit_pair_id,
        positive_summary_id=positive_pair.earlier_summary_id,
        positive_witness_summary_id=positive_pair.later_summary_id,
        intra_interest_negative_summary_ids=intra_interest_negative_summary_ids,
        inter_interest_negative_summary_ids=inter_interest_negative_summary_ids,
        matcher_id=positive_pair.matcher_id,
        matcher_version=positive_pair.matcher_version,
    )


@dataclass(frozen=True, slots=True)
class PreferencePair:
    preference_pair_id: str
    context_scope: str
    manifest_id: str
    exposure_id: str
    preferred_card_revision_ids: tuple[str, ...]
    negative_card_revision_id: str
    negative_type: str
    ranker_version: str
    diversity_version: str
    generator_version: str
    created_at: float
    schema_version: str = PREFERENCE_PAIR_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _value_items5 = (
            self.preference_pair_id,
            self.manifest_id,
            self.exposure_id,
            self.negative_card_revision_id,
            self.ranker_version,
            self.diversity_version,
            self.generator_version,
            self.schema_version,
        )
        _rejected5 = (
            any(
                not _nonempty(value)
                for value in _value_items5
            )
            or not _valid_scope(self.context_scope)
            or not self.preferred_card_revision_ids
            or any(
                not _nonempty(revision_id)
                for revision_id in self.preferred_card_revision_ids
            )
            or self.negative_card_revision_id in self.preferred_card_revision_ids
            or self.negative_type
            not in {
                "utility_neg",
                "diversity_neg",
                "joint_failure_neg",
                "not_interested",
            }
            or not _finite_timestamp(self.created_at)
        )
        if (
            _rejected5
        ):
            raise ValueError("preference pair contract failed")

    def as_payload(self) -> dict[str, Any]:
        return {
            "preference_pair_id": self.preference_pair_id,
            "context_scope": self.context_scope,
            "manifest_id": self.manifest_id,
            "exposure_id": self.exposure_id,
            "preferred_card_revision_ids": list(self.preferred_card_revision_ids),
            "negative_card_revision_id": self.negative_card_revision_id,
            "negative_type": self.negative_type,
            "ranker_version": self.ranker_version,
            "diversity_version": self.diversity_version,
            "generator_version": self.generator_version,
            "created_at": self.created_at,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_payload(cls, payload: Any) -> PreferencePair:
        return _from_payload(
            cls,
            payload,
            schema_version=PREFERENCE_PAIR_SCHEMA_VERSION,
            tuple_fields=("preferred_card_revision_ids",),
        )


@dataclass(frozen=True, slots=True)
class TrainingSliceManifest:
    training_slice_manifest_id: str
    window_start: float
    window_end: float
    label_horizon: float
    filters: dict[str, Any]
    context_scope: str
    input_artifact_versions: dict[str, str]
    included_artifact_ids: tuple[str, ...]
    excluded_artifact_reasons: dict[str, str]
    offline_metrics: dict[str, float]
    schema_version: str = TRAINING_SLICE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _rejected6 = (
            not _nonempty(self.training_slice_manifest_id)
            or not _valid_scope(self.context_scope)
            or not _finite_timestamp(self.window_start)
            or not _finite_timestamp(self.window_end)
            or not _finite_timestamp(self.label_horizon)
            or self.window_end <= self.window_start
            or not self.window_start <= self.label_horizon <= self.window_end
            or not self.input_artifact_versions
            or any(
                not _nonempty(key) or not _nonempty(value)
                for key, value in self.input_artifact_versions.items()
            )
        )
        if (
            _rejected6
        ):
            raise ValueError("training slice contract failed")

    def as_payload(self) -> dict[str, Any]:
        return {
            "training_slice_manifest_id": self.training_slice_manifest_id,
            "window_start": self.window_start,
            "window_end": self.window_end,
            "label_horizon": self.label_horizon,
            "filters": dict(self.filters),
            "context_scope": self.context_scope,
            "input_artifact_versions": dict(self.input_artifact_versions),
            "included_artifact_ids": list(self.included_artifact_ids),
            "excluded_artifact_reasons": dict(self.excluded_artifact_reasons),
            "offline_metrics": dict(self.offline_metrics),
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_payload(cls, payload: Any) -> TrainingSliceManifest:
        return _from_payload(
            cls,
            payload,
            schema_version=TRAINING_SLICE_SCHEMA_VERSION,
            tuple_fields=("included_artifact_ids",),
        )


def build_training_slice_manifest(
    *,
    training_slice_manifest_id: str,
    window_start: float,
    window_end: float,
    label_horizon: float,
    filters: dict[str, Any],
    context_scope: str,
    input_artifact_versions: dict[str, str],
    utility_examples: tuple[UtilityTrainingExample, ...],
    offline_metrics: dict[str, float],
    input_artifact_ids: tuple[str, ...] = (),
    input_excluded_artifact_reasons: dict[str, str] | None = None,
) -> TrainingSliceManifest:
    included: list[str] = list(dict.fromkeys(input_artifact_ids))
    excluded: dict[str, str] = dict(input_excluded_artifact_reasons or {})
    for example in utility_examples:
        reason = example.training_exclusion_reason
        if example.context_scope != context_scope:
            reason = "context_scope_mismatch"
        elif not window_start <= example.label_horizon <= label_horizon:
            reason = "example_after_label_horizon"
        elif example.feature_snapshot is None:
            reason = reason or "missing_feature_snapshot"
        elif float(example.feature_snapshot["observed_at"]) > min(
            example.label_horizon,
            label_horizon,
        ):
            reason = "feature_snapshot_after_label_horizon"
        if reason is None:
            included.append(example.utility_example_id)
        else:
            excluded[example.utility_example_id] = reason
    return TrainingSliceManifest(
        training_slice_manifest_id=training_slice_manifest_id,
        window_start=float(window_start),
        window_end=float(window_end),
        label_horizon=float(label_horizon),
        filters=dict(filters),
        context_scope=context_scope,
        input_artifact_versions=dict(input_artifact_versions),
        included_artifact_ids=tuple(included),
        excluded_artifact_reasons=excluded,
        offline_metrics={key: float(value) for key, value in offline_metrics.items()},
    )


@dataclass(frozen=True, slots=True)
class StarterOutcome:
    outcome_id: str
    user_state_snapshot_id: str
    manifest_id: str
    generation_batch_id: str
    exposure_id: str
    exposure_position: int
    card_revision_id: str
    context_scope: str
    project_id: str | None
    shown: bool
    clicked: bool
    not_interested: bool
    started: bool
    started_session_id: str | None
    conversation_depth: int
    future_activity_observed: bool
    future_natural_activity: bool
    future_pa_active_activity: bool
    future_activity_exclusion_reasons: tuple[str, ...]
    outcome_label: str
    exposed_at: float
    clicked_at: float | None
    not_interested_at: float | None
    started_at: float | None
    label_horizon: float
    version_refs: dict[str, str]
    component_request_ids: tuple[tuple[str, str], ...]
    provider_response_ids: tuple[str, ...]
    search_receipt: dict[str, Any] | None
    schema_version: str = STARTER_OUTCOME_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _value_items7 = (
            self.clicked_at,
            self.not_interested_at,
            self.started_at,
        )
        _value_items6 = (
            self.outcome_id,
            self.user_state_snapshot_id,
            self.manifest_id,
            self.generation_batch_id,
            self.exposure_id,
            self.card_revision_id,
            self.outcome_label,
            self.schema_version,
        )
        _rejected7 = (
            any(
                not _nonempty(value)
                for value in _value_items6
            )
            or not _valid_scope(self.context_scope)
            or not self.provider_response_ids
            or any(not _nonempty(value) for value in self.provider_response_ids)
            or len(self.provider_response_ids) != len(set(self.provider_response_ids))
            or (
                self.search_receipt is not None
                and not valid_search_receipt(self.search_receipt)
            )
            or not self.component_request_ids
            or any(
                not _nonempty(component_id) or not _nonempty(request_id)
                for component_id, request_id in self.component_request_ids
            )
            or len({item[0] for item in self.component_request_ids})
            != len(self.component_request_ids)
            or not _plain_int(self.exposure_position)
            or self.exposure_position < 1
            or not _plain_int(self.conversation_depth)
            or self.conversation_depth < 0
            or not _finite_timestamp(self.exposed_at)
            or not _finite_timestamp(self.label_horizon)
            or self.label_horizon < self.exposed_at
            or any(
                value is not None and not _finite_timestamp(value)
                for value in _value_items7
            )
        )
        if (
            _rejected7
        ):
            raise ValueError("starter outcome contract failed")

    def as_payload(self) -> dict[str, Any]:
        return {
            "outcome_id": self.outcome_id,
            "user_state_snapshot_id": self.user_state_snapshot_id,
            "manifest_id": self.manifest_id,
            "generation_batch_id": self.generation_batch_id,
            "exposure_id": self.exposure_id,
            "exposure_position": self.exposure_position,
            "card_revision_id": self.card_revision_id,
            "context_scope": self.context_scope,
            "project_id": self.project_id,
            "shown": self.shown,
            "clicked": self.clicked,
            "not_interested": self.not_interested,
            "started": self.started,
            "started_session_id": self.started_session_id,
            "conversation_depth": self.conversation_depth,
            "future_activity_observed": self.future_activity_observed,
            "future_natural_activity": self.future_natural_activity,
            "future_pa_active_activity": self.future_pa_active_activity,
            "future_activity_exclusion_reasons": list(
                self.future_activity_exclusion_reasons
            ),
            "outcome_label": self.outcome_label,
            "exposed_at": self.exposed_at,
            "clicked_at": self.clicked_at,
            "not_interested_at": self.not_interested_at,
            "started_at": self.started_at,
            "label_horizon": self.label_horizon,
            "version_refs": dict(self.version_refs),
            "component_request_ids": [
                list(item) for item in self.component_request_ids
            ],
            "provider_response_ids": list(self.provider_response_ids),
            "search_receipt": self.search_receipt,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_payload(cls, payload: Any) -> StarterOutcome:
        return _from_payload(
            cls,
            payload,
            schema_version=STARTER_OUTCOME_SCHEMA_VERSION,
            tuple_fields=("future_activity_exclusion_reasons", "provider_response_ids"),
            tuple_pair_fields=("component_request_ids",),
        )


@dataclass(frozen=True, slots=True)
class UtilityTrainingExample:
    utility_example_id: str
    outcome_id: str
    user_state_snapshot_id: str
    context_scope: str
    label_horizon: float
    labels: dict[str, Any]
    natural_lane_eligible: bool
    pa_active_eligible: bool
    natural_lane_exclusion_reasons: tuple[str, ...]
    training_exclusion_reason: str | None
    feature_snapshot: dict[str, Any] | None
    version_refs: dict[str, str]
    schema_version: str = UTILITY_TRAINING_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _value_items8 = (
            self.utility_example_id,
            self.outcome_id,
            self.user_state_snapshot_id,
            self.schema_version,
        )
        _rejected8 = (
            any(
                not _nonempty(value)
                for value in _value_items8
            )
            or not _valid_scope(self.context_scope)
            or not _finite_timestamp(self.label_horizon)
            or not isinstance(self.labels, dict)
            or (
                self.feature_snapshot is None
                and not _nonempty(self.training_exclusion_reason)
            )
            or (
                self.feature_snapshot is not None
                and self.training_exclusion_reason is not None
            )
        )
        if (
            _rejected8
        ):
            raise ValueError("utility training example contract failed")

    def as_payload(self) -> dict[str, Any]:
        return {
            "utility_example_id": self.utility_example_id,
            "outcome_id": self.outcome_id,
            "user_state_snapshot_id": self.user_state_snapshot_id,
            "context_scope": self.context_scope,
            "label_horizon": self.label_horizon,
            "labels": dict(self.labels),
            "natural_lane_eligible": self.natural_lane_eligible,
            "pa_active_eligible": self.pa_active_eligible,
            "natural_lane_exclusion_reasons": list(self.natural_lane_exclusion_reasons),
            "training_exclusion_reason": self.training_exclusion_reason,
            "feature_snapshot": (
                dict(self.feature_snapshot)
                if self.feature_snapshot is not None
                else None
            ),
            "version_refs": dict(self.version_refs),
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_payload(cls, payload: Any) -> UtilityTrainingExample:
        return _from_payload(
            cls,
            payload,
            schema_version=UTILITY_TRAINING_SCHEMA_VERSION,
            tuple_fields=("natural_lane_exclusion_reasons",),
        )


def build_utility_training_example(
    outcome: StarterOutcome,
    *,
    feature_snapshot: dict[str, Any] | None,
) -> UtilityTrainingExample:
    """Keep product labels while excluding leaked or mismatched features."""

    admitted_snapshot: dict[str, Any] | None = None
    exclusion_reason: str | None = None
    if not isinstance(feature_snapshot, dict):
        exclusion_reason = "missing_feature_snapshot"
    elif feature_snapshot.get("snapshot_id") != outcome.user_state_snapshot_id:
        exclusion_reason = "feature_snapshot_id_mismatch"
    elif not _finite_timestamp(feature_snapshot.get("observed_at")):
        exclusion_reason = "feature_snapshot_time_invalid"
    elif float(feature_snapshot["observed_at"]) > outcome.label_horizon:
        exclusion_reason = "feature_snapshot_after_label_horizon"
    elif not _nonempty(feature_snapshot.get("version")) or not isinstance(
        feature_snapshot.get("features"), dict
    ):
        exclusion_reason = "feature_snapshot_contract_failed"
    else:
        admitted_snapshot = {
            "snapshot_id": str(feature_snapshot["snapshot_id"]),
            "observed_at": float(feature_snapshot["observed_at"]),
            "version": str(feature_snapshot["version"]),
            "features": dict(feature_snapshot["features"]),
        }
    labels = {
        "shown": outcome.shown,
        "clicked": outcome.clicked,
        "not_interested": outcome.not_interested,
        "started": outcome.started,
        "conversation_depth": outcome.conversation_depth,
        "future_activity_observed": outcome.future_activity_observed,
        "future_natural_activity": outcome.future_natural_activity,
        "future_pa_active_activity": outcome.future_pa_active_activity,
        "outcome_label": outcome.outcome_label,
    }
    return UtilityTrainingExample(
        utility_example_id=f"utility:{outcome.outcome_id}",
        outcome_id=outcome.outcome_id,
        user_state_snapshot_id=outcome.user_state_snapshot_id,
        context_scope=outcome.context_scope,
        label_horizon=outcome.label_horizon,
        labels=labels,
        natural_lane_eligible=outcome.future_natural_activity,
        pa_active_eligible=outcome.future_pa_active_activity,
        natural_lane_exclusion_reasons=(outcome.future_activity_exclusion_reasons),
        training_exclusion_reason=exclusion_reason,
        feature_snapshot=admitted_snapshot,
        version_refs=dict(outcome.version_refs),
    )


def artifact_from_payload(payload: Any) -> Any:
    """Strict version dispatch for persisted v6 outcome-plane artifacts."""

    if not isinstance(payload, dict):
        raise ValueError("artifact payload must be an object")
    schema_version = payload.get("schema_version")
    if schema_version == SESSION_SUMMARY_SCHEMA_VERSION:
        return SessionSummaryArtifact.from_payload(payload)
    if schema_version == REVISIT_PAIR_SCHEMA_VERSION:
        return RevisitPair.from_payload(payload)
    if schema_version == RID_TRAINING_SCHEMA_VERSION:
        return RIDTrainingTuple.from_payload(payload)
    if schema_version == RID_USER_CONTEXT_SCHEMA_VERSION:
        return RIDUserContextSnapshot.from_payload(payload)
    if schema_version == STARTER_OUTCOME_SCHEMA_VERSION:
        return StarterOutcome.from_payload(payload)
    if schema_version == UTILITY_TRAINING_SCHEMA_VERSION:
        return UtilityTrainingExample.from_payload(payload)
    if schema_version == PREFERENCE_PAIR_SCHEMA_VERSION:
        return PreferencePair.from_payload(payload)
    if schema_version == TRAINING_SLICE_SCHEMA_VERSION:
        return TrainingSliceManifest.from_payload(payload)
    raise ValueError("unsupported artifact schema_version")


def _artifact_storage_fields(artifact: Any) -> tuple[str, str, str, str]:
    if isinstance(artifact, SessionSummaryArtifact):
        return (
            "session_summary",
            artifact.session_summary_id,
            artifact.session_id,
            artifact.context_scope,
        )
    if isinstance(artifact, RevisitPair):
        return (
            "revisit_pair",
            artifact.revisit_pair_id,
            artifact.later_session_id,
            artifact.context_scope,
        )
    if isinstance(artifact, RIDTrainingTuple):
        return (
            "rid_training_tuple",
            artifact.rid_training_tuple_id,
            artifact.user_history_snapshot_id,
            artifact.context_scope,
        )
    if isinstance(artifact, RIDUserContextSnapshot):
        return (
            "rid_user_context_snapshot",
            artifact.user_history_snapshot_id,
            artifact.user_history_snapshot_id,
            artifact.context_scope,
        )
    if isinstance(artifact, StarterOutcome):
        return (
            "starter_outcome",
            artifact.outcome_id,
            artifact.started_session_id or artifact.exposure_id,
            artifact.exposure_id,
        )
    if isinstance(artifact, UtilityTrainingExample):
        return (
            "utility_training_example",
            artifact.utility_example_id,
            artifact.user_state_snapshot_id,
            artifact.context_scope,
        )
    if isinstance(artifact, PreferencePair):
        return (
            "preference_pair",
            artifact.preference_pair_id,
            "",
            artifact.exposure_id,
        )
    if isinstance(artifact, TrainingSliceManifest):
        return (
            "training_slice_manifest",
            artifact.training_slice_manifest_id,
            "",
            artifact.context_scope,
        )
    raise ValueError("unsupported outcome artifact type")


def outcome_artifact_identity(artifact: Any) -> tuple[str, str]:
    event_type, artifact_id, _, _ = _artifact_storage_fields(artifact)
    return event_type, artifact_id


def persist_outcome_artifact(
    store: TopicStarterStore,
    artifact: Any,
    *,
    created_at: float,
) -> bool:
    """Append one versioned artifact to the existing bounded event ledger."""

    if not _finite_timestamp(created_at):
        return False
    event_type, artifact_id, session_id, slate_id = _artifact_storage_fields(artifact)
    return store.record_event(
        {
            "session_id": session_id,
            "slate_id": slate_id,
            "event_type": event_type,
            "topic_id": artifact_id,
            "policy_id": POLICY_ID,
            "selection_mode": "training_data_plane",
            "propensity_reason": "not_applicable",
            "payload": artifact.as_payload(),
            "created_at": created_at,
        }
    )


def _exact_exposure(
    store: TopicStarterStore,
    *,
    manifest_id: str,
    exposure_id: str,
) -> dict[str, Any] | None:
    rows = store.query_events(
        event_type="dynamic_exposure",
        slate_id=exposure_id,
        policy_id=POLICY_ID,
        limit=2,
        newest_first=True,
    )
    matches: list[dict[str, Any]] = []
    for row in rows:
        payload = row.get("payload")
        if row.get("topic_id") != manifest_id:
            continue
        if not isinstance(payload, dict):
            continue
        if payload.get("manifest_id") != manifest_id:
            continue
        matches.append(row)
    return matches[0] if len(matches) == 1 else None


def _version_refs(card: dict[str, Any]) -> dict[str, str]:
    ref_fields = (
        "profile_semantic_version",
        "short_intent_version",
        "generation_config_version",
        "corpus_version",
        "document_snapshot_version",
        "retrieval_config_version",
        "evidence_extractor_version",
        "list_search_version",
    )
    return {
        field: str(card[field])
        for field in ref_fields
        if card.get(field) is not None and str(card[field]).strip()
    }


def _provider_response_ids(card: dict[str, Any]) -> tuple[str, ...]:
    return card_provider_response_ids(card)


def _component_request_ids(card: dict[str, Any]) -> tuple[tuple[str, str], ...]:
    invocations = card.get("component_invocation_receipts")
    if not isinstance(invocations, list):
        return ()
    result: list[tuple[str, str]] = []
    for invocation in invocations:
        if not isinstance(invocation, dict):
            return ()
        component_id = invocation.get("component_id")
        request_id = invocation.get("request_id")
        if not _nonempty(component_id) or not _nonempty(request_id):
            return ()
        result.append((str(component_id), str(request_id)))
    return tuple(result) if len({item[0] for item in result}) == len(result) else ()


def _deduplicated_text(values: list[Any]) -> list[str]:
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def _negative_preference(
    card: dict[str, Any],
    *,
    context_scope: str,
    project_id: str | None,
    dismissed_at: float,
) -> dict[str, Any]:
    source_ids = list(card.get("source_memory_unit_ids") or [])
    preference = {
        "context_scope": context_scope,
        "project_id": project_id,
        "direction": str(card.get("intent") or "").strip(),
        "source_id": str(source_ids[0]) if source_ids else "",
        "aliases": _deduplicated_text([card.get("title"), card.get("prompt")]),
        "anchors": _deduplicated_text(
            [card.get("identity_anchor"), *(card.get("context_anchors") or [])]
        ),
        "reason": "explicit_not_interested",
        "dismissed_at": dismissed_at,
    }
    if card_is_news(card):
        # The news event itself is never offered again, whatever its direction's tier.
        preference["news_source_url"] = str(card["evidence_bundle"].get("source_url") or "")
        if card.get("news_match"):
            preference["news_match"] = card["news_match"]
    if context_scope == GLOBAL_NEW_CHAT_SCOPE and card.get("direction"):
        preference["direction_anchor"] = str(card["direction"]).strip()
        preference["direction"] = preference["direction_anchor"]
        preference["anchors"] = [preference["direction_anchor"]]
    return preference


def _dismissal_record(
    store: TopicStarterStore,
    state: dict[str, Any],
    card: dict[str, Any],
    *,
    manifest_id: str,
    context_scope: str,
    project_id: str | None,
    dismissed_at: float,
) -> dict[str, Any]:
    """A dismissal of this card under this state, with the conversations of its direction."""

    preference = _negative_preference(
        card,
        context_scope=context_scope,
        project_id=project_id,
        dismissed_at=dismissed_at,
    )
    # The direction's conversations as they stand now; a rebuilt profile finds the
    # direction again in the unit that holds most of them. A card served from a pool
    # written before the profile was rebuilt names its unit's conversations itself.
    sessions = memory_sessions_by_unit(state).get(preference["source_id"]) or frozenset(
        str(locator[0]) for locator in card.get("source_locators") or () if preference["source_id"] and locator
    )
    if context_scope == GLOBAL_NEW_CHAT_SCOPE and (card_is_news(card) or not preference["source_id"]):
        manifests = store.query_events(
            event_type="starter_slate_manifest", slate_id=manifest_id, policy_id=POLICY_ID,
            limit=1, newest_first=True,
        )
        manifest = manifests[0].get("payload") if manifests else None
        manifest = manifest if isinstance(manifest, dict) else {}
        # A news card's event is the hot-list term it was made for, whichever site reported it.
        news_event = (
            news_card_event(store, manifest, card.get("card_id")) if card_is_news(card) else None
        )
        if news_event:
            preference["news_event"] = news_event
        if not preference["source_id"]:
            # A global card built from a conversation names that conversation's events
            # instead; a project's cards all continue its own conversations.
            interest_id = str(card.get("interest_summary_id") or "")
            events = interest_evidence_ids(manifest).get(interest_id)
            if events:
                preference["source_event_ids"] = list(events)
                sessions = interest_session_ids(manifest).get(interest_id, frozenset())
    preference["evidence_session_ids"] = sorted(sessions)
    return preference


def _card_dismissal_record(
    store: TopicStarterStore,
    card: dict[str, Any],
    *,
    manifest_id: str,
    context_scope: str,
    project_id: str | None,
    dismissed_at: float,
) -> dict[str, Any]:
    """A card's dismissal: that card and its question, and a news card's event.

    It names no direction, unit or source conversation, so the card's interest
    stays with RID, generation and the reader.
    """

    record: dict[str, Any] = {
        "context_scope": context_scope,
        "project_id": project_id,
        "dismissal_kind": "card",
        "card_id": str(card.get("card_id") or ""),
        "aliases": _deduplicated_text([card.get("title"), card.get("prompt")]),
        "reason": "explicit_not_interested",
        "dismissed_at": dismissed_at,
    }
    if card_is_news(card):
        record["news_source_url"] = str(card["evidence_bundle"].get("source_url") or "")
        manifests = store.query_events(
            event_type="starter_slate_manifest", slate_id=manifest_id, policy_id=POLICY_ID,
            limit=1, newest_first=True,
        )
        manifest = manifests[0].get("payload") if manifests else None
        news_event = news_card_event(
            store, manifest if isinstance(manifest, dict) else {}, card.get("card_id")
        )
        if news_event:
            record["news_event"] = news_event
    return record


_WRITTEN_DISMISSAL_FIELDS = ("context_scope", "project_id", "direction", "source_id", "aliases", "anchors")
_DISMISSAL_MIGRATION_EVENT_TYPE = "dismissal_records_migrated"


def migrate_dismissal_records(store: TopicStarterStore, *, now: float) -> None:
    """Give dismissals written before strikes were counted their time and conversations, once.

    Each ledger ``not_interested`` event is matched to the record it wrote by the
    same card; its time is the dismissal's, and the conversations are its
    direction's in the profile then in force. Earlier writes kept one record for
    the same card dismissed twice: the further dismissals are appended, each a
    strike. A record no event matches is left as written, in force as before.
    Records only gain fields or follow, so pools keep their versions. Every
    dismissal written since carries its time, so the ledger keeps a mark of the
    run and later starts read nothing more.
    """

    if store.query_events(event_type=_DISMISSAL_MIGRATION_EVENT_TYPE, policy_id=POLICY_ID, limit=1):
        return
    _time_dismissal_records(store, now=now)
    store.record_event({"event_type": _DISMISSAL_MIGRATION_EVENT_TYPE, "policy_id": POLICY_ID, "created_at": now})


def _time_dismissal_records(store: TopicStarterStore, *, now: float) -> None:
    state = load_latest_history_state(store)
    sidecar = state.get("memory_sidecar") if state else None
    if not isinstance(sidecar, dict):
        return
    preferences = [dict(item) for item in sidecar.get("negative_preferences") or [] if isinstance(item, dict)]
    untimed = [
        index for index, item in enumerate(preferences)
        if item.get("reason", "explicit_not_interested") == "explicit_not_interested" and "dismissed_at" not in item
    ]
    if not untimed:
        return
    claimed: set[int] = set()
    repeated: list[dict[str, Any]] = []
    for event in store.query_events(event_type="not_interested", policy_id=POLICY_ID, limit=100_000):
        payload = event.get("payload")
        if not isinstance(payload, dict):
            continue
        manifest_id = str(payload.get("manifest_id") or "")
        cards = store.query_events(
            event_type="starter_card", slate_id=manifest_id, topic_id=str(payload.get("card_revision_id") or ""),
            policy_id=POLICY_ID, limit=1, newest_first=True,
        )
        card = cards[0].get("payload") if cards else None
        at = float(event.get("created_at") or 0.0)
        if not isinstance(card, dict):
            continue
        written = _dismissal_record(
            store, load_history_state_at(store, at) or {}, card, manifest_id=manifest_id,
            context_scope=str(payload.get("context_scope") or ""), project_id=payload.get("project_id"),
            dismissed_at=at,
        )
        matches = [
            index for index in untimed
            if all(preferences[index].get(field) == written[field] for field in _WRITTEN_DISMISSAL_FIELDS)
        ]
        if not matches:
            continue
        timed = {
            key: written[key] for key in ("dismissed_at", "evidence_session_ids", "news_source_url") if key in written
        }
        unclaimed = [index for index in matches if index not in claimed]
        if unclaimed:
            claimed.add(unclaimed[0])
            preferences[unclaimed[0]].update(timed)
        else:
            repeated.append({**preferences[matches[0]], **timed})
    if not claimed:
        return
    preferences.extend(repeated)
    sidecar["negative_preferences"] = preferences
    sidecar["negative_preference_version"] = max(
        int(sidecar.get("negative_preference_version") or 0), len(preferences)
    )
    save_history_state(store, state, now=now)


def _shown_card(
    store: TopicStarterStore,
    *,
    manifest_id: str,
    generation_batch_id: str,
    exposure_id: str,
    card_revision_id: str,
    context_scope: str,
    project_id: str | None,
) -> tuple[dict[str, Any], dict[str, Any], int] | None:
    exposure = _exact_exposure(
        store,
        manifest_id=manifest_id,
        exposure_id=exposure_id,
    )
    if exposure is None:
        return None
    payload = exposure["payload"]
    revision_ids = payload.get("card_revision_ids")
    if payload.get("generation_batch_id") != generation_batch_id:
        return None
    if payload.get("context_scope") != context_scope:
        return None
    if payload.get("project_id") != project_id:
        return None
    if not isinstance(revision_ids, list):
        return None
    if card_revision_id not in revision_ids:
        return None
    card = find_exact_terminal_card(
        store,
        manifest_id=manifest_id,
        card_revision_id=card_revision_id,
        generation_batch_id=generation_batch_id,
        context_scope=context_scope,
        project_id=project_id,
    )
    if card is None:
        return None
    search_receipt = card_search_receipt(card)
    if card_uses_host_evidence(card) or is_cold_start_card(card):
        if card.get("search_receipt") is not None:
            return None
    elif search_receipt is None:
        return None
    exposure_search_receipts = payload.get("search_receipts_by_card_revision")
    if (
        not isinstance(exposure_search_receipts, dict)
        or exposure_search_receipts.get(card_revision_id) != search_receipt
    ):
        return None
    return exposure, card, revision_ids.index(card_revision_id) + 1


# Each undo names the event it undoes; a dismissal or pause stands until one withdraws it.
UNDONE_EVENT_TYPES = {
    "not_interested_undone": "not_interested",
    "interest_pause_undone": "interest_paused",
}
TERMINAL_INTERACTION_EVENT_TYPES = frozenset({"topic_click", "not_interested", "interest_paused", *UNDONE_EVENT_TYPES})


def _standing_record(
    preferences: list[Any], event_type: str, *, exposure_id: str, card_revision_id: str,
) -> dict[str, Any] | None:
    """The newest dismissal or pause of this shown card that no withdrawal names."""

    withdrawn = {record.get("withdraws") for record in preferences
                 if isinstance(record, dict) and record.get("reason") == "withdrawn"}
    for record in reversed(preferences):
        if not isinstance(record, dict):
            continue
        if record.get("exposure_id") != exposure_id:
            continue
        if record.get("card_revision_id") != card_revision_id:
            continue
        matches_kind = (record.get("dismissal_kind") == "pause") is (
            event_type == "interest_paused"
        )
        if not matches_kind:
            continue
        if record.get("record_id") in withdrawn:
            continue
        return record
    return None


def record_terminal_interaction(
    store: TopicStarterStore,
    *,
    event_type: str,
    manifest_id: str,
    generation_batch_id: str,
    exposure_id: str,
    card_revision_id: str,
    context_scope: str,
    project_id: str | None,
    created_at: float,
) -> bool:
    """Record a click, dismissal, interest pause or undo only for an exact visible revision.

    A dismissal takes off that card. Only a global regular card's interest can
    be paused. An undo appends a withdrawal of the record it undoes, so the card
    may be dismissed or its interest paused again; each of them is a ledger event
    of its own.
    """

    if event_type not in TERMINAL_INTERACTION_EVENT_TYPES or not _finite_timestamp(
        created_at
    ):
        return False
    joined = _shown_card(
        store,
        manifest_id=manifest_id,
        generation_batch_id=generation_batch_id,
        exposure_id=exposure_id,
        card_revision_id=card_revision_id,
        context_scope=context_scope,
        project_id=project_id,
    )
    if joined is None:
        return False
    exposure, card, position = joined
    if created_at < float(exposure.get("created_at") or 0):
        return False
    provider_response_ids = _provider_response_ids(card)
    component_request_ids = _component_request_ids(card)
    if not is_cold_start_card(card) and (
        not provider_response_ids or not component_request_ids
    ):
        return False
    event_payload: dict[str, Any] = {
        "manifest_id": manifest_id,
        "generation_batch_id": generation_batch_id,
        "exposure_id": exposure_id,
        "exposure_position": position,
        "card_revision_id": card_revision_id,
        "card_id": card.get("card_id"),
        "source_starter_id": card.get("source_starter_id"),
        "context_scope": context_scope,
        "project_id": project_id,
        "user_state_snapshot_id": card.get("generation_input_snapshot_id"),
        "version_refs": _version_refs(card),
        "component_request_ids": [list(item) for item in component_request_ids],
        "provider_response_ids": list(provider_response_ids),
        "search_receipt": card_search_receipt(card),
    }
    event: dict[str, Any] = {
        "session_id": str(exposure.get("session_id") or ""),
        "slate_id": exposure_id,
        "event_type": event_type,
        "topic_id": card_revision_id,
        "profile_version": card.get("profile_semantic_version"),
        "policy_id": POLICY_ID,
        "selection_mode": "unknown",
        "propensity_reason": "not_estimated",
        "payload": event_payload,
        "created_at": created_at,
    }
    if event_type == "topic_click":
        return store.record_event(event)
    global_regular = context_scope == GLOBAL_NEW_CHAT_SCOPE and not card_is_news(card)
    if event_type == "interest_paused" and not global_regular:
        return False
    # A dismissal recorded before dismissals could be undone stands for good.
    _row_items = store.query_events(
        event_type="not_interested", slate_id=exposure_id, topic_id=card_revision_id,
        policy_id=POLICY_ID, limit=1,
    )
    if event_type in {"not_interested", "not_interested_undone"} and any(
        "dismissal_record_id" not in row["payload"]
        for row in _row_items
    ):
        return False
    previous = load_latest_history_state(store)
    state = json.loads(json.dumps(previous)) if previous else empty_history_state()
    sidecar_value = state.get("memory_sidecar")
    sidecar = sidecar_value if isinstance(sidecar_value, dict) else {}
    state["memory_sidecar"] = sidecar
    preferences = list(sidecar.get("negative_preferences") or [])
    undone_type = UNDONE_EVENT_TYPES.get(event_type)
    standing = _standing_record(
        preferences, undone_type or event_type, exposure_id=exposure_id, card_revision_id=card_revision_id,
    )
    # A dismissal or pause already standing is not recorded twice; an undo needs one standing.
    if (standing is None) is (undone_type is not None):
        return False
    if standing is not None:
        undone_record_id = standing["record_id"]
        record: dict[str, Any] = {
            "context_scope": context_scope,
            "project_id": project_id,
            "reason": "withdrawn",
            "withdraws": undone_record_id,
            "withdrawn_at": created_at,
        }
        event_payload["withdrawn_record_id"] = undone_record_id
        event["feedback_record_id"] = undone_record_id
    else:
        if event_type == "not_interested":
            record = _card_dismissal_record(
                store, card, manifest_id=manifest_id, context_scope=context_scope,
                project_id=project_id, dismissed_at=created_at,
            )
        else:
            # A pause holds the card's direction and the interest the card came from.
            record = _dismissal_record(
                store, state, card, manifest_id=manifest_id, context_scope=context_scope,
                project_id=project_id, dismissed_at=created_at,
            )
            record["dismissal_kind"] = "pause"
        record.update({
            "record_id": f"dismissal-{uuid.uuid4().hex}",
            "exposure_id": exposure_id,
            "card_revision_id": card_revision_id,
        })
        event_payload["dismissal_record_id"] = record["record_id"]
        event["feedback_record_id"] = record["record_id"]
        if event_type == "not_interested":
            event_payload["reason"] = "explicit_not_interested"
    preferences.append(record)
    sidecar["negative_preferences"] = preferences
    sidecar.setdefault("explicit_corrections", [])
    negative_version = int(sidecar.get("negative_preference_version") or 0) + 1
    sidecar["negative_preference_version"] = negative_version
    event_payload["negative_preference_version"] = negative_version
    save_history_state(store, state, events=[event], now=created_at)
    return True


def _exact_event(
    store: TopicStarterStore,
    *,
    event_type: str,
    manifest_id: str,
    exposure_id: str,
    card_revision_id: str,
    newest_first: bool = False,
) -> dict[str, Any] | None:
    rows = store.query_events(
        event_type=event_type,
        slate_id=exposure_id,
        topic_id=card_revision_id,
        policy_id=POLICY_ID,
        limit=8,
        newest_first=newest_first,
    )
    matches: list[dict[str, Any]] = []
    for row in rows:
        payload = row.get("payload")
        if not isinstance(payload, dict):
            continue
        if payload.get("manifest_id") != manifest_id:
            continue
        if payload.get("exposure_id") != exposure_id:
            continue
        if payload.get("card_revision_id") != card_revision_id:
            continue
        matches.append(row)
    return matches[0] if matches else None


def record_terminal_session_start(
    store: TopicStarterStore,
    *,
    manifest_id: str,
    generation_batch_id: str,
    exposure_id: str,
    card_revision_id: str,
    context_scope: str,
    project_id: str | None,
    started_session_id: str,
    created_at: float,
) -> bool:
    """Bind a clicked revision to the exact conversation it started."""

    if not _nonempty(started_session_id) or not _finite_timestamp(created_at):
        return False
    joined = _shown_card(
        store,
        manifest_id=manifest_id,
        generation_batch_id=generation_batch_id,
        exposure_id=exposure_id,
        card_revision_id=card_revision_id,
        context_scope=context_scope,
        project_id=project_id,
    )
    click = _exact_event(
        store,
        event_type="topic_click",
        manifest_id=manifest_id,
        exposure_id=exposure_id,
        card_revision_id=card_revision_id,
    )
    existing_start = _exact_event(
        store,
        event_type="session_bound",
        manifest_id=manifest_id,
        exposure_id=exposure_id,
        card_revision_id=card_revision_id,
    )
    if joined is None:
        return False
    if click is None:
        return False
    if existing_start is not None:
        return False
    if created_at < float(click["created_at"]):
        return False
    _, card, position = joined
    return store.record_event(
        {
            "session_id": started_session_id,
            "slate_id": exposure_id,
            "event_type": "session_bound",
            "topic_id": card_revision_id,
            "profile_version": card.get("profile_semantic_version"),
            "policy_id": POLICY_ID,
            "selection_mode": "unknown",
            "propensity_reason": "not_estimated",
            "payload": {
                "manifest_id": manifest_id,
                "generation_batch_id": generation_batch_id,
                "exposure_id": exposure_id,
                "exposure_position": position,
                "card_revision_id": card_revision_id,
                "context_scope": context_scope,
                "project_id": project_id,
                "started_session_id": started_session_id,
                "search_receipt": card_search_receipt(card),
            },
            "created_at": created_at,
        }
    )


def record_terminal_conversation_depth(
    store: TopicStarterStore,
    *,
    manifest_id: str,
    exposure_id: str,
    card_revision_id: str,
    started_session_id: str,
    conversation_depth: int,
    created_at: float,
) -> bool:
    """Record the final observed conversation depth for one started session."""

    if not _nonempty(started_session_id):
        return False
    if not _plain_int(conversation_depth):
        return False
    if conversation_depth < 0:
        return False
    if not _finite_timestamp(created_at):
        return False
    start = _exact_event(
        store,
        event_type="session_bound",
        manifest_id=manifest_id,
        exposure_id=exposure_id,
        card_revision_id=card_revision_id,
    )
    if start is None:
        return False
    if start.get("session_id") != started_session_id:
        return False
    if created_at < float(start["created_at"]):
        return False
    start_payload = start["payload"]
    return store.record_event(
        {
            "session_id": started_session_id,
            "slate_id": exposure_id,
            "event_type": "conversation_depth",
            "topic_id": card_revision_id,
            "policy_id": POLICY_ID,
            "selection_mode": "unknown",
            "propensity_reason": "not_estimated",
            "payload": {
                **start_payload,
                "conversation_depth": conversation_depth,
            },
            "created_at": created_at,
        }
    )


def _natural_exclusion_reason(
    *,
    input_origin: str,
    assistance_origin: str,
    started_session_id: str,
    later_session_id: str,
) -> str | None:
    if input_origin != "manual_free_text":
        return "ineligible_input_origin"
    if assistance_origin != "none":
        return "assisted_input_excluded"
    if later_session_id == started_session_id:
        return "same_session_not_revisit"
    return None


def record_terminal_future_activity(
    store: TopicStarterStore,
    *,
    manifest_id: str,
    exposure_id: str,
    card_revision_id: str,
    started_session_id: str,
    later_session_id: str,
    source_event_id: str,
    source_locator: str,
    input_origin: str,
    assistance_origin: str,
    context_scope: str,
    created_at: float,
) -> bool:
    """Retain later activity while marking natural/PA eligibility explicitly."""

    _value_items2 = (
        started_session_id,
        later_session_id,
        source_event_id,
        source_locator,
        input_origin,
        assistance_origin,
        context_scope,
    )
    if any(
        not _nonempty(value)
        for value in _value_items2
    ) or not _finite_timestamp(created_at):
        return False
    start = _exact_event(
        store,
        event_type="session_bound",
        manifest_id=manifest_id,
        exposure_id=exposure_id,
        card_revision_id=card_revision_id,
    )
    if start is None:
        return False
    if start.get("session_id") != started_session_id:
        return False
    if start["payload"].get("context_scope") != context_scope:
        return False
    if created_at < float(start["created_at"]):
        return False
    reason = _natural_exclusion_reason(
        input_origin=input_origin,
        assistance_origin=assistance_origin,
        started_session_id=started_session_id,
        later_session_id=later_session_id,
    )
    return store.record_event(
        {
            "session_id": later_session_id,
            "slate_id": exposure_id,
            "event_type": "future_activity",
            "topic_id": card_revision_id,
            "policy_id": POLICY_ID,
            "selection_mode": "unknown",
            "propensity_reason": "not_estimated",
            "payload": {
                **start["payload"],
                "later_session_id": later_session_id,
                "source_event_id": source_event_id,
                "source_locator": source_locator,
                "input_origin": input_origin,
                "assistance_origin": assistance_origin,
                "natural_revisit_eligible": reason is None,
                "pa_active_eligible": reason is None,
                "exclusion_reason": reason,
            },
            "created_at": created_at,
        }
    )


def _outcome_events(
    store: TopicStarterStore,
    *,
    event_type: str,
    exposure_id: str,
    manifest_id: str,
    exposed_at: float,
    label_horizon: float,
) -> list[dict[str, Any]]:
    _event_items = store.query_events(
        event_type=event_type,
        slate_id=exposure_id,
        policy_id=POLICY_ID,
        limit=64,
    )
    matches: list[dict[str, Any]] = []
    for event in _event_items:
        created_at = float(event.get("created_at") or 0)
        if not exposed_at <= created_at <= label_horizon:
            continue
        payload = event.get("payload")
        if not isinstance(payload, dict):
            continue
        if payload.get("manifest_id") != manifest_id:
            continue
        if payload.get("exposure_id") != exposure_id:
            continue
        matches.append(event)
    return matches


def join_starter_outcomes(
    store: TopicStarterStore,
    *,
    manifest_id: str,
    exposure_id: str,
    label_horizon: float,
    deep_conversation_min_depth: int = 4,
) -> tuple[StarterOutcome, ...]:
    """Join one exposure to later signals without title/text matching."""

    if not _nonempty(manifest_id):
        return ()
    if not _nonempty(exposure_id):
        return ()
    if not _finite_timestamp(label_horizon):
        return ()
    if not _plain_int(deep_conversation_min_depth):
        return ()
    if deep_conversation_min_depth < 1:
        return ()
    exposure = _exact_exposure(
        store,
        manifest_id=manifest_id,
        exposure_id=exposure_id,
    )
    if exposure is None or float(exposure.get("created_at") or 0) > label_horizon:
        return ()
    payload = exposure["payload"]
    revision_ids = payload.get("card_revision_ids")
    exposure_search_receipts = payload.get("search_receipts_by_card_revision")
    if not isinstance(revision_ids, list):
        return ()
    if not revision_ids:
        return ()
    if not isinstance(exposure_search_receipts, dict):
        return ()
    if set(exposure_search_receipts) != set(revision_ids):
        return ()
    context_scope = str(payload.get("context_scope") or "")
    project_id = payload.get("project_id")
    batch_id = str(payload.get("generation_batch_id") or "")
    exposed_at = float(exposure.get("created_at") or 0)
    _event_type_items = (
        "topic_click",
        "not_interested",
        "not_interested_undone",
        "post_exposure_direct",
        "page_left",
        "session_bound",
        "conversation_depth",
        "future_activity",
    )
    events_by_type = {
        event_type: _outcome_events(
            store,
            event_type=event_type,
            exposure_id=exposure_id,
            manifest_id=manifest_id,
            exposed_at=exposed_at,
            label_horizon=float(label_horizon),
        )
        for event_type in _event_type_items
    }
    direct_input = bool(events_by_type["post_exposure_direct"])
    passive_ignored = bool(events_by_type["page_left"])

    result: list[StarterOutcome] = []
    for position, revision_id_value in enumerate(revision_ids, start=1):
        revision_id = str(revision_id_value or "")
        card = find_exact_terminal_card(
            store,
            manifest_id=manifest_id,
            card_revision_id=revision_id,
            generation_batch_id=batch_id,
            context_scope=context_scope,
            project_id=project_id,
        )
        if card is None:
            return ()
        provider_response_ids = _provider_response_ids(card)
        component_request_ids = _component_request_ids(card)
        if not provider_response_ids or not component_request_ids:
            return ()
        search_receipt = card_search_receipt(card)
        if (
            (not card_uses_host_evidence(card) and search_receipt is None)
            or exposure_search_receipts.get(revision_id) != search_receipt
        ):
            return ()
        card_events: dict[str, list[dict[str, Any]]] = {}
        for event_type, events in events_by_type.items():
            type_events: list[dict[str, Any]] = []
            for event in events:
                if event.get("topic_id") != revision_id:
                    continue
                payload = event.get("payload")
                if not isinstance(payload, dict):
                    continue
                if payload.get("card_revision_id") != revision_id:
                    continue
                type_events.append(event)
            card_events[event_type] = type_events
        clicks = card_events["topic_click"]
        # A dismissal the user undid before the horizon is no negative.
        undone = {
            event["payload"].get("withdrawn_record_id") for event in card_events["not_interested_undone"]
        } - {None}
        negatives = [
            event for event in card_events["not_interested"]
            if event["payload"].get("dismissal_record_id") not in undone
        ]
        starts = card_events["session_bound"]
        start = starts[0] if starts else None
        started_session_id = (
            str(start.get("session_id") or "") if start is not None else None
        )
        depths: list[dict[str, Any]] = []
        if started_session_id is not None:
            depths = [
                event
                for event in card_events["conversation_depth"]
                if event.get("session_id") == started_session_id
            ]
        conversation_depth = max(
            (
                int(event["payload"].get("conversation_depth") or 0)
                for event in depths
                if _plain_int(event["payload"].get("conversation_depth"))
            ),
            default=0,
        )
        future: list[dict[str, Any]] = []
        if started_session_id is not None:
            future = [
                event
                for event in card_events["future_activity"]
                if event["payload"].get("started_session_id") == started_session_id
            ]
        future_natural = any(
            event["payload"].get("natural_revisit_eligible") is True for event in future
        )
        future_pa_active = any(
            event["payload"].get("pa_active_eligible") is True for event in future
        )
        exclusions = tuple(
            dict.fromkeys(
                str(event["payload"].get("exclusion_reason") or "")
                for event in future
                if _nonempty(event["payload"].get("exclusion_reason"))
            )
        )
        clicked = bool(clicks)
        negative = bool(negatives)
        started = start is not None
        if future_natural:
            outcome_label = "future_revisit"
        elif started and conversation_depth >= deep_conversation_min_depth:
            outcome_label = "started_deep"
        elif started:
            outcome_label = "started_shallow"
        elif clicked:
            outcome_label = "clicked_no_start"
        elif negative:
            outcome_label = "not_interested"
        elif direct_input:
            outcome_label = "direct_input"
        elif passive_ignored:
            outcome_label = "passive_ignored"
        else:
            outcome_label = "shown_only"
        result.append(
            StarterOutcome(
                outcome_id=(
                    f"outcome:{exposure_id}:{revision_id}:h{label_horizon:.6f}"
                ),
                user_state_snapshot_id=str(
                    card.get("generation_input_snapshot_id") or ""
                ),
                manifest_id=manifest_id,
                generation_batch_id=batch_id,
                exposure_id=exposure_id,
                exposure_position=position,
                card_revision_id=revision_id,
                context_scope=context_scope,
                project_id=project_id,
                shown=True,
                clicked=clicked,
                not_interested=negative,
                started=started,
                started_session_id=started_session_id,
                conversation_depth=conversation_depth,
                future_activity_observed=bool(future),
                future_natural_activity=future_natural,
                future_pa_active_activity=future_pa_active,
                future_activity_exclusion_reasons=exclusions,
                outcome_label=outcome_label,
                exposed_at=exposed_at,
                clicked_at=(float(clicks[0]["created_at"]) if clicks else None),
                not_interested_at=(
                    float(negatives[0]["created_at"]) if negatives else None
                ),
                started_at=(float(start["created_at"]) if start is not None else None),
                label_horizon=float(label_horizon),
                version_refs=_version_refs(card),
                component_request_ids=component_request_ids,
                provider_response_ids=provider_response_ids,
                search_receipt=search_receipt,
            )
        )
    return tuple(result)

OUTCOME_SIGNAL_SCHEMA_VERSION = "topic-starter-v6-new-page-outcome-signal-v1"
DeliveryStatus = Literal["queued", "recorded", "rejected"]
OutcomeName = Literal[
    "accepted", "not_interested", "interest_paused", "not_interested_undone", "interest_pause_undone",
]
_OUTCOME_NAMES = frozenset({
    "accepted", "not_interested", "interest_paused", "not_interested_undone", "interest_pause_undone",
})


@dataclass(frozen=True)
class NewPageOutcomeSignal:
    """One accepted or dismissed card, a paused interest or an undo, bound to its shown revision."""

    outcome: OutcomeName
    manifest_id: str
    generation_batch_id: str
    exposure_id: str
    card_revision_id: str
    context_scope: str
    project_id: str | None
    component_request_ids: tuple[tuple[str, str], ...]
    provider_response_ids: tuple[str, ...]
    search_receipt: dict[str, Any] | None
    occurred_at: float
    schema_version: str = OUTCOME_SIGNAL_SCHEMA_VERSION
    host_written: bool = False

    def __post_init__(self) -> None:
        if self.schema_version != OUTCOME_SIGNAL_SCHEMA_VERSION:
            raise ValueError("unsupported outcome signal schema_version")
        if self.outcome not in _OUTCOME_NAMES:
            raise ValueError("outcome must be accepted, not_interested, interest_paused or an undo of either")
        _value_items9 = (
            self.manifest_id,
            self.generation_batch_id,
            self.exposure_id,
            self.card_revision_id,
            self.context_scope,
        )
        if not all(
            _nonempty(value)
            for value in _value_items9
        ):
            raise ValueError("outcome lineage IDs must be non-empty")
        try:
            expected_project_id = project_id_for_scope(self.context_scope)
        except ValueError as exc:
            raise ValueError("invalid outcome context scope") from exc
        if self.project_id != expected_project_id:
            raise ValueError("outcome project does not match context scope")
        if not math.isfinite(self.occurred_at):
            raise ValueError("occurred_at must be finite")
        if self.host_written:
            if self.component_request_ids or self.provider_response_ids:
                raise ValueError("host-written outcome must not claim model provenance")
            if self.search_receipt is not None:
                raise ValueError("host-written outcome must not claim a search receipt")
            return
        if not self.component_request_ids or any(
            not _nonempty(component_id) or not _nonempty(request_id)
            for component_id, request_id in self.component_request_ids
        ):
            raise ValueError("component request provenance must be non-empty")
        component_ids = [item[0] for item in self.component_request_ids]
        if len(component_ids) != len(set(component_ids)):
            raise ValueError("component request provenance contains duplicates")
        if not self.provider_response_ids or any(
            not _nonempty(response_id) for response_id in self.provider_response_ids
        ):
            raise ValueError("provider response provenance must be non-empty")
        if len(self.provider_response_ids) != len(set(self.provider_response_ids)):
            raise ValueError("provider response provenance contains duplicates")
        if self.search_receipt is not None and not isinstance(
            self.search_receipt, dict
        ):
            raise ValueError("search receipt provenance must be an object or null")


class NewPageOutcomeQueue:
    """Queue outcome signals until the single production sink is attached."""

    def __init__(self) -> None:
        self._pending: list[NewPageOutcomeSignal] = []
        self._sink: Callable[[NewPageOutcomeSignal], bool] | None = None

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    def publish(self, signal: NewPageOutcomeSignal) -> DeliveryStatus:
        if self._sink is None:
            self._pending.append(signal)
            return "queued"
        return "recorded" if self._sink(signal) else "rejected"

    def attach_sink(
        self,
        sink: Callable[[NewPageOutcomeSignal], bool],
    ) -> list[bool] | None:
        """Attach once and synchronously drain the pre-attach snapshot in order."""

        if self._sink is not None:
            return None
        self._sink = sink
        pending = self._pending
        self._pending = []
        return [sink(signal) for signal in pending]


class TopicStarterStoreOutcomeSink:
    """Persist accepted/disliked signals only when exact shown provenance matches."""

    def __init__(self, store: TopicStarterStore) -> None:
        self._store = store

    def __call__(self, signal: NewPageOutcomeSignal) -> bool:
        manifest_rows = self._store.query_events(
            event_type="starter_slate_manifest",
            slate_id=signal.manifest_id,
            policy_id=POLICY_ID,
            limit=1,
            newest_first=True,
        )
        card_rows = self._store.query_events(
            event_type="starter_card",
            slate_id=signal.manifest_id,
            topic_id=signal.card_revision_id,
            policy_id=POLICY_ID,
            limit=1,
            newest_first=True,
        )
        exposure_rows = self._store.query_events(
            event_type="dynamic_exposure",
            slate_id=signal.exposure_id,
            topic_id=signal.manifest_id,
            policy_id=POLICY_ID,
            limit=1,
            newest_first=True,
        )
        if not manifest_rows:
            return False
        if not card_rows:
            return False
        if not exposure_rows:
            return False
        manifest_payload = manifest_rows[0].get("payload")
        card_payload = card_rows[0].get("payload")
        exposure_payload = exposure_rows[0].get("payload")
        if not isinstance(manifest_payload, dict):
            return False
        if not isinstance(card_payload, dict):
            return False
        if not isinstance(exposure_payload, dict):
            return False
        manifest = manifest_payload
        card = card_payload
        exposure = exposure_payload
        expected = {
            "manifest_id": signal.manifest_id,
            "generation_batch_id": signal.generation_batch_id,
            "context_scope": signal.context_scope,
            "project_id": signal.project_id,
        }
        if any(manifest.get(key) != value for key, value in expected.items()):
            return False
        if any(exposure.get(key) != value for key, value in expected.items()):
            return False
        if any(
            card.get(key) != expected.get(key)
            for key in ("generation_batch_id", "context_scope", "project_id")
        ):
            return False
        if signal.card_revision_id not in manifest.get("card_revision_ids", ()):
            return False
        if card.get("card_revision_id") != signal.card_revision_id:
            return False
        if signal.card_revision_id not in exposure.get("card_revision_ids", ()):
            return False
        receipts = card.get("component_receipts")
        if not isinstance(receipts, dict):
            return False
        if not is_cold_start_card(card):
            stored_request_ids: dict[str, str] = {}
            for component_id, receipt in receipts.items():
                if not isinstance(receipt, dict):
                    continue
                if not _nonempty(component_id):
                    continue
                request_id = receipt.get("request_id")
                if not _nonempty(request_id):
                    continue
                stored_request_ids[str(component_id)] = str(request_id or "")
            if stored_request_ids != dict(signal.component_request_ids):
                return False
            stored_provider_response_ids = card_provider_response_ids(card)
            if stored_provider_response_ids != signal.provider_response_ids:
                return False
            stored_search_receipt = card_search_receipt(card)
            if card.get("search_receipt") != signal.search_receipt:
                return False
            exposure_provider_ids = exposure.get(
                "provider_response_ids_by_card_revision"
            )
            if (
                not isinstance(exposure_provider_ids, dict)
                or exposure_provider_ids.get(signal.card_revision_id)
                != list(stored_provider_response_ids)
            ):
                return False
            exposure_search_receipts = exposure.get(
                "search_receipts_by_card_revision"
            )
            if (
                not isinstance(exposure_search_receipts, dict)
                or exposure_search_receipts.get(signal.card_revision_id)
                != stored_search_receipt
            ):
                return False
        return record_terminal_interaction(
            self._store,
            event_type=(
                "topic_click" if signal.outcome == "accepted" else signal.outcome
            ),
            manifest_id=signal.manifest_id,
            generation_batch_id=signal.generation_batch_id,
            exposure_id=signal.exposure_id,
            card_revision_id=signal.card_revision_id,
            context_scope=signal.context_scope,
            project_id=signal.project_id,
            created_at=signal.occurred_at,
        )


def record_terminal_slate_outcome(
    store: TopicStarterStore,
    *,
    event_type: str,
    manifest_id: str,
    generation_batch_id: str,
    exposure_id: str,
    context_scope: str,
    project_id: str | None,
    created_at: float,
) -> bool:
    """Persist passive/direct slate outcomes against one exact shown exposure."""

    if event_type not in {"post_exposure_direct", "page_left"}:
        return False
    _value_items10 = (
        manifest_id,
        generation_batch_id,
        exposure_id,
        context_scope,
    )
    if not math.isfinite(created_at) or not all(
        _nonempty(value)
        for value in _value_items10
    ):
        return False
    try:
        if project_id_for_scope(context_scope) != project_id:
            return False
    except ValueError:
        return False
    manifest_rows = store.query_events(
        event_type="starter_slate_manifest",
        slate_id=manifest_id,
        policy_id=POLICY_ID,
        limit=2,
        newest_first=True,
    )
    exposure_rows = store.query_events(
        event_type="dynamic_exposure",
        slate_id=exposure_id,
        topic_id=manifest_id,
        policy_id=POLICY_ID,
        limit=2,
        newest_first=True,
    )
    if len(manifest_rows) != 1 or len(exposure_rows) != 1:
        return False
    manifest = manifest_rows[0].get("payload")
    exposure = exposure_rows[0].get("payload")
    if not isinstance(manifest, dict) or not isinstance(exposure, dict):
        return False
    expected = {
        "manifest_id": manifest_id,
        "generation_batch_id": generation_batch_id,
        "context_scope": context_scope,
        "project_id": project_id,
    }
    if any(manifest.get(key) != value for key, value in expected.items()):
        return False
    if any(exposure.get(key) != value for key, value in expected.items()):
        return False
    exposed_at = float(exposure_rows[0].get("created_at") or 0.0)
    if created_at < exposed_at:
        return False
    return store.record_event(
        {
            "session_id": str(exposure_rows[0].get("session_id") or ""),
            "slate_id": exposure_id,
            "event_type": event_type,
            "topic_id": manifest_id,
            "profile_version": manifest.get("profile_semantic_version"),
            "policy_id": POLICY_ID,
            "selection_mode": "unknown",
            "propensity_reason": "not_estimated",
            "payload": {
                **expected,
                "exposure_id": exposure_id,
                "negative_feedback": False,
            },
            "created_at": created_at,
        }
    )

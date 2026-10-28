# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Collect terminal Topic Starter outcomes from persisted session history."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any, NamedTuple

from jiuwenswarm.agents.harness.common.recommendation import (
    topic_history as history,
)
from jiuwenswarm.agents.harness.common.recommendation.topic_model import (
    PROVIDER_REFUSALS,
    provider_failure_reason,
)
from jiuwenswarm.agents.harness.common.recommendation.topic_outcomes import (
    PREFERENCE_PAIR_SCHEMA_VERSION,
    RID_TRAINING_SCHEMA_VERSION,
    RID_USER_CONTEXT_SCHEMA_VERSION,
    STARTER_OUTCOME_SCHEMA_VERSION,
    UTILITY_TRAINING_SCHEMA_VERSION,
    PreferencePair,
    RevisitPair,
    RIDUserContextSnapshot,
    build_revisit_pair,
    build_rid_training_tuple,
    build_training_slice_manifest,
    build_utility_training_example,
    join_starter_outcomes,
    persist_outcome_artifact,
    record_terminal_conversation_depth,
    record_terminal_future_activity,
)
from jiuwenswarm.agents.harness.common.recommendation.topic_product import (
    GLOBAL_PROJECT_BUCKETS,
    POLICY_ID,
    SESSION_SUMMARY_COVERAGE_EVENT_TYPE,
    SESSION_SUMMARY_COVERAGE_RECEIPT_VERSION,
    find_exact_terminal_card,
    project_id_for_scope,
    session_summary_coverage_topic_id,
)
from jiuwenswarm.agents.harness.common.recommendation.topic_retrieval import (
    ACTIVE_EXPRESSION_EXAMPLE_SCHEMA_VERSION,
    ACTIVE_EXPRESSION_OBSERVATION_SCHEMA_VERSION,
    PA_TRAINING_EXAMPLE_SCHEMA_VERSION,
    PASSIVE_STARTER_EXAMPLE_SCHEMA_VERSION,
    ActiveExpressionExample,
    ActiveExpressionObservation,
    PassiveStarterExample,
    PATrainingExample,
    admit_active_expression,
)
from jiuwenswarm.agents.harness.common.recommendation.topic_starter import (
    TopicStarterStore,
)
from jiuwenswarm.agents.harness.common.recommendation.topic_terminal import (
    SessionSummaryArtifact,
)
from jiuwenswarm.server.runtime.session.session_history import (
    SessionCompletionBoundary,
    load_history_records,
    project_completed_session_prefix,
    project_completed_session_prefixes,
)
from jiuwenswarm.server.runtime.session.session_metadata import (
    get_all_sessions_metadata,
)

DEFAULT_DEPTH_QUIESCENCE_SECONDS = 24 * 60 * 60
DEFAULT_LABEL_HORIZON_SECONDS = 7 * 24 * 60 * 60
DEFAULT_MAX_BOUND_EVENTS = 2_048
DEFAULT_MAX_EXPOSURES = 2_048
DEFAULT_MAX_SESSIONS = 10_000
LOCAL_USER_ID = "local"
FEATURE_SNAPSHOT_VERSION = "topic-starter-v6-prelabel-feature-v1"
TRAINING_EXCLUSION_SCHEMA_VERSION = "topic-starter-v6-training-exclusion-v1"

HistoryLoader = Callable[[str], list[dict[str, Any]]]
SessionsLoader = Callable[[], Iterable[dict[str, Any]]]
SummaryPairMatcher = Callable[
    [SessionSummaryArtifact, SessionSummaryArtifact],
    bool,
]


def _plain_int(value: Any) -> bool:
    # bool is an int subclass; a JSON boolean is never a version or a count.
    return isinstance(value, int) and not isinstance(value, bool)


@dataclass(frozen=True, slots=True)
class OutcomeCollectionReport:
    bound_sessions_scanned: int
    depth_events_recorded: int
    future_activity_events_recorded: int
    session_summary_artifacts_recorded: int
    revisit_pair_artifacts_recorded: int
    rid_training_tuple_artifacts_recorded: int
    active_expression_observation_artifacts_recorded: int
    active_expression_example_artifacts_recorded: int
    passive_starter_example_artifacts_recorded: int
    pa_training_example_artifacts_recorded: int
    training_exclusion_artifacts_recorded: int
    starter_outcome_artifacts_recorded: int
    utility_example_artifacts_recorded: int
    preference_pair_artifacts_recorded: int
    training_slice_artifacts_recorded: int


@dataclass(frozen=True, slots=True)
class TrainingExclusionRecord:
    """Versioned reason why one real product artifact is not trainable."""

    exclusion_id: str
    training_domain: str
    source_artifact_id: str
    reason: str
    user_state_snapshot_id: str
    context_scope: str
    manifest_id: str
    exposure_id: str
    created_at: float
    schema_version: str = TRAINING_EXCLUSION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _value_items = (
            self.exclusion_id,
            self.source_artifact_id,
            self.reason,
            self.user_state_snapshot_id,
            self.manifest_id,
            self.exposure_id,
        )
        _rejected = (
            self.training_domain not in {"rid", "pa_active", "pa_passive"}
            or any(
                not isinstance(value, str) or not value.strip()
                for value in _value_items
            )
            or self.schema_version != TRAINING_EXCLUSION_SCHEMA_VERSION
            or _timestamp(self.created_at) is None
        )
        if (
            _rejected
        ):
            raise ValueError("training exclusion contract failed")
        try:
            project_id_for_scope(self.context_scope)
        except ValueError as exc:
            raise ValueError("training exclusion contract failed") from exc

    @property
    def artifact_id(self) -> str:
        return self.exclusion_id

    def as_payload(self) -> dict[str, Any]:
        return {
            "exclusion_id": self.exclusion_id,
            "training_domain": self.training_domain,
            "source_artifact_id": self.source_artifact_id,
            "reason": self.reason,
            "user_state_snapshot_id": self.user_state_snapshot_id,
            "context_scope": self.context_scope,
            "manifest_id": self.manifest_id,
            "exposure_id": self.exposure_id,
            "created_at": self.created_at,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_payload(cls, payload: Any) -> TrainingExclusionRecord:
        if not isinstance(payload, dict) or set(payload) != {
            "exclusion_id",
            "training_domain",
            "source_artifact_id",
            "reason",
            "user_state_snapshot_id",
            "context_scope",
            "manifest_id",
            "exposure_id",
            "created_at",
            "schema_version",
        }:
            raise ValueError("training exclusion contract failed")
        return cls(**payload)


class _RIDCorpusMaterialization(NamedTuple):
    summary_recorded: int
    pair_recorded: int
    tuple_recorded: int
    exclusion_recorded: int
    slice_recorded: int
    labels: dict[tuple[str, str], bool]


class _TrainingPlaneMaterialization(NamedTuple):
    starter_outcomes_recorded: int
    utility_examples_recorded: int
    preference_pairs_recorded: int
    training_slices_recorded: int
    passive_starter_examples_recorded: int
    passive_pa_examples_recorded: int
    passive_exclusions_recorded: int


@dataclass(frozen=True, slots=True)
class _HistorySignal:
    session_id: str
    ordinal: int
    source_event_id: str
    source_locator: str
    observed_at: float
    input_origin: str
    assistance_origin: str
    intent_cluster: str | None


def _default_sessions_loader() -> list[dict[str, Any]]:
    sessions, _ = get_all_sessions_metadata(
        limit=DEFAULT_MAX_SESSIONS,
        offset=0,
    )
    return sessions


def _timestamp(value: Any) -> float | None:
    if type(value) not in (int, float):
        return None
    result = float(value)
    return result if math.isfinite(result) and result >= 0 else None


def _session_user_id(metadata: dict[str, Any] | None) -> str | None:
    if not isinstance(metadata, dict):
        return None
    value = metadata.get("user_id")
    # Web chat leaves user_id empty; this data directory's sessions are its one local user's.
    return value.strip() if isinstance(value, str) and value.strip() else LOCAL_USER_ID


def _persist_training_event(
    store: TopicStarterStore,
    *,
    event_type: str,
    artifact_id: str,
    payload: dict[str, Any],
    session_id: str,
    exposure_id: str,
    created_at: float,
) -> bool:
    if _timestamp(created_at) is None:
        return False
    return store.record_event(
        {
            "session_id": session_id,
            "slate_id": exposure_id,
            "event_type": event_type,
            "topic_id": artifact_id,
            "policy_id": POLICY_ID,
            "selection_mode": "training_data_plane",
            "propensity_reason": "not_applicable",
            "payload": payload,
            "created_at": created_at,
        }
    )


def _origin(value: Any, *, fallback: str) -> str:
    return str(value).strip() if isinstance(value, str) and value.strip() else fallback


def _user_records(records: Iterable[dict[str, Any]]) -> list[tuple[int, dict[str, Any]]]:
    return [
        (ordinal, record)
        for ordinal, record in enumerate(records)
        if isinstance(record, dict) and record.get("role") == "user"
    ]


def _scope_matches(metadata: dict[str, Any], context_scope: str) -> bool:
    expected_project_id = project_id_for_scope(context_scope)
    actual_project_id = str(metadata.get("project_id") or "").strip()
    if expected_project_id is None:
        return actual_project_id in GLOBAL_PROJECT_BUCKETS
    return actual_project_id == expected_project_id


def _exact_manifest(
    store: TopicStarterStore,
    *,
    manifest_id: str,
) -> dict[str, Any] | None:
    rows = store.query_events(
        event_type="starter_slate_manifest",
        slate_id=manifest_id,
        policy_id=POLICY_ID,
        limit=2,
        newest_first=True,
    )
    if len(rows) != 1 or not isinstance(rows[0].get("payload"), dict):
        return None
    manifest = rows[0]["payload"]
    return manifest if manifest.get("manifest_id") == manifest_id else None


def _manifest_session_summaries(
    manifest: dict[str, Any],
) -> tuple[SessionSummaryArtifact, ...]:
    raw = manifest.get("session_summary_artifacts")
    if not isinstance(raw, list):
        return ()
    try:
        summaries = tuple(SessionSummaryArtifact.from_payload(item) for item in raw)
    except (TypeError, ValueError):
        return ()
    summary_ids = [item.session_summary_id for item in summaries]
    if len(summary_ids) != len(set(summary_ids)):
        return ()
    return summaries


def _exact_exposed_at(
    store: TopicStarterStore,
    *,
    manifest_id: str,
    exposure_id: str,
) -> float | None:
    rows = store.query_events(
        event_type="dynamic_exposure",
        slate_id=exposure_id,
        topic_id=manifest_id,
        policy_id=POLICY_ID,
        limit=2,
    )
    matches: list[dict[str, Any]] = []
    for row in rows:
        payload = row.get("payload")
        if not isinstance(payload, dict):
            continue
        if payload.get("manifest_id") != manifest_id:
            continue
        if payload.get("exposure_id", exposure_id) != exposure_id:
            continue
        matches.append(row)
    return _timestamp(matches[0].get("created_at")) if len(matches) == 1 else None


def _mapped_intents(
    store: TopicStarterStore,
) -> dict[tuple[str, int], str]:
    state = history.load_latest_history_state(store) or {}
    mapping = history.state_mapping_entries(state)
    by_position: dict[tuple[str, int], str] = {}
    ledger = state.get("history_ledger")
    sessions = ledger.get("sessions") if isinstance(ledger, dict) else None
    if not isinstance(sessions, dict):
        return by_position
    for session_id, session in sessions.items():
        entries = session.get("entries") if isinstance(session, dict) else None
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict) or entry.get("retracted"):
                continue
            behavior_index = entry.get("behavior_index")
            ordinal = entry.get("ordinal")
            if not _plain_int(behavior_index) or not _plain_int(ordinal):
                continue
            mapped = mapping.get(behavior_index) or {}
            tag = str(mapped.get("tag") or "").strip()
            position = (str(session_id), ordinal)
            if tag:
                by_position[position] = tag
    return by_position


def _signal(
    *,
    session_id: str,
    ordinal: int,
    record: dict[str, Any],
    mapped_intent: str | None,
) -> _HistorySignal | None:
    observed_at = _timestamp(record.get("timestamp"))
    if observed_at is None:
        return None
    fallback_locator = f"session:{session_id}:history:{ordinal}"
    source_locator = _origin(record.get("source_locator"), fallback=fallback_locator)
    source_event_id = str(
        record.get("id") or record.get("request_id") or source_locator
    ).strip()
    explicit_intent = record.get("intent_cluster")
    intent_cluster = (
        explicit_intent.strip()
        if isinstance(explicit_intent, str) and explicit_intent.strip()
        else mapped_intent
    )
    return _HistorySignal(
        session_id=session_id,
        ordinal=ordinal,
        source_event_id=source_event_id or source_locator,
        source_locator=source_locator,
        observed_at=observed_at,
        input_origin=_origin(
            record.get("input_origin"),
            fallback="pasted_or_unknown",
        ),
        assistance_origin=_origin(
            record.get("assistance_origin"),
            fallback="unknown",
        ),
        intent_cluster=intent_cluster,
    )


def _event_pages(
    store: TopicStarterStore,
    *,
    event_type: str,
    page_size: int,
) -> list[dict[str, Any]]:
    """Read a stable oldest-first snapshot in bounded SQLite pages."""

    result: list[dict[str, Any]] = []
    offset = 0
    while True:
        page = store.query_events(
            event_type=event_type,
            policy_id=POLICY_ID,
            limit=page_size,
            offset=offset,
        )
        result.extend(page)
        if len(page) < page_size:
            return result
        offset += len(page)


def _summary_coverage_times(
    store: TopicStarterStore,
    *,
    page_size: int,
) -> dict[str, float]:
    """When summary coverage last carried each summary ID."""

    times: dict[str, float] = {}
    for row in _event_pages(
        store,
        event_type=SESSION_SUMMARY_COVERAGE_EVENT_TYPE,
        page_size=page_size,
    ):
        payload = row.get("payload")
        sessions = payload.get("sessions") if isinstance(payload, dict) else None
        created_at = row.get("created_at")
        if not isinstance(sessions, list) or not isinstance(created_at, (int, float)):
            continue
        for item in sessions:
            if not isinstance(item, dict):
                continue
            summary_id = item.get("session_summary_id")
            if isinstance(summary_id, str):
                times[summary_id] = max(times.get(summary_id, 0.0), float(created_at))
    return times


_COVERAGE_ITEM_FIELDS = (
    "session_id",
    "session_summary_id",
    "input_origin",
    "assistance_origin",
)


def _is_covered_session_item(item: Any) -> bool:
    """A coverage entry is trusted only when its shape is exactly as written."""

    if not isinstance(item, dict):
        return False
    if set(item) != {
        "session_id",
        "session_summary_id",
        "input_origin",
        "assistance_origin",
        "rid_eligible",
        "rid_exclusion_reason",
    }:
        return False
    if not all(
        isinstance(item.get(field), str) and item[field]
        for field in _COVERAGE_ITEM_FIELDS
    ):
        return False
    if not isinstance(item.get("rid_eligible"), bool):
        return False
    if item.get("rid_exclusion_reason") not in {
        None,
        "non_natural_or_assisted_input",
    }:
        return False
    return item["rid_eligible"] == (
        item.get("rid_exclusion_reason") is None
    )


def _covered_standalone_session_summaries(
    store: TopicStarterStore,
    *,
    page_size: int,
) -> tuple[SessionSummaryArtifact, ...]:
    """Load independently persisted summaries only through exact coverage."""

    coverage_by_summary_id: dict[
        str,
        tuple[str, str, dict[str, Any]],
    ] = {}
    ambiguous_coverage_ids: set[str] = set()
    for row in _event_pages(
        store,
        event_type=SESSION_SUMMARY_COVERAGE_EVENT_TYPE,
        page_size=page_size,
    ):
        payload = row.get("payload")
        if not isinstance(payload, dict):
            continue
        if (
            set(payload)
            != {
                "schema_version",
                "input_snapshot_id",
                "context_scope",
                "summary_config_version",
                "sessions",
            }
        ):
            continue
        if (
            payload.get("schema_version")
            != SESSION_SUMMARY_COVERAGE_RECEIPT_VERSION
        ):
            continue
        input_snapshot_id = payload.get("input_snapshot_id")
        context_scope = payload.get("context_scope")
        summary_config_version = payload.get("summary_config_version")
        sessions = payload.get("sessions")
        if not isinstance(input_snapshot_id, str):
            continue
        if not input_snapshot_id:
            continue
        if not isinstance(context_scope, str):
            continue
        if not context_scope:
            continue
        if not isinstance(summary_config_version, str):
            continue
        if not summary_config_version:
            continue
        if not isinstance(sessions, list):
            continue
        if row.get("session_id") != "topic-starter-session-summary":
            continue
        if row.get("slate_id") != context_scope:
            continue
        # One malformed entry invalidates the batch, exactly as the receipt was written.
        valid_items = [
            item for item in sessions if _is_covered_session_item(item)
        ]
        session_ids = [item["session_id"] for item in valid_items]
        summary_ids = [item["session_summary_id"] for item in valid_items]
        if len(valid_items) != len(sessions):
            continue
        if len(session_ids) != len(set(session_ids)):
            continue
        if len(summary_ids) != len(set(summary_ids)):
            continue
        # Existing databases contain coverage keyed only by natural state.
        if row.get("topic_id") not in {
            f"{input_snapshot_id}|{summary_config_version}",
            session_summary_coverage_topic_id(payload),
        }:
            continue
        for item in valid_items:
            summary_id = item["session_summary_id"]
            coverage = (
                context_scope,
                summary_config_version,
                item,
            )
            existing = coverage_by_summary_id.get(summary_id)
            if existing is not None and existing != coverage:
                ambiguous_coverage_ids.add(summary_id)
                coverage_by_summary_id.pop(summary_id, None)
                continue
            if summary_id not in ambiguous_coverage_ids:
                coverage_by_summary_id[summary_id] = coverage

    summaries: list[SessionSummaryArtifact] = []
    for row in _event_pages(
        store,
        event_type="session_summary",
        page_size=page_size,
    ):
        payload = row.get("payload")
        if not isinstance(payload, dict):
            continue
        try:
            summary = SessionSummaryArtifact.from_payload(payload)
        except (TypeError, ValueError):
            continue
        matched_coverage = coverage_by_summary_id.get(summary.session_summary_id)
        if matched_coverage is None:
            continue
        context_scope, summary_config_version, item = matched_coverage
        expected_item = {
            "session_id": summary.session_id,
            "session_summary_id": summary.session_summary_id,
            "input_origin": summary.input_origin,
            "assistance_origin": summary.assistance_origin,
            "rid_eligible": summary.natural_revisit_eligible,
            "rid_exclusion_reason": (
                None
                if summary.natural_revisit_eligible
                else "non_natural_or_assisted_input"
            ),
        }
        prefix = f"summary:{summary.session_id}:"
        semantic_digest = summary.session_summary_id.removeprefix(prefix)
        if row.get("topic_id") != summary.session_summary_id:
            continue
        if row.get("session_id") != summary.session_id:
            continue
        if row.get("slate_id") != summary.context_scope:
            continue
        if summary.context_scope != context_scope:
            continue
        if summary.summary_config_version != summary_config_version:
            continue
        if not summary.session_summary_id.startswith(prefix):
            continue
        if len(semantic_digest) != 64:
            continue
        if any(character not in "0123456789abcdef" for character in semantic_digest):
            continue
        if item != expected_item:
            continue
        summaries.append(summary)
    return tuple(summaries)


def _started_depths_finalized(
    store: TopicStarterStore,
    *,
    manifest_id: str,
    exposure_id: str,
    exposed_at: float,
    label_horizon: float,
) -> bool:
    _row_items = store.query_events(
        event_type="session_bound",
        slate_id=exposure_id,
        policy_id=POLICY_ID,
        limit=8,
    )
    starts: list[dict[str, Any]] = []
    for row in _row_items:
        created_at = float(row.get("created_at") or 0)
        if not exposed_at <= created_at <= label_horizon:
            continue
        payload = row.get("payload")
        if not isinstance(payload, dict):
            continue
        if payload.get("manifest_id") != manifest_id:
            continue
        if payload.get("exposure_id") != exposure_id:
            continue
        starts.append(row)
    for start in starts:
        revision_id = str(start.get("topic_id") or "")
        started_session_id = str(start.get("session_id") or "")
        depths = store.query_events(
            event_type="conversation_depth",
            slate_id=exposure_id,
            topic_id=revision_id,
            policy_id=POLICY_ID,
            limit=8,
        )
        if not any(
            float(row.get("created_at") or 0) <= label_horizon
            and row.get("session_id") == started_session_id
            and isinstance(row.get("payload"), dict)
            and row["payload"].get("manifest_id") == manifest_id
            for row in depths
        ):
            return False
    return True


class TopicStarterOutcomeCollector:
    """Finalize depth and later-activity labels through stable lineage only."""

    def __init__(
        self,
        store: TopicStarterStore,
        *,
        history_loader: HistoryLoader = load_history_records,
        sessions_loader: SessionsLoader = _default_sessions_loader,
        now: Callable[[], float] = time.time,
        depth_quiescence_seconds: float = DEFAULT_DEPTH_QUIESCENCE_SECONDS,
        label_horizon_seconds: float = DEFAULT_LABEL_HORIZON_SECONDS,
        max_bound_events: int = DEFAULT_MAX_BOUND_EVENTS,
        max_exposures: int = DEFAULT_MAX_EXPOSURES,
        summary_pair_matcher: SummaryPairMatcher | None = None,
        summary_pair_matcher_id: str | None = None,
        summary_pair_matcher_version: str | None = None,
        cancel_event: threading.Event | None = None,
    ) -> None:
        _rejected2 = (
            depth_quiescence_seconds < 0
            or label_horizon_seconds < 0
            or max_bound_events < 1
            or max_exposures < 1
        )
        if (
            _rejected2
        ):
            raise ValueError("collector bounds must be non-negative")
        _rejected3 = (
            not callable(summary_pair_matcher)
            or not isinstance(summary_pair_matcher_id, str)
            or not summary_pair_matcher_id.strip()
            or not isinstance(summary_pair_matcher_version, str)
            or not summary_pair_matcher_version.strip()
        )
        if summary_pair_matcher is None:
            if summary_pair_matcher_id is not None or summary_pair_matcher_version is not None:
                raise ValueError("summary pair matcher binding must be complete")
        elif (
            _rejected3
        ):
            raise ValueError("summary pair matcher binding must be complete")
        self._cancel_event = cancel_event
        self._store = store
        self._history_loader = history_loader
        self._sessions_loader = sessions_loader
        self._now = now
        self._depth_quiescence_seconds = float(depth_quiescence_seconds)
        self._label_horizon_seconds = float(label_horizon_seconds)
        self._max_bound_events = int(max_bound_events)
        self._max_exposures = int(max_exposures)
        self._summary_pair_matcher = summary_pair_matcher
        self._summary_pair_matcher_id = (
            summary_pair_matcher_id.strip() if summary_pair_matcher_id else None
        )
        self._summary_pair_matcher_version = (
            summary_pair_matcher_version.strip()
            if summary_pair_matcher_version
            else None
        )
        self._summary_pair_matcher_refused = False

    def _check_cancelled(self) -> None:
        if self._cancel_event is not None and self._cancel_event.is_set():
            raise asyncio.CancelledError()

    def _record_exclusion(
        self,
        *,
        training_domain: str,
        source_artifact_id: str,
        reason: str,
        user_state_snapshot_id: str,
        context_scope: str,
        manifest_id: str,
        exposure_id: str,
        session_id: str,
        created_at: float,
    ) -> bool:
        self._check_cancelled()
        record = TrainingExclusionRecord(
            exclusion_id=(
                f"training-exclusion:{manifest_id}:{exposure_id}:"
                f"{training_domain}:{source_artifact_id}"
            ),
            training_domain=training_domain,
            source_artifact_id=source_artifact_id,
            reason=reason,
            user_state_snapshot_id=user_state_snapshot_id,
            context_scope=context_scope,
            manifest_id=manifest_id,
            exposure_id=exposure_id,
            created_at=created_at,
        )
        return _persist_training_event(
            self._store,
            event_type="training_exclusion",
            artifact_id=record.artifact_id,
            payload=record.as_payload(),
            session_id=session_id,
            exposure_id=exposure_id,
            created_at=created_at,
        )

    def _match_summary_pair(
        self,
        earlier: SessionSummaryArtifact,
        later: SessionSummaryArtifact,
    ) -> tuple[bool | None, str | None]:
        self._check_cancelled()
        matcher = self._summary_pair_matcher
        if matcher is None or self._summary_pair_matcher_refused:
            return None, "summary_pair_matcher_unavailable"
        try:
            result = matcher(earlier, later)
        except Exception as exc:
            # A refused key, account or model refuses the remaining pairs of this run too.
            self._summary_pair_matcher_refused = (
                provider_failure_reason(exc) in PROVIDER_REFUSALS
            )
            return None, "summary_pair_matcher_unavailable"
        self._check_cancelled()
        if not isinstance(result, bool):
            return None, "summary_pair_matcher_invalid_output"
        return result, None

    def _materialize_rid_corpus(
        self,
        *,
        summaries: Iterable[SessionSummaryArtifact],
        session_metadata: dict[str, dict[str, Any]],
        summary_signals: Callable[
            [SessionSummaryArtifact], tuple[_HistorySignal, ...]
        ],
    ) -> _RIDCorpusMaterialization:
        """Build IceBreaker labels from frozen history, independent of exposure."""

        context_snapshots: list[RIDUserContextSnapshot] = []
        for row in _event_pages(
            self._store,
            event_type="rid_user_context_snapshot",
            page_size=max(1, self._max_exposures),
        ):
            try:
                context_snapshots.append(
                    RIDUserContextSnapshot.from_payload(row["payload"])
                )
            except ValueError:
                continue

        summary_recorded = 0
        valid: list[tuple[SessionSummaryArtifact, str]] = []
        # Summaries arrive most recently carried by a pool or summary coverage first; a
        # completed boundary summarized under several summary configs is compared once,
        # through the first of them.
        compared_boundaries: set[SessionCompletionBoundary] = set()
        for summary in summaries:
            metadata = session_metadata.get(summary.session_id)
            if metadata is None:
                continue
            if not _scope_matches(metadata, summary.context_scope):
                continue
            if not summary_signals(summary):
                continue
            if persist_outcome_artifact(
                self._store,
                summary,
                created_at=summary.completed_at,
            ):
                summary_recorded += 1
            user_id = _session_user_id(metadata)
            if (
                user_id is not None
                and summary.natural_revisit_eligible
                and summary.completion_boundary not in compared_boundaries
            ):
                compared_boundaries.add(summary.completion_boundary)
                valid.append((summary, user_id))

        pair_recorded = 0
        tuple_recorded = 0
        exclusion_recorded = 0
        slice_recorded = 0
        labels: dict[tuple[str, str], bool] = {}
        by_scope: dict[str, list[tuple[SessionSummaryArtifact, str]]] = {}
        for summary, user_id in valid:
            by_scope.setdefault(summary.context_scope, []).append((summary, user_id))

        for context_scope, scoped_items in sorted(by_scope.items()):
            self._check_cancelled()
            scoped_items.sort(
                key=lambda item: (
                    item[0].completed_at,
                    item[0].session_summary_id,
                    item[1],
                )
            )
            batch_digest = hashlib.sha256(
                json.dumps(
                    [
                        (user_id, summary.session_summary_id, summary.completed_at)
                        for summary, user_id in scoped_items
                    ],
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()[:24]
            batch_id = f"rid-corpus-batch:{context_scope}:{batch_digest}"
            pair_ids: list[str] = []
            tuple_ids: list[str] = []
            context_ids: set[str] = set()
            excluded: dict[str, str] = {}
            by_user: dict[str, list[SessionSummaryArtifact]] = {}
            for summary, user_id in scoped_items:
                by_user.setdefault(user_id, []).append(summary)

            if self._summary_pair_matcher is None:
                has_comparison = any(
                    len({item.session_id for item in items}) > 1
                    for items in by_user.values()
                )
                if has_comparison:
                    source_id = f"rid-matcher-binding:{batch_id}"
                    reason = "summary_pair_matcher_unavailable"
                    exclusion_recorded += int(
                        self._record_exclusion(
                            training_domain="rid",
                            source_artifact_id=source_id,
                            reason=reason,
                            user_state_snapshot_id=batch_id,
                            context_scope=context_scope,
                            manifest_id=batch_id,
                            exposure_id=batch_id,
                            session_id="rid-corpus",
                            created_at=max(item[0].completed_at for item in scoped_items),
                        )
                    )
                    excluded[source_id] = reason
            else:
                comparisons_complete: dict[str, bool] = {}
                comparison_count: dict[str, int] = {}
                matches_by_candidate: dict[str, list[RevisitPair]] = {}
                summary_by_id = {
                    summary.session_summary_id: summary
                    for summary, _ in scoped_items
                }
                user_by_summary_id = {
                    summary.session_summary_id: user_id
                    for summary, user_id in scoped_items
                }
                for user_id, user_summaries in sorted(by_user.items()):
                    user_summaries.sort(
                        key=lambda item: (item.completed_at, item.session_summary_id)
                    )
                    for index, earlier in enumerate(user_summaries[:-1]):
                        comparisons_complete[earlier.session_summary_id] = True
                        comparison_count[earlier.session_summary_id] = 0
                        for later in user_summaries[index + 1:]:
                            if earlier.session_id == later.session_id:
                                continue
                            comparison_count[earlier.session_summary_id] += 1
                            match_label, reason = self._match_summary_pair(
                                earlier,
                                later,
                            )
                            if match_label is None:
                                comparisons_complete[earlier.session_summary_id] = False
                                source_id = (
                                    f"rid-match:{earlier.session_summary_id}:"
                                    f"{later.session_summary_id}:"
                                    f"{self._summary_pair_matcher_version}"
                                )
                                exclusion_recorded += int(
                                    self._record_exclusion(
                                        training_domain="rid",
                                        source_artifact_id=source_id,
                                        reason=str(reason),
                                        user_state_snapshot_id=batch_id,
                                        context_scope=context_scope,
                                        manifest_id=batch_id,
                                        exposure_id=batch_id,
                                        session_id=later.session_id,
                                        created_at=later.completed_at,
                                    )
                                )
                                excluded[source_id] = str(reason)
                                continue
                            pair = build_revisit_pair(
                                earlier,
                                later,
                                match_label=match_label,
                                matcher_id=str(self._summary_pair_matcher_id),
                                matcher_version=str(
                                    self._summary_pair_matcher_version
                                ),
                            )
                            labels[(
                                earlier.session_summary_id,
                                later.session_summary_id,
                            )] = match_label
                            pair_ids.append(pair.revisit_pair_id)
                            if persist_outcome_artifact(
                                self._store,
                                pair,
                                created_at=pair.observed_at,
                            ):
                                pair_recorded += 1
                            if match_label:
                                matches_by_candidate.setdefault(
                                    earlier.session_summary_id,
                                    [],
                                ).append(pair)

                negative_ids_by_user: dict[str, tuple[str, ...]] = {}
                for user_id, user_summaries in by_user.items():
                    negative_ids: list[str] = []
                    for summary in user_summaries:
                        summary_id = summary.session_summary_id
                        if comparison_count.get(summary_id, 0) <= 0:
                            continue
                        if not comparisons_complete.get(summary_id, False):
                            continue
                        if matches_by_candidate.get(summary_id):
                            continue
                        negative_ids.append(summary_id)
                    negative_ids_by_user[user_id] = tuple(negative_ids)
                for positive_summary_id, positive_pairs in sorted(
                    matches_by_candidate.items()
                ):
                    positive_pair = min(
                        positive_pairs,
                        key=lambda item: (item.observed_at, item.later_summary_id),
                    )
                    positive_user_id = user_by_summary_id[positive_summary_id]
                    witness = summary_by_id[positive_pair.later_summary_id]
                    witness_started_at = min(
                        signal.observed_at for signal in summary_signals(witness)
                    )
                    eligible_contexts = []
                    for snapshot in context_snapshots:
                        members = snapshot.request_payload["session_summaries"]
                        member_ids = {item["session_summary_id"] for item in members}
                        _condition6 = (
                            snapshot.context_scope == context_scope
                            and snapshot.cutoff_at < witness_started_at
                            and positive_summary_id in member_ids
                            and all(
                                user_by_summary_id.get(summary_id) == positive_user_id
                                for summary_id in member_ids
                            )
                        )
                        if (
                            _condition6
                        ):
                            eligible_contexts.append(snapshot)
                    context = max(
                        eligible_contexts,
                        key=lambda item: (item.cutoff_at, item.user_history_snapshot_id),
                        default=None,
                    )
                    history_ids = (
                        {
                            item["session_summary_id"]
                            for item in context.request_payload["session_summaries"]
                        }
                        if context is not None else set()
                    )
                    intra_ids = tuple(
                        summary_id
                        for summary_id in negative_ids_by_user[positive_user_id]
                        if summary_id in history_ids
                    )
                    inter_ids: tuple[str, ...] = ()
                    if context is not None:
                        cutoff_at = context.cutoff_at
                        inter_id_items: list[str] = []
                        for summary, other_user_id in scoped_items:
                            if other_user_id == positive_user_id:
                                continue
                            if summary.completed_at >= cutoff_at:
                                continue
                            inter_id_items.append(summary.session_summary_id)
                        inter_ids = tuple(inter_id_items)
                    reason = None
                    if context is None:
                        reason = "missing_pre_revisit_user_context"
                    elif not intra_ids:
                        reason = "missing_intra_user_negative"
                    elif not inter_ids:
                        reason = "missing_inter_user_negative"
                    if reason is not None:
                        exclusion_recorded += int(
                            self._record_exclusion(
                                training_domain="rid",
                                source_artifact_id=positive_pair.revisit_pair_id,
                                reason=reason,
                                user_state_snapshot_id=batch_id,
                                context_scope=context_scope,
                                manifest_id=batch_id,
                                exposure_id=batch_id,
                                session_id=positive_pair.later_session_id,
                                created_at=positive_pair.observed_at,
                            )
                        )
                        excluded[positive_pair.revisit_pair_id] = reason
                        continue
                    if context is None:
                        raise ValueError("rid training tuple requires a resolved context")
                    context_ids.add(context.user_history_snapshot_id)
                    tuple_artifact = build_rid_training_tuple(
                        rid_training_tuple_id=(
                            f"rid-training-v3:{batch_id}:{positive_summary_id}:"
                            f"{context.user_history_snapshot_id}:"
                            f"{self._summary_pair_matcher_version}"
                        ),
                        corpus_batch_id=batch_id,
                        user_history_snapshot_id=context.user_history_snapshot_id,
                        user_id=positive_user_id,
                        positive_pair=positive_pair,
                        intra_interest_negative_summary_ids=intra_ids,
                        inter_interest_negative_summary_ids=inter_ids,
                    )
                    tuple_ids.append(tuple_artifact.rid_training_tuple_id)
                    if persist_outcome_artifact(
                        self._store,
                        tuple_artifact,
                        created_at=positive_pair.observed_at,
                    ):
                        tuple_recorded += 1

            if pair_ids or tuple_ids or excluded:
                window_start = min(item[0].completed_at for item in scoped_items)
                label_horizon = max(item[0].completed_at for item in scoped_items)
                training_slice = build_training_slice_manifest(
                    training_slice_manifest_id=f"rid-training-slice-v3:{batch_id}",
                    window_start=window_start,
                    window_end=label_horizon + 1.0,
                    label_horizon=label_horizon,
                    filters={
                        "training_domain": "rid",
                        "corpus_batch_id": batch_id,
                        "matcher_id": self._summary_pair_matcher_id or "unbound",
                        "matcher_version": (
                            self._summary_pair_matcher_version or "unbound"
                        ),
                    },
                    context_scope=context_scope,
                    input_artifact_versions={
                        "revisit_pair": "topic-starter-v6-revisit-pair-v1",
                        "rid_training_tuple": RID_TRAINING_SCHEMA_VERSION,
                        "rid_user_context_snapshot": RID_USER_CONTEXT_SCHEMA_VERSION,
                        "training_exclusion": TRAINING_EXCLUSION_SCHEMA_VERSION,
                    },
                    utility_examples=(),
                    offline_metrics={
                        "pair_count": float(len(set(pair_ids))),
                        "training_tuple_count": float(len(set(tuple_ids))),
                    },
                    input_artifact_ids=tuple(
                        (*pair_ids, *tuple_ids, *sorted(context_ids))
                    ),
                    input_excluded_artifact_reasons=excluded,
                )
                if persist_outcome_artifact(
                    self._store,
                    training_slice,
                    created_at=label_horizon,
                ):
                    slice_recorded += 1

        return _RIDCorpusMaterialization(
            summary_recorded=summary_recorded,
            pair_recorded=pair_recorded,
            tuple_recorded=tuple_recorded,
            exclusion_recorded=exclusion_recorded,
            slice_recorded=slice_recorded,
            labels=labels,
        )

    def _record_active_signal(
        self,
        *,
        signal: _HistorySignal,
        user_state_snapshot_id: str,
        context_scope: str,
        manifest_id: str,
        exposure_id: str,
    ) -> tuple[int, int, int, int]:
        observation = ActiveExpressionObservation(
            user_state_snapshot_id=user_state_snapshot_id,
            source_locator=signal.source_locator,
            observed_at=signal.observed_at,
            input_origin=signal.input_origin,
            assistance_origin=signal.assistance_origin,
            context_scope=context_scope,
        )
        observation_recorded = int(
            _persist_training_event(
                self._store,
                event_type="active_expression_observation",
                artifact_id=observation.artifact_id,
                payload=observation.as_payload(),
                session_id=signal.session_id,
                exposure_id=exposure_id,
                created_at=signal.observed_at,
            )
        )
        admission = admit_active_expression(observation)
        if admission.example is None:
            exclusion_recorded = int(
                self._record_exclusion(
                    training_domain="pa_active",
                    source_artifact_id=observation.artifact_id,
                    reason=str(admission.exclusion_reason),
                    user_state_snapshot_id=user_state_snapshot_id,
                    context_scope=context_scope,
                    manifest_id=manifest_id,
                    exposure_id=exposure_id,
                    session_id=signal.session_id,
                    created_at=signal.observed_at,
                )
            )
            return observation_recorded, 0, 0, exclusion_recorded

        example = admission.example
        example_recorded = int(
            _persist_training_event(
                self._store,
                event_type="active_expression_example",
                artifact_id=example.artifact_id,
                payload=example.as_payload(),
                session_id=signal.session_id,
                exposure_id=exposure_id,
                created_at=signal.observed_at,
            )
        )
        training_example = example.to_training_example(
            positive_label=True,
            event_time=signal.observed_at,
        )
        pa_recorded = int(
            _persist_training_event(
                self._store,
                event_type="pa_training_example",
                artifact_id=training_example.artifact_id,
                payload=training_example.as_payload(),
                session_id=signal.session_id,
                exposure_id=exposure_id,
                created_at=signal.observed_at,
            )
        )
        return observation_recorded, example_recorded, pa_recorded, 0

    def collect(self, *, exposure_window: tuple[float, float] | None = None) -> OutcomeCollectionReport:
        """Collect one idempotent bounded maintenance pass."""

        self._check_cancelled()
        now = float(self._now())
        if not math.isfinite(now) or now < 0:
            raise ValueError("collector clock must be a finite timestamp")
        bound_events = _event_pages(
            self._store,
            event_type="session_bound",
            page_size=self._max_bound_events,
        )
        if exposure_window is not None:
            start, end = exposure_window
            if not (math.isfinite(start) and math.isfinite(end) and 0 <= start < end):
                raise ValueError("invalid exposure collection window")
            exposure_rows = _event_pages(
                self._store,
                event_type="dynamic_exposure",
                page_size=self._max_exposures,
            )
            exposure_ids: set[str] = set()
            for row in exposure_rows:
                created_at = row.get("created_at")
                if type(created_at) not in (int, float):
                    continue
                if not start <= created_at < end:
                    continue
                exposure_ids.add(str(row.get("slate_id")))
            bound_events = [
                row for row in bound_events
                if (row.get("payload") or {}).get("exposure_id") in exposure_ids
            ]
        session_metadata = {
            str(item.get("session_id") or "").strip(): item
            for item in self._sessions_loader()
            if isinstance(item, dict) and str(item.get("session_id") or "").strip()
        }
        history_cache: dict[str, list[dict[str, Any]]] = {}

        def load_history(session_id: str) -> list[dict[str, Any]]:
            if session_id not in history_cache:
                history_cache[session_id] = self._history_loader(session_id)
            return history_cache[session_id]

        mapped_intents = _mapped_intents(self._store)
        signals_by_session: dict[str, tuple[_HistorySignal, ...]] = {}

        def signals(session_id: str) -> tuple[_HistorySignal, ...]:
            if session_id not in signals_by_session:
                values: list[_HistorySignal] = []
                completed_prefix = project_completed_session_prefix(
                    session_id,
                    load_history(session_id),
                    allow_gaps=True,
                )
                completed_ordinals = (
                    set(completed_prefix.completed_user_ordinals)
                    if completed_prefix is not None
                    else set()
                )
                for ordinal, record in _user_records(load_history(session_id)):
                    if ordinal not in completed_ordinals:
                        continue
                    value = _signal(
                        session_id=session_id,
                        ordinal=ordinal,
                        record=record,
                        mapped_intent=mapped_intents.get((session_id, ordinal)),
                    )
                    if value is not None:
                        values.append(value)
                signals_by_session[session_id] = tuple(values)
            return signals_by_session[session_id]

        def summary_signals(
            summary: SessionSummaryArtifact,
        ) -> tuple[_HistorySignal, ...]:
            revisions = project_completed_session_prefixes(
                summary.session_id,
                load_history(summary.session_id),
                allow_gaps=True,
            )
            revision = next(
                (
                    item
                    for item in revisions
                    if item.boundary == summary.completion_boundary
                ),
                None,
            )
            if revision is None:
                return ()
            completed_ordinals = set(revision.completed_user_ordinals)
            session_signals = {
                (item.ordinal, item.source_event_id): item
                for item in signals(summary.session_id)
            }
            values: list[_HistorySignal] = []
            for locator in summary.source_locators:
                if locator.ordinal not in completed_ordinals:
                    return ()
                signal = session_signals.get((locator.ordinal, locator.event_id))
                if signal is None:
                    return ()
                values.append(signal)
            return tuple(values)

        serving_summaries: dict[str, SessionSummaryArtifact] = {}
        ambiguous_summary_ids: set[str] = set()
        page_size = max(1, self._max_exposures)
        carried_at = _summary_coverage_times(self._store, page_size=page_size)

        def remember_summary(summary: SessionSummaryArtifact) -> None:
            if summary.session_summary_id in ambiguous_summary_ids:
                return
            existing = serving_summaries.get(summary.session_summary_id)
            if existing is not None and existing != summary:
                ambiguous_summary_ids.add(summary.session_summary_id)
                serving_summaries.pop(summary.session_summary_id, None)
                return
            serving_summaries[summary.session_summary_id] = summary

        manifest_rows = self._store.query_events(
            event_type="starter_slate_manifest",
            policy_id=POLICY_ID,
            limit=self._max_exposures,
            newest_first=True,
        )
        for row in manifest_rows:
            manifest_payload = row.get("payload")
            if not isinstance(manifest_payload, dict):
                continue
            for summary in _manifest_session_summaries(manifest_payload):
                remember_summary(summary)
                if type(row.get("created_at")) in (int, float):
                    carried_at[summary.session_summary_id] = max(
                        carried_at.get(summary.session_summary_id, 0.0),
                        float(row["created_at"]),
                    )
        for summary in _covered_standalone_session_summaries(
            self._store,
            page_size=page_size,
        ):
            remember_summary(summary)
        for summary_id in ambiguous_summary_ids:
            serving_summaries.pop(summary_id, None)

        (
            summary_recorded,
            revisit_pair_recorded,
            rid_tuple_recorded,
            exclusion_recorded,
            rid_slice_recorded,
            rid_pair_labels,
        ) = self._materialize_rid_corpus(
            summaries=sorted(
                serving_summaries.values(),
                key=lambda summary: carried_at.get(summary.session_summary_id, 0.0),
                reverse=True,
            ),
            session_metadata=session_metadata,
            summary_signals=summary_signals,
        )
        depth_recorded = 0
        future_recorded = 0
        active_observation_recorded = 0
        active_example_recorded = 0
        active_pa_recorded = 0
        for bound in bound_events:
            self._check_cancelled()
            payload = bound.get("payload")
            if not isinstance(payload, dict):
                continue
            manifest_id = str(payload.get("manifest_id") or "").strip()
            exposure_id = str(payload.get("exposure_id") or "").strip()
            card_revision_id = str(
                payload.get("card_revision_id") or bound.get("topic_id") or ""
            ).strip()
            started_session_id = str(
                payload.get("started_session_id") or bound.get("session_id") or ""
            ).strip()
            context_scope = str(payload.get("context_scope") or "").strip()
            started_at = _timestamp(bound.get("created_at"))
            if not all(
                (
                    manifest_id,
                    exposure_id,
                    card_revision_id,
                    started_session_id,
                    context_scope,
                )
            ) or started_at is None:
                continue
            try:
                if project_id_for_scope(context_scope) != payload.get("project_id"):
                    continue
            except ValueError:
                continue
            if bound.get("session_id") != started_session_id:
                continue
            if bound.get("slate_id") != exposure_id:
                continue
            if bound.get("topic_id") != card_revision_id:
                continue

            started_users = _user_records(load_history(started_session_id))
            user_timestamps = [
                _timestamp(record.get("timestamp")) for _, record in started_users
            ]
            last_user_at = max(
                (stamp for stamp in user_timestamps if stamp is not None),
                default=started_at,
            )
            metadata_last_user_at = _timestamp(
                session_metadata.get(started_session_id, {}).get(
                    "last_user_message_at"
                )
            )
            if metadata_last_user_at is not None:
                last_user_at = max(last_user_at, metadata_last_user_at)
            exposed_at = _exact_exposed_at(
                self._store,
                manifest_id=manifest_id,
                exposure_id=exposure_id,
            )
            depth_label_horizon = (
                exposed_at if exposed_at is not None else started_at
            ) + self._label_horizon_seconds
            depth_users = []
            for _, record in started_users:
                record_timestamp = _timestamp(record.get("timestamp"))
                if record_timestamp is not None and record_timestamp <= depth_label_horizon:
                    depth_users.append(record)
            existing_depth = self._store.query_events(
                event_type="conversation_depth",
                slate_id=exposure_id,
                topic_id=card_revision_id,
                policy_id=POLICY_ID,
                limit=1,
            )
            _condition = (
                not existing_depth
                and now >= depth_label_horizon
                and now - last_user_at >= self._depth_quiescence_seconds
                and record_terminal_conversation_depth(
                    self._store,
                    manifest_id=manifest_id,
                    exposure_id=exposure_id,
                    card_revision_id=card_revision_id,
                    started_session_id=started_session_id,
                    conversation_depth=len(depth_users),
                    created_at=depth_label_horizon,
                )
            )
            if (
                _condition
            ):
                depth_recorded += 1

            manifest = _exact_manifest(self._store, manifest_id=manifest_id)
            batch_id = str(payload.get("generation_batch_id") or "").strip()
            project_id = payload.get("project_id")
            card = (
                find_exact_terminal_card(
                    self._store,
                    manifest_id=manifest_id,
                    card_revision_id=card_revision_id,
                    generation_batch_id=batch_id,
                    context_scope=context_scope,
                    project_id=project_id,
                )
                if manifest is not None and batch_id
                else None
            )
            user_state_snapshot_id = str(
                card.get("generation_input_snapshot_id")
                if isinstance(card, dict)
                else ""
            ).strip()
            if not user_state_snapshot_id:
                continue
            for started_signal in signals(started_session_id):
                if not started_at <= started_signal.observed_at <= depth_label_horizon:
                    continue
                recorded = self._record_active_signal(
                    signal=started_signal,
                    user_state_snapshot_id=user_state_snapshot_id,
                    context_scope=context_scope,
                    manifest_id=manifest_id,
                    exposure_id=exposure_id,
                )
                active_observation_recorded += recorded[0]
                active_example_recorded += recorded[1]
                active_pa_recorded += recorded[2]
                exclusion_recorded += recorded[3]
            interest_summary_id = str(
                card.get("interest_summary_id") or ""
                if isinstance(card, dict)
                else ""
            ).strip()
            rid = manifest.get("rid_result") if isinstance(manifest, dict) else None
            decisions: list[dict[str, Any]] = []
            if isinstance(rid, dict):
                for field in ("trigger_interests", "rejected_interests"):
                    values = rid.get(field)
                    if isinstance(values, list):
                        decisions.extend(
                            item for item in values if isinstance(item, dict)
                        )
            matched_decisions = [
                item
                for item in decisions
                if item.get("interest_summary_id") == interest_summary_id
            ]
            earlier: SessionSummaryArtifact | None = None
            _condition2 = len(matched_decisions) == 1 and interest_summary_id
            if _condition2:
                decision = matched_decisions[0]
                summary_ids = decision.get("source_session_summary_ids")
                source_event_ids = decision.get("source_event_ids")
                manifest_summaries = {
                    summary.session_summary_id: summary
                    for summary in _manifest_session_summaries(manifest)
                }
                candidate_summary = manifest_summaries.get(interest_summary_id)
                matched_summary = (
                    candidate_summary is not None
                    and summary_ids == [interest_summary_id]
                    and source_event_ids == list(candidate_summary.source_event_ids)
                    and candidate_summary.context_scope == context_scope
                )
                if matched_summary:
                    unique_sources = summary_signals(candidate_summary)
                    if unique_sources:
                        earlier = candidate_summary
                        if persist_outcome_artifact(
                            self._store,
                            earlier,
                            created_at=earlier.completed_at,
                        ):
                            summary_recorded += 1
            if earlier is None:
                continue

            subject_user_id = _session_user_id(
                session_metadata.get(earlier.session_id)
            )
            later_by_session: dict[
                str, tuple[SessionSummaryArtifact, tuple[_HistorySignal, ...]]
            ] = {}
            for candidate_summary in serving_summaries.values():
                later_session_id = candidate_summary.session_id
                metadata = session_metadata.get(later_session_id)
                if later_session_id == started_session_id:
                    continue
                if later_session_id == earlier.session_id:
                    continue
                if metadata is None:
                    continue
                if not _scope_matches(metadata, context_scope):
                    continue
                if candidate_summary.context_scope != context_scope:
                    continue
                if (
                    not (
                        started_at
                        < candidate_summary.completed_at
                        <= depth_label_horizon
                    )
                ):
                    continue
                candidate_signals = tuple(
                    item
                    for item in summary_signals(candidate_summary)
                    if started_at < item.observed_at <= depth_label_horizon
                )
                if not candidate_signals:
                    continue
                existing = later_by_session.get(later_session_id)
                if (
                    existing is None
                    or candidate_summary.completed_at < existing[0].completed_at
                ):
                    later_by_session[later_session_id] = (
                        candidate_summary,
                        candidate_signals,
                    )
            for later_session_id in sorted(later_by_session):
                self._check_cancelled()
                later, later_signals = later_by_session[later_session_id]
                metadata = session_metadata[later_session_id]
                later_signal = later_signals[0]
                if persist_outcome_artifact(
                    self._store,
                    later,
                    created_at=later.completed_at,
                ):
                    summary_recorded += 1
                later_user_id = _session_user_id(metadata)
                same_user = bool(
                    subject_user_id
                    and later_user_id
                    and subject_user_id == later_user_id
                )
                if same_user:
                    recorded = self._record_active_signal(
                        signal=later_signal,
                        user_state_snapshot_id=user_state_snapshot_id,
                        context_scope=context_scope,
                        manifest_id=manifest_id,
                        exposure_id=exposure_id,
                    )
                    active_observation_recorded += recorded[0]
                    active_example_recorded += recorded[1]
                    active_pa_recorded += recorded[2]
                    exclusion_recorded += recorded[3]
                else:
                    continue
                match_label = rid_pair_labels.get(
                    (earlier.session_summary_id, later.session_summary_id)
                )
                if match_label is None:
                    match_label, _ = self._match_summary_pair(earlier, later)
                if match_label is None:
                    continue
                pair = build_revisit_pair(
                    earlier,
                    later,
                    match_label=match_label,
                    matcher_id=str(self._summary_pair_matcher_id),
                    matcher_version=str(self._summary_pair_matcher_version),
                )
                if persist_outcome_artifact(
                    self._store,
                    pair,
                    created_at=pair.observed_at,
                ):
                    revisit_pair_recorded += 1
                if pair.match_label and (
                    pair.natural_eligible
                    or pair.exclusion_reason
                    in {"ineligible_input_origin", "assisted_input_excluded"}
                ):
                    if record_terminal_future_activity(
                        self._store,
                        manifest_id=manifest_id,
                        exposure_id=exposure_id,
                        card_revision_id=card_revision_id,
                        started_session_id=started_session_id,
                        later_session_id=later_session_id,
                        source_event_id=later_signal.source_event_id,
                        source_locator=later_signal.source_locator,
                        input_origin=later_signal.input_origin,
                        assistance_origin=later_signal.assistance_origin,
                        context_scope=context_scope,
                        created_at=later_signal.observed_at,
                    ):
                        future_recorded += 1

        (
            starter_outcomes_recorded,
            utility_examples_recorded,
            preference_pairs_recorded,
            training_slices_recorded,
            passive_starter_examples_recorded,
            passive_pa_examples_recorded,
            passive_exclusions_recorded,
        ) = self._materialize_training_plane(now=now, exposure_window=exposure_window)

        return OutcomeCollectionReport(
            bound_sessions_scanned=len(bound_events),
            depth_events_recorded=depth_recorded,
            future_activity_events_recorded=future_recorded,
            session_summary_artifacts_recorded=summary_recorded,
            revisit_pair_artifacts_recorded=revisit_pair_recorded,
            rid_training_tuple_artifacts_recorded=rid_tuple_recorded,
            active_expression_observation_artifacts_recorded=(
                active_observation_recorded
            ),
            active_expression_example_artifacts_recorded=active_example_recorded,
            passive_starter_example_artifacts_recorded=(
                passive_starter_examples_recorded
            ),
            pa_training_example_artifacts_recorded=(
                active_pa_recorded + passive_pa_examples_recorded
            ),
            training_exclusion_artifacts_recorded=(
                exclusion_recorded + passive_exclusions_recorded
            ),
            starter_outcome_artifacts_recorded=starter_outcomes_recorded,
            utility_example_artifacts_recorded=utility_examples_recorded,
            preference_pair_artifacts_recorded=preference_pairs_recorded,
            training_slice_artifacts_recorded=(
                rid_slice_recorded + training_slices_recorded
            ),
        )

    def _materialize_training_plane(
        self,
        *,
        now: float,
        exposure_window: tuple[float, float] | None = None,
    ) -> _TrainingPlaneMaterialization:
        outcome_recorded = 0
        utility_recorded = 0
        preference_recorded = 0
        slice_recorded = 0
        passive_recorded = 0
        passive_pa_recorded = 0
        exclusion_recorded = 0
        exposures = _event_pages(
            self._store,
            event_type="dynamic_exposure",
            page_size=self._max_exposures,
        )
        for exposure in exposures:
            self._check_cancelled()
            payload = exposure.get("payload")
            exposed_at = _timestamp(exposure.get("created_at"))
            if not isinstance(payload, dict) or exposed_at is None:
                continue
            if exposure_window is not None and not exposure_window[0] <= exposed_at < exposure_window[1]:
                continue
            manifest_id = str(payload.get("manifest_id") or "").strip()
            exposure_id = str(exposure.get("slate_id") or "").strip()
            if not manifest_id or not exposure_id:
                continue
            label_horizon = exposed_at + self._label_horizon_seconds
            if now < label_horizon:
                continue
            if not _started_depths_finalized(
                self._store,
                manifest_id=manifest_id,
                exposure_id=exposure_id,
                exposed_at=exposed_at,
                label_horizon=label_horizon,
            ):
                continue
            joined = join_starter_outcomes(
                self._store,
                manifest_id=manifest_id,
                exposure_id=exposure_id,
                label_horizon=label_horizon,
            )
            if not joined:
                continue
            utilities = []
            input_artifact_ids: list[str] = []
            excluded_artifacts: dict[str, str] = {}
            for outcome in joined:
                if persist_outcome_artifact(
                    self._store,
                    outcome,
                    created_at=label_horizon,
                ):
                    outcome_recorded += 1
                utility = build_utility_training_example(
                    outcome,
                    feature_snapshot={
                        "snapshot_id": outcome.user_state_snapshot_id,
                        "observed_at": outcome.exposed_at,
                        "version": FEATURE_SNAPSHOT_VERSION,
                        "features": {
                            "exposure_position": outcome.exposure_position,
                            "version_refs": dict(outcome.version_refs),
                        },
                    },
                )
                utilities.append(utility)
                if persist_outcome_artifact(
                    self._store,
                    utility,
                    created_at=label_horizon,
                ):
                    utility_recorded += 1

            context_scope = joined[0].context_scope
            user_state_snapshot_id = joined[0].user_state_snapshot_id
            exclusion_rows = self._store.query_events(
                event_type="training_exclusion",
                slate_id=exposure_id,
                policy_id=POLICY_ID,
                limit=self._max_bound_events,
            )
            exclusion_records: list[TrainingExclusionRecord] = []
            for row in exclusion_rows:
                payload = row.get("payload")
                if not isinstance(payload, dict):
                    continue
                if payload.get("manifest_id") != manifest_id:
                    continue
                if _timestamp(row.get("created_at")) is None:
                    continue
                if float(row["created_at"]) > label_horizon:
                    continue
                exclusion_records.append(
                    TrainingExclusionRecord.from_payload(payload)
                )
            excluded_sources = {
                record.source_artifact_id: record.reason
                for record in exclusion_records
            }
            excluded_artifacts.update(excluded_sources)

            active_observation_rows = self._store.query_events(
                event_type="active_expression_observation",
                slate_id=exposure_id,
                policy_id=POLICY_ID,
                limit=self._max_bound_events,
            )
            for row in active_observation_rows:
                observation = ActiveExpressionObservation.from_payload(row["payload"])
                _condition4 = (
                    observation.user_state_snapshot_id == user_state_snapshot_id
                    and observation.context_scope == context_scope
                    and observation.observed_at <= label_horizon
                    and observation.artifact_id not in excluded_sources
                )
                if (
                    _condition4
                ):
                    input_artifact_ids.append(observation.artifact_id)

            for event_type, artifact_type in (
                ("active_expression_example", ActiveExpressionExample),
                ("pa_training_example", PATrainingExample),
            ):
                for row in self._store.query_events(
                    event_type=event_type,
                    slate_id=exposure_id,
                    policy_id=POLICY_ID,
                    limit=self._max_bound_events,
                ):
                    artifact = artifact_type.from_payload(row["payload"])
                    artifact_time = (
                        artifact.event_time
                        if isinstance(artifact, PATrainingExample)
                        else artifact.observed_at
                    )
                    _condition5 = (
                        artifact.user_state_snapshot_id == user_state_snapshot_id
                        and artifact.context_scope == context_scope
                        and artifact_time <= label_horizon
                        and (
                            not isinstance(artifact, PATrainingExample)
                            or artifact.domain == "active"
                        )
                    )
                    if (
                        _condition5
                    ):
                        input_artifact_ids.append(artifact.artifact_id)

            manifest = _exact_manifest(self._store, manifest_id=manifest_id)
            trace = None
            if isinstance(manifest, dict):
                list_search = manifest.get("list_search")
                if isinstance(list_search, dict):
                    value = list_search.get("bounded_beam_trace")
                    if isinstance(value, dict):
                        trace = value
            batch_id = str(payload.get("generation_batch_id") or "").strip()
            project_id = payload.get("project_id")
            for outcome in joined:
                card = (
                    find_exact_terminal_card(
                        self._store,
                        manifest_id=manifest_id,
                        card_revision_id=outcome.card_revision_id,
                        generation_batch_id=batch_id,
                        context_scope=context_scope,
                        project_id=project_id,
                    )
                    if manifest is not None and batch_id
                    else None
                )
                source_starter_id = str(
                    (
                        card.get("source_starter_id")
                        if isinstance(card, dict)
                        else ""
                    )
                    or ""
                ).strip()
                corpus_version = str(
                    card.get("corpus_version")
                    if isinstance(card, dict)
                    else ""
                ).strip()
                if not source_starter_id:
                    continue
                if not corpus_version or not outcome.version_refs:
                    exclusion_recorded += int(
                        self._record_exclusion(
                            training_domain="pa_passive",
                            source_artifact_id=outcome.outcome_id,
                            reason="missing_passive_feature_lineage",
                            user_state_snapshot_id=user_state_snapshot_id,
                            context_scope=context_scope,
                            manifest_id=manifest_id,
                            exposure_id=exposure_id,
                            session_id=outcome.started_session_id or exposure_id,
                            created_at=label_horizon,
                        )
                    )
                    excluded_artifacts[outcome.outcome_id] = (
                        "missing_passive_feature_lineage"
                    )
                    continue
                passive = PassiveStarterExample(
                    user_state_snapshot_id=user_state_snapshot_id,
                    context_scope=context_scope,
                    corpus_version=corpus_version,
                    starter_id=source_starter_id,
                    exposure_id=exposure_id,
                    manifest_id=manifest_id,
                    card_revision_id=outcome.card_revision_id,
                    exposed_at=outcome.exposed_at,
                    clicked_at=outcome.clicked_at,
                    not_interested_at=outcome.not_interested_at,
                    feature_version_refs=dict(outcome.version_refs),
                )
                if _persist_training_event(
                    self._store,
                    event_type="passive_starter_example",
                    artifact_id=passive.artifact_id,
                    payload=passive.as_payload(),
                    session_id=outcome.started_session_id or exposure_id,
                    exposure_id=exposure_id,
                    created_at=label_horizon,
                ):
                    passive_recorded += 1
                if outcome.clicked == outcome.not_interested:
                    reason = (
                        "conflicting_passive_labels"
                        if outcome.clicked
                        else "missing_passive_label"
                    )
                    exclusion_recorded += int(
                        self._record_exclusion(
                            training_domain="pa_passive",
                            source_artifact_id=passive.artifact_id,
                            reason=reason,
                            user_state_snapshot_id=user_state_snapshot_id,
                            context_scope=context_scope,
                            manifest_id=manifest_id,
                            exposure_id=exposure_id,
                            session_id=outcome.started_session_id or exposure_id,
                            created_at=label_horizon,
                        )
                    )
                    excluded_artifacts[passive.artifact_id] = reason
                    continue
                positive_label = outcome.clicked
                event_time = (
                    outcome.clicked_at
                    if positive_label
                    else outcome.not_interested_at
                )
                if event_time is None:
                    continue
                passive_training = passive.to_training_example(
                    positive_label=positive_label,
                    event_time=event_time,
                )
                if _persist_training_event(
                    self._store,
                    event_type="pa_training_example",
                    artifact_id=passive_training.artifact_id,
                    payload=passive_training.as_payload(),
                    session_id=outcome.started_session_id or exposure_id,
                    exposure_id=exposure_id,
                    created_at=event_time,
                ):
                    passive_pa_recorded += 1
                input_artifact_ids.extend(
                    (passive.artifact_id, passive_training.artifact_id)
                )
            positive_id_items: list[str] = []
            for outcome in joined:
                if outcome.not_interested:
                    continue
                engaged = any(
                    (
                        outcome.clicked,
                        outcome.started,
                        outcome.future_natural_activity,
                    )
                )
                if not engaged:
                    continue
                positive_id_items.append(outcome.card_revision_id)
            positive_ids = tuple(positive_id_items)
            ranker_version = str(
                trace.get("utility_scorer_version") if trace else ""
            ).strip()
            diversity_version = str(
                trace.get("diversity_scorer_version") if trace else ""
            ).strip()
            generator_version = str(
                manifest.get("generation_config_version")
                if isinstance(manifest, dict)
                else ""
            ).strip()
            _condition3 = (
                positive_ids
                and ranker_version
                and diversity_version
                and generator_version
            )
            if (
                _condition3
            ):
                for negative in joined:
                    if not negative.not_interested:
                        continue
                    preference = PreferencePair(
                        preference_pair_id=(
                            f"preference:{exposure_id}:"
                            f"{negative.card_revision_id}:h{label_horizon:.6f}"
                        ),
                        context_scope=negative.context_scope,
                        manifest_id=manifest_id,
                        exposure_id=exposure_id,
                        preferred_card_revision_ids=positive_ids,
                        negative_card_revision_id=negative.card_revision_id,
                        negative_type="not_interested",
                        ranker_version=ranker_version,
                        diversity_version=diversity_version,
                        generator_version=generator_version,
                        created_at=label_horizon,
                    )
                    input_artifact_ids.append(preference.preference_pair_id)
                    if persist_outcome_artifact(
                        self._store,
                        preference,
                        created_at=label_horizon,
                    ):
                        preference_recorded += 1

            training_slice = build_training_slice_manifest(
                training_slice_manifest_id=(
                    f"training-slice:{exposure_id}:h{label_horizon:.6f}"
                ),
                window_start=exposed_at,
                window_end=label_horizon + 1.0,
                label_horizon=label_horizon,
                filters={
                    "manifest_id": manifest_id,
                    "exposure_id": exposure_id,
                    "label_horizon_seconds": self._label_horizon_seconds,
                },
                context_scope=context_scope,
                input_artifact_versions={
                    "starter_outcome": STARTER_OUTCOME_SCHEMA_VERSION,
                    "utility_training": UTILITY_TRAINING_SCHEMA_VERSION,
                    "preference_pair": PREFERENCE_PAIR_SCHEMA_VERSION,
                    "rid_training_tuple": RID_TRAINING_SCHEMA_VERSION,
                    "active_expression_observation": (
                        ACTIVE_EXPRESSION_OBSERVATION_SCHEMA_VERSION
                    ),
                    "active_expression_example": (
                        ACTIVE_EXPRESSION_EXAMPLE_SCHEMA_VERSION
                    ),
                    "passive_starter_example": (
                        PASSIVE_STARTER_EXAMPLE_SCHEMA_VERSION
                    ),
                    "pa_training_example": PA_TRAINING_EXAMPLE_SCHEMA_VERSION,
                    "training_exclusion": TRAINING_EXCLUSION_SCHEMA_VERSION,
                },
                utility_examples=tuple(utilities),
                offline_metrics={"example_count": float(len(utilities))},
                input_artifact_ids=tuple(input_artifact_ids),
                input_excluded_artifact_reasons=excluded_artifacts,
            )
            if persist_outcome_artifact(
                self._store,
                training_slice,
                created_at=label_horizon,
            ):
                slice_recorded += 1
        return _TrainingPlaneMaterialization(
            starter_outcomes_recorded=outcome_recorded,
            utility_examples_recorded=utility_recorded,
            preference_pairs_recorded=preference_recorded,
            training_slices_recorded=slice_recorded,
            passive_starter_examples_recorded=passive_recorded,
            passive_pa_examples_recorded=passive_pa_recorded,
            passive_exclusions_recorded=exclusion_recorded,
        )

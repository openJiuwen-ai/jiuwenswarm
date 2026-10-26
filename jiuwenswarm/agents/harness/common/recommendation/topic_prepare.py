# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Prepared card pool: background orchestration and the foreground pool service."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from functools import partial
from typing import Any

from jiuwenswarm.agents.harness.common.recommendation import (
    topic_history as history,
)
from jiuwenswarm.agents.harness.common.recommendation.topic_feedback import (
    build_feedback_context,
)
from jiuwenswarm.agents.harness.common.recommendation.topic_generation import (
    ArkCandidateGenerationAdapter,
    ArkInterestRankingAdapter,
    ArkSlateSelectionAdapter,
    I2_PROMPT_VERSION,
    I4_PROMPT_VERSION,
    I5_PROMPT_VERSION,
    RankedInterest,
    generate_starter_candidates,
    rank_interest_summaries,
    select_starter_slate,
)
from jiuwenswarm.agents.harness.common.recommendation.topic_history import (
    load_latest_history_state,
)
from jiuwenswarm.agents.harness.common.recommendation.topic_model import (
    ArkBackgroundConfig,
    build_ark_stage_invoker,
    persist_provider_invocation,
    provider_invocation_context,
)
from jiuwenswarm.agents.harness.common.recommendation.topic_pool import (
    BackgroundGenerationCoordinator,
    BackgroundGenerationResult,
    PAYLOAD_VERSION,
    POLICY_ID,
    build_generation_trigger_key,
    build_serving_compatibility_key,
    find_exact_pool_card,
    read_starter_pool,
)
from jiuwenswarm.agents.harness.common.recommendation.topic_sources import (
    ArkFixedStarterRankingAdapter,
    CORPUS_VERSION,
    FetchFn,
    I3_RETRIEVAL_PROMPT_VERSION,
    RankedInterest as CorpusRankedInterest,
    RETRIEVAL_CONFIG_VERSION,
    SearchFn,
    SourceClassifier,
    StarterHit,
    default_fetch,
    default_search,
    default_source_classifier,
    ground_candidates,
    load_fixed_starter_corpus,
    retrieve_fixed_starters,
)
from jiuwenswarm.agents.harness.common.recommendation.topic_starter import (
    TopicStarterStore,
)

CURRENT_EVENT_TTL_SECONDS = 48 * 60 * 60
RECENT_DEVELOPMENT_TTL_SECONDS = 7 * 24 * 60 * 60
EVERGREEN_TTL_SECONDS = 30 * 24 * 60 * 60
RECENT_EXPOSURE_WINDOW_SECONDS = 7 * 24 * 60 * 60
STATE_MAINTENANCE_RECEIPT_VERSION = "topic-starter-v6-state-maintenance-v1"
STATE_MAINTENANCE_EVENT_TYPE = "state_maintenance_receipt"
STATE_MAINTENANCE_POLICY_ID = "topic-starter-state-v1"
EXTERNAL_INVOCATION_ATTEMPT_EVENT_TYPE = "external_invocation_attempt"
EXTERNAL_INVOCATION_ATTEMPT_SCHEMA_VERSION = (
    "topic-starter-v5-external-invocation-attempt-v1"
)
EXTERNAL_INVOCATION_AUDIT_SCHEMA_VERSION = (
    "topic-starter-v5-external-invocation-audit-v1"
)

_STATE_MAINTENANCE_RECEIPT_FIELDS = frozenset(
    {
        "receipt_version",
        "receipt_id",
        "input_snapshot_id",
        "source_watermark",
        "history_revision",
        "resolved_mapping_id",
        "mapping_watermark",
        "v4_state_id",
        "v4_state_version",
        "profile_snapshot_id",
        "profile_semantic_version",
        "negative_preference_version",
        "refresh_reason",
    }
)

StageInvoker = Callable[[str, str, str], Awaitable[Any]]
StageInvokerFactory = Callable[[ArkBackgroundConfig], StageInvoker]
RecordsLoader = Callable[[], dict[str, list[dict[str, Any]]]]


@dataclass(frozen=True)
class V5GenerationInputs:
    state: dict[str, Any]
    active_behaviors: tuple[history.CanonicalBehavior, ...]
    delta_behaviors: tuple[history.CanonicalBehavior, ...]
    generation_input_snapshot: dict[str, Any]


InputLoader = Callable[[], V5GenerationInputs]


class StateMaintenanceReceiptError(ValueError):
    """A frozen v4 state cannot form the production S1 receipt."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class _ExternalInvocationAttemptAudit:
    """Persist only safe identities immediately before external calls."""

    def __init__(
        self,
        store: TopicStarterStore,
        *,
        now: Callable[[], float],
    ) -> None:
        self._store = store
        self._now = now
        self._generation_run_id = f"v5-generation-run-{uuid.uuid4().hex}"
        self._counts = {"ark": 0, "search": 0, "fetch": 0}
        self._sequence = 0

    def record(self, operation: str, component_id: str) -> str:
        if operation not in self._counts or not component_id:
            raise ValueError("unsupported external invocation attempt")
        self._sequence += 1
        attempt_id = f"v5-external-attempt-{uuid.uuid4().hex}"
        recorded = self._store.record_event(
            {
                "session_id": self._generation_run_id,
                "slate_id": self._generation_run_id,
                "event_type": EXTERNAL_INVOCATION_ATTEMPT_EVENT_TYPE,
                "topic_id": attempt_id,
                "policy_id": POLICY_ID,
                "selection_mode": "external_invocation_attempt",
                "propensity_reason": "not_estimated",
                "payload": {
                    "schema_version": EXTERNAL_INVOCATION_ATTEMPT_SCHEMA_VERSION,
                    "generation_run_id": self._generation_run_id,
                    "attempt_id": attempt_id,
                    "sequence": self._sequence,
                    "operation": operation,
                    "component_id": component_id,
                },
                "created_at": self._now(),
            }
        )
        if not recorded:
            raise RuntimeError("external invocation attempt was not persisted")
        self._counts[operation] += 1
        return attempt_id

    def receipt(self) -> dict[str, Any]:
        return {
            "schema_version": EXTERNAL_INVOCATION_AUDIT_SCHEMA_VERSION,
            "generation_run_id": self._generation_run_id,
            "policy_id": POLICY_ID,
            "attempt_event_type": EXTERNAL_INVOCATION_ATTEMPT_EVENT_TYPE,
            "counters": {
                "total": sum(self._counts.values()),
                **self._counts,
            },
        }


def _state_maintenance_identity(state: Mapping[str, Any]) -> dict[str, Any]:
    ledger = state.get("history_ledger")
    if not isinstance(ledger, dict):
        raise StateMaintenanceReceiptError("state_unavailable")
    active = history.active_ledger_behaviors(ledger)
    if not active:
        raise StateMaintenanceReceiptError("state_unavailable")
    state_version = int(state.get("version") or 0)
    if state_version <= 0:
        raise StateMaintenanceReceiptError("state_unavailable")
    active_indices = {item.behavior_index for item in active}
    mapping = history.state_mapping_entries(dict(state))
    if not active_indices <= set(mapping):
        raise StateMaintenanceReceiptError("state_mapping_unavailable")
    source_watermark = max(
        int(ledger.get("next_behavior_index") or 1) - 1,
        max(active_indices),
    )
    mapping_watermark = max(active_indices)
    history_revision = int(ledger.get("history_revision") or 0)
    input_snapshot_id = (
        f"v4-state-input:{state_version}:{source_watermark}:{history_revision}"
    )
    return {
        "receipt_version": STATE_MAINTENANCE_RECEIPT_VERSION,
        "receipt_id": f"state-receipt:{input_snapshot_id}",
        "input_snapshot_id": input_snapshot_id,
        "source_watermark": source_watermark,
        "history_revision": history_revision,
        "resolved_mapping_id": (f"v4-mapping:{state_version}:{mapping_watermark}"),
        "mapping_watermark": mapping_watermark,
        "v4_state_id": f"v4-state:{state_version}",
        "v4_state_version": state_version,
        "profile_snapshot_id": f"profile-snapshot:{state_version}",
        "profile_semantic_version": int(
            ((state.get("memory") or {}).get("semantic_version")) or 0
        ),
        "negative_preference_version": int(
            ((state.get("memory_sidecar") or {}).get("negative_preference_version"))
            or 0
        ),
    }


def build_state_maintenance_receipt(
    state: Mapping[str, Any], *, refresh_reason: str
) -> dict[str, Any]:
    reason = str(refresh_reason or "").strip()
    if not reason:
        raise StateMaintenanceReceiptError("state_receipt_invalid")
    return {
        **_state_maintenance_identity(state),
        "refresh_reason": reason,
    }


def _load_state_maintenance_receipt(
    store: TopicStarterStore,
    state: Mapping[str, Any],
) -> dict[str, Any] | None:
    try:
        identity = _state_maintenance_identity(state)
    except StateMaintenanceReceiptError:
        return None
    rows = store.query_events(
        event_type=STATE_MAINTENANCE_EVENT_TYPE,
        slate_id=str(identity["receipt_id"]),
        topic_id=str(identity["receipt_id"]),
        policy_id=STATE_MAINTENANCE_POLICY_ID,
        limit=1,
        newest_first=True,
    )
    if len(rows) != 1:
        return None
    payload = rows[0].get("payload")
    if not isinstance(payload, dict):
        return None
    if set(payload) != _STATE_MAINTENANCE_RECEIPT_FIELDS:
        return None
    if any(payload.get(field) != value for field, value in identity.items()):
        return None
    if not str(payload.get("refresh_reason") or "").strip():
        return None
    return dict(payload)


def persist_state_maintenance_receipt(
    store: TopicStarterStore,
    state: Mapping[str, Any],
    *,
    refresh_reason: str,
    created_at: float,
) -> dict[str, Any]:
    existing = _load_state_maintenance_receipt(store, state)
    if existing is not None:
        return existing
    receipt = build_state_maintenance_receipt(
        state,
        refresh_reason=refresh_reason,
    )
    store.record_event(
        {
            "session_id": "v4-state",
            "slate_id": receipt["receipt_id"],
            "event_type": STATE_MAINTENANCE_EVENT_TYPE,
            "topic_id": receipt["receipt_id"],
            "profile_version": receipt["v4_state_version"],
            "policy_id": STATE_MAINTENANCE_POLICY_ID,
            "selection_mode": "state_maintenance",
            "propensity_reason": "not_estimated",
            "payload": receipt,
            "created_at": created_at,
        }
    )
    stored = _load_state_maintenance_receipt(store, state)
    if stored is None:
        raise StateMaintenanceReceiptError("state_receipt_persist_failed")
    return stored


def _recent_exposure_window(
    store: TopicStarterStore,
    *,
    now: float,
) -> list[dict[str, Any]]:
    cutoff = now - RECENT_EXPOSURE_WINDOW_SECONDS
    exposed: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for event in store.query_events(
        event_type="dynamic_exposure",
        policy_id=POLICY_ID,
        limit=256,
        newest_first=True,
    ):
        if float(event.get("created_at") or 0.0) < cutoff:
            continue
        payload = event.get("payload")
        if not isinstance(payload, dict):
            continue
        manifest_id = str(payload.get("manifest_id") or "")
        batch_id = str(payload.get("generation_batch_id") or "")
        for topic in payload.get("topics") or []:
            if not isinstance(topic, dict):
                continue
            revision_id = str(topic.get("card_revision_id") or "")
            key = (manifest_id, batch_id, revision_id)
            if not all(key) or key in seen:
                continue
            card = find_exact_pool_card(
                store,
                manifest_id=manifest_id,
                card_revision_id=revision_id,
                generation_batch_id=batch_id,
            )
            if card is None:
                continue
            seen.add(key)
            exposed.append(
                {
                    "card_revision_id": revision_id,
                    "source_starter_id": card.get("source_starter_id"),
                    "title": card.get("title"),
                    "intent": card.get("intent"),
                    "observed_at": float(event.get("created_at") or 0.0),
                }
            )
    return exposed


def load_generation_inputs(
    store: TopicStarterStore,
    *,
    now: float,
    records_loader: Callable[[], dict[str, list[dict[str, Any]]]] = (
        history.load_all_session_records
    ),
) -> V5GenerationInputs:
    """Freeze state, active delta, and pre-generation exposure context once."""

    state = history.load_latest_history_state(store) or history.empty_history_state()
    records = records_loader()
    active = history.active_behaviors_from_records(
        state.get("history_ledger") or {}, records
    )
    cursor = int(
        (state.get("memory_cursor") or {}).get("consumed_through_behavior_index") or 0
    )
    delta = history.uncurated_mapped_behaviors(state, active)
    active_delta_version = max(
        (behavior.behavior_index for behavior in active), default=cursor
    )
    state_receipt = _load_state_maintenance_receipt(store, state)
    generation_input_snapshot: dict[str, Any] = {
        "active_delta_version": active_delta_version,
        "external_memory_version": int(state.get("version") or 0),
        "recent_exposure_window": _recent_exposure_window(store, now=now),
    }
    if state_receipt is not None:
        generation_input_snapshot["state_maintenance_receipt"] = state_receipt
    return V5GenerationInputs(
        state=state,
        active_behaviors=tuple(active),
        delta_behaviors=tuple(delta),
        generation_input_snapshot=generation_input_snapshot,
    )


def _source_messages(
    active: tuple[history.CanonicalBehavior, ...],
) -> list[dict[str, Any]]:
    return [
        {
            "behavior_id": behavior.behavior_index,
            "source_locator": [behavior.session_id, behavior.ordinal],
            "content": behavior.content,
        }
        for behavior in active
    ]


def _interest_snapshot(
    inputs: V5GenerationInputs,
    buckets: dict[str, history.AnchorBucket],
) -> list[dict[str, Any]]:
    units = {
        str((unit.get("companion") or {}).get("unit_id") or ""): unit
        for unit in ((inputs.state.get("memory") or {}).get("units") or [])
    }
    delta_contents = [behavior.content for behavior in inputs.delta_behaviors]
    snapshot: list[dict[str, Any]] = []
    for interest_id, bucket in buckets.items():
        if not bucket.anchors or not bucket.locators:
            continue
        unit = units.get(interest_id) or {}
        summary = str(
            ((unit.get("core") or {}).get("preference_summary")) or bucket.anchors[0]
        ).strip()
        supporting_delta = any(
            anchor in content for content in delta_contents for anchor in bucket.anchors
        )
        snapshot.append(
            {
                "interest_summary_id": interest_id,
                "interest_summary": summary,
                "source_memory_unit_ids": [interest_id],
                "source_locators": [list(locator) for locator in bucket.locators],
                "allowed_anchors": list(bucket.anchors),
                "provenance_relation": (
                    "recent_follow_up" if supporting_delta else "stable_progress"
                ),
            }
        )
    return snapshot


def _delta_payload(
    inputs: V5GenerationInputs,
) -> list[dict[str, Any]]:
    mapping = history.state_mapping_entries(inputs.state)
    return [
        {
            "behavior_id": behavior.behavior_index,
            "content": behavior.content,
            "source_locator": [behavior.session_id, behavior.ordinal],
            "tag": (mapping.get(behavior.behavior_index) or {}).get("tag"),
        }
        for behavior in inputs.delta_behaviors
    ]


def _corpus_ranked_interests(
    ranked: list[RankedInterest],
    context_by_id: dict[str, dict[str, Any]],
) -> list[CorpusRankedInterest]:
    result: list[CorpusRankedInterest] = []
    for interest in ranked:
        context = context_by_id[interest.interest_summary_id]
        result.append(
            CorpusRankedInterest(
                interest_summary_id=interest.interest_summary_id,
                interest_summary=str(context["interest_summary"]),
                rank=interest.rank,
                allowed_anchors=tuple(context["allowed_anchors"]),
                source_locators=tuple(
                    (str(locator[0]), int(locator[1]))
                    for locator in context["source_locators"]
                ),
            )
        )
    return result


def _fixed_hit_payload(
    hit: StarterHit,
    context_by_id: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    if hit.interest_summary_id is None:
        return {
            "starter_id": hit.starter_id,
            "text": hit.text,
            "interest_summary_id": None,
            "source_memory_unit_ids": [],
            "source_locators": [],
            "identity_anchor": "",
            "context_anchors": [],
            "freshness_class": hit.freshness_class,
        }
    context = context_by_id[hit.interest_summary_id]
    identity = str(hit.matched_anchor or "")
    return {
        "starter_id": hit.starter_id,
        "text": hit.text,
        "interest_summary_id": hit.interest_summary_id,
        "source_memory_unit_ids": list(context["source_memory_unit_ids"]),
        "source_locators": [list(locator) for locator in hit.source_locators],
        "identity_anchor": identity,
        "context_anchors": [
            anchor for anchor in context["allowed_anchors"] if anchor != identity
        ],
        "freshness_class": hit.freshness_class,
    }


def _recently_exposed(
    candidate: dict[str, Any],
    *,
    titles: set[str],
    source_ids: set[str],
) -> bool:
    if str(candidate.get("title") or "") in titles:
        return True
    source_starter_id = candidate.get("source_starter_id")
    if not source_starter_id:
        return False
    return str(source_starter_id) in source_ids


def _not_recently_exposed(
    candidates: list[dict[str, Any]],
    window: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    titles = {str(item.get("title") or "") for item in window}
    source_ids = {str(item.get("source_starter_id") or "") for item in window}
    return [
        candidate
        for candidate in candidates
        if not _recently_exposed(candidate, titles=titles, source_ids=source_ids)
    ]


def _ttl_seconds(freshness_class: str) -> int:
    if freshness_class == "current_event":
        return CURRENT_EVENT_TTL_SECONDS
    if freshness_class == "recent_development":
        return RECENT_DEVELOPMENT_TTL_SECONDS
    return EVERGREEN_TTL_SECONDS


def _empty_result(
    trigger_key: dict[str, Any],
    trigger_origin: str,
    generation_input_snapshot: dict[str, Any],
    reason: str,
    *,
    external_invocation_audit: dict[str, Any] | None = None,
) -> BackgroundGenerationResult:
    return BackgroundGenerationResult(
        cards=[],
        manifest={
            "generation_trigger_key": trigger_key,
            "trigger_origin": trigger_origin,
            "generation_input_snapshot": generation_input_snapshot,
            **(
                {"external_invocation_audit": external_invocation_audit}
                if external_invocation_audit is not None
                else {}
            ),
        },
        failure_reason=reason,
    )


class TopicPreparePipeline:
    """One replaceable five-interface pipeline; persistence stays outside."""

    def __init__(
        self,
        store: TopicStarterStore,
        *,
        input_loader: InputLoader | None = None,
        records_loader: RecordsLoader | None = None,
        stage_invoker_factory: StageInvokerFactory = build_ark_stage_invoker,
        search: SearchFn = default_search,
        fetch: FetchFn = default_fetch,
        classify_source: SourceClassifier = default_source_classifier,
        now: Callable[[], float] = time.time,
    ) -> None:
        self.store = store
        self._now = now
        self._records_loader = records_loader or history.load_all_session_records
        self._input_loader = input_loader or (
            lambda: load_generation_inputs(
                store,
                now=now(),
                records_loader=self._records_loader,
            )
        )
        if stage_invoker_factory is build_ark_stage_invoker:
            stage_invoker_factory = partial(
                build_ark_stage_invoker,
                receipt_sink=partial(persist_provider_invocation, store),
            )
        self._stage_invoker_factory = stage_invoker_factory
        self._search = search
        self._fetch = fetch
        self._classify_source = classify_source

    async def refresh_state(self, config: ArkBackgroundConfig) -> str:
        """Run the shared history/mapping/memory lane before v5 I1–I5."""

        audit = _ExternalInvocationAttemptAudit(self.store, now=self._now)
        return await self._refresh_state(config, audit)

    async def _refresh_state(
        self,
        config: ArkBackgroundConfig,
        audit: _ExternalInvocationAttemptAudit,
    ) -> str:
        if config.context_window_tokens is None:
            return "context_limit_unverified"
        try:
            base_invoke_stage = self._stage_invoker_factory(config)
        except Exception:  # noqa: BLE001 - unavailable provider is fail-closed
            return "background_model_unavailable"
        provider_unavailable = False

        async def invoke_stage(stage: str, system_prompt: str, payload: str) -> Any:
            nonlocal provider_unavailable
            attempt_id = audit.record("ark", stage)
            try:
                with provider_invocation_context(host_attempt_id=attempt_id):
                    return await base_invoke_stage(stage, system_prompt, payload)
            except Exception:  # noqa: BLE001 - v4 adapters classify the stage
                provider_unavailable = True
                raise

        setattr(invoke_stage, "record_validation", getattr(base_invoke_stage, "record_validation", None))

        try:
            outcome = await history.TopicHistoryEngine(
                store=self.store,
                invoke_stage=invoke_stage,
                model_name=config.model,
                context_limit=history.ContextLimitInfo(
                    value=config.context_window_tokens,
                    source="ARK_CONTEXT_WINDOW_TOKENS",
                ),
                model_provenance=config.safe_provenance(),
                records_loader=self._records_loader,
                now=self._now,
            ).refresh(state_only_for_v5=True)
        except Exception:  # noqa: BLE001 - do not start I1–I5 on state failure
            return (
                "background_model_unavailable"
                if provider_unavailable
                else "state_refresh_failed"
            )
        if outcome.status == "ok":
            state = outcome.state or history.load_latest_history_state(self.store) or {}
            ledger = state.get("history_ledger")
            if isinstance(ledger, dict) and not history.active_ledger_behaviors(ledger):
                return "no_eligible_history"
            refresh_reason = (
                outcome.memory_mode
                if outcome.memory_outcome == "success" and outcome.memory_mode
                else (
                    "history_rewrite"
                    if outcome.rewrite_detected
                    else (
                        "behavior_mapping"
                        if outcome.mapping_batches > 0
                        else "idempotent_reuse"
                    )
                )
            )
            try:
                persist_state_maintenance_receipt(
                    self.store,
                    state,
                    refresh_reason=refresh_reason,
                    created_at=self._now(),
                )
            except StateMaintenanceReceiptError as exc:
                return exc.reason
            except Exception:  # noqa: BLE001 - receipt is a hard S1 boundary
                return "state_receipt_persist_failed"
            return ""
        if provider_unavailable:
            return "background_model_unavailable"
        return outcome.reason or "state_refresh_failed"

    async def generate(
        self,
        trigger_key: dict[str, Any],
        trigger_origin: str,
        config: ArkBackgroundConfig,
    ) -> BackgroundGenerationResult:
        audit = _ExternalInvocationAttemptAudit(self.store, now=self._now)
        state_reason = await self._refresh_state(config, audit)
        if not state_reason:
            return await self._generate_prepared(
                trigger_key,
                trigger_origin,
                config,
                audit,
            )
        inputs = self._input_loader()
        snapshot = json.loads(
            json.dumps(inputs.generation_input_snapshot, ensure_ascii=False)
        )
        return _empty_result(
            trigger_key,
            trigger_origin,
            snapshot,
            state_reason,
            external_invocation_audit=audit.receipt(),
        )

    async def generate_prepared(
        self,
        trigger_key: dict[str, Any],
        trigger_origin: str,
        config: ArkBackgroundConfig,
    ) -> BackgroundGenerationResult:
        """Run I1–I5 against the state frozen by ``refresh_state``."""

        return await self._generate_prepared(
            trigger_key,
            trigger_origin,
            config,
            _ExternalInvocationAttemptAudit(self.store, now=self._now),
        )

    async def _generate_prepared(
        self,
        trigger_key: dict[str, Any],
        trigger_origin: str,
        config: ArkBackgroundConfig,
        audit: _ExternalInvocationAttemptAudit,
    ) -> BackgroundGenerationResult:
        inputs = self._input_loader()
        snapshot = json.loads(
            json.dumps(inputs.generation_input_snapshot, ensure_ascii=False)
        )
        trigger_key = {
            **trigger_key,
            "profile_semantic_version": int(
                ((inputs.state.get("memory") or {}).get("semantic_version")) or 0
            ),
        }

        def failed(reason: str) -> BackgroundGenerationResult:
            return _empty_result(
                trigger_key,
                trigger_origin,
                snapshot,
                reason,
                external_invocation_audit=audit.receipt(),
            )

        try:
            base_invoke_stage = self._stage_invoker_factory(config)
        except Exception:  # noqa: BLE001 - unavailable provider is fail-closed
            return failed("background_model_unavailable")
        provider_unavailable = False

        async def invoke_stage(stage: str, system_prompt: str, payload: str) -> Any:
            nonlocal provider_unavailable
            attempt_id = audit.record("ark", stage)
            try:
                with provider_invocation_context(host_attempt_id=attempt_id):
                    return await base_invoke_stage(stage, system_prompt, payload)
            except Exception:  # noqa: BLE001 - adapters close the stage safely
                provider_unavailable = True
                raise

        source_messages = _source_messages(inputs.active_behaviors)
        span_proposals = await history.extract_verbatim_spans(
            source_messages,
            list(inputs.active_behaviors),
            invoke_stage=invoke_stage,
        )
        if provider_unavailable:
            return failed("background_model_unavailable")
        buckets = history.build_allowed_grounded_anchors(
            list((inputs.state.get("memory") or {}).get("units") or []),
            list(inputs.active_behaviors),
            history.state_mapping_entries(inputs.state),
            span_proposals=span_proposals,
            policy_id=POLICY_ID,
        )
        interest_snapshot = _interest_snapshot(inputs, buckets)
        feedback_context = build_feedback_context(
            self.store,
            [item["interest_summary_id"] for item in interest_snapshot],
        )
        feedback_context["generation_input_snapshot"] = snapshot
        ranked, reason = await rank_interest_summaries(
            interest_snapshot,
            _delta_payload(inputs),
            feedback_context,
            invoke_ranker=ArkInterestRankingAdapter(invoke_stage),
        )
        if provider_unavailable:
            return failed("background_model_unavailable")
        if reason:
            return failed(reason)

        context_by_id = {
            str(item["interest_summary_id"]): dict(item) for item in interest_snapshot
        }
        for interest in ranked:
            context_by_id[interest.interest_summary_id]["rank"] = interest.rank
        try:
            hits = await retrieve_fixed_starters(
                _corpus_ranked_interests(ranked, context_by_id),
                CORPUS_VERSION,
                adapter=ArkFixedStarterRankingAdapter(invoke_stage),
            )
        except Exception:  # noqa: BLE001 - I3 contract fails the whole batch
            return failed(
                "background_model_unavailable"
                if provider_unavailable
                else "retrieval_contract_failed"
            )
        fixed_hits = [_fixed_hit_payload(hit, context_by_id) for hit in hits]
        candidates, reason = await generate_starter_candidates(
            ranked,
            fixed_hits,
            feedback_context,
            cold_start=not ranked,
            invoke_generator=ArkCandidateGenerationAdapter(invoke_stage),
            interest_context_by_id=context_by_id,
        )
        if provider_unavailable:
            return failed("background_model_unavailable")
        if reason:
            return failed(reason)

        candidates = _not_recently_exposed(
            candidates,
            list(snapshot.get("recent_exposure_window") or []),
        )
        negative_preferences = list(
            ((inputs.state.get("memory_sidecar") or {}).get("negative_preferences"))
            or []
        )
        candidates, _ = history.apply_negative_preferences(candidates, negative_preferences)

        async def audited_search(
            query: str,
            max_results: int,
            timeout_seconds: int,
        ) -> tuple[str, list[dict[str, str]]]:
            audit.record("search", "live_retrieval.search")
            return await self._search(query, max_results, timeout_seconds)

        async def audited_fetch(
            url: str,
            timeout_seconds: int,
        ) -> dict[str, Any]:
            audit.record("fetch", "live_retrieval.fetch")
            return await self._fetch(url, timeout_seconds)

        grounded = await ground_candidates(
            candidates,
            search=audited_search,
            fetch=audited_fetch,
            classify_source=self._classify_source,
            now=self._now(),
        )
        selections, reason = await select_starter_slate(
            grounded,
            {
                "trend_max": 1,
                "personalized_interest_buckets_distinct": True,
                "personalized_identity_anchors_distinct": True,
                "require_retrieved_fixed_when_eligible": True,
                "feedback_context": feedback_context,
            },
            invoke_selector=ArkSlateSelectionAdapter(invoke_stage),
        )
        if provider_unavailable:
            return failed("background_model_unavailable")
        if reason or not selections:
            return failed(reason or "no_eligible_selection")

        grounded_by_id = {
            str(candidate["candidate_id"]): candidate for candidate in grounded
        }
        now = self._now()
        batch_id = f"v5-batch-{uuid.uuid4().hex}"
        cards: list[dict[str, Any]] = []
        for selection in selections:
            candidate = grounded_by_id[selection.candidate_id]
            interest_id = candidate.get("interest_summary_id")
            interest_context = context_by_id.get(str(interest_id)) or {}
            cards.append(
                {
                    "card_id": str(candidate["candidate_id"]),
                    "card_revision_id": f"v5-revision-{uuid.uuid4().hex}",
                    "generation_batch_id": batch_id,
                    "selector_rank": selection.selector_rank,
                    "source_starter_id": candidate.get("source_starter_id"),
                    "interest_summary_id": interest_id,
                    "interest_rank": interest_context.get("rank"),
                    "observed_at": now,
                    "lane": candidate["lane"],
                    "origin": candidate["origin"],
                    "intent": candidate["intent"],
                    "title": candidate["title"],
                    "prompt": candidate["title"],
                    "freshness_class": candidate["freshness_class"],
                    "effective_time": now,
                    "expire_time": now
                    + _ttl_seconds(str(candidate["freshness_class"])),
                    "evidence_bundle": candidate["evidence_bundle"],
                    "source_memory_unit_ids": list(candidate["source_memory_unit_ids"]),
                    "source_locators": list(candidate["source_locators"]),
                    "identity_anchor": candidate["identity_anchor"],
                    "context_anchors": list(candidate["context_anchors"]),
                    "model_provenance": config.safe_provenance(),
                    "prompt_versions": dict(trigger_key["prompt_versions"]),
                    "policy_id": POLICY_ID,
                    "payload_version": PAYLOAD_VERSION,
                    "retrieval_config_version": trigger_key["retrieval_config_version"],
                    "corpus_version": trigger_key["corpus_version"],
                }
            )

        manifest_id = f"v5-manifest-{uuid.uuid4().hex}"
        manifest = {
            "manifest_id": manifest_id,
            "generation_batch_id": batch_id,
            "card_revision_ids": [card["card_revision_id"] for card in cards],
            "trigger_origin": trigger_origin,
            "local_date": trigger_key["local_date"],
            "generation_trigger_key": trigger_key,
            "generation_input_snapshot": snapshot,
            "serving_compatibility_key": build_serving_compatibility_key(trigger_key),
            "negative_preference_version": trigger_key["negative_preference_version"],
            "policy_id": POLICY_ID,
            "payload_version": PAYLOAD_VERSION,
            "external_invocation_audit": audit.receipt(),
        }
        corpus_text_by_id = {
            starter.starter_id: starter.text
            for starter in load_fixed_starter_corpus(CORPUS_VERSION)
        }
        return BackgroundGenerationResult(
            cards=cards,
            manifest=manifest,
            corpus_text_by_id=corpus_text_by_id,
        )

I1_PROMPT_VERSION = "topic-starter-v5-i1-span-v1"
PROMPT_VERSIONS = {
    "i1": I1_PROMPT_VERSION,
    "i2": I2_PROMPT_VERSION,
    "i3": I3_RETRIEVAL_PROMPT_VERSION,
    "i4": I4_PROMPT_VERSION,
    "i5": I5_PROMPT_VERSION,
}

GenerateFn = Callable[
    [dict[str, Any], str, ArkBackgroundConfig],
    Awaitable[BackgroundGenerationResult],
]
PrepareStateFn = Callable[[ArkBackgroundConfig], Awaitable[str]]
ConfigLoader = Callable[[], ArkBackgroundConfig | None]


class TopicPrepareService:
    """One process-local service shared by WebChannel and scheduled cron."""

    def __init__(
        self,
        store: TopicStarterStore,
        *,
        generate: GenerateFn,
        prepare_state: PrepareStateFn | None = None,
        config_loader: ConfigLoader = ArkBackgroundConfig.from_environ,
        now: Callable[[], float] = time.time,
        coordinator: BackgroundGenerationCoordinator | None = None,
    ) -> None:
        self.store = store
        self._generate = generate
        self._prepare_state = prepare_state
        self._ark_config = config_loader()
        self._now = now
        self._coordinator = coordinator or BackgroundGenerationCoordinator(store)
        self._tasks: set[asyncio.Task[str]] = set()
        self._seed_task: asyncio.Task[str] | None = None

    @property
    def inflight_count(self) -> int:
        seed_running = self._seed_task is not None and not self._seed_task.done()
        return max(self._coordinator.inflight_count, int(seed_running))

    def _state_versions(self) -> tuple[int, int, int, list[dict[str, Any]]]:
        state = load_latest_history_state(self.store) or {}
        memory_value = state.get("memory")
        memory = memory_value if isinstance(memory_value, dict) else {}
        sidecar_value = state.get("memory_sidecar")
        sidecar = sidecar_value if isinstance(sidecar_value, dict) else {}
        return (
            int(state.get("version") or 0),
            int(memory.get("semantic_version") or 0),
            int(sidecar.get("negative_preference_version") or 0),
            list(sidecar.get("negative_preferences") or []),
        )

    def _trigger_key(self) -> dict[str, Any]:
        _, profile_semantic_version, negative_version, _ = self._state_versions()
        config = self._ark_config
        return build_generation_trigger_key(
            local_date=time.strftime(
                "%Y-%m-%d", time.localtime(self._now())
            ),
            profile_semantic_version=profile_semantic_version,
            negative_preference_version=negative_version,
            ark_model=config.model if config is not None else "",
            ark_base_url=config.base_url if config is not None else "",
            effective_temperature=(
                config.effective_temperature if config is not None else 0
            ),
            effective_top_p=(
                config.effective_top_p if config is not None else 1
            ),
            context_window_tokens=(
                int(config.context_window_tokens or 0)
                if config is not None
                else 0
            ),
            prompt_versions=PROMPT_VERSIONS,
            corpus_version=CORPUS_VERSION,
            retrieval_config_version=RETRIEVAL_CONFIG_VERSION,
        )

    def _track(self, task: asyncio.Task[str]) -> asyncio.Task[str]:
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def _start_seed(
        self,
        trigger_origin: str,
        trigger_key: dict[str, Any] | None = None,
    ) -> asyncio.Task[str]:
        key = trigger_key or self._trigger_key()
        config = self._ark_config

        if self._prepare_state is None or config is None:
            async def generate() -> BackgroundGenerationResult:
                if config is None:  # coordinator rejects before invoking this closure
                    raise RuntimeError("background model unavailable")
                return await self._generate(key, trigger_origin, config)

            task = self._coordinator.start(
                key,
                generate,
                ark_config=config,
                require_ark_config=True,
            )
            return self._track(task)

        if self._seed_task is not None and not self._seed_task.done():
            return self._seed_task

        async def prepare_then_generate() -> str:
            state_reason = await self._prepare_state(config)
            refreshed_key = self._trigger_key()

            async def generate() -> BackgroundGenerationResult:
                if state_reason:
                    return BackgroundGenerationResult(
                        cards=[],
                        manifest={"generation_trigger_key": refreshed_key},
                        failure_reason=state_reason,
                    )
                return await self._generate(
                    refreshed_key, trigger_origin, config
                )

            return await self._coordinator.start(
                refreshed_key,
                generate,
                ark_config=config,
                require_ark_config=True,
            )

        task = asyncio.create_task(prepare_then_generate())
        self._seed_task = task

        def clear_seed_task(done: asyncio.Task[str]) -> None:
            if self._seed_task is done:
                self._seed_task = None

        task.add_done_callback(clear_seed_task)
        return self._track(task)

    def schedule_seed(self) -> asyncio.Task[str]:
        """Wake-only Cron callback: enqueue and return without awaiting 180s."""

        return self._start_seed("scheduled_cron")

    def schedule_successor(self) -> asyncio.Task[str]:
        """Enqueue after ``record_v5_interaction`` accepts ``not_interested``."""

        return self._start_seed("lazy_seed")

    def serve(self, session_id: str) -> dict[str, Any]:
        """Read one manifest synchronously; a miss only schedules later work."""

        profile_version, _, negative_version, negative_preferences = (
            self._state_versions()
        )
        trigger_key = self._trigger_key()
        if self._ark_config is None:
            self._start_seed("lazy_seed", trigger_key)
            return {
                "available": False,
                "reason": "background_model_unavailable",
                "session_id": session_id,
                "policy_id": POLICY_ID,
            }

        served = read_starter_pool(
            self.store,
            serving_compatibility_key=build_serving_compatibility_key(trigger_key),
            negative_preference_version=negative_version,
            negative_preferences=negative_preferences,
            now=self._now(),
        )
        manifest_negative_version = served.get(
            "manifest_negative_preference_version"
        )
        successor_needed = (
            manifest_negative_version is not None
            and int(manifest_negative_version) < negative_version
        )
        if successor_needed or not served["cards"]:
            self._start_seed("lazy_seed", trigger_key)
        if not served["cards"]:
            return {
                "available": False,
                "reason": "v5_pool_miss",
                "session_id": session_id,
                "policy_id": POLICY_ID,
            }

        slate_id = f"v5-slate-{uuid.uuid4().hex}"
        topics = [
            {
                "id": card["card_revision_id"],
                "card_revision_id": card["card_revision_id"],
                "generation_batch_id": served["generation_batch_id"],
                "interest_summary_id": card.get("interest_summary_id"),
                "source_starter_id": card.get("source_starter_id"),
                "position": int(card["selector_rank"]) - 1,
                "selector_rank": int(card["selector_rank"]),
                "title": card["title"],
                "prompt": card["prompt"],
                "intent": card["intent"],
                "grounding_mode": "web_required",
            }
            for card in served["cards"]
        ]
        return {
            "available": True,
            "session_id": session_id,
            "slate_id": slate_id,
            "manifest_id": served["manifest_id"],
            "generation_batch_id": served["generation_batch_id"],
            "profile_version": profile_version,
            "policy_id": POLICY_ID,
            "retrieval_health": "eligible",
            "selection_mode": served["selection_mode"],
            "slate_card_count": served["slate_card_count"],
            "personalized_card_count": served["personalized_card_count"],
            "trend_card_count": served["trend_card_count"],
            "topics": topics,
        }

    async def wait_for_background(self) -> None:
        tasks = [task for task in self._tasks if not task.done()]
        if tasks:
            await asyncio.gather(*tasks)

    async def shutdown(self) -> None:
        tasks = [task for task in self._tasks if not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Prepared card pool: background orchestration and the foreground pool service."""

from __future__ import annotations

import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from functools import partial
from typing import Any

from jiuwenswarm.agents.harness.common.recommendation import (
    topic_history as history,
)
from jiuwenswarm.agents.harness.common.recommendation.topic_model import (
    ArkBackgroundConfig,
    build_ark_stage_invoker,
    persist_provider_invocation,
    provider_invocation_context,
)
from jiuwenswarm.agents.harness.common.recommendation.topic_pool import (
    BackgroundGenerationResult,
    POLICY_ID,
    find_exact_pool_card,
)
from jiuwenswarm.agents.harness.common.recommendation.topic_sources import (
    FetchFn,
    corpus_version_for_language,
    RankedInterest,
    RankedInterest as CorpusRankedInterest,
    SearchFn,
    SourceClassifier,
    StarterHit,
    default_fetch,
    default_search,
    default_source_classifier,
)
from jiuwenswarm.agents.harness.common.recommendation.topic_starter import (
    TopicStarterStore,
)


def _corpus_version() -> str:
    """The prepare lane follows the same UI-language corpus as the product lane."""

    from .topic_controls import recommendation_language

    return corpus_version_for_language(recommendation_language())


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

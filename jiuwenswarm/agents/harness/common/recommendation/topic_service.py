# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Foreground topic recommendation service for global and project new-chat pages."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass
from typing import Any, NamedTuple

from jiuwenswarm.agents.harness.common.recommendation.topic_history import (
    load_latest_history_state,
)
from jiuwenswarm.agents.harness.common.recommendation.topic_model import (
    PROVIDER_REFUSALS,
    ArkBackgroundConfig,
    background_configuration_status,
    provider_invocation_context,
)
from jiuwenswarm.agents.harness.common.recommendation.topic_news import (
    HOT_LIST_MATCH_LIMIT,
    HotListFeed,
    hot_event_key,
    load_hot_list_pull,
)
from jiuwenswarm.agents.harness.common.recommendation.topic_outcomes import (
    TERMINAL_INTERACTION_EVENT_TYPES,
    NewPageOutcomeQueue,
    NewPageOutcomeSignal,
)
from jiuwenswarm.agents.harness.common.recommendation.topic_pool import (
    _GENERATION_ATTEMPT_ID,
    BackgroundGenerationCoordinator,
    BackgroundGenerationResult,
    GenerationTrigger,
)
from jiuwenswarm.agents.harness.common.recommendation.topic_product import (
    ARK_REASONING_POLICY_VERSION,
    DEFAULT_TOPIC_WORK_MODE,
    FIRST_SCREEN_CARD_TARGET,
    GENERATION_CONFIG_VERSION,
    GLOBAL_NEW_CHAT_SCOPE,
    GLOBAL_POOL_TARGET_SIZE,
    HOT_LIST_SOURCE_KIND,
    INPUT_REVISION_RECEIPT_VERSION,
    NEWS_ONLY_SUCCESSOR,
    POLICY_ID,
    TERMINAL_COMPONENT_IDS,
    InterestFeedback,
    build_cold_start_cards,
    build_generation_trigger_key,
    build_serving_compatibility_key,
    card_is_news,
    card_provider_response_ids,
    card_search_receipt,
    find_exact_terminal_card,
    is_cold_start_card,
    is_cold_start_manifest,
    news_lead_card_id,
    normalize_topic_work_mode,
    pool_inventory,
    project_id_for_scope,
    read_terminal_pool,
    record_terminal_exposure,
    retainable_pool,
    scope_for_project,
    serving_compatibility_keys_match,
    topic_slot_key,
    write_cold_start_pool,
    write_terminal_pool,
)
from jiuwenswarm.agents.harness.common.recommendation.topic_retrieval import (
    EVIDENCE_EXTRACTOR_VERSION,
)
from jiuwenswarm.agents.harness.common.recommendation.topic_short_intent import (
    ShortIntentStateStore,
)
from jiuwenswarm.agents.harness.common.recommendation.topic_sources import (
    CORPUS_VERSION,
)
from jiuwenswarm.agents.harness.common.recommendation.topic_starter import (
    TopicStarterStore,
)
from jiuwenswarm.agents.harness.common.recommendation.topic_terminal import (
    TERMINAL_CONTRACT_VERSION,
)

logger = logging.getLogger(__name__)

RETRIEVAL_CONFIG_VERSION = "topic-starter-v6-web-retrieval-v18"
# No wall-clock cap on one successor run: stages are sequential LLM calls and a
# fixed total (formerly 300s) cancelled mid-pipeline before a pool could write.
# Per-call client timeout matches Designer (1200s in topic_model).
V6_BACKGROUND_GENERATION_DEADLINE_SECONDS: float | None = None
INPUT_REVISION_EVENT_TYPE = "v6_input_revision_receipt"
ISSUED_SLATE_EVENT_TYPE = "v6_issued_slate"
NEWS_CHECK_EVENT_TYPE = "v6_news_feed_check"
NEWS_REFRESH_EVENT_TYPE = "v6_news_refresh"
NEWS_CHECK_INTERVAL_SECONDS = 30 * 60
# Legacy daily cap kept for older news_refresh receipts; new checks only
# schedule a normal same-batch successor (hot news + personalized together).
NEWS_REFRESH_DAILY_LIMIT = 12

GenerateFn = Callable[
    [GenerationTrigger, ArkBackgroundConfig, str, str | None, str],
    Awaitable[BackgroundGenerationResult],
]
RefreshNewsFn = Callable[[ArkBackgroundConfig, str, str], Awaitable[BackgroundGenerationResult]]
PrepareStateFn = Callable[[ArkBackgroundConfig], Awaitable[str]]
PrepareShortIntentFn = Callable[[str], Awaitable[str]]
PrepareOutcomesFn = Callable[[], Awaitable[str]]
ShortIntentVersionLoader = Callable[[str], int]
ConfigLoader = Callable[[], ArkBackgroundConfig | None]
InputRevisionLoader = Callable[[str, str], object]


# Merged requests keep the strongest origin: a news check's whole regeneration,
# then its news-card refresh, then an automatic request.
_ORIGIN_RANK = {"lazy_seed": 0, "news_refresh": 1, "news_check": 2}
# The generated interest lane travels into a successor; every other ordinary card leaves with its pool.
_CARRIED_CATEGORY = "profile_interest_continuation"
# Outcomes of a request that found nothing to do; the page has nothing new to read.
_QUIET_OUTCOMES = frozenset({"unchanged", "news_unchanged"})
_WRITTEN_OUTCOMES = frozenset({"written", "already_written"})


def _issued_slate_in_scope(
    payload: Any,
    *,
    context_scope: str,
    project_id: str | None,
    work_mode: str,
) -> bool:
    """Whether an issued slate was written for the scope now being served."""

    if not isinstance(payload, dict):
        return False
    if payload.get("context_scope") != context_scope:
        return False
    if payload.get("project_id") != project_id:
        return False
    return normalize_topic_work_mode(payload.get("work_mode")) == work_mode


class _RunRequest(NamedTuple):
    origin: str
    visit_id: str


@dataclass
class _ScopeRun:
    """A scope's one replenishment run and the single request merged behind it."""

    current: asyncio.Task[str] | None = None
    queued: asyncio.Task[str] | None = None
    queued_request: _RunRequest = _RunRequest("lazy_seed", "")
    receipt: dict[str, str] | None = None
    candidate_task: asyncio.Task[BackgroundGenerationResult] | None = None
    # An undo or a pause came while the current run was preparing: it gives way before generating.
    feedback_changed_before_candidates: bool = False
    # The latest terminal outcome; failures are reported when the chain ends.
    outcome: str | None = None

    @property
    def busy(self) -> bool:
        return any(task is not None and not task.done() for task in (self.current, self.queued))


class TopicService:
    """One process-local v6 service shared by WebChannel and global cron."""

    def __init__(
        self,
        store: TopicStarterStore,
        *,
        generate: GenerateFn,
        search_provider_id: str,
        prepare_state: PrepareStateFn | None = None,
        prepare_short_intent: PrepareShortIntentFn | None = None,
        prepare_outcomes: PrepareOutcomesFn | None = None,
        short_intent_version_loader: ShortIntentVersionLoader | None = None,
        input_revision_loader: InputRevisionLoader | None = None,
        config_loader: ConfigLoader = ArkBackgroundConfig.from_environ,
        now: Callable[[], float] = time.time,
        coordinator: BackgroundGenerationCoordinator | None = None,
        enabled: bool = True,
        history_project_ids_loader: Callable[[], list[str]] = lambda: [],
        refresh_news: RefreshNewsFn | None = None,
        hot_list: HotListFeed | None = None,
    ) -> None:
        self.store = store
        self._generate = generate
        self._refresh_news = refresh_news
        self._hot_list = hot_list if hot_list is not None else HotListFeed(store, now=now)
        self._news_check_task: asyncio.Task[str] | None = None
        self._news_refresh_task: asyncio.Task[BackgroundGenerationResult] | None = None
        self._history_project_ids_loader = history_project_ids_loader
        if not isinstance(search_provider_id, str) or not search_provider_id.strip():
            raise ValueError("search_provider_id is required")
        self._search_provider_id = search_provider_id
        self._prepare_state = prepare_state
        self._prepare_short_intent = prepare_short_intent
        self._prepare_outcomes = prepare_outcomes
        self._outcome_task: asyncio.Task[str] | None = None
        self._short_intent_version_loader: ShortIntentVersionLoader
        if short_intent_version_loader is None:
            short_store = ShortIntentStateStore(store)

            def load_short_intent_version(scope: str) -> int:
                state = short_store.load_latest_for_scope(scope)
                return state.short_intent_version if state is not None else 0

            self._short_intent_version_loader = load_short_intent_version
        else:
            self._short_intent_version_loader = short_intent_version_loader
        self._input_revision_loader = input_revision_loader or (lambda _scope, _mode: None)
        self._config_loader = config_loader
        self._ark_config = config_loader()
        self._configuration_status = background_configuration_status(self._ark_config)
        self._now = now
        self._coordinator = coordinator or BackgroundGenerationCoordinator(
            store,
            pool_writer=write_terminal_pool,
            policy_id=POLICY_ID,
            deadline_seconds=V6_BACKGROUND_GENERATION_DEADLINE_SECONDS,
        )
        self._runs: dict[str, _ScopeRun] = {}
        self._tasks: set[asyncio.Task[str]] = set()
        self._accepting_work = enabled
        self._control_lock = asyncio.Lock()
        # The scope's preparation state, and whether a pool was just written for the page to read.
        self.on_prepared: Callable[..., None] | None = None


    @staticmethod
    def _slot_key(context_scope: str, work_mode: object) -> str:
        return topic_slot_key(context_scope, work_mode)

    def configuration_status(self) -> dict[str, Any]:
        status = {**self._configuration_status, "search_provider_id": self._search_provider_id,
                  "enabled": self._accepting_work}
        config = self._ark_config
        if config is not None:
            # Only calls to the configured model speak for the setup now in force.
            model = config.expected_model or config.model
            failures = self.store.query_events(
                event_type="provider_invocation",
                payload_equals={"status": "request_failed", "model": model},
                newest_first=True, limit=1,
            )
            # A call the provider answered after the failure shows it works again.
            # Every answered call keeps the provider's response id, whatever its status.
            answered = self.store.query_events(
                event_type="provider_invocation",
                payload_equals={"model": model},
                payload_present="provider_response_id",
                newest_first=True, limit=1,
            )
            if failures and not (answered and answered[0]["created_at"] > failures[0]["created_at"]):
                failure = failures[0].get("payload") or {}
                status["last_failure"] = failure.get("failure_reason", "provider_request_failed")
        status["provider_refused"] = status.get("last_failure") in PROVIDER_REFUSALS
        return status

    async def set_enabled(self, enabled: bool, *, include_history: bool = False,
                          persist: bool = True) -> dict[str, Any]:
        """Put a switch change in force, recording the consent boundary unless told not to.

        ``persist=False`` is for the config-save echo: by then the panel has already written the
        switch, and a runtime object must not read that configuration back and write it again.
        """

        from .topic_controls import save_recommendation_controls
        async with self._control_lock:
            if persist:
                save_recommendation_controls(
                    enabled=enabled, include_history=include_history, now=self._now(),
                )
            if not enabled:
                await self.shutdown()
            else:
                if self._ark_config is None:
                    # Enabling is the moment the configured model first matters; a config the
                    # process never saw (started before the model was set) reads fresh here.
                    self._ark_config = self._config_loader()
                    self._configuration_status = background_configuration_status(self._ark_config)
                self._accepting_work = True
                self.schedule_seed("recommendation-enabled")
            return self.configuration_status()

    @property
    def ark_config(self) -> ArkBackgroundConfig | None:
        """The model in force; ``reload_config`` alone replaces it."""
        return self._ark_config

    async def reload_config(self) -> dict[str, Any]:
        """Put a changed recommendation model in force without a restart.

        Work under the old model is cancelled and drained before the new one
        takes over, so none of it writes afterwards. Existing topic pools keep
        serving; the new model is used only on the next natural generation.
        """
        async with self._control_lock:
            config = self._config_loader()
            if config != self._ark_config:
                accepting = self._accepting_work
                await self.shutdown()
                self._ark_config = config
                self._configuration_status = background_configuration_status(config)
                self._accepting_work = accepting
            return self.configuration_status()

    async def delete_data(self) -> dict[str, Any]:
        from .topic_controls import save_recommendation_controls
        async with self._control_lock:
            save_recommendation_controls(enabled=False, deleted=True, now=self._now())
            await self.shutdown()
            self.store.delete_recommendation_data()
            self._runs.clear()
            return self.configuration_status()

    @property
    def inflight_count(self) -> int:
        return max(
            self._coordinator.inflight_count,
            sum(slot.busy for slot in self._runs.values()),
        )

    @staticmethod
    def _input_revision_receipt(
        context_scope: str,
        revision: object,
    ) -> dict[str, str]:
        """Compact an exact scoped witness used only to skip external work."""

        serialized = json.dumps(
            revision,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        receipt = {
            "receipt_version": INPUT_REVISION_RECEIPT_VERSION,
            "context_scope": context_scope,
            "revision_digest": hashlib.sha256(serialized.encode("utf-8")).hexdigest(),
        }
        coverage = [
            item for item in revision
            if isinstance(item, tuple) and len(item) == 3 and item[1] == "completed_answers"
        ] if isinstance(revision, tuple) else []
        if coverage:
            # Coverage can change without a new natural-interest version. Keep
            # its witness separate so ordinary non-triggering input stays cheap.
            serialized_coverage = json.dumps(coverage, ensure_ascii=False, separators=(",", ":"))
            receipt["discussion_coverage_digest"] = hashlib.sha256(
                serialized_coverage.encode("utf-8")
            ).hexdigest()
        return receipt

    def _load_input_revision_receipt(
        self,
        *,
        context_scope: str,
        manifest_id: str,
    ) -> dict[str, str] | None:
        receipt_rows = self.store.query_events(
            event_type=INPUT_REVISION_EVENT_TYPE,
            slate_id=manifest_id,
            policy_id=POLICY_ID,
            limit=1,
            newest_first=True,
        )
        candidates = [row.get("payload") for row in receipt_rows]
        if not candidates:
            manifest_rows = self.store.query_events(
                event_type="starter_slate_manifest",
                slate_id=manifest_id,
                policy_id=POLICY_ID,
                limit=1,
                newest_first=True,
            )
            candidates = [
                row.get("payload", {}).get("consumed_input_revision")
                for row in manifest_rows
                if isinstance(row.get("payload"), dict)
            ]
        for candidate in candidates:
            _failed2 = (
                isinstance(candidate, dict)
                and candidate.get("receipt_version")
                == INPUT_REVISION_RECEIPT_VERSION
                and candidate.get("context_scope") == context_scope
                and isinstance(candidate.get("revision_digest"), str)
                and candidate["revision_digest"]
            )
            if (
                _failed2
            ):
                return {
                    key: candidate[key]
                    for key in ("receipt_version", "context_scope", "revision_digest", "discussion_coverage_digest")
                    if key in candidate
                }
        return None

    def _observed_input_revision(self, context_scope: str) -> dict[str, str] | None:
        rows = self.store.query_events(
            event_type=INPUT_REVISION_EVENT_TYPE,
            topic_id=f"observed:{context_scope}",
            policy_id=POLICY_ID,
            limit=1,
            newest_first=True,
        )
        return rows[0]["payload"] if rows else None

    def _record_observed_input_revision(self, context_scope: str, receipt: dict[str, str]) -> None:
        if self._observed_input_revision(context_scope) == receipt:
            return
        self.store.record_event({
            "session_id": "topic-starter-background",
            "slate_id": f"v6-input-observation-{uuid.uuid4().hex}",
            "event_type": INPUT_REVISION_EVENT_TYPE,
            "topic_id": f"observed:{context_scope}",
            "policy_id": POLICY_ID,
            "selection_mode": "background_revision_observed",
            "propensity_reason": "not_estimated",
            "payload": dict(receipt),
            "created_at": time.time(),
        })

    def _record_input_revision_receipt(
        self,
        *,
        context_scope: str,
        manifest_id: str,
        receipt: dict[str, str],
    ) -> None:
        self.store.record_event(
            {
                "session_id": "topic-starter-background",
                "slate_id": manifest_id,
                "event_type": INPUT_REVISION_EVENT_TYPE,
                "topic_id": receipt["revision_digest"],
                "policy_id": POLICY_ID,
                "selection_mode": "background_revision_probe",
                "propensity_reason": "not_estimated",
                "payload": dict(receipt),
                "created_at": time.time(),
            }
        )

    def _manifest_id_for_trigger(self, trigger_key: dict[str, Any]) -> str | None:
        rows = self.store.query_events(
            event_type="starter_slate_manifest",
            policy_id=POLICY_ID,
            payload_equals={"generation_trigger_key": trigger_key},
            limit=1,
            newest_first=True,
        )
        if rows and isinstance(rows[0].get("payload"), dict):
            manifest_id = str(rows[0]["payload"].get("manifest_id") or "")
            return manifest_id or None
        return None

    def _unsettled_attempt(self, filters: dict[str, Any], *, context_scope: str) -> dict[str, Any] | None:
        """The last unsuccessful attempt matching ``filters``, unless the scope wrote a pool since."""
        events = []
        for event_type in ("background_generation_rejected", "background_generation_failed",
                           "background_generation_deadline", "background_generation_abstained"):
            events.extend(self.store.query_events(event_type=event_type, policy_id=POLICY_ID,
                          payload_equals=filters, limit=1, newest_first=True))
        if not events:
            return None
        failed = max(events, key=lambda row: row["created_at"])
        written = self.store.query_events(
            event_type="starter_slate_manifest", policy_id=POLICY_ID,
            payload_equals={"context_scope": context_scope}, limit=1, newest_first=True,
        )
        # A pool written after the attempt settles it.
        if written and written[0]["created_at"] >= failed["created_at"]:
            return None
        return failed

    def preparation_state(
        self,
        context_scope: str,
        work_mode: str = DEFAULT_TOPIC_WORK_MODE,
    ) -> str:
        normalized_work_mode = normalize_topic_work_mode(work_mode)
        if not self._accepting_work:
            return "disabled"
        if self._ark_config is None:
            return "missing_configuration"
        slot = self._runs.get(self._slot_key(context_scope, normalized_work_mode))
        if slot is not None and slot.busy:
            return "preparing"
        # Attempts under another model belong to a setup that is no longer in force.
        failed = self._unsettled_attempt({
            "generation_trigger_key.context_scope": context_scope,
            "generation_trigger_key.work_mode": normalized_work_mode,
            "generation_trigger_key.model": self._ark_config.expected_model or self._ark_config.model,
        }, context_scope=context_scope)
        if failed:
            reason = failed["payload"].get("reason", "")
            if reason == "unread_cards_kept":
                return "ready"
            if reason == "no_eligible_history":
                return "no_content"
            if reason in {
                "rid_abstained",
                "quality_abstained",
                "no_feasible_complete_list",
                # The project generator found nothing in the project worth continuing.
                "insufficient_active_project_actions",
            }:
                return "no_content"
            # Retrying cannot help while the provider refuses the key, account or model.
            if self.configuration_status()["provider_refused"]:
                return "provider_unavailable"
            return "failed"
        return "ready"

    def _has_rid_abstention_for_trigger(self, trigger_key: dict[str, Any]) -> bool:
        rows = self.store.query_events(
            event_type="rid_abstention",
            policy_id=POLICY_ID,
            payload_equals={"generation_trigger_key": trigger_key},
            limit=1,
            newest_first=True,
        )
        return bool(rows)

    def _pool_stock(
        self,
        *,
        context_scope: str,
        project_id: str | None,
        trigger_key: dict[str, Any],
    ) -> str:
        """How the served pool stands against its replenishment target.

        ``stocked`` meets it. A project pool that started below four meets it
        too: only opening, dismissing or expiry of its shown cards is a
        shortfall. A global pool below seven is ``as_written`` while it still
        holds every card it could offer when written, which answers only the input
        it was written for, and ``short`` once any of them has gone.
        """
        _, feedback = self._scope_state(context_scope=context_scope, project_id=project_id)
        inventory = pool_inventory(
            self.store,
            serving_compatibility_key=build_serving_compatibility_key(trigger_key),
            context_scope=context_scope,
            project_id=project_id,
            feedback=feedback,
            now=self._now(),
        )
        if inventory is None:
            return "short"
        initial, eligible = inventory
        if context_scope != GLOBAL_NEW_CHAT_SCOPE:
            return "stocked" if eligible > 0 and eligible >= min(FIRST_SCREEN_CARD_TARGET, initial) else "short"
        if eligible >= GLOBAL_POOL_TARGET_SIZE:
            return "stocked"
        return "as_written" if 0 < eligible == initial else "short"

    def _manifest_id_for_successor_state(
        self,
        trigger_key: dict[str, Any],
    ) -> str | None:
        """Find a manifest whose accepted successor triggers are unchanged.

        Negative preferences are applied when the pool is read, so a manifest
        written before the latest dismissal still serves; whether it needs a
        successor is a stock question, not an identity one.
        """

        expected_serving = build_serving_compatibility_key(trigger_key)
        # Match serving keys in Python so rows that still embed model id or
        # generation_config_version remain successors after a connection change.
        rows = self.store.query_events(
            event_type="starter_slate_manifest",
            policy_id=POLICY_ID,
            payload_equals={
                "context_scope": trigger_key["context_scope"],
                "project_id": trigger_key["project_id"],
            },
            payload_integer_lte=(
                "negative_preference_version",
                trigger_key["negative_preference_version"],
            ),
            order_by_payload_integer_desc="negative_preference_version",
            limit=64,
            newest_first=True,
        )
        manifest: dict[str, Any] | None = None
        for row in rows:
            payload = row.get("payload")
            if not isinstance(payload, dict):
                continue
            if not serving_compatibility_keys_match(
                payload.get("serving_compatibility_key"),
                expected_serving,
            ):
                continue
            manifest = payload
            break
        if manifest is None:
            return None
        # Preset cards are what the page shows while recommendation is off, or before the
        # first personalized pool exists. They must not answer a replenishment: turning
        # the switch on has to prepare a new pool instead of keeping the templates.
        if is_cold_start_manifest(manifest):
            return None
        generation_trigger = manifest.get("generation_trigger_key")
        if not isinstance(generation_trigger, dict):
            return None
        ignored = {
            "input_revision_receipt",
            "negative_preference_version",
            "model",
            "generation_config_version",
        }
        accepted_trigger = {
            key: value
            for key, value in trigger_key.items()
            if key not in ignored
        }
        manifest_trigger = {
            key: value
            for key, value in generation_trigger.items()
            if key not in ignored
        }
        if manifest_trigger != accepted_trigger:
            return None
        manifest_id = str(manifest.get("manifest_id") or "")
        return manifest_id or None

    def _scope_state(
        self,
        *,
        context_scope: str,
        project_id: str | None,
    ) -> tuple[int, InterestFeedback]:
        """The profile's semantic version and the scope's interest feedback, from one state."""

        state = load_latest_history_state(self.store) or {}
        memory = state.get("memory")
        semantic_version = int(memory.get("semantic_version") or 0) if isinstance(memory, dict) else 0
        return semantic_version, InterestFeedback.load(
            self.store, state, context_scope=context_scope, project_id=project_id, now=self._now(),
        )

    def _generation_config_version(self) -> str:
        config = self._ark_config
        if config is None:
            return f"{GENERATION_CONFIG_VERSION}:unavailable"
        return (
            f"{GENERATION_CONFIG_VERSION}:"
            f"temperature={config.effective_temperature}:"
            f"top_p={config.effective_top_p}:"
            f"context={config.context_window_tokens}:"
            f"base_url={config.base_url}:"
            f"reasoning_policy={ARK_REASONING_POLICY_VERSION}"
        )

    def _trigger_key(
        self,
        *,
        context_scope: str,
        project_id: str | None,
        work_mode: str = DEFAULT_TOPIC_WORK_MODE,
        input_revision_receipt: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        profile_semantic_version, feedback = self._scope_state(
            context_scope=context_scope,
            project_id=project_id,
        )
        local_date = time.strftime("%Y-%m-%d", time.localtime(self._now()))
        model = "unavailable"
        if self._ark_config is not None:
            model = self._ark_config.expected_model or self._ark_config.model
        revision_receipt = (
            input_revision_receipt
            if input_revision_receipt is not None
            else self._input_revision_receipt(context_scope, None)
        )
        return build_generation_trigger_key(
            local_date=local_date,
            context_scope=context_scope,
            project_id=project_id,
            work_mode=normalize_topic_work_mode(work_mode),
            profile_semantic_version=profile_semantic_version,
            short_intent_version=int(
                self._short_intent_version_loader(context_scope)
            ),
            negative_preference_version=feedback.version,
            component_contract_versions={
                component_id: (
                    "topic-starter-v6-quality-decisions-v2"
                    if component_id in {"interaction_utility_scorer", "diversity_scorer"}
                    else TERMINAL_CONTRACT_VERSION
                )
                for component_id in TERMINAL_COMPONENT_IDS
            },
            model=model,
            generation_config_version=self._generation_config_version(),
            corpus_version=CORPUS_VERSION,
            document_snapshot_version=f"documents:{local_date}:v2",
            retrieval_config_version=RETRIEVAL_CONFIG_VERSION,
            evidence_extractor_version=EVIDENCE_EXTRACTOR_VERSION,
            search_provider_id=self._search_provider_id,
            input_revision_receipt=revision_receipt,
        )

    @staticmethod
    async def _finished(outcome: str) -> str:
        return outcome

    def _track(self, task: asyncio.Task[str]) -> asyncio.Task[str]:
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def _start_outcome_collection(self) -> None:
        """Label earlier exposures without holding up the pool being prepared.

        Generation reads none of the collector's labels or training records, so
        the pass runs beside it; one pass at a time, drained by shutdown.
        """
        if self._prepare_outcomes is None:
            return
        if self._outcome_task is not None and not self._outcome_task.done():
            return
        prepare_outcomes = self._prepare_outcomes

        async def collect() -> str:
            try:
                return str(await prepare_outcomes() or "")
            except Exception:  # noqa: BLE001 - labels are retried by the next pass
                logger.warning("[Topic] outcome collection failed", exc_info=True)
                return "outcome_collector_failed"

        self._outcome_task = self._track(asyncio.create_task(collect()))

    def _reason_not_to_run(
        self,
        *,
        origin: str,
        context_scope: str,
        project_id: str | None,
        work_mode: str = DEFAULT_TOPIC_WORK_MODE,
        receipt: dict[str, str],
        prepared: bool,
    ) -> str | None:
        """Why the scope owes no run now, or None when it owes one.

        Asked before preparation and again after it. A news check's regeneration
        always runs; an automatic request, and a news-card refresh, run only
        while the pool is short or has input left to consume: before preparation
        an input revision the pool has not consumed, after it a trigger the pool
        was not written under. A global pool written below seven cards has
        consumed only its own input revision, so a new one runs even after
        preparation.

        Replenishment uses the durable completed discussion boundary at trigger
        time (chat completion, enable seed). In-flight chats are not held back;
        their completion schedules another successor.
        """

        if origin == "news_check":
            return None
        trigger_key = self._trigger_key(
            context_scope=context_scope,
            project_id=project_id,
            work_mode=work_mode,
            input_revision_receipt=receipt,
        )
        manifest_id = self._manifest_id_for_successor_state(trigger_key)
        if manifest_id is None:
            return None
        stock = self._pool_stock(
            context_scope=context_scope, project_id=project_id, trigger_key=trigger_key,
        )
        if stock == "short":
            return None
        if (not prepared or stock == "as_written") and self._load_input_revision_receipt(
            context_scope=context_scope, manifest_id=manifest_id,
        ) != receipt:
            return None
        return "unchanged"

    def _keeps_unread_cards(
        self,
        result: BackgroundGenerationResult,
        *,
        trigger_key: dict[str, Any],
        receipt: dict[str, str],
        manifest_id: str | None,
    ) -> bool:
        """A global round that adds no ordinary card leaves the pool it would replace serving.

        Web cards do not travel into a successor, since their evidence belongs to
        their own round's document snapshot, so a news-only pool would take the
        old pool's unread web cards off the page. The old pool was written for the
        same input and is read under today's feedback, so while one of those cards
        is still eligible it keeps serving, as long as it also still holds a news
        card when the round found one.
        """

        if trigger_key["context_scope"] != GLOBAL_NEW_CHAT_SCOPE:
            return False
        if manifest_id is None:
            return False
        if result.failure_reason:
            return False
        if not all(card_is_news(card) for card in result.cards):
            return False
        if (
            self._load_input_revision_receipt(
                context_scope=GLOBAL_NEW_CHAT_SCOPE, manifest_id=manifest_id,
            ) != receipt
        ):
            return False
        _, feedback = self._scope_state(context_scope=GLOBAL_NEW_CHAT_SCOPE, project_id=None)
        unread = retainable_pool(
            self.store,
            serving_compatibility_key=build_serving_compatibility_key(trigger_key),
            context_scope=GLOBAL_NEW_CHAT_SCOPE,
            project_id=None,
            feedback=feedback,
            now=self._now(),
        ).cards
        return any(
            not card_is_news(card) and card.get("category") != _CARRIED_CATEGORY for card in unread
        ) and (not result.cards or any(card_is_news(card) for card in unread))

    async def _prepare_then_generate(
        self,
        *,
        slot: _ScopeRun,
        origin: str,
        context_scope: str,
        project_id: str | None,
        work_mode: str = DEFAULT_TOPIC_WORK_MODE,
        visit_id: str,
        receipt: dict[str, str],
    ) -> str:
        config = self._ark_config
        previous = self._unsettled_attempt({"generation_trigger_key": self._trigger_key(
            context_scope=context_scope,
            project_id=project_id,
            work_mode=work_mode,
            input_revision_receipt=receipt)},
            context_scope=context_scope)
        if previous is not None:
            return ("abstained" if previous["event_type"] == "background_generation_abstained"
                    else previous["event_type"])
        self._start_outcome_collection()
        reason = ""
        if config is not None and self._prepare_state is not None:
            reason = await self._prepare_state(config)
        if not reason and self._prepare_short_intent is not None:
            reason = await self._prepare_short_intent(context_scope)
        trigger_key = self._trigger_key(
            context_scope=context_scope,
            project_id=project_id,
            work_mode=work_mode,
            input_revision_receipt=receipt,
        )
        stable_manifest_id = self._manifest_id_for_successor_state(trigger_key)
        if not reason and self._has_rid_abstention_for_trigger(trigger_key):
            return "abstained"
        answer = None
        if not reason:
            answer = self._reason_not_to_run(
                origin=origin, context_scope=context_scope, project_id=project_id,
                work_mode=work_mode, receipt=receipt, prepared=True,
            )
        if answer is not None:
            if (
                answer == "unchanged" and stable_manifest_id is not None
                and self._load_input_revision_receipt(
                    context_scope=context_scope, manifest_id=stable_manifest_id,
                ) != receipt
            ):
                # Preparation folded this input into the trigger the stocked pool was written under.
                self._record_input_revision_receipt(
                    context_scope=context_scope, manifest_id=stable_manifest_id, receipt=receipt,
                )
            if answer == "unchanged" and origin == "news_refresh":
                return await self._replace_news_card(receipt=receipt, visit_id=visit_id)
            return answer

        async def generate() -> BackgroundGenerationResult:
            if reason:
                return BackgroundGenerationResult(
                    cards=[],
                    manifest={"generation_trigger_key": trigger_key},
                    failure_reason=reason,
                )
            if config is None:
                raise RuntimeError("background model unavailable")
            result = await self._generate(
                GenerationTrigger(
                    trigger_key,
                    # A refresh the pool cannot answer is the run the scope already owed.
                    "lazy_seed" if origin == "news_refresh" else origin,
                ),
                config,
                context_scope,
                project_id,
                visit_id,
            )
            if not isinstance(result.manifest, dict):
                return result
            keeps_unread = origin not in {"news_check", "news_refresh"} and self._keeps_unread_cards(
                result, trigger_key=trigger_key, receipt=receipt, manifest_id=stable_manifest_id,
            )
            return BackgroundGenerationResult(
                cards=[] if keeps_unread else result.cards,
                manifest={
                    **result.manifest,
                    "consumed_input_revision": dict(receipt),
                },
                corpus_text_by_id=result.corpus_text_by_id,
                failure_reason="unread_cards_kept" if keeps_unread else result.failure_reason,
            )

        try:
            status = await self._coordinator.start(
                trigger_key,
                generate,
                ark_config=config,
                require_ark_config=not bool(reason),
                bind_pool_writer_model=True,
                restock=not reason and stable_manifest_id is not None,
            )
        except asyncio.CancelledError:
            parent = asyncio.current_task()
            if parent is None or parent.cancelling():
                raise
            return "background_generation_cancelled"
        finally:
            slot.candidate_task = None
        if status == "written":
            manifest_id = self._manifest_id_for_trigger(trigger_key)
            if manifest_id is not None:
                self._record_input_revision_receipt(
                    context_scope=context_scope,
                    manifest_id=manifest_id,
                    receipt=receipt,
                )
                if self._accepting_work and self.on_prepared:
                    self.on_prepared(
                        context_scope, "ready", True, work_mode=work_mode,
                    )
        return status

    def _request(
        self,
        *,
        origin: str,
        context_scope: str,
        project_id: str | None,
        work_mode: str = DEFAULT_TOPIC_WORK_MODE,
        visit_id: str,
    ) -> asyncio.Task[str]:
        """Ask for a replenishment run of one scope.

        A scope runs one at a time. Requests made while it runs merge into the
        single run queued behind it, which judges afresh once the first ends and
        keeps the strongest origin among them: a news check's regeneration, its
        news-card refresh, then an automatic request.
        """

        if not isinstance(visit_id, str) or not visit_id.strip():
            raise ValueError("visit_id is required")
        if not self._accepting_work:
            return asyncio.create_task(self._finished("disabled"))
        normalized_work_mode = normalize_topic_work_mode(work_mode)
        slot_key = self._slot_key(context_scope, normalized_work_mode)
        request = _RunRequest(origin, visit_id)
        slot = self._runs.get(slot_key)
        if slot is not None and slot.queued is not None and not slot.queued.done():
            if _ORIGIN_RANK[request.origin] >= _ORIGIN_RANK[slot.queued_request.origin]:
                slot.queued_request = request
            return slot.queued
        if slot is not None and slot.current is not None and not slot.current.done():
            slot.queued_request = request
            slot.queued = self._launch(slot, self._run_after(
                slot, slot.current,
                context_scope=context_scope,
                project_id=project_id,
                work_mode=normalized_work_mode,
            ), context_scope=context_scope, work_mode=normalized_work_mode)
            return slot.queued
        slot = self._runs[slot_key] = _ScopeRun()
        slot.current = self._launch(slot, self._run(
            slot, request,
            context_scope=context_scope,
            project_id=project_id,
            work_mode=normalized_work_mode,
        ), context_scope=context_scope, work_mode=normalized_work_mode)
        return slot.current

    def _launch(
        self,
        slot: _ScopeRun,
        run: Coroutine[Any, Any, str],
        *,
        context_scope: str,
        work_mode: str = DEFAULT_TOPIC_WORK_MODE,
    ) -> asyncio.Task[str]:
        task = asyncio.create_task(run)
        slot_key = self._slot_key(context_scope, work_mode)

        def report(done: asyncio.Task[str]) -> None:
            outcome = "failed" if done.cancelled() or done.exception() else done.result()
            if self._runs.get(slot_key) is not slot:
                return
            if outcome not in _QUIET_OUTCOMES:
                slot.outcome = outcome
            if slot.current is not done or slot.queued is not None:
                return
            del self._runs[slot_key]
            if not self._accepting_work:
                return
            if done.cancelled():
                return
            if not self.on_prepared:
                return
            if slot.outcome is None:
                return
            if slot.outcome in _WRITTEN_OUTCOMES:
                return
            # The chain wrote no pool: the page learns the state and keeps its cards.
            self.on_prepared(
                context_scope,
                self.preparation_state(context_scope, work_mode),
                False,
                work_mode=work_mode,
            )

        task.add_done_callback(report)
        return self._track(task)

    async def _run_after(
        self,
        slot: _ScopeRun,
        previous: asyncio.Task[str],
        *,
        context_scope: str,
        project_id: str | None,
        work_mode: str = DEFAULT_TOPIC_WORK_MODE,
    ) -> str:
        """The queued run: once the scope's current run ends, serve the request merged into it."""

        answered = ""
        try:
            answered = await previous
        except Exception:  # noqa: BLE001 - the queued run keeps its own audit
            pass
        # From here this is the scope's run; later requests queue behind it.
        slot.current, slot.queued = asyncio.current_task(), None
        slot.feedback_changed_before_candidates = False
        return await self._run(
            slot,
            slot.queued_request,
            context_scope=context_scope,
            project_id=project_id,
            work_mode=work_mode,
            answered=answered,
        )

    async def _run(
        self,
        slot: _ScopeRun,
        request: _RunRequest,
        *,
        context_scope: str,
        project_id: str | None,
        work_mode: str = DEFAULT_TOPIC_WORK_MODE,
        answered: str = "",
    ) -> str:
        """Serve one queued request; no total wall-clock cancel (per-call timeouts remain)."""

        attempt_id = f"background-attempt-{uuid.uuid4().hex}"
        context_token = _GENERATION_ATTEMPT_ID.set(attempt_id)
        try:
            return await self._serve_request(
                slot,
                request,
                context_scope=context_scope,
                project_id=project_id,
                work_mode=work_mode,
                answered=answered,
            )
        finally:
            _GENERATION_ATTEMPT_ID.reset(context_token)

    async def _serve_request(
        self,
        slot: _ScopeRun,
        request: _RunRequest,
        *,
        context_scope: str,
        project_id: str | None,
        work_mode: str = DEFAULT_TOPIC_WORK_MODE,
        answered: str,
    ) -> str:
        normalized_work_mode = normalize_topic_work_mode(work_mode)
        input_revision = await asyncio.to_thread(
            self._input_revision_loader, context_scope, normalized_work_mode,
        )
        receipt = self._input_revision_receipt(context_scope, input_revision)
        slot.receipt = receipt
        self._record_observed_input_revision(context_scope, receipt)
        # After a run that wrote, the request behind it is judged like an
        # automatic one: a pool written for this input and still stocked
        # answers it.
        after_write = answered in _WRITTEN_OUTCOMES
        outcome = self._reason_not_to_run(
            origin="lazy_seed" if after_write else request.origin,
            context_scope=context_scope,
            project_id=project_id,
            work_mode=normalized_work_mode,
            receipt=receipt,
            prepared=False,
        )
        if outcome == "unchanged" and after_write:
            return answered
        # A news-card refresh the stocked pool answers still reads the state as
        # it stands, which only preparation maintains: a dismissal since the
        # pool was written saved a new one.
        if outcome is None or (outcome == "unchanged" and request.origin == "news_refresh"):
            with provider_invocation_context(visit_id=request.visit_id):
                outcome = await self._prepare_then_generate(
                    slot=slot,
                    origin=request.origin,
                    context_scope=context_scope,
                    project_id=project_id,
                    work_mode=normalized_work_mode,
                    visit_id=request.visit_id,
                    receipt=receipt,
                )
        return outcome

    async def _replace_news_card(self, *, receipt: dict[str, str], visit_id: str) -> str:
        """Swap the news card of the stocked global pool; a failure leaves pool and page as they are.

        The successor keeps every other card as the pool wrote it, so it
        answers the same input the pool did, this one included.
        """

        config = self._ark_config
        trigger_key = self._trigger_key(
            context_scope=GLOBAL_NEW_CHAT_SCOPE,
            project_id=None,
            work_mode=DEFAULT_TOPIC_WORK_MODE,
            input_revision_receipt=receipt,
        )
        manifest_id = self._manifest_id_for_successor_state(trigger_key)
        if config is None:
            return "unchanged"
        if self._refresh_news is None:
            return "unchanged"
        if manifest_id is None:
            return "unchanged"
        refresh = asyncio.create_task(self._refresh_news(config, manifest_id, visit_id))
        self._news_refresh_task = refresh
        result = None
        cancellation_reason = None
        try:
            result = await refresh
        except asyncio.CancelledError:
            parent = asyncio.current_task()
            if parent is not None and parent.cancelling():
                raise
            cancellation_reason = "interaction_replenishment"
        finally:
            self._news_refresh_task = None
        reason = None
        written_id = None
        if result is not None and not self._accepting_work:
            # The switch can land while this refresh is already answering. Drop the
            # successor instead of putting personalized cards back on the home page.
            reason = "disabled"
        elif result is not None:
            reason = result.failure_reason
            if not reason:
                reason = write_terminal_pool(
                    self.store, result.cards, result.manifest,
                    created_at=time.time(), expected_model=config.expected_model or config.model,
                ).reason
            written_id = None if reason else str(result.manifest["manifest_id"])
        self.store.record_event({
            "session_id": "topic-starter-background",
            "slate_id": manifest_id,
            "event_type": NEWS_REFRESH_EVENT_TYPE,
            "topic_id": written_id or manifest_id,
            "policy_id": POLICY_ID,
            "selection_mode": "background_news_refresh",
            "propensity_reason": "not_estimated",
            "payload": {
                "predecessor_manifest_id": manifest_id,
                "manifest_id": written_id,
                "failure_reason": reason or None,
                **({"cancellation_reason": cancellation_reason} if cancellation_reason else {}),
            },
            "created_at": self._now(),
        })
        if written_id is None:
            logger.info("[Topic] news card kept: %s", cancellation_reason or reason)
            return "news_unchanged"
        self._record_input_revision_receipt(
            context_scope=GLOBAL_NEW_CHAT_SCOPE, manifest_id=written_id, receipt=receipt,
        )
        if self._accepting_work and self.on_prepared:
            self.on_prepared(
                GLOBAL_NEW_CHAT_SCOPE,
                "ready",
                True,
                work_mode=DEFAULT_TOPIC_WORK_MODE,
            )
        return "written"

    def _seen_news_events(self) -> set[str]:
        """The events the recent matches read: the lists behind the last pools' news, and scheduled checks'."""

        seen: set[str] = set()
        for row in self.store.query_events(
            event_type="starter_slate_manifest",
            policy_id=POLICY_ID,
            payload_equals={"context_scope": GLOBAL_NEW_CHAT_SCOPE},
            limit=5,
            newest_first=True,
        ):
            slot = (row.get("payload") or {}).get("news_slot") or {}
            pull = load_hot_list_pull(self.store, slot.get("hot_list_pull_receipt_id"))
            if pull is not None:
                seen.update(hot_event_key(entry.word) for entry in pull.entries[:HOT_LIST_MATCH_LIMIT])
        # A check that could not schedule (limit, a global run in flight) keeps
        # its need: its events stay unseen for the next check.
        for row in self.store.query_events(
            event_type=NEWS_CHECK_EVENT_TYPE, policy_id=POLICY_ID, limit=20, newest_first=True,
        ):
            payload = row.get("payload")
            if isinstance(payload, dict) and payload.get("scheduled") is True:
                seen.update(str(event) for event in payload.get("observed_events") or [])
        return seen

    def _news_led_pool(self) -> bool:
        """The global pool the page reads now leads with a news card that a new event could replace."""

        manifest_id = self._manifest_id_for_successor_state(
            self._trigger_key(context_scope=GLOBAL_NEW_CHAT_SCOPE, project_id=None)
        )
        rows = self.store.query_events(
            event_type="starter_slate_manifest", slate_id=manifest_id, policy_id=POLICY_ID, limit=1,
        ) if manifest_id is not None else []
        return bool(rows) and news_lead_card_id(rows[0].get("payload")) is not None

    def _news_successors_today(self, kind: str) -> int:
        """Checks of this local day that scheduled this kind of successor; a check without a kind regenerated."""

        local_date = time.strftime("%Y-%m-%d", time.localtime(self._now()))

        def _schedules_successor(row: Any) -> bool:
            row_payload = row.get("payload")
            if not isinstance(row_payload, dict):
                return False
            if row_payload.get("local_date") != local_date:
                return False
            if row_payload.get("scheduled") is not True:
                return False
            return row_payload.get("successor_kind", "full") == kind

        _row_items = self.store.query_events(
            event_type=NEWS_CHECK_EVENT_TYPE, policy_id=POLICY_ID, limit=48, newest_first=True,
        )
        return sum(
            1
            for row in _row_items
            if _schedules_successor(row)
        )

    def _record_news_check(self, payload: dict[str, Any]) -> None:
        check_id = f"news-check-{uuid.uuid4().hex}"
        self.store.record_event(
            {
                "session_id": "topic-starter-background",
                "slate_id": check_id,
                "event_type": NEWS_CHECK_EVENT_TYPE,
                "topic_id": check_id,
                "policy_id": POLICY_ID,
                "selection_mode": "background_news_check",
                "propensity_reason": "not_estimated",
                "payload": {
                    "local_date": time.strftime("%Y-%m-%d", time.localtime(self._now())),
                    **payload,
                },
                "created_at": self._now(),
            }
        )

    @staticmethod
    def schedule_news_check(visit_id: str) -> asyncio.Task[str] | None:
        """No-op: hot-news cards ride the ordinary personalized generation batch.

        Enable, chat successors, and manual retry already run the hot-news lane
        beside interest cards. A separate homepage/foreground news check is not
        scheduled.
        """

        del visit_id
        return None

    def schedule_seed(self, visit_id: str) -> asyncio.Task[str]:
        """Prepare global and historical project pools before the next visit."""
        async def prepare_history() -> str:
            if not self._accepting_work:
                return "disabled"
            project_ids = await asyncio.to_thread(self._history_project_ids_loader)
            tasks = [self.schedule_successor(
                context_scope=GLOBAL_NEW_CHAT_SCOPE,
                project_id=None,
                work_mode=DEFAULT_TOPIC_WORK_MODE,
                visit_id=visit_id,
            )]
            tasks.extend(self.schedule_successor(
                context_scope=scope_for_project(pid),
                project_id=pid,
                work_mode=DEFAULT_TOPIC_WORK_MODE,
                visit_id=visit_id,
            ) for pid in project_ids)
            results = await asyncio.gather(*tasks)
            return results[0]
        return self._track(asyncio.create_task(prepare_history()))

    def _candidate_input_changed(
        self,
        context_scope: str,
        slot: _ScopeRun,
        *,
        work_mode: str = DEFAULT_TOPIC_WORK_MODE,
    ) -> bool:
        if slot.queued is None:
            return False
        if slot.receipt is None:
            return False
        normalized_work_mode = normalize_topic_work_mode(work_mode)
        return self._input_revision_receipt(
            context_scope,
            self._input_revision_loader(context_scope, normalized_work_mode),
        ) != slot.receipt

    def candidate_generation_ready(
        self,
        context_scope: str,
        work_mode: str = DEFAULT_TOPIC_WORK_MODE,
    ) -> None:
        """Register candidate work after the scope's state and summaries are durable."""

        normalized_work_mode = normalize_topic_work_mode(work_mode)
        slot = self._runs.get(self._slot_key(context_scope, normalized_work_mode))
        if slot is None:
            return
        if slot.current is None:
            return
        if slot.current.done():
            return
        slot.candidate_task = asyncio.current_task()
        if slot.feedback_changed_before_candidates or self._candidate_input_changed(
            context_scope, slot, work_mode=normalized_work_mode,
        ):
            slot.feedback_changed_before_candidates = False
            slot.candidate_task = None
            raise asyncio.CancelledError

    def schedule_successor(
        self,
        *,
        context_scope: str,
        project_id: str | None,
        work_mode: str = DEFAULT_TOPIC_WORK_MODE,
        visit_id: str,
    ) -> asyncio.Task[str]:
        """Request one scoped successor from the durable completed discussion boundary.

        Chat completion and enable-seed call this with whatever completed history
        already exists; a later completion schedules another run if needed.
        """

        if project_id_for_scope(context_scope) != project_id:
            raise ValueError("project mismatch")
        normalized_work_mode = normalize_topic_work_mode(work_mode)
        task = self._request(
            origin="lazy_seed",
            context_scope=context_scope,
            project_id=project_id,
            work_mode=normalized_work_mode,
            visit_id=visit_id,
        )
        slot = self._runs.get(self._slot_key(context_scope, normalized_work_mode))
        _condition = (
            slot is not None and slot.candidate_task is not None
            and not slot.candidate_task.done()
            and not slot.candidate_task.cancelling()
            and self._candidate_input_changed(
                context_scope, slot, work_mode=normalized_work_mode,
            )
        )
        if (_condition):
            slot.candidate_task.cancel()
        return task

    def _record_issued_slate(
        self,
        *,
        session_id: str,
        slate_id: str,
        served: dict[str, Any],
    ) -> bool:
        return self.store.record_event(
            {
                "session_id": session_id,
                "slate_id": slate_id,
                "event_type": ISSUED_SLATE_EVENT_TYPE,
                "topic_id": served["manifest_id"],
                "profile_version": served.get("profile_semantic_version"),
                "policy_id": POLICY_ID,
                "selection_mode": served["selection_mode"],
                "propensity_reason": "not_estimated",
                "payload": {
                    "manifest_id": served["manifest_id"],
                    "generation_batch_id": served["generation_batch_id"],
                    "context_scope": served["context_scope"],
                    "project_id": served["project_id"],
                    "work_mode": normalize_topic_work_mode(served.get("work_mode")),
                    "profile_semantic_version": served.get(
                        "profile_semantic_version"
                    ),
                    "short_intent_version": served.get("short_intent_version"),
                    "component_receipts": served.get("component_receipts"),
                    "generation_config_version": served.get(
                        "generation_config_version"
                    ),
                    "exposure_slice_id": served["exposure_slice_id"],
                    "exposure_window_index": served["exposure_window_index"],
                    "card_revision_ids": [
                        card["card_revision_id"] for card in served["cards"]
                    ],
                },
                "created_at": self._now(),
            }
        )

    def _load_issued_slate(self, event: dict[str, Any]) -> dict[str, Any] | None:
        slate_id = str(event.get("slate_id") or "")
        manifest_id = str(event.get("manifest_id") or "")
        rows = self.store.query_events(
            event_type=ISSUED_SLATE_EVENT_TYPE,
            slate_id=slate_id,
            topic_id=manifest_id,
            policy_id=POLICY_ID,
            limit=2,
            newest_first=True,
        )
        if len(rows) != 1 or rows[0].get("session_id") != event.get("session_id"):
            return None
        issued = rows[0].get("payload")
        if not isinstance(issued, dict):
            return None
        supplied_topics = event.get("topics")
        supplied_ids = [
            str(topic.get("card_revision_id") or "")
            for topic in supplied_topics or []
            if isinstance(topic, dict)
        ]
        expected = {
            "manifest_id": manifest_id,
            "generation_batch_id": event.get("generation_batch_id"),
            "context_scope": event.get("context_scope"),
            "project_id": event.get("project_id"),
            "exposure_slice_id": event.get("exposure_slice_id"),
            "card_revision_ids": supplied_ids,
        }
        if any(issued.get(key) != value for key, value in expected.items()):
            return None
        return {
            **issued,
            "cards": [
                {"card_revision_id": revision_id}
                for revision_id in issued["card_revision_ids"]
            ],
            "selection_mode": str(rows[0].get("selection_mode") or ""),
        }

    def _opened_cold_start_card_filter(
        self,
        work_mode: str = DEFAULT_TOPIC_WORK_MODE,
    ) -> Callable[[dict[str, Any]], bool]:
        """A preset card the user already opened stays out of a rebuilt pool.

        Suppression is scoped to the same work_mode so a work click does not
        remove the corpus card from design/code rebuilds.
        """

        normalized_work_mode = normalize_topic_work_mode(work_mode)
        opened_cards: set[str] = set()
        opened_starters: set[str] = set()
        for row in self.store.query_events(
            event_type="topic_click", policy_id=POLICY_ID, limit=100_000,
        ):
            payload = row.get("payload")
            if not isinstance(payload, dict):
                continue
            if payload.get("context_scope") not in (None, GLOBAL_NEW_CHAT_SCOPE):
                continue
            click_mode = payload.get("work_mode")
            if click_mode in (None, ""):
                # Legacy clicks without work_mode only suppress the work pool.
                if normalized_work_mode != DEFAULT_TOPIC_WORK_MODE:
                    continue
            elif normalize_topic_work_mode(click_mode) != normalized_work_mode:
                continue
            card_id = str(payload.get("card_id") or "")
            starter_id = str(payload.get("source_starter_id") or "")
            if card_id:
                opened_cards.add(card_id)
            if starter_id:
                opened_starters.add(starter_id)

        def is_opened(card: dict[str, Any]) -> bool:
            return (
                str(card.get("card_id") or "") in opened_cards
                or str(card.get("source_starter_id") or "") in opened_starters
            )

        return is_opened

    def _serve_cold_start(
        self,
        *,
        trigger_key: dict[str, Any],
        feedback: InterestFeedback,
        issued_slate: dict[str, Any] | None,
        preset_only: bool = False,
    ) -> dict[str, Any]:
        """Write packaged preset cards so a scope visit has a slate to show.

        Global and project scopes share this stand-in: when recommendation is off
        or the personalized pool is empty, only the packaged corpus fills the page.
        Hot search stays on the personalized generation path, never mixed here.
        """

        context_scope = str(trigger_key["context_scope"])
        project_id = trigger_key.get("project_id")
        empty = read_terminal_pool(
            self.store,
            serving_compatibility_key=build_serving_compatibility_key(trigger_key),
            context_scope=context_scope,
            project_id=project_id,
            feedback=feedback,
            now=self._now(),
            issued_slate=issued_slate,
        )
        existing_is_preset = bool(empty["cards"]) and all(
            is_cold_start_card(card) for card in empty["cards"]
        )
        if empty["cards"]:
            if not preset_only:
                return empty
            # With recommendations off the packaged pool is the whole page: a card
            # the user dismissed leaves the screen short, so the corpus is sampled
            # again instead of showing one card fewer.
            if existing_is_preset and len(empty["cards"]) >= FIRST_SCREEN_CARD_TARGET:
                return empty
        # Personalized pool miss still fills the page with packaged presets only.
        cards = build_cold_start_cards(
            trigger_key=trigger_key,
            now=self._now(),
            feedback=feedback,
            exclude=self._opened_cold_start_card_filter(
                normalize_topic_work_mode(trigger_key.get("work_mode")),
            ),
        )
        if not cards:
            return empty
        # Compare like with like: the rebuilt pool against what the current pool
        # can still serve, not against the one window it currently exposes.
        current_serviceable = len(empty.get("served_card_ids") or empty["cards"])
        if preset_only and existing_is_preset and len(cards) <= current_serviceable:
            # The corpus cannot fill the screen again; keep what the page has.
            return empty
        written = write_cold_start_pool(
            self.store,
            cards,
            trigger_key=trigger_key,
            created_at=self._now(),
        )
        if not written:
            return empty
        return read_terminal_pool(
            self.store,
            serving_compatibility_key=build_serving_compatibility_key(trigger_key),
            context_scope=context_scope,
            project_id=project_id,
            feedback=feedback,
            now=self._now(),
            issued_slate=issued_slate,
        )

    def serve(
        self,
        session_id: str,
        *,
        context_scope: str,
        project_id: str | None,
        work_mode: str = DEFAULT_TOPIC_WORK_MODE,
        slate_id: str | None = None,
    ) -> dict[str, Any]:
        """Read the prepared pool without starting background work."""

        try:
            if project_id_for_scope(context_scope) != project_id:
                raise ValueError("project mismatch")
        except ValueError:
            return {
                "available": False,
                "reason": "invalid_context_scope",
                "session_id": session_id,
                "policy_id": POLICY_ID,
            }

        normalized_work_mode = normalize_topic_work_mode(work_mode)
        try:
            self.store.prune(now=self._now())
        except Exception:  # noqa: BLE001 - serving must not fail on maintenance
            logger.warning("[Topic] store prune failed", exc_info=True)
        _, feedback = self._scope_state(context_scope=context_scope, project_id=project_id)
        trigger_key = self._trigger_key(
            context_scope=context_scope,
            project_id=project_id,
            work_mode=normalized_work_mode,
        )
        issued = None
        if slate_id:
            rows = self.store.query_events(
                event_type=ISSUED_SLATE_EVENT_TYPE,
                slate_id=slate_id,
                policy_id=POLICY_ID,
                limit=2,
            )
            if len(rows) == 1 and rows[0].get("session_id") == session_id:
                payload = rows[0].get("payload")
                if _issued_slate_in_scope(
                    payload,
                    context_scope=context_scope,
                    project_id=project_id,
                    work_mode=normalized_work_mode,
                ):
                    issued = payload
        # Packaged corpus presets are the work-mode welcome stand-in only.
        # Design / code keep separate personalized pools and must not fall back
        # to the shared lifestyle corpus (or resurface an older cold-start write).
        allow_packaged_presets = normalized_work_mode == DEFAULT_TOPIC_WORK_MODE
        preset_only = not self._accepting_work or self._ark_config is None
        if allow_packaged_presets and preset_only:
            # Global and project welcome pages both stand on packaged cards while
            # recommendation is off or the background model is unset.
            served = self._serve_cold_start(
                trigger_key=trigger_key,
                feedback=feedback,
                issued_slate=issued,
                preset_only=True,
            )
        elif allow_packaged_presets:
            served = read_terminal_pool(
                self.store,
                serving_compatibility_key=build_serving_compatibility_key(trigger_key),
                context_scope=context_scope,
                project_id=project_id,
                feedback=feedback,
                now=self._now(),
                issued_slate=issued,
            )
            if not served["cards"]:
                served = self._serve_cold_start(
                    trigger_key=trigger_key,
                    feedback=feedback,
                    issued_slate=issued,
                )
        else:
            served = read_terminal_pool(
                self.store,
                serving_compatibility_key=build_serving_compatibility_key(trigger_key),
                context_scope=context_scope,
                project_id=project_id,
                feedback=feedback,
                now=self._now(),
                issued_slate=issued,
            )
            if served["cards"] and all(
                is_cold_start_card(card) for card in served["cards"]
            ):
                served = {**served, "cards": [], "served_card_ids": [], "pool_card_count": 0}
        if not served["cards"]:
            return {
                "available": False,
                "reason": "v6_pool_miss",
                "preparation_state": self.preparation_state(
                    context_scope, normalized_work_mode,
                ),
                "session_id": session_id,
                "policy_id": POLICY_ID,
                "context_scope": context_scope,
                "project_id": project_id,
                "work_mode": normalized_work_mode,
            }

        slate_id = f"v6-slate-{uuid.uuid4().hex}"
        topics = [
            {
                "id": card["card_revision_id"],
                "card_revision_id": card["card_revision_id"],
                "card_id": card["card_id"],
                "generation_batch_id": served["generation_batch_id"],
                "interest_summary_id": card.get("interest_summary_id"),
                "source_starter_id": card.get("source_starter_id"),
                "position": position,
                "selector_rank": int(card["selector_rank"]),
                "category": card["category"],
                "title": card["title"],
                "prompt": card["prompt"],
                "intent": card["intent"],
                "grounding_mode": "exact_evidence",
                "evidence": [{
                    "source_url": (card.get("evidence_bundle") or {}).get("source_url"),
                    "source_kind": (card.get("evidence_bundle") or {}).get("source_kind"),
                    "publisher": (card.get("evidence_bundle") or {}).get("publisher"),
                    "published_at": (card.get("evidence_bundle") or {}).get("published_at"),
                    "date_page_url": (
                        (((card.get("evidence_bundle") or {}).get("publication_date_provenance") or {})
                         .get("linked_page") or {}).get("date_page_url")
                    ),
                    "listed_at": (card.get("evidence_bundle") or {}).get("listed_at"),
                    "hot_list_rank": (
                        ((card.get("evidence_bundle") or {}).get("host_receipt") or {}).get("hot_list_rank")
                    ),
                    **({
                        "source_text": card["evidence_bundle"]["content_boundary"]["text"],
                        "search_url": card["evidence_bundle"].get("search_url", card["evidence_bundle"]["source_url"]),
                    } if card["evidence_bundle"].get("source_kind") == HOT_LIST_SOURCE_KIND else {}),
                }],
            }
            for position, card in enumerate(served["cards"])
        ]
        response = {
            "available": True,
            "session_id": session_id,
            "slate_id": slate_id,
            "manifest_id": served["manifest_id"],
            "generation_batch_id": served["generation_batch_id"],
            "profile_version": served["profile_semantic_version"],
            "profile_semantic_version": served["profile_semantic_version"],
            "policy_id": POLICY_ID,
            "context_scope": context_scope,
            "project_id": project_id,
            "work_mode": normalized_work_mode,
            "short_intent_version": served["short_intent_version"],
            "pool_card_count": served["pool_card_count"],
            "exposure_slice_id": served["exposure_slice_id"],
            "exposure_window_index": served["exposure_window_index"],
            "retrieval_health": "eligible",
            "selection_mode": served["selection_mode"],
            "slate_card_count": len(topics),
            "topics": topics,
            "served_card_ids": served["served_card_ids"],
        }
        # The cards stay usable while a refresh runs or after it fails.
        state = self.preparation_state(context_scope, normalized_work_mode)
        # Preset cards are a stand-in when the personalized pool has nothing to
        # show. Serving them must not itself start a run: enable seeds, chat /
        # news successors, and manual retry own generation. Changing the
        # recommendation model only reloads config; a welcome re-serve of the
        # same presets must keep those cards and stay quiet.
        only_preset_cards = bool(served["cards"]) and all(
            is_cold_start_card(card) for card in served["cards"]
        )
        # Writing the stand-in pool settles a prior failed attempt, which would
        # otherwise clear provider_unavailable; keep the refusal visible.
        if (
            only_preset_cards
            and self._accepting_work
            and self.configuration_status().get("provider_refused")
        ):
            state = "provider_unavailable"
        elif only_preset_cards:
            slot = self._runs.get(
                self._slot_key(context_scope, normalized_work_mode)
            )
            # Only this scope × work_mode slot. Global _tasks include other
            # scopes' successors and outcome collection, which must not paint
            # an unrelated welcome page as preparing.
            if slot is not None and slot.busy:
                state = "preparing"
        if state in {
            "preparing",
            "failed",
            "provider_unavailable",
            "disabled",
            "no_content",
        }:
            response["preparation_state"] = state
        if not self._record_issued_slate(
            session_id=session_id,
            slate_id=slate_id,
            served=served,
        ):
            return {
                "available": False,
                "reason": "v6_slate_issue_not_recorded",
                "session_id": session_id,
                "policy_id": POLICY_ID,
                "context_scope": context_scope,
                "project_id": project_id,
            }
        return response

    def record_exposure(
        self, event: dict[str, Any], *, created_at: float | None = None
    ) -> bool:
        """Load the exact issued exposure before accepting client IDs."""

        served = self._load_issued_slate(event)
        if served is None:
            return False
        return record_terminal_exposure(
            self.store,
            session_id=str(event.get("session_id") or ""),
            slate_id=str(event.get("slate_id") or ""),
            served=served,
            created_at=float(created_at if created_at is not None else self._now()),
        )

    def record_interaction(
        self,
        event: dict[str, Any],
        *,
        outcome_queue: NewPageOutcomeQueue,
        created_at: float | None = None,
    ) -> dict[str, bool]:
        """Record an exact shown card click. Refill waits for chat completion."""

        event_type = str(event.get("event_type") or "").strip()
        payload = event["payload"]
        card_revision_id = str(payload.get("card_revision_id") or "").strip()
        if event_type not in TERMINAL_INTERACTION_EVENT_TYPES:
            return {"recorded": False}
        if not card_revision_id:
            return {"recorded": False}
        if card_revision_id != str(event.get("topic_id") or "").strip():
            return {"recorded": False}
        manifest_id = str(payload.get("manifest_id") or "").strip()
        generation_batch_id = str(payload.get("generation_batch_id") or "").strip()
        context_scope = str(payload.get("context_scope") or "")
        project_id = payload.get("project_id")
        card = find_exact_terminal_card(
            self.store,
            manifest_id=manifest_id,
            card_revision_id=card_revision_id,
            generation_batch_id=generation_batch_id,
            context_scope=context_scope,
            project_id=project_id,
        )
        if card is None:
            return {"recorded": False}
        receipts = card.get("component_receipts")
        component_receipt_entries: list[tuple[str, str]] = []
        for component_id, receipt in (receipts or {}).items():
            if not isinstance(receipt, dict):
                continue
            request_id = str(receipt.get("request_id") or "")
            if not str(component_id).strip():
                continue
            if not request_id.strip():
                continue
            component_receipt_entries.append((str(component_id), request_id))
        component_request_ids = tuple(sorted(component_receipt_entries))
        provider_response_ids = card_provider_response_ids(card)
        _failed = (
            not is_cold_start_card(card) and (
                not component_request_ids or len(component_request_ids) != len(receipts or {})
                or not provider_response_ids
            )
        )
        if _failed:
            return {"recorded": False}
        occurred_at = float(created_at if created_at is not None else self._now())
        exposure_id = str(event["slate_id"])
        status = outcome_queue.publish(NewPageOutcomeSignal(
            outcome="accepted",
            manifest_id=manifest_id,
            generation_batch_id=generation_batch_id,
            exposure_id=exposure_id,
            card_revision_id=card_revision_id,
            context_scope=context_scope,
            project_id=project_id,
            component_request_ids=component_request_ids,
            provider_response_ids=provider_response_ids,
            search_receipt=card_search_receipt(card),
            occurred_at=occurred_at,
            host_written=is_cold_start_card(card),
        ))
        return {"recorded": status == "recorded"}

    async def wait_for_background(self) -> None:
        tasks = [task for task in self._tasks if not task.done()]
        if tasks:
            await asyncio.gather(*tasks)

    async def shutdown(self) -> None:
        self._accepting_work = False
        news = self._news_refresh_task
        if news is not None and not news.done():
            news.cancel()
        tasks = [task for task in self._tasks if not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

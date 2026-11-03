# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Append-only topic card pool, ordered manifests, and background generation."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, NamedTuple

from jiuwenswarm.agents.harness.common.recommendation.topic_history import (
    apply_negative_preferences,
    filter_distinct_personalized_candidates,
)
from jiuwenswarm.agents.harness.common.recommendation.topic_model import (
    ArkBackgroundConfig,
)
from jiuwenswarm.agents.harness.common.recommendation.topic_starter import (
    TopicStarterStore,
)

POLICY_ID = "topic-starter-mvp-v5"
PAYLOAD_VERSION = "topic-starter-pool-v1"
BACKGROUND_GENERATION_DEADLINE_SECONDS = 180.0

_PROMPT_IDS = frozenset({"i1", "i2", "i3", "i4", "i5"})
_CARD_REQUIRED_FIELDS = frozenset(
    {
        "card_id",
        "card_revision_id",
        "generation_batch_id",
        "selector_rank",
        "source_starter_id",
        "interest_summary_id",
        "lane",
        "origin",
        "intent",
        "title",
        "prompt",
        "freshness_class",
        "effective_time",
        "expire_time",
        "evidence_bundle",
        "source_memory_unit_ids",
        "source_locators",
        "identity_anchor",
        "context_anchors",
        "model_provenance",
        "prompt_versions",
        "policy_id",
        "payload_version",
        "retrieval_config_version",
        "corpus_version",
    }
)
_MANIFEST_REQUIRED_FIELDS = frozenset(
    {
        "manifest_id",
        "generation_batch_id",
        "card_revision_ids",
        "trigger_origin",
        "local_date",
        "generation_trigger_key",
        "generation_input_snapshot",
        "serving_compatibility_key",
        "negative_preference_version",
        "policy_id",
        "payload_version",
    }
)


def _plain_int(value: Any) -> bool:
    # bool is an int subclass; a JSON boolean is never a version or a count.
    return isinstance(value, int) and not isinstance(value, bool)


def build_generation_trigger_key(
    *,
    local_date: str,
    profile_semantic_version: int,
    negative_preference_version: int,
    ark_model: str,
    ark_base_url: str,
    effective_temperature: int | float,
    effective_top_p: int | float,
    context_window_tokens: int,
    prompt_versions: dict[str, str],
    corpus_version: str,
    retrieval_config_version: str,
) -> dict[str, Any]:
    """Why a new batch may be generated; no digest or secret is needed."""

    return {
        "local_date": str(local_date),
        "profile_semantic_version": int(profile_semantic_version),
        "negative_preference_version": int(negative_preference_version),
        "ark_model": str(ark_model),
        "ark_base_url": str(ark_base_url),
        "effective_temperature": effective_temperature,
        "effective_top_p": effective_top_p,
        "context_window_tokens": int(context_window_tokens),
        "prompt_versions": dict(prompt_versions),
        "policy_id": POLICY_ID,
        "payload_version": PAYLOAD_VERSION,
        "corpus_version": str(corpus_version),
        "retrieval_config_version": str(retrieval_config_version),
    }


def build_serving_compatibility_key(
    generation_trigger_key: dict[str, Any],
) -> dict[str, Any]:
    """Serving compatibility deliberately excludes only negative version."""

    return {
        key: value
        for key, value in generation_trigger_key.items()
        if key != "negative_preference_version"
    }


def _has_secret_field(value: Any) -> bool:
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = str(key).casefold()
            if "api_key" in normalized or normalized == "authorization":
                return True
            if _has_secret_field(child):
                return True
    elif isinstance(value, list):
        return any(_has_secret_field(child) for child in value)
    return False


def _valid_evidence(card: dict[str, Any]) -> bool:
    bundle = card.get("evidence_bundle")
    if not isinstance(bundle, dict):
        return False
    boundary = bundle.get("content_boundary")
    if not isinstance(boundary, dict):
        return False
    text = boundary.get("text")
    start = boundary.get("start")
    end = boundary.get("end")
    if bundle.get("fetch_status") != "success":
        return False
    if not str(bundle.get("source_url") or "").strip():
        return False
    if not str(bundle.get("source_kind") or "").strip():
        return False
    if not isinstance(text, str):
        return False
    if not text:
        return False
    if not _plain_int(start):
        return False
    if not _plain_int(end):
        return False
    if start < 0:
        return False
    if end - start != len(text):
        return False
    if card.get("freshness_class") in {"current_event", "recent_development"}:
        return bool(
            str(bundle.get("published_at") or "").strip()
            and str(bundle.get("published_at_source") or "").strip()
        )
    return True


def _valid_card(
    card: dict[str, Any],
    *,
    corpus_text_by_id: dict[str, str] | None,
) -> bool:
    if not isinstance(card, dict) or not _CARD_REQUIRED_FIELDS.issubset(card):
        return False
    if (
        card.get("policy_id") != POLICY_ID
        or card.get("payload_version") != PAYLOAD_VERSION
    ):
        return False
    if set(card.get("prompt_versions") or {}) != _PROMPT_IDS:
        return False
    if _has_secret_field(card) or not _valid_evidence(card):
        return False
    try:
        effective = float(card["effective_time"])
        expire = float(card["expire_time"])
    except (TypeError, ValueError):
        return False
    if effective >= expire:
        return False
    freshness = str(card.get("freshness_class") or "")
    if freshness == "current_event" and expire - effective > 48 * 60 * 60:
        return False
    if freshness == "evergreen_corpus":
        if expire - effective > 30 * 24 * 60 * 60:
            return False
        if (card.get("evidence_bundle") or {}).get("source_kind") not in {
            "authoritative",
            "stable",
        }:
            return False
    if card.get("origin") == "retrieved-fixed":
        source_id = str(card.get("source_starter_id") or "")
        if not source_id or corpus_text_by_id is None:
            return False
        if corpus_text_by_id.get(source_id) != card.get("title"):
            return False
    _field_items = (
        "card_id",
        "card_revision_id",
        "generation_batch_id",
        "intent",
        "title",
        "prompt",
    )
    return all(
        bool(str(card.get(field) or "").strip())
        for field in _field_items
    )


def _valid_manifest(manifest: dict[str, Any], cards: list[dict[str, Any]]) -> bool:
    if not isinstance(manifest, dict) or not _MANIFEST_REQUIRED_FIELDS.issubset(
        manifest
    ):
        return False
    if (
        manifest.get("policy_id") != POLICY_ID
        or manifest.get("payload_version") != PAYLOAD_VERSION
    ):
        return False
    if manifest.get("trigger_origin") not in {"scheduled_cron", "lazy_seed"}:
        return False
    trigger_key = manifest.get("generation_trigger_key")
    if not isinstance(trigger_key, dict) or _has_secret_field(manifest):
        return False
    if set(trigger_key.get("prompt_versions") or {}) != _PROMPT_IDS:
        return False
    if manifest.get("serving_compatibility_key") != build_serving_compatibility_key(
        trigger_key
    ):
        return False
    if manifest.get("negative_preference_version") != trigger_key.get(
        "negative_preference_version"
    ):
        return False
    expected_model_provenance = {
        "model": trigger_key.get("ark_model"),
        "base_url": trigger_key.get("ark_base_url"),
        "effective_temperature": trigger_key.get("effective_temperature"),
        "effective_top_p": trigger_key.get("effective_top_p"),
        "context_window_tokens": trigger_key.get("context_window_tokens"),
    }
    if manifest.get("local_date") != trigger_key.get("local_date"):
        return False
    for card in cards:
        model_provenance = card.get("model_provenance")
        if not isinstance(model_provenance, dict):
            return False
        if any(
            model_provenance.get(key) != value
            for key, value in expected_model_provenance.items()
        ):
            return False
        if model_provenance.get("context_window_source") != "ARK_CONTEXT_WINDOW_TOKENS":
            return False
        if model_provenance.get("decoding_parameter_source") != "request_parameters":
            return False
        if (
            model_provenance.get("requested_temperature")
            != model_provenance.get("effective_temperature")
            or model_provenance.get("requested_top_p")
            != model_provenance.get("effective_top_p")
        ):
            return False
        if card.get("prompt_versions") != trigger_key.get("prompt_versions"):
            return False
        if card.get("policy_id") != trigger_key.get("policy_id"):
            return False
        if card.get("payload_version") != trigger_key.get("payload_version"):
            return False
        if card.get("corpus_version") != trigger_key.get("corpus_version"):
            return False
        if (
            card.get("retrieval_config_version")
            != trigger_key.get("retrieval_config_version")
        ):
            return False
    ranks = [card.get("selector_rank") for card in cards]
    if any(not _plain_int(rank) or rank < 1 for rank in ranks):
        return False
    if len(ranks) != len(set(ranks)):
        return False
    ordered = sorted(cards, key=lambda card: card["selector_rank"])
    revisions = [str(card.get("card_revision_id") or "") for card in ordered]
    return (
        len(revisions) == len(set(revisions))
        and manifest.get("card_revision_ids") == revisions
        and all(
            card.get("generation_batch_id") == manifest.get("generation_batch_id")
            for card in cards
        )
    )


def write_starter_pool(
    store: TopicStarterStore,
    cards: list[dict[str, Any]],
    manifest: dict[str, Any],
    *,
    created_at: float,
    corpus_text_by_id: dict[str, str] | None = None,
) -> bool:
    """Atomically append 1–4 immutable cards and their ordered manifest."""

    if not 1 <= len(cards) <= 4:
        return False
    if any(
        not _valid_card(card, corpus_text_by_id=corpus_text_by_id) for card in cards
    ):
        return False
    if not _valid_manifest(manifest, cards):
        return False
    manifest_id = str(manifest["manifest_id"])
    events = [
        {
            "session_id": "v5-pool",
            "slate_id": manifest_id,
            "event_type": "starter_card",
            "topic_id": card["card_revision_id"],
            "position": card["selector_rank"],
            "policy_id": POLICY_ID,
            "selection_mode": "pool_generation",
            "propensity_reason": "not_estimated",
            "payload": card,
            "created_at": created_at,
        }
        for card in cards
    ]
    events.append(
        {
            "session_id": "v5-pool",
            "slate_id": manifest_id,
            "event_type": "starter_slate_manifest",
            "topic_id": manifest_id,
            "policy_id": POLICY_ID,
            "selection_mode": "pool_generation",
            "propensity_reason": "not_estimated",
            "payload": manifest,
            "created_at": created_at,
        }
    )
    return store.record_events_atomic(events)


def _is_personalized(card: dict[str, Any]) -> bool:
    return bool(
        card.get("interest_summary_id")
        and card.get("source_memory_unit_ids")
        and card.get("source_locators")
        and card.get("identity_anchor")
    )


def classify_starter_slate(cards: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(cards)
    personalized = sum(_is_personalized(card) for card in cards)
    trends = sum(card.get("lane") == "fresh-trend" for card in cards)
    if total == 0:
        mode = "static"
    elif personalized == 0:
        mode = "generic_dynamic"
    elif personalized / total < 0.75:
        mode = "mixed_dynamic"
    else:
        mode = "personalized_dynamic"
    return {
        "selection_mode": mode,
        "slate_card_count": total,
        "personalized_card_count": personalized,
        "trend_card_count": trends,
    }


def _static_read() -> dict[str, Any]:
    return {
        "manifest_id": None,
        "generation_batch_id": None,
        "manifest_negative_preference_version": None,
        "cards": [],
        **classify_starter_slate([]),
    }


def find_exact_pool_card(
    store: TopicStarterStore,
    *,
    manifest_id: str,
    card_revision_id: str,
    generation_batch_id: str,
) -> dict[str, Any] | None:
    """Join immutable manifest membership to one exact card revision."""

    events = store.query_events(
        event_type="starter_slate_manifest",
        slate_id=manifest_id,
        policy_id=POLICY_ID,
        limit=1,
        newest_first=True,
    )
    manifest: dict[str, Any] | None = None
    for event in events:
        payload = event.get("payload")
        if event.get("event_type") != "starter_slate_manifest":
            continue
        if event.get("slate_id") != manifest_id:
            continue
        if not isinstance(payload, dict):
            continue
        if payload.get("manifest_id") != manifest_id:
            continue
        if payload.get("generation_batch_id") != generation_batch_id:
            continue
        manifest = payload
        break
    if manifest is None or card_revision_id not in (
        manifest.get("card_revision_ids") or []
    ):
        return None
    card_events = store.query_events(
        event_type="starter_card",
        slate_id=manifest_id,
        policy_id=POLICY_ID,
        limit=7,
    )
    matched_card: dict[str, Any] | None = None
    for event in card_events:
        payload = event.get("payload")
        if event.get("slate_id") != manifest_id:
            continue
        if event.get("topic_id") != card_revision_id:
            continue
        if not isinstance(payload, dict):
            continue
        if payload.get("card_revision_id") != card_revision_id:
            continue
        if payload.get("generation_batch_id") != generation_batch_id:
            continue
        matched_card = payload
        break
    return matched_card


def _negative_preference_version(item: dict[str, Any]) -> int:
    return int(item.get("negative_preference_version") or 0)


def read_starter_pool(
    store: TopicStarterStore,
    *,
    serving_compatibility_key: dict[str, Any],
    negative_preference_version: int,
    negative_preferences: list[dict[str, Any]],
    now: float,
) -> dict[str, Any]:
    """Read one compatible batch in selector order; never fill from another."""

    events = store.query_events(
        event_type="starter_slate_manifest",
        policy_id=POLICY_ID,
        limit=64,
        newest_first=True,
    )
    manifests: list[dict[str, Any]] = []
    for event in events:
        payload = event.get("payload")
        if not isinstance(payload, dict):
            continue
        if payload.get("serving_compatibility_key") != serving_compatibility_key:
            continue
        if (
            _negative_preference_version(payload)
            > negative_preference_version
        ):
            continue
        manifests.append(
            {
                **payload,
                "_created_at": float(event.get("created_at") or 0.0),
            }
        )
    if not manifests:
        return _static_read()
    exact = [
        manifest
        for manifest in manifests
        if _negative_preference_version(manifest) == negative_preference_version
    ]
    choices = exact or manifests
    manifest = max(
        choices,
        key=lambda item: (
            int(item.get("negative_preference_version") or 0),
            float(item.get("_created_at") or 0.0),
        ),
    )
    manifest_id = str(manifest.get("manifest_id") or "")
    batch_id = str(manifest.get("generation_batch_id") or "")
    card_events = store.query_events(
        event_type="starter_card",
        slate_id=manifest_id,
        policy_id=POLICY_ID,
        limit=7,
    )
    cards_by_revision: dict[str, dict[str, Any]] = {}
    for event in card_events:
        payload = event.get("payload")
        if event.get("slate_id") != manifest_id:
            continue
        if not isinstance(payload, dict):
            continue
        if payload.get("generation_batch_id") != batch_id:
            continue
        revision = str(payload.get("card_revision_id") or "")
        cards_by_revision[revision] = payload
    cards: list[dict[str, Any]] = []
    trend_seen = False
    for revision in manifest.get("card_revision_ids") or []:
        card = cards_by_revision.get(str(revision))
        if card is None:
            continue
        if (
            not float(card.get("effective_time") or 0.0)
            <= now
            < float(card.get("expire_time") or 0.0)
        ):
            continue
        if not _valid_evidence(card):
            continue
        if card.get("lane") == "fresh-trend":
            if trend_seen:
                continue
            trend_seen = True
        candidate_view = {
            **card,
            "candidate_id": card.get("card_revision_id"),
            "persona": card.get("title"),
            "primary_query": card.get("prompt"),
            "fallback_query": card.get("title"),
        }
        survivors, _ = apply_negative_preferences(
            [candidate_view], negative_preferences
        )
        if survivors:
            cards.append(card)
    cards = filter_distinct_personalized_candidates(cards, target_count=4)
    return {
        "manifest_id": manifest_id,
        "generation_batch_id": batch_id,
        "manifest_negative_preference_version": int(
            manifest.get("negative_preference_version") or 0
        ),
        "cards": cards,
        **classify_starter_slate(cards),
    }

_GENERATION_ATTEMPT_ID: ContextVar[str | None] = ContextVar(
    "topic_starter_background_generation_attempt_id",
    default=None,
)
TerminalExternalAuditLoader = Callable[[str], dict[str, Any] | None]


def current_background_generation_attempt_id() -> str | None:
    """Return the exact coordinator attempt inherited by the generation task."""

    return _GENERATION_ATTEMPT_ID.get()


class GenerationTrigger(NamedTuple):
    """One trigger: the key that scopes it and the origin that asked for it."""

    key: dict[str, Any]
    origin: str


@dataclass(frozen=True)
class BackgroundGenerationResult:
    cards: list[dict[str, Any]]
    manifest: dict[str, Any]
    corpus_text_by_id: dict[str, str] | None = None
    failure_reason: str = ""


class BackgroundGenerationCoordinator:
    """One in-process task per complete generation trigger key."""

    def __init__(
        self,
        store: TopicStarterStore,
        *,
        deadline_seconds: float = BACKGROUND_GENERATION_DEADLINE_SECONDS,
        pool_writer: Callable[..., Any] = write_starter_pool,
        policy_id: str = POLICY_ID,
        terminal_external_audit_loader: TerminalExternalAuditLoader | None = None,
    ) -> None:
        self.store = store
        self.deadline_seconds = deadline_seconds
        self._pool_writer = pool_writer
        self._policy_id = policy_id
        self._terminal_external_audit_loader = terminal_external_audit_loader
        self._inflight: dict[str, asyncio.Task[str]] = {}

    @property
    def inflight_count(self) -> int:
        return len(self._inflight)

    @staticmethod
    def _token(trigger_key: dict[str, Any]) -> str:
        return json.dumps(trigger_key, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def _already_written(self, trigger_key: dict[str, Any]) -> bool:
        filters = (
            {"generation_trigger_key.context_scope": trigger_key["context_scope"]}
            if "context_scope" in trigger_key else {"generation_trigger_key": trigger_key}
        )
        rows = self.store.query_events(
            event_type="starter_slate_manifest",
            policy_id=self._policy_id,
            payload_equals=filters,
            limit=1,
            newest_first=True,
        )
        if not rows:
            return False
        payload = rows[0]["payload"]
        # A preset pool shares the trigger of the page it stands in for. It is not
        # the personalized pool that trigger asked for, so it does not close the run.
        if payload.get("cold_start") is True:
            return False
        return payload.get("generation_trigger_key") == trigger_key

    def terminal_external_audit(
        self,
        generation_attempt_id: str,
    ) -> dict[str, Any] | None:
        if self._terminal_external_audit_loader is None:
            return None
        audit = self._terminal_external_audit_loader(generation_attempt_id)
        return audit if isinstance(audit, dict) else None

    def record_generation_event(
        self,
        event_type: str,
        trigger_key: dict[str, Any],
        *,
        generation_attempt_id: str,
        reason: str = "",
        grounding_decision_receipt: dict[str, Any] | None = None,
        grounding_evidence_receipt: dict[str, Any] | None = None,
        external_invocation_audit: dict[str, Any] | None = None,
        rid_result: dict[str, Any] | None = None,
    ) -> None:
        event_id = uuid.uuid4().hex
        self.store.record_event(
            {
                "session_id": "topic-starter-background",
                "slate_id": event_id,
                "event_type": event_type,
                "topic_id": event_id,
                "policy_id": self._policy_id,
                "selection_mode": "background_generation",
                "propensity_reason": "not_estimated",
                "payload": {
                    "generation_trigger_key": trigger_key,
                    "generation_attempt_id": generation_attempt_id,
                    **({"reason": reason} if reason else {}),
                    **(
                        {"grounding_decision_receipt": grounding_decision_receipt}
                        if grounding_decision_receipt is not None
                        else {}
                    ),
                    **(
                        {"grounding_evidence_receipt": grounding_evidence_receipt}
                        if grounding_evidence_receipt is not None
                        else {}
                    ),
                    **(
                        {"external_invocation_audit": external_invocation_audit}
                        if external_invocation_audit is not None
                        else {}
                    ),
                    **({"rid_result": rid_result} if rid_result is not None else {}),
                },
                "created_at": time.time(),
            }
        )

    def start(
        self,
        trigger_key: dict[str, Any],
        generate: Callable[[], Awaitable[BackgroundGenerationResult]],
        *,
        ark_config: ArkBackgroundConfig | None = None,
        require_ark_config: bool = False,
        bind_pool_writer_model: bool = False,
        restock: bool = False,
    ) -> asyncio.Task[str]:
        """Start one generation; ``restock`` regenerates an already written trigger."""
        token = self._token(trigger_key)
        existing = self._inflight.get(token)
        if existing is not None:
            return existing
        task = asyncio.create_task(
            self._run(
                token,
                trigger_key,
                generate,
                ark_config=ark_config,
                require_ark_config=require_ark_config,
                bind_pool_writer_model=bind_pool_writer_model,
                restock=restock,
            )
        )
        self._inflight[token] = task
        return task

    async def _run(
        self,
        token: str,
        trigger_key: dict[str, Any],
        generate: Callable[[], Awaitable[BackgroundGenerationResult]],
        *,
        ark_config: ArkBackgroundConfig | None,
        require_ark_config: bool,
        bind_pool_writer_model: bool,
        restock: bool = False,
    ) -> str:
        generation_task: asyncio.Future[BackgroundGenerationResult] | None = None
        generation_attempt_id = current_background_generation_attempt_id() or f"background-attempt-{uuid.uuid4().hex}"
        try:
            if not restock and self._already_written(trigger_key):
                return "already_written"
            if require_ark_config and ark_config is None:
                self.record_generation_event(
                    "background_model_unavailable",
                    trigger_key,
                    generation_attempt_id=generation_attempt_id,
                )
                return "background_model_unavailable"

            self.record_generation_event(
                "background_generation_started",
                trigger_key,
                generation_attempt_id=generation_attempt_id,
            )
            context_token = _GENERATION_ATTEMPT_ID.set(generation_attempt_id)
            try:
                generation_task = asyncio.ensure_future(generate())
            finally:
                _GENERATION_ATTEMPT_ID.reset(context_token)
            done, _ = await asyncio.wait(
                {generation_task}, timeout=self.deadline_seconds
            )
            if generation_task not in done:
                generation_task.cancel()
                try:
                    await generation_task
                except asyncio.CancelledError:
                    pass
                except Exception:  # noqa: BLE001
                    pass
                self.record_generation_event(
                    "background_generation_deadline",
                    trigger_key,
                    generation_attempt_id=generation_attempt_id,
                    external_invocation_audit=self.terminal_external_audit(
                        generation_attempt_id
                    ),
                )
                return "background_generation_deadline"
            try:
                result = generation_task.result()
            except Exception:  # noqa: BLE001
                self.record_generation_event(
                    "background_generation_failed",
                    trigger_key,
                    generation_attempt_id=generation_attempt_id,
                    external_invocation_audit=self.terminal_external_audit(
                        generation_attempt_id
                    ),
                )
                return "background_generation_failed"
            grounding_decision_receipt = (
                result.manifest.get("grounding_decision_receipt")
                if isinstance(result.manifest, dict)
                and isinstance(
                    result.manifest.get("grounding_decision_receipt"), dict
                )
                else None
            )
            grounding_evidence_receipt = (
                result.manifest.get("grounding_evidence_receipt")
                if isinstance(result.manifest, dict)
                and isinstance(
                    result.manifest.get("grounding_evidence_receipt"), dict
                )
                else None
            )
            external_invocation_audit = (
                result.manifest.get("external_invocation_audit")
                if isinstance(result.manifest, dict)
                and isinstance(result.manifest.get("external_invocation_audit"), dict)
                else None
            )
            if external_invocation_audit is None:
                external_invocation_audit = self.terminal_external_audit(
                    generation_attempt_id
                )
            if result.failure_reason == "background_model_unavailable":
                self.record_generation_event(
                    "background_model_unavailable",
                    trigger_key,
                    generation_attempt_id=generation_attempt_id,
                    reason=result.failure_reason,
                    grounding_decision_receipt=grounding_decision_receipt,
                    grounding_evidence_receipt=grounding_evidence_receipt,
                    external_invocation_audit=external_invocation_audit,
                )
                return "background_model_unavailable"
            if (
                not isinstance(result.manifest, dict)
                or result.manifest.get("generation_trigger_key") != trigger_key
            ):
                self.record_generation_event(
                    "background_generation_rejected",
                    trigger_key,
                    generation_attempt_id=generation_attempt_id,
                    reason=result.failure_reason,
                    grounding_decision_receipt=grounding_decision_receipt,
                    grounding_evidence_receipt=grounding_evidence_receipt,
                    external_invocation_audit=external_invocation_audit,
                )
                return "background_generation_rejected"
            if bind_pool_writer_model and ark_config is None:
                self.record_generation_event(
                    "background_generation_rejected",
                    trigger_key,
                    generation_attempt_id=generation_attempt_id,
                    reason=result.failure_reason or "background_model_unavailable",
                    grounding_decision_receipt=grounding_decision_receipt,
                    grounding_evidence_receipt=grounding_evidence_receipt,
                    external_invocation_audit=external_invocation_audit,
                )
                return "background_generation_rejected"
            if result.failure_reason in {
                "rid_abstained", "quality_abstained", "no_eligible_history", "unread_cards_kept",
            }:
                self.record_generation_event(
                    "background_generation_abstained",
                    trigger_key,
                    generation_attempt_id=generation_attempt_id,
                    reason=result.failure_reason,
                    external_invocation_audit=external_invocation_audit,
                )
                return "abstained"
            writer_kwargs = {
                "created_at": time.time(),
                "corpus_text_by_id": result.corpus_text_by_id,
            }
            if bind_pool_writer_model:
                if ark_config is None:
                    raise ValueError("pool writer binding requires a background config")
                writer_kwargs["expected_model"] = (
                    ark_config.expected_model or ark_config.model
                )
            write_result = self._pool_writer(
                self.store,
                result.cards,
                result.manifest,
                **writer_kwargs,
            )
            if not write_result:
                writer_reason = str(getattr(write_result, "reason", "") or "")
                # With no pool written, what RID let through is recorded here.
                rid_result = result.manifest.get("rid_result")
                self.record_generation_event(
                    "background_generation_rejected",
                    trigger_key,
                    generation_attempt_id=generation_attempt_id,
                    reason=result.failure_reason or writer_reason,
                    grounding_decision_receipt=grounding_decision_receipt,
                    grounding_evidence_receipt=grounding_evidence_receipt,
                    external_invocation_audit=external_invocation_audit,
                    rid_result=rid_result if isinstance(rid_result, dict) else None,
                )
                return "background_generation_rejected"
            return "written"
        except asyncio.CancelledError:
            if generation_task is not None and not generation_task.done():
                generation_task.cancel()
                try:
                    await generation_task
                except asyncio.CancelledError:
                    pass
                except Exception:  # noqa: BLE001
                    pass
            self.record_generation_event(
                "background_generation_cancelled",
                trigger_key,
                generation_attempt_id=generation_attempt_id,
                external_invocation_audit=self.terminal_external_audit(
                    generation_attempt_id
                ),
            )
            raise
        finally:
            if generation_task is not None and not generation_task.done():
                generation_task.cancel()
                try:
                    await generation_task
                except asyncio.CancelledError:
                    pass
                except Exception:  # noqa: BLE001
                    pass
            current = asyncio.current_task()
            if self._inflight.get(token) is current:
                self._inflight.pop(token, None)

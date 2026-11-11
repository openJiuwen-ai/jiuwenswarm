# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Auditable in-context feedback projection for topic recommendation."""

from __future__ import annotations

import json
import time
from typing import Any

from jiuwenswarm.agents.harness.common.recommendation.topic_history import (
    empty_history_state,
    load_latest_history_state,
    save_history_state,
)
from jiuwenswarm.agents.harness.common.recommendation.topic_pool import (
    POLICY_ID,
    classify_starter_slate,
    find_exact_pool_card,
)
from jiuwenswarm.agents.harness.common.recommendation.topic_starter import (
    TopicStarterStore,
)


def _deduplicated(values: list[Any]) -> list[str]:
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def _exact_interaction_payload(
    card: dict[str, Any],
    *,
    manifest_id: str,
    generation_batch_id: str,
    request_id: str,
) -> dict[str, Any]:
    return {
        "manifest_id": manifest_id,
        "card_revision_id": card["card_revision_id"],
        "generation_batch_id": generation_batch_id,
        "request_id": request_id,
        "interest_summary_id": card.get("interest_summary_id"),
        "source_memory_unit_ids": list(card.get("source_memory_unit_ids") or []),
        "source_locators": list(card.get("source_locators") or []),
        "identity_anchor": str(card.get("identity_anchor") or ""),
        "context_anchors": list(card.get("context_anchors") or []),
        "intent": str(card.get("intent") or ""),
        "title": str(card.get("title") or ""),
        "prompt": str(card.get("prompt") or ""),
    }


def record_v5_interaction(
    store: TopicStarterStore,
    *,
    event_type: str,
    session_id: str,
    slate_id: str,
    manifest_id: str,
    card_revision_id: str,
    generation_batch_id: str,
    created_at: float,
    request_id: str = "",
    client_payload: dict[str, Any] | None = None,
) -> bool:
    """Record a click after an exact immutable-card join."""

    del client_payload  # Client-authored text is never an attribution source.
    if event_type != "topic_click":
        return False
    card = find_exact_pool_card(
        store,
        manifest_id=manifest_id,
        card_revision_id=card_revision_id,
        generation_batch_id=generation_batch_id,
    )
    if card is None:
        return False
    payload = _exact_interaction_payload(
        card,
        manifest_id=manifest_id,
        generation_batch_id=generation_batch_id,
        request_id=request_id,
    )
    event = {
        "session_id": session_id,
        "slate_id": slate_id,
        "event_type": event_type,
        "topic_id": card_revision_id,
        "policy_id": POLICY_ID,
        "selection_mode": "unknown",
        "propensity_reason": "not_estimated",
        "payload": payload,
        "created_at": created_at,
    }
    event_key = TopicStarterStore.event_key_of(event)
    _existing_items = store.query_events(
        event_type=event_type,
        slate_id=slate_id,
        policy_id=POLICY_ID,
        limit=8,
    )
    if any(
        existing.get("event_key") == event_key
        for existing in _existing_items
    ):
        return False
    return store.record_event(event)


def record_v5_exposure(
    store: TopicStarterStore,
    *,
    session_id: str,
    slate_id: str,
    served: dict[str, Any],
    created_at: float,
) -> bool:
    manifest_id = str(served.get("manifest_id") or "")
    batch_id = str(served.get("generation_batch_id") or "")
    cards = served.get("cards")
    if not manifest_id:
        return False
    if not batch_id:
        return False
    if not isinstance(cards, list):
        return False
    if not cards:
        return False
    exact_cards = []
    seen_revisions: set[str] = set()
    for shown in cards:
        if not isinstance(shown, dict):
            return False
        revision_id = str(shown.get("card_revision_id") or "")
        if not revision_id or revision_id in seen_revisions:
            return False
        exact = find_exact_pool_card(
            store,
            manifest_id=manifest_id,
            card_revision_id=revision_id,
            generation_batch_id=batch_id,
        )
        if exact is None:
            return False
        seen_revisions.add(revision_id)
        exact_cards.append(exact)
    selection = classify_starter_slate(exact_cards)
    return store.record_event(
        {
            "session_id": session_id,
            "slate_id": slate_id,
            "event_type": "dynamic_exposure",
            "topic_id": manifest_id,
            "policy_id": POLICY_ID,
            "selection_mode": selection["selection_mode"],
            "propensity_reason": "not_estimated",
            "payload": {
                "manifest_id": manifest_id,
                "generation_batch_id": batch_id,
                "topics": [
                    {
                        "id": card.get("interest_summary_id"),
                        "interest_summary_id": card.get("interest_summary_id"),
                        "card_revision_id": card.get("card_revision_id"),
                    }
                    for card in exact_cards
                ],
            },
            "created_at": created_at,
        }
    )


def record_v5_slate_event(
    store: TopicStarterStore,
    *,
    event_type: str,
    session_id: str,
    slate_id: str,
    manifest_id: str,
    generation_batch_id: str,
    created_at: float,
) -> bool:
    """Record a v5 slate-level signal using only server-owned semantics and IDs."""

    if event_type not in {"post_exposure_direct", "page_left", "session_bound"}:
        return False
    _event_items = store.query_events(
        event_type="starter_slate_manifest",
        slate_id=manifest_id,
        policy_id=POLICY_ID,
        limit=1,
    )
    manifest_exists = any(
        isinstance(event.get("payload"), dict)
        and event["payload"].get("manifest_id") == manifest_id
        and event["payload"].get("generation_batch_id") == generation_batch_id
        for event in _event_items
    )
    if not manifest_exists:
        return False
    return store.record_event(
        {
            "session_id": session_id,
            "slate_id": slate_id,
            "event_type": event_type,
            "topic_id": None,
            "policy_id": POLICY_ID,
            "selection_mode": "unknown",
            "propensity_reason": "not_estimated",
            "payload": {
                "manifest_id": manifest_id,
                "generation_batch_id": generation_batch_id,
            },
            "created_at": created_at,
        }
    )


def build_feedback_context(
    store: TopicStarterStore,
    interest_summary_ids: list[str | None],
) -> dict[str, Any]:
    """Project prompt evidence: counts plus explicit exemplars, never scores."""

    identifiers = _deduplicated(list(interest_summary_ids))
    counts = {
        identifier: {"exposure_days": 0, "clicks": 0}
        for identifier in identifiers
    }
    exposure_days: dict[str, set[str]] = {identifier: set() for identifier in identifiers}
    positive: list[dict[str, Any]] = []
    slate_signals: list[dict[str, str]] = []
    events = []
    for event_type in (
        "dynamic_exposure",
        "topic_click",
        "post_exposure_direct",
    ):
        events.extend(
            store.query_events(
                event_type=event_type,
                policy_id=POLICY_ID,
                limit=256,
                newest_first=True,
            )
        )
    for event in events:
        event_type = str(event.get("event_type") or "")
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        if event_type == "dynamic_exposure":
            local_day = time.strftime(
                "%Y-%m-%d", time.localtime(float(event.get("created_at") or 0.0))
            )
            for topic in payload.get("topics") or []:
                if not isinstance(topic, dict):
                    continue
                identifier = str(
                    topic.get("interest_summary_id") or topic.get("id") or ""
                )
                if identifier in exposure_days:
                    exposure_days[identifier].add(local_day)
            continue
        if event_type == "topic_click":
            identifier = str(payload.get("interest_summary_id") or "")
            exemplar = {
                "interest_summary_id": identifier or None,
                "card_revision_id": payload.get("card_revision_id"),
                "title": payload.get("title"),
                "prompt": payload.get("prompt"),
                "intent": payload.get("intent"),
                "observed_at": float(event.get("created_at") or 0.0),
            }
            if identifier in counts:
                counts[identifier]["clicks"] += 1
            positive.append(exemplar)
            continue
        if event_type == "post_exposure_direct":
            slate_signals.append(
                {
                    "event_type": "post_exposure_direct",
                    "slate_id": str(event.get("slate_id") or ""),
                }
            )
    for identifier, days in exposure_days.items():
        counts[identifier]["exposure_days"] = len(days)
    return {
        "feedback_counts": counts,
        "positive_exemplars": positive,
        "negative_exemplars": [],
        "slate_signals": slate_signals,
    }

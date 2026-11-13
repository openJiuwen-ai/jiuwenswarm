# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Short-delta successor write must accept a cold-start first-screen predecessor."""

from __future__ import annotations

from datetime import datetime, timezone

from jiuwenswarm.agents.harness.common.recommendation.topic_product import (
    GLOBAL_NEW_CHAT_SCOPE,
    INPUT_REVISION_RECEIPT_VERSION,
    TERMINAL_COMPONENT_IDS,
    _short_delta_successor_rejection_reason,
    build_cold_start_cards,
    build_generation_trigger_key,
    load_exact_global_pool,
    load_readable_global_pool,
    write_cold_start_pool,
)
from jiuwenswarm.agents.harness.common.recommendation.topic_starter import (
    TopicStarterStore,
)
from jiuwenswarm.agents.harness.common.recommendation.topic_terminal import (
    TERMINAL_CONTRACT_VERSION,
)


def _trigger_key(*, work_mode: str = "work") -> dict:
    receipt = {
        "receipt_version": INPUT_REVISION_RECEIPT_VERSION,
        "context_scope": GLOBAL_NEW_CHAT_SCOPE,
        "revision_digest": "digest",
    }
    return build_generation_trigger_key(
        local_date="2026-11-11",
        context_scope=GLOBAL_NEW_CHAT_SCOPE,
        project_id=None,
        work_mode=work_mode,
        profile_semantic_version=0,
        short_intent_version=0,
        negative_preference_version=0,
        component_contract_versions={
            component_id: TERMINAL_CONTRACT_VERSION
            for component_id in TERMINAL_COMPONENT_IDS
        },
        model="test-model",
        generation_config_version="cfg",
        corpus_version="topic-starter-corpus-v1",
        document_snapshot_version="documents:2026-11-11:v2",
        retrieval_config_version="retrieval",
        evidence_extractor_version="evidence",
        search_provider_id="test",
        input_revision_receipt=receipt,
    )


def _write_cold_start(
    store: TopicStarterStore,
    *,
    work_mode: str = "work",
    created_at: float | None = None,
) -> str:
    trigger = _trigger_key(work_mode=work_mode)
    now = created_at
    if now is None:
        now = datetime(2026, 11, 11, tzinfo=timezone.utc).timestamp()
    cards = build_cold_start_cards(trigger_key=trigger, now=now)
    assert cards
    result = write_cold_start_pool(
        store,
        cards,
        trigger_key=trigger,
        created_at=now,
    )
    assert result.written, result.reason
    rows = store.query_events(
        event_type="starter_slate_manifest",
        limit=32,
        newest_first=True,
    )
    for row in rows:
        manifest = row["payload"]
        if (
            isinstance(manifest, dict)
            and manifest.get("work_mode") == work_mode
            and manifest.get("generation_batch_id") == cards[0]["generation_batch_id"]
        ):
            return str(manifest["manifest_id"])
    raise AssertionError(f"cold-start manifest missing for work_mode={work_mode}")


def test_readable_global_pool_loads_cold_start_that_exact_rejects(tmp_path) -> None:
    store = TopicStarterStore(tmp_path / "recommendation.db")
    manifest_id = _write_cold_start(store)

    assert load_exact_global_pool(store, manifest_id=manifest_id) is None
    loaded = load_readable_global_pool(store, manifest_id=manifest_id)
    assert loaded is not None
    manifest, cards = loaded
    assert manifest.get("cold_start") is True
    assert len(cards) >= 1


def test_short_delta_successor_accepts_cold_start_predecessor(tmp_path) -> None:
    store = TopicStarterStore(tmp_path / "recommendation.db")
    predecessor_id = _write_cold_start(store)
    interest_id = "summary:web_marathon:delta"

    successor_manifest = {
        "context_scope": GLOBAL_NEW_CHAT_SCOPE,
        "supersedes_manifest_id": predecessor_id,
        "rid_interest_selection": {
            "short_trigger_delta_event_ids": ["evt-marathon"],
            "delta_linked_interest_summary_id": interest_id,
        },
        "rid_result": {
            "trigger_interests": [
                {
                    "interest_summary_id": interest_id,
                    "source_event_ids": ["evt-marathon"],
                    "source_locators": [{"event_id": "evt-marathon"}],
                }
            ]
        },
    }
    successor_cards = [
        {
            "selector_rank": 1,
            "card_revision_id": "rev-1",
            "category": "profile_interest_continuation",
            "title": "杭州马拉松候补递补一般要等多久？",
            "prompt": "结合报名已截止的情况，说明候补递补的时间节点和常见规则。",
            "interest_summary_id": interest_id,
        }
    ]

    assert (
        _short_delta_successor_rejection_reason(
            store,
            successor_manifest,
            successor_cards,
        )
        is None
    )


def test_latest_scope_manifest_id_respects_work_mode(tmp_path) -> None:
    from jiuwenswarm.agents.harness.common.recommendation.topic_pipeline import (
        TopicPipeline,
    )

    store = TopicStarterStore(tmp_path / "recommendation.db")
    base = datetime(2026, 11, 11, tzinfo=timezone.utc).timestamp()
    work_id = _write_cold_start(store, work_mode="work", created_at=base)
    design_id = _write_cold_start(store, work_mode="design", created_at=base + 10)

    class _Host:
        pass

    host = _Host()
    host.store = store
    assert (
        TopicPipeline._latest_scope_manifest_id(
            host,
            GLOBAL_NEW_CHAT_SCOPE,
            work_mode="work",
        )
        == work_id
    )
    assert (
        TopicPipeline._latest_scope_manifest_id(
            host,
            GLOBAL_NEW_CHAT_SCOPE,
            work_mode="design",
        )
        == design_id
    )


def test_short_delta_still_rejects_missing_predecessor(tmp_path) -> None:
    store = TopicStarterStore(tmp_path / "recommendation.db")
    interest_id = "summary:missing:delta"
    successor_manifest = {
        "context_scope": GLOBAL_NEW_CHAT_SCOPE,
        "supersedes_manifest_id": "v6-manifest-does-not-exist",
        "rid_interest_selection": {
            "short_trigger_delta_event_ids": ["evt-1"],
            "delta_linked_interest_summary_id": interest_id,
        },
        "rid_result": {
            "trigger_interests": [
                {
                    "interest_summary_id": interest_id,
                    "source_event_ids": ["evt-1"],
                    "source_locators": [{"event_id": "evt-1"}],
                }
            ]
        },
    }
    successor_cards = [
        {
            "selector_rank": 1,
            "card_revision_id": "rev-1",
            "category": "profile_interest_continuation",
            "title": "新题",
            "prompt": "新题说明",
            "interest_summary_id": interest_id,
        }
    ]
    assert (
        _short_delta_successor_rejection_reason(
            store,
            successor_manifest,
            successor_cards,
        )
        == "manifest_short_delta_predecessor_exact_load_failed"
    )

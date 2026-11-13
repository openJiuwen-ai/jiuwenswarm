# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Bounded summarizer input: turn/character clipping and context-sized batches."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from jiuwenswarm.agents.harness.common.recommendation import (
    topic_pipeline as pipeline,
)
from jiuwenswarm.agents.harness.common.recommendation.topic_model import (
    ArkBackgroundConfig,
)
from jiuwenswarm.server.runtime.session.session_history import (
    CompletedSessionPrefix,
    SessionCompletionBoundary,
)


class _CharCounter:
    """Estimates one token per character; enough to drive the packing loop."""

    def estimate_stage_tokens(
        self,
        system_prompt: str,
        user_payload: str,
        reserved_output: int,
    ) -> int:
        return len(user_payload) + int(reserved_output)


def _message(
    session_id: str,
    ordinal: int,
    content: str,
    answer: str | None = None,
) -> pipeline.V6SourceMessage:
    return pipeline.V6SourceMessage(
        event_id=f"{session_id}:{ordinal}",
        session_id=session_id,
        ordinal=ordinal,
        content=content,
        observed_at=float(ordinal),
        input_origin="manual_free_text",
        assistance_origin="none",
        intent_cluster="tech-ai-ml",
        action_type="natural_question",
        project_id=None,
        request_id=f"request-{session_id}",
        assistant_answer=answer,
    )


def _snapshot(messages: tuple[pipeline.V6SourceMessage, ...]) -> SimpleNamespace:
    sessions = sorted({message.session_id for message in messages})
    prefixes = tuple(
        CompletedSessionPrefix(
            boundary=SessionCompletionBoundary(
                session_id=session_id,
                event_id=f"{session_id}:completed",
                request_id=f"request-{session_id}",
                ordinal=10_000,
                completed_at=1_000.0,
                status="success",
            ),
            completed_request_ids=(f"request-{session_id}",),
            completed_user_ordinals=(0,),
        )
        for session_id in sessions
    )
    return SimpleNamespace(
        messages=messages,
        completed_session_prefixes=prefixes,
        input_snapshot_id="snapshot-1",
        context_scope="global_new_chat",
        delta_event_ids=(),
    )


def test_bounded_turns_keep_the_newest_under_turn_and_char_budgets() -> None:
    messages = [_message("s", ordinal, "用" * 3_000, "答" * 3_000) for ordinal in range(30)]

    bounded = pipeline._bounded_summary_turns(messages)

    assert 1 <= len(bounded) <= pipeline._SUMMARY_MAX_TURNS_PER_SESSION
    assert bounded[-1][0].ordinal == 29
    assert all(
        len(content) <= pipeline._SUMMARY_MAX_USER_CHARS
        and len(answer or "") <= pipeline._SUMMARY_MAX_ANSWER_CHARS
        for _, content, answer in bounded
    )
    assert (
        sum(len(content) + len(answer or "") for _, content, answer in bounded)
        <= pipeline._SUMMARY_MAX_SESSION_CHARS
    )


def test_identity_and_model_payload_cover_the_same_bounded_turns() -> None:
    messages = [_message("s", ordinal, "用" * 500) for ordinal in range(30)]

    identity = pipeline._session_identity_payload("s", messages)
    model = pipeline._session_model_payload("s", messages)
    covered = pipeline._summary_coverage_ids(messages)

    assert len(identity["messages"]) == len(model["messages"])
    assert [item["event_id"] for item in identity["messages"]] == [
        item["event_id"] for item in model["messages"]
    ]
    assert covered == {item["event_id"] for item in model["messages"]}
    assert len(covered) < len(messages)
    assert len(covered) <= pipeline._SUMMARY_MAX_TURNS_PER_SESSION
    assert model["messages"][-1]["ordinal"] == 29
    assert model["source_turn_count"] == 30
    assert len(model["interaction_pattern"]) == len(model["messages"])
    assert not pipeline._summary_lags(
        {"completed_at": 1_000.0, "source_event_ids": sorted(covered)},
        1_000.0,
        covered,
    )
    assert pipeline._summary_lags(
        {"completed_at": 1_000.0, "source_event_ids": sorted(covered)},
        1_000.0,
        {message.event_id for message in messages},
    )


def test_fit_summary_session_drops_the_oldest_turns_to_meet_the_limit() -> None:
    payload = pipeline._session_model_payload(
        "s",
        [_message("s", ordinal, "用" * 400) for ordinal in range(4)],
    )
    snapshot = _snapshot(tuple())
    full_limit = pipeline._summary_batch_estimate(
        _CharCounter(),
        snapshot,
        [payload],
        reserved_output=(
            pipeline._SUMMARY_BASE_RESERVED_OUTPUT_TOKENS
            + pipeline._SUMMARY_RESERVED_OUTPUT_TOKENS
        ),
    )
    single_limit = pipeline._summary_batch_estimate(
        _CharCounter(),
        snapshot,
        [{**payload, "messages": payload["messages"][-1:]}],
        reserved_output=(
            pipeline._SUMMARY_BASE_RESERVED_OUTPUT_TOKENS
            + pipeline._SUMMARY_RESERVED_OUTPUT_TOKENS
        ),
    )

    fitted = pipeline._fit_summary_session(
        payload,
        counter=_CharCounter(),
        snapshot=snapshot,
        limit=(single_limit + full_limit) // 2,
    )

    assert fitted is not None
    assert 1 <= len(fitted["messages"]) < len(payload["messages"])
    assert fitted["messages"][-1]["ordinal"] == 3
    assert pipeline._fit_summary_session(
        payload,
        counter=_CharCounter(),
        snapshot=snapshot,
        limit=1,
    ) is None


def test_plan_summary_batches_covers_every_session_once() -> None:
    snapshot = _snapshot(tuple())
    payloads = [
        pipeline._session_model_payload(
            f"s{index}", [_message(f"s{index}", 0, "用" * 300)]
        )
        for index in range(5)
    ]
    two = pipeline._summary_batch_estimate(
        _CharCounter(),
        snapshot,
        payloads[:2],
        reserved_output=(
            pipeline._SUMMARY_BASE_RESERVED_OUTPUT_TOKENS
            + pipeline._SUMMARY_RESERVED_OUTPUT_TOKENS * 2
        ),
    )

    batches = pipeline._plan_summary_batches(
        payloads,
        counter=_CharCounter(),
        snapshot=snapshot,
        limit=two,
    )

    assert batches is not None
    assert [index for batch in batches for index in batch] == list(range(5))
    assert all(len(batch) <= 2 for batch in batches)
    assert pipeline._plan_summary_batches(
        [payloads[0]],
        counter=_CharCounter(),
        snapshot=snapshot,
        limit=1,
    ) is None


def _prefix(session_id: str, completed_at: float) -> Any:
    return SimpleNamespace(
        boundary=SessionCompletionBoundary(
            session_id=session_id,
            event_id=f"{session_id}:completed",
            request_id=f"request-{session_id}",
            ordinal=10_000,
            completed_at=completed_at,
            status="success",
        )
    )


def test_recent_summary_sessions_keeps_the_window_and_the_delta_sessions() -> None:
    now = 2_000_000_000.0
    old_in_delta = _message("old", 0, "旧会话但本轮有 delta")
    stale = _message("stale", 0, "很久以前")
    recent = _message("new", 0, "刚完成")
    prefixes = {
        "old": _prefix(
            "old", now - pipeline._SUMMARY_SESSION_WINDOW_SECONDS - 1
        ),
        "stale": _prefix(
            "stale", now - pipeline._SUMMARY_SESSION_WINDOW_SECONDS - 1
        ),
        "new": _prefix("new", now - 10.0),
    }

    selected = pipeline._recent_summary_sessions(
        {"old": [old_in_delta], "stale": [stale], "new": [recent]},
        prefixes,
        delta_event_ids=(old_in_delta.event_id,),
        now=now,
    )

    assert selected == ["new", "old"]


def test_recent_rid_summaries_cap_and_keep_required() -> None:
    summaries = [
        SimpleNamespace(
            session_summary_id=f"summary-{index:03d}",
            completed_at=float(index),
        )
        for index in range(pipeline._RID_SESSION_SUMMARY_LIMIT + 10)
    ]

    capped = pipeline._recent_rid_summaries(
        summaries,
        required_ids=("summary-000",),
    )

    assert len(capped) == pipeline._RID_SESSION_SUMMARY_LIMIT
    assert "summary-000" in {item.session_summary_id for item in capped}
    assert capped[1].session_summary_id == "summary-039"  # the rest comes newest-first
    assert capped[-1].session_summary_id == "summary-011"


def test_store_prune_keeps_newest_counts_and_drops_training(tmp_path) -> None:
    from jiuwenswarm.agents.harness.common.recommendation.topic_starter import (
        TopicStarterStore,
    )

    store = TopicStarterStore(tmp_path / "recommendation.db")
    now = 10_000_000.0
    day = 24 * 60 * 60

    def event(event_type: str, topic_id: str, created_at: float, payload=None) -> None:
        store.record_event({
            "session_id": "s",
            "slate_id": "slate",
            "event_type": event_type,
            "topic_id": topic_id,
            "policy_id": "test",
            "selection_mode": "test",
            "propensity_reason": "test",
            "payload": payload or {},
            "created_at": created_at,
        })

    event("dynamic_exposure", "old-exposure", now - 31 * day)
    event("dynamic_exposure", "new-exposure", now - day)
    event("pa_training_example", "drop-me", now - day)
    event("training_exclusion", "drop-me-too", now - day)
    event("topic_click", "opened", now - 400 * day)
    event("session_bound", "bound", now - 400 * day)
    event("not_interested", "dismissed", now - 400 * day)
    event("session_summary", "old-summary", now - 400 * day, {"session_id": "session-old"})
    for revision, offset in enumerate((1, 2, 3)):
        event(
            "session_summary",
            f"summary-{revision}",
            now - offset * day,
            {"session_id": "session-a"},
        )
    for index in range(45):
        event("provider_invocation", f"call-{index}", now - index)

    removed = store.prune(now=now)

    remaining = {(row["event_type"], row["topic_id"]) for row in store.list_events()}
    assert removed.get("pa_training_example") == 1
    assert removed.get("training_exclusion") == 1
    assert removed.get("provider_invocation") == 5
    assert removed.get("session_summary_superseded") == 1
    assert ("pa_training_example", "drop-me") not in remaining
    assert ("training_exclusion", "drop-me-too") not in remaining
    assert ("dynamic_exposure", "new-exposure") in remaining
    assert ("dynamic_exposure", "old-exposure") in remaining
    assert ("session_summary", "summary-2") not in remaining
    assert ("session_summary", "summary-0") in remaining
    assert ("session_summary", "summary-1") in remaining
    assert ("topic_click", "opened") in remaining
    assert ("session_bound", "bound") in remaining
    assert ("not_interested", "dismissed") in remaining
    assert ("session_summary", "old-summary") in remaining
    assert len([
        row for row in store.list_events() if row["event_type"] == "provider_invocation"
    ]) == 40
    # The interval marker skips a second pass within an hour.
    assert store.prune(now=now) == {}


@pytest.mark.asyncio
async def test_summarize_sessions_splits_a_long_first_preparation() -> None:
    messages = tuple(
        _message(f"s{index}", ordinal, "用" * 400, "答" * 400)
        for index in range(4)
        for ordinal in range(2)
    )
    snapshot = _snapshot(messages)
    calls: list[dict[str, Any]] = []
    counter = pipeline.history.StageTokenCounter("gpt-4o")
    payloads = [
        pipeline._session_model_payload(
            f"s{index}",
            [message for message in messages if message.session_id == f"s{index}"],
        )
        for index in range(4)
    ]
    one = pipeline._summary_batch_estimate(
        counter,
        snapshot,
        payloads[:1],
        reserved_output=(
            pipeline._SUMMARY_BASE_RESERVED_OUTPUT_TOKENS
            + pipeline._SUMMARY_RESERVED_OUTPUT_TOKENS
        ),
    )
    two = pipeline._summary_batch_estimate(
        counter,
        snapshot,
        payloads[:2],
        reserved_output=(
            pipeline._SUMMARY_BASE_RESERVED_OUTPUT_TOKENS
            + pipeline._SUMMARY_RESERVED_OUTPUT_TOKENS * 2
        ),
    )
    assert two > one

    async def invoke_stage(
        stage: str, system_prompt: str, payload: str
    ) -> dict[str, Any]:
        import json

        assert stage == "v6_session_summarizer"
        parsed = json.loads(payload)
        assert parsed["summary_input_policy"] == pipeline._SUMMARY_INPUT_POLICY_VERSION
        calls.append(parsed)
        return {
            "summaries": [
                {"session_id": session["session_id"], "summary": "摘要"}
                for session in parsed["sessions"]
            ]
        }

    class _Store:
        def query_events(self, **kwargs: Any) -> list[Any]:
            return []

    topic = object.__new__(pipeline.TopicPipeline)
    topic.store = _Store()
    topic._now = lambda: 1_010.0
    config = ArkBackgroundConfig(
        api_key="key",
        model="gpt-4o",
        base_url="https://example.invalid",
        context_window_tokens=one + (two - one) // 2,
    )

    batch = await topic._summarize_sessions(
        snapshot,
        config=config,
        invoke_stage=invoke_stage,
    )

    assert len(calls) > 1
    assert len({session["session_id"] for call in calls for session in call["sessions"]}) == 4
    assert {artifact.session_id for artifact in batch.artifacts} == {
        f"s{index}" for index in range(4)
    }
    assert all(len(artifact.source_event_ids) == 2 for artifact in batch.artifacts)


def test_fit_rid_inputs_drops_oldest_optional_summaries_first() -> None:
    class _Summary:
        def __init__(self, sid: str, completed_at: float, body: str) -> None:
            self.session_summary_id = sid
            self.completed_at = completed_at
            self._body = body

        def as_payload(self) -> dict[str, Any]:
            return {
                "session_summary_id": self.session_summary_id,
                "completed_at": self.completed_at,
                "body": self._body,
            }

    class _Interest:
        def __init__(self, sid: str, version: int, body: str) -> None:
            self.interest_summary_id = sid
            self.profile_semantic_version = version
            self._body = body

        def as_payload(self) -> dict[str, Any]:
            return {
                "interest_summary_id": self.interest_summary_id,
                "profile_semantic_version": self.profile_semantic_version,
                "body": self._body,
            }

    rid_inputs = {
        "binding": {"component": "rid"},
        "input_snapshot_id": "snap",
        "user_profile": {"name": "u"},
        "long_term_profile_interests": (
            _Interest("p-old", 1, "interest-body-" * 80),
            _Interest("p-new", 2, "interest-body-" * 80),
        ),
        "session_summaries": (
            _Summary("s-old", 1.0, "summary-body-" * 200),
            _Summary("s-keep", 2.0, "summary-body-" * 200),
            _Summary("s-new", 3.0, "summary-body-" * 200),
        ),
        "activity_group": "active",
        "threshold_version": "t",
        "feedback_context": {},
        "delta": [],
        "decision_context": {},
    }
    counter = _CharCounter()
    full_tokens = counter.estimate_stage_tokens(
        pipeline._RID_API_SYSTEM_PROMPT,
        pipeline._rid_request_estimate_payload(rid_inputs),
        pipeline._RID_RESERVED_OUTPUT_TOKENS,
    )
    # Force at least one optional summary drop while keeping the required one.
    ok = pipeline._fit_rid_inputs_to_budget(
        rid_inputs,
        counter=counter,
        limit=max(full_tokens - 50, 1),
        required_summary_ids={"s-keep"},
    )
    assert ok is True
    kept = [item.session_summary_id for item in rid_inputs["session_summaries"]]
    assert "s-keep" in kept
    assert "s-old" not in kept
    assert len(kept) < 3


def test_fit_recent_finalizer_drops_trailing_documents_then_candidates() -> None:
    counter = _CharCounter()
    candidates = [
        {
            "candidate_id": "c1",
            "interest": "a",
            "draft_title": "t1",
            "intent": "i",
            "primary_query": "q",
            "documents": [
                {"id": "d1", "text": "doc-text-" * 300},
                {"id": "d2", "text": "doc-text-" * 300},
            ],
        },
        {
            "candidate_id": "c2",
            "interest": "b",
            "draft_title": "t2",
            "intent": "i",
            "primary_query": "q",
            "documents": [{"id": "d3", "text": "doc-text-" * 300}],
        },
    ]
    full = counter.estimate_stage_tokens(
        pipeline._RECENT_FINALIZER_PROMPT,
        pipeline._recent_finalizer_estimate_payload(
            input_snapshot_id="snap",
            discussion_coverage=[],
            previously_offered=[],
            candidates=candidates,
        ),
        10,
    )
    fitted = pipeline._fit_recent_finalizer_candidates(
        candidates,
        counter=counter,
        limit=max(full - 40, 1),
        input_snapshot_id="snap",
        discussion_coverage=[],
        previously_offered=[],
        reserved_output=10,
    )
    assert fitted is not None
    assert len(fitted) >= 1
    original_docs = sum(len(item["documents"]) for item in candidates)
    fitted_docs = sum(len(item["documents"]) for item in fitted)
    assert fitted_docs < original_docs or len(fitted) < len(candidates)


def test_session_summary_content_digest_ignores_summarizer_config() -> None:
    messages = (_message("s1", 0, "hello", "world"),)
    session_payload = pipeline._session_identity_payload("s1", messages)
    boundary = SessionCompletionBoundary(
        session_id="s1",
        event_id="s1:completed",
        request_id="request-s1",
        ordinal=10_000,
        completed_at=1_000.0,
        status="success",
    )
    locators = (
        pipeline.SessionSourceLocator(session_id="s1", ordinal=0, event_id="s1:0"),
    )
    identity = pipeline._session_summary_content_identity(
        context_scope="global_new_chat",
        session_payload=session_payload,
        source_event_ids=("s1:0",),
        source_locators=locators,
        completion_boundary=boundary,
    )
    digest = pipeline._session_summary_content_digest(identity)
    # Model/config are intentionally absent from the content identity.
    assert "summary_config_version" not in identity
    assert "summary_provider_version" not in identity
    assert digest == pipeline._session_summary_content_digest(identity)


@pytest.mark.asyncio
async def test_summarize_sessions_reuses_content_matched_summary_after_model_change() -> None:
    messages = (_message("s1", 0, "qing emperors", "twelve"),)
    snapshot = _snapshot(messages)
    first_calls: list[str] = []

    async def first_invoke(stage: str, system_prompt: str, payload: str) -> dict[str, Any]:
        import json
        first_calls.append(stage)
        parsed = json.loads(payload)
        return {
            "summaries": [
                {"session_id": session["session_id"], "summary": "qing summary"}
                for session in parsed["sessions"]
            ]
        }

    class _EmptyStore:
        def query_events(self, **kwargs: Any) -> list[Any]:
            return []

    topic = object.__new__(pipeline.TopicPipeline)
    topic.store = _EmptyStore()
    topic._now = lambda: 1_010.0
    config_a = ArkBackgroundConfig(
        api_key="key",
        model="model-a",
        base_url="https://a.example",
        context_window_tokens=128_000,
    )
    batch_a = await topic._summarize_sessions(
        snapshot, config=config_a, invoke_stage=first_invoke,
    )
    assert first_calls == ["v6_session_summarizer"]
    assert len(batch_a.artifacts) == 1
    stored = batch_a.artifacts[0].as_payload()

    second_calls: list[str] = []

    async def second_invoke(stage: str, system_prompt: str, payload: str) -> dict[str, Any]:
        second_calls.append(stage)
        raise AssertionError("content-matched summary must not be rewritten")

    class _ReuseStore:
        def query_events(self, **kwargs: Any) -> list[Any]:
            topic_id = kwargs.get("topic_id")
            if topic_id == stored["session_summary_id"]:
                return [{"payload": stored, "created_at": 1.0}]
            if kwargs.get("event_type") == "session_summary":
                # Latest-summary scan for legacy digests.
                return [{"payload": stored, "created_at": 1.0}]
            return []

    topic.store = _ReuseStore()
    config_b = ArkBackgroundConfig(
        api_key="key",
        model="model-b",
        base_url="https://b.example",
        context_window_tokens=262_144,
    )
    batch_b = await topic._summarize_sessions(
        snapshot, config=config_b, invoke_stage=second_invoke,
    )
    assert second_calls == []
    assert batch_b.generated_summary_ids == ()
    assert batch_b.artifacts[0].summary_text == "qing summary"
    assert batch_b.artifacts[0].session_summary_id == stored["session_summary_id"]
    # Pool write must accept reused provenance under the new run's coverage stamp.
    from jiuwenswarm.agents.harness.common.recommendation.topic_product import (
        _valid_session_summary_coverage_receipt,
        _valid_session_summary_materialization_receipt,
    )

    reused = batch_b.artifacts[0]
    assert reused.summary_config_version != batch_b.summary_config_version
    materialization = pipeline._session_summary_materialization_receipt(
        snapshot=snapshot,
        summaries=batch_b.artifacts,
        generated_summary_ids=batch_b.generated_summary_ids,
        summary_config_version=batch_b.summary_config_version,
    )
    manifest = {
        "generation_input_snapshot_id": snapshot.input_snapshot_id,
        "context_scope": snapshot.context_scope,
        "session_summary_artifacts": [summary.as_payload() for summary in batch_b.artifacts],
        "session_summary_coverage_receipt": dict(batch_b.coverage_receipt),
        "session_summary_materialization_receipt": materialization,
    }
    assert _valid_session_summary_coverage_receipt(manifest) is True
    assert _valid_session_summary_materialization_receipt(manifest) is True

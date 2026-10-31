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


def test_identity_payload_keeps_every_turn_while_the_model_payload_is_bounded() -> None:
    messages = [_message("s", ordinal, "用" * 500) for ordinal in range(30)]

    identity = pipeline._session_identity_payload("s", messages)
    model = pipeline._session_model_payload("s", messages)

    assert len(identity["messages"]) == 30
    assert len(model["messages"]) <= pipeline._SUMMARY_MAX_TURNS_PER_SESSION
    assert model["messages"][-1]["ordinal"] == 29
    assert len(model["interaction_pattern"]) == len(model["messages"])


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


def test_store_prune_drops_aged_types_and_superseded_summaries(tmp_path) -> None:
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
    event("pa_training_example", "kept", now - 365 * day)
    for revision, offset in enumerate((1, 2, 3)):
        event(
            "session_summary",
            f"summary-{revision}",
            now - offset * day,
            {"session_id": "session-a"},
        )

    removed = store.prune(now=now)

    remaining = {(row["event_type"], row["topic_id"]) for row in store.list_events()}
    assert removed.get("dynamic_exposure") == 1
    assert removed.get("session_summary_superseded") == 1
    assert ("pa_training_example", "kept") in remaining
    assert ("dynamic_exposure", "new-exposure") in remaining
    assert ("session_summary", "summary-2") not in remaining
    assert ("session_summary", "summary-0") in remaining
    assert ("session_summary", "summary-1") in remaining
    # The interval marker skips a second pass on the same day.
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

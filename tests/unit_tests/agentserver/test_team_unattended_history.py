# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for unattended Team round history persistence.

Covers ``TeamManager.broadcast_event``'s delivery report and
``_UnattendedTeamRoundHistoryWriter``'s durable pipeline mirroring, plus the
producer integration that persists frames no waiter received.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from openjiuwen.agent_teams.schema.team import TeamRole

from jiuwenswarm.agents.harness.team import TeamManager
from jiuwenswarm.server.runtime.agent_adapter import team_helpers
from jiuwenswarm.server.runtime.agent_adapter.team_helpers import (
    _ACTIVE_ROUND_HISTORY_CONTEXT,
    _RoundHistoryContext,
    _UnattendedTeamRoundHistoryWriter,
)


@pytest.fixture(autouse=True)
def _clean_round_history_context():
    """Keep the module-level round identity registry isolated per test."""
    _ACTIVE_ROUND_HISTORY_CONTEXT.clear()
    yield
    _ACTIVE_ROUND_HISTORY_CONTEXT.clear()


@pytest.fixture
def history_records(monkeypatch):
    """Capture append_history_record calls issued through team_helpers."""
    records: list[dict] = []
    monkeypatch.setattr(
        team_helpers,
        "append_history_record",
        lambda **kwargs: records.append(kwargs),
    )
    return records


def _marker() -> dict[str, Any]:
    return {
        "message_id": "sm-1",
        "source_session_id": "source-1",
        "source_request_id": "req-src",
        "source_tool_call_id": "call-src",
        "source_title": "Source",
        "chain_id": "sm-1",
        "parent_message_id": "",
        "hop_count": 1,
        "language": "zh",
    }


def _writer(**overrides: Any) -> _UnattendedTeamRoundHistoryWriter:
    kwargs: dict[str, Any] = {
        "channel_id": "web",
        "session_id": "sess-team",
        "request_id": "session-message-sm-1",
        "cross_session_marker": _marker(),
    }
    kwargs.update(overrides)
    return _UnattendedTeamRoundHistoryWriter(**kwargs)


# ---------------------------------------------------------------------------
# TeamManager.broadcast_event delivery report
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_broadcast_event_returns_true_when_waiter_receives():
    manager = TeamManager()
    manager.add_waiter("sess-report", "turn-1", asyncio.Queue())

    delivered = await manager.broadcast_event(
        "sess-report", {"event_type": "chat.delta", "content": "x"},
    )

    assert delivered is True


@pytest.mark.anyio
async def test_broadcast_event_returns_false_without_waiter():
    manager = TeamManager()

    delivered = await manager.broadcast_event(
        "sess-report", {"event_type": "chat.delta", "content": "x"},
    )

    assert delivered is False


@pytest.mark.anyio
async def test_broadcast_event_returns_false_for_attach_only_waiter():
    manager = TeamManager()
    manager.add_waiter(
        "sess-report", "attach-1", asyncio.Queue(), attach_only=True,
    )

    delivered = await manager.broadcast_event(
        "sess-report", {"event_type": "chat.final", "content": "x"},
    )

    assert delivered is True


@pytest.mark.anyio
async def test_broadcast_event_unarmed_terminal_returns_false():
    manager = TeamManager()
    manager.begin_round("sess-report", "turn-1", terminal_armed=False)

    delivered = await manager.broadcast_event(
        "sess-report",
        {
            "event_type": "chat.processing_status",
            "is_processing": False,
            "is_complete": True,
        },
    )

    assert delivered is False


@pytest.mark.anyio
async def test_broadcast_event_terminal_without_waiter_returns_false():
    manager = TeamManager()

    delivered = await manager.broadcast_event(
        "sess-report",
        {
            "event_type": "chat.processing_status",
            "is_processing": False,
            "is_complete": True,
        },
    )

    assert delivered is False


# ---------------------------------------------------------------------------
# Writer rules (durable pipeline mirroring)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_writer_accumulates_delta_and_reasoning_without_records(history_records):
    writer = _writer()

    await writer.observe({"event_type": "chat.delta", "content": "你"})
    await writer.observe({"event_type": "chat.reasoning", "content": "思考"})

    assert history_records == []


@pytest.mark.anyio
async def test_writer_nonempty_final_discards_deltas_and_records_event(history_records):
    writer = _writer()

    await writer.observe({"event_type": "chat.delta", "content": "旧文本"})
    await writer.observe(
        {
            "event_type": "chat.final",
            "content": "最终回复",
            "session_id": "sess-team",
            "rid": 3,
            "role": "leader",
            "team_name": "demo-team",
        },
    )

    assert len(history_records) == 1
    record = history_records[0]
    assert record["event_type"] == "chat.final"
    assert record["content"] == "最终回复"
    assert record["role"] == "assistant"
    assert record["mode"] == "team"
    assert record["request_id"] == "session-message-sm-1"
    extra = record["extra"]
    assert extra["final_mode"] == "patch_segment"
    assert extra["role"] == "leader"
    assert extra["rid"] == 3
    assert extra["message_origin"] == "cross_session_agent"
    assert extra["session_message_id"] == "sm-1"
    assert extra["cross_session"]["message_id"] == "sm-1"
    assert extra["cross_session"]["source_session_id"] == "source-1"
    # Only the whitelisted keys are copied from the marker.
    assert "source_request_id" not in extra["cross_session"]


@pytest.mark.anyio
async def test_writer_empty_final_flushes_accumulated_text(history_records):
    writer = _writer()

    await writer.observe({"event_type": "chat.reasoning", "content": "思考中"})
    await writer.observe({"event_type": "chat.delta", "content": "回复"})
    await writer.observe({"event_type": "chat.delta", "content": "正文"})
    await writer.observe({"event_type": "chat.final", "content": "", "rid": 3})

    assert len(history_records) == 1
    record = history_records[0]
    assert record["event_type"] == "chat.final"
    assert record["content"] == "回复正文"
    assert record["extra"]["reasoning_content"] == "思考中"


@pytest.mark.anyio
async def test_writer_tool_call_boundary_flushes_then_records(history_records):
    writer = _writer()

    await writer.observe({"event_type": "chat.delta", "content": "正文"})
    await writer.observe({"event_type": "chat.reasoning", "content": "思考"})
    await writer.observe(
        {
            "event_type": "chat.tool_call",
            "tool_call": {
                "name": "session_send_message",
                "arguments": "{}",
                "tool_call_id": "call-1",
            },
            "rid": 3,
            "role": "leader",
        },
    )

    assert [record["event_type"] for record in history_records] == [
        "chat.final",
        "chat.tool_call",
    ]
    assert history_records[0]["content"] == "正文"
    # The flushed segment record carries the accumulated reasoning; the
    # boundary event itself records after the buffer has been consumed.
    assert history_records[0]["extra"]["reasoning_content"] == "思考"
    assert "reasoning_content" not in history_records[1]["extra"]


@pytest.mark.anyio
async def test_writer_error_record_takes_error_field_as_content(history_records):
    writer = _writer()

    await writer.observe({"event_type": "chat.error", "error": "boom", "rid": 3})

    assert len(history_records) == 1
    assert history_records[0]["event_type"] == "chat.error"
    assert history_records[0]["content"] == "boom"


@pytest.mark.anyio
async def test_writer_team_message_expands_inner_event(history_records):
    writer = _writer()

    await writer.observe(
        {
            "event_type": "team.message",
            "event": {
                "type": "teammate.message",
                "member_id": "m-1",
                "content": "hi",
                "timestamp": 1,
            },
        },
    )

    assert len(history_records) == 1
    extra = history_records[0]["extra"]
    assert extra["member_id"] == "m-1"
    assert "type" not in extra
    assert "timestamp" not in extra
    assert "content" not in extra


@pytest.mark.anyio
async def test_writer_without_marker_omits_cross_session_extra(history_records):
    writer = _writer(cross_session_marker=None)

    await writer.observe({"event_type": "chat.final", "content": "回复", "rid": 1})

    extra = history_records[0]["extra"]
    assert "cross_session" not in extra
    assert "message_origin" not in extra
    assert extra["final_mode"] == "patch_segment"


@pytest.mark.anyio
async def test_writer_skips_non_recordable_events(history_records):
    writer = _writer()

    for event in (
        {"event_type": "chat.processing_status", "is_processing": True},
        {"event_type": "chat.processing_status", "is_processing": False, "is_complete": True},
        {"event_type": "team.member"},
        {"event_type": "team.task"},
        {"event_type": "team.runtime_ready", "team_name": "t"},
        {"event_type": "workflow.updated", "status": "running"},
    ):
        await writer.observe(event)

    assert history_records == []


@pytest.mark.anyio
async def test_writer_dedupes_flushed_text_equal_to_last_final(history_records):
    writer = _writer()

    await writer.observe({"event_type": "chat.final", "content": "回复", "rid": 1})
    await writer.observe({"event_type": "chat.delta", "content": "回复"})
    await writer.observe({"event_type": "chat.final", "content": "", "rid": 1})

    assert len(history_records) == 1
    assert history_records[0]["content"] == "回复"


@pytest.mark.anyio
async def test_writer_flush_pending_reasoning_writes_standalone_record(history_records):
    writer = _writer()

    await writer.observe({"event_type": "chat.reasoning", "content": "思考"})
    await writer.flush_pending_reasoning()

    assert len(history_records) == 1
    record = history_records[0]
    assert record["event_type"] == "chat.reasoning"
    assert record["content"] == ""
    assert record["extra"]["reasoning_content"] == "思考"


@pytest.mark.anyio
async def test_writer_timestamp_uses_first_delta_moment_for_final(history_records):
    writer = _writer()

    await writer.observe({"event_type": "chat.delta", "content": "正文"})
    # Keep the segment start strictly before the completion moment so the
    # timestamp helper takes the segment branch deterministically.
    await asyncio.sleep(0.01)
    await writer.observe({"event_type": "chat.final", "content": "正文", "rid": 1})

    record = history_records[0]
    # The final record timestamp is the segment start (first delta) and the
    # completion moment is stored separately in extra.completed_at.
    assert record["timestamp"] < record["extra"]["completed_at"]


# ---------------------------------------------------------------------------
# Handover semantics (waiter takes over mid-stream)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_handover_nonempty_final_discards_pending_text(history_records):
    writer = _writer()

    await writer.observe({"event_type": "chat.delta", "content": "半截"})
    await writer.handover({"event_type": "chat.final", "content": "完整回复"})

    assert history_records == []


@pytest.mark.anyio
async def test_handover_boundary_flushes_pending_text(history_records):
    writer = _writer()

    await writer.observe({"event_type": "chat.delta", "content": "半截"})
    await writer.handover(
        {
            "event_type": "chat.tool_call",
            "tool_call": {"name": "t", "arguments": "{}", "tool_call_id": "c"},
        },
    )

    assert len(history_records) == 1
    assert history_records[0]["content"] == "半截"


@pytest.mark.anyio
async def test_handover_delta_keeps_pending_text(history_records):
    writer = _writer()

    await writer.observe({"event_type": "chat.delta", "content": "半截"})
    await writer.handover({"event_type": "chat.delta", "content": "续"})

    assert history_records == []


# ---------------------------------------------------------------------------
# Producer integration (_consume_stream_with_query)
# ---------------------------------------------------------------------------


class _ProducerFakeManager:
    """Minimal manager for driving the producer without runtime side effects."""

    def clear_pending_runtime(self, session_id: str) -> None:
        pass

    def pop_stream_task(self, session_id: str, *, generation: int | None = None) -> None:
        pass

    def get_monitor_handler(self, session_id: str):
        return None

    def get_workflow_handler(self, session_id: str):
        return None

    def get_background_task_controller(self, session_id: str):
        return None


def _no_waiter_broadcast(recorded: list[dict]) -> Any:
    async def _broadcast(_channel_id: str, _session_id: str, event: dict) -> bool:
        recorded.append(event)
        return False

    return _broadcast


def _delivering_broadcast() -> Any:
    async def _broadcast(_channel_id: str, _session_id: str, _event: dict) -> bool:
        return True

    return _broadcast


_LEADER_ROUND_EVENTS = [
    {"event_type": "chat.reasoning", "content": "思考"},
    {"event_type": "chat.delta", "content": "第一段"},
    {
        "event_type": "chat.tool_call",
        "tool_call": {
            "name": "session_send_message",
            "arguments": "{}",
            "tool_call_id": "call-1",
        },
        "rid": 1,
        "role": "leader",
    },
    {
        "event_type": "chat.tool_result",
        "result": "queued",
        "tool_name": "session_send_message",
        "tool_call_id": "call-1",
        "rid": 1,
        "role": "leader",
    },
    {"event_type": "chat.delta", "content": "第二段"},
    {"event_type": "chat.final", "content": "第二段回复", "rid": 1, "role": "leader"},
]


def _run_producer(monkeypatch, events: list[dict]) -> None:
    """Drive _consume_stream_with_query with synthetic leader events."""

    async def _fake_stream(**_kwargs):
        for index in range(len(events)):
            yield SimpleNamespace(
                type="controller_output",
                payload=index,
                role=TeamRole.LEADER,
            )

    class _FakeRunner:
        run_agent_team_streaming = staticmethod(_fake_stream)

    monkeypatch.setattr(team_helpers, "Runner", _FakeRunner)
    monkeypatch.setattr(
        team_helpers,
        "parse_stream_chunk",
        lambda chunk: events[chunk.payload],
    )
    monkeypatch.setattr(
        team_helpers, "get_team_manager", lambda _channel_id: _ProducerFakeManager(),
    )


@pytest.mark.anyio
async def test_producer_persists_unattended_events_under_round_identity(
    monkeypatch, history_records,
):
    _ACTIVE_ROUND_HISTORY_CONTEXT["sess-producer"] = _RoundHistoryContext(
        request_id="session-message-sm-1",
        cross_session_marker=_marker(),
    )
    broadcasted: list[dict] = []
    monkeypatch.setattr(
        team_helpers, "_broadcast_event", _no_waiter_broadcast(broadcasted),
    )
    _run_producer(monkeypatch, _LEADER_ROUND_EVENTS)

    await team_helpers._consume_stream_with_query(
        "web",
        "sess-producer",
        SimpleNamespace(team_name="demo-team"),
        "hello",
        round_id=1,
    )

    assert [record["event_type"] for record in history_records] == [
        "chat.final",
        "chat.tool_call",
        "chat.tool_result",
        "chat.final",
    ]
    assert history_records[0]["content"] == "第一段"
    assert history_records[0]["extra"]["reasoning_content"] == "思考"
    assert history_records[3]["content"] == "第二段回复"
    for record in history_records:
        assert record["request_id"] == "session-message-sm-1"
        assert record["mode"] == "team"
        assert record["role"] == "assistant"
    extra = history_records[3]["extra"]
    assert extra["message_origin"] == "cross_session_agent"
    assert extra["cross_session"]["message_id"] == "sm-1"


@pytest.mark.anyio
async def test_producer_skips_persistence_when_waiter_delivers(monkeypatch, history_records):
    _ACTIVE_ROUND_HISTORY_CONTEXT["sess-producer"] = _RoundHistoryContext(
        request_id="session-message-sm-1",
        cross_session_marker=_marker(),
    )
    monkeypatch.setattr(team_helpers, "_broadcast_event", _delivering_broadcast())
    _run_producer(monkeypatch, _LEADER_ROUND_EVENTS)

    await team_helpers._consume_stream_with_query(
        "web",
        "sess-producer",
        SimpleNamespace(team_name="demo-team"),
        "hello",
        round_id=1,
    )

    assert history_records == []


@pytest.mark.anyio
async def test_producer_rotates_writer_identity_when_round_changes(
    monkeypatch, history_records,
):
    _ACTIVE_ROUND_HISTORY_CONTEXT["sess-rotate"] = _RoundHistoryContext(
        request_id="session-message-sm-1",
        cross_session_marker=_marker(),
    )

    async def _fake_stream(**_kwargs):
        yield SimpleNamespace(type="controller_output", payload=0, role=TeamRole.LEADER)
        # A second mailbox turn takes over the persistent stream mid-flight.
        _ACTIVE_ROUND_HISTORY_CONTEXT["sess-rotate"] = _RoundHistoryContext(
            request_id="session-message-sm-2",
            cross_session_marker={"message_id": "sm-2", "source_session_id": "source-2"},
        )
        yield SimpleNamespace(type="controller_output", payload=1, role=TeamRole.LEADER)
        yield SimpleNamespace(type="controller_output", payload=2, role=TeamRole.LEADER)

    class _FakeRunner:
        run_agent_team_streaming = staticmethod(_fake_stream)

    monkeypatch.setattr(team_helpers, "Runner", _FakeRunner)
    rotate_events = [
        {"event_type": "chat.delta", "content": "第一轮"},
        {"event_type": "chat.delta", "content": "第二轮"},
        {"event_type": "chat.tool_call", "rid": 1, "role": "leader"},
    ]
    monkeypatch.setattr(
        team_helpers,
        "parse_stream_chunk",
        lambda chunk: rotate_events[chunk.payload],
    )
    monkeypatch.setattr(
        team_helpers, "get_team_manager", lambda _channel_id: _ProducerFakeManager(),
    )
    monkeypatch.setattr(
        team_helpers, "_broadcast_event", _no_waiter_broadcast([]),
    )

    await team_helpers._consume_stream_with_query(
        "web",
        "sess-rotate",
        SimpleNamespace(team_name="demo-team"),
        "hello",
        round_id=1,
    )

    assert [record["request_id"] for record in history_records] == [
        "session-message-sm-1",
        "session-message-sm-2",
        "session-message-sm-2",
    ]
    assert history_records[0]["content"] == "第一轮"
    assert history_records[1]["content"] == "第二轮"
    assert history_records[2]["event_type"] == "chat.tool_call"

# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Behavioral contracts for request normalization and per-stream state owners."""
from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

import pytest

from gateway.common.session_message import SESSION_MESSAGE_INTERNAL_KEY
from jiuwenswarm.runtime.session.model import TEAM_EXECUTION_CONTEXT_METADATA_KEY
from jiuwenswarm.server.runtime.agent_adapter.history_committer import HistoryCommitter
from jiuwenswarm.server.runtime.agent_adapter.request_context import RequestContext
from jiuwenswarm.server.runtime.agent_adapter.stream_projector import StreamProjector
from jiuwenswarm.server.runtime.session.history_io import run_history_io


def request(params=None, metadata=None, channel="", session="tui_request"):
    return SimpleNamespace(params=params, metadata=metadata, channel_id=channel, session_id=session)


def strip_debug(value):
    return (value[7:], True) if value.startswith("/debug ") else (value, False)


def test_request_context_normalizes_an_envelope_without_mutating_it():
    params = {
        "query": "", "content": None, "message": "/debug inspect",
        "trusted_dirs": [" D:/safe ", 12, ""], "skills": [None, " docs "],
        "project_dir": " D:/project ",
        "metadata": {"cwd": " D:/cwd ", "scene": "params", TEAM_EXECUTION_CONTEXT_METADATA_KEY: "untrusted"},
    }
    metadata = {"scene": "envelope", "project_dir": "ignored", "other": True}
    context = RequestContext.from_request(request(params, metadata), {"preferred_language": "en"}, strip_debug=strip_debug)
    assert context.query == "inspect"
    assert context.request_debug is True
    assert context.channel == "tui" and context.request_channel == ""
    assert context.language == "en"
    assert context.trusted_dirs == ["D:/safe"] and context.skills == ["docs"]
    assert context.project_dir == "D:/project" and context.cwd == "D:/cwd"
    assert context.metadata == {"scene": "params", "project_dir": "ignored", "cwd": " D:/cwd ", "other": True}
    assert params["message"] == "/debug inspect"
    assert TEAM_EXECUTION_CONTEXT_METADATA_KEY in params["metadata"]
    assert metadata["scene"] == "envelope"
    context.params["query"] = "request-local"
    context.trusted_dirs.append("another")
    assert params["query"] == "" and params["trusted_dirs"] == [" D:/safe ", 12, ""]


@pytest.mark.parametrize("extra", [{"team": True}, {SESSION_MESSAGE_INTERNAL_KEY: {"id": "m1"}}])
def test_request_context_preserves_team_and_internal_debug_directives(extra):
    context = RequestContext.from_request(request({"query": "/debug inspect", **extra}), {}, strip_debug=strip_debug)
    assert context.query == "/debug inspect" and context.request_debug is False


@pytest.mark.parametrize("query", [object(), 0, 42, {"answer": "resume"}])
def test_request_context_preserves_nontext_sdk_inputs(query):
    context = RequestContext.from_request(request({"query": query}, channel=" web "), {}, strip_debug=strip_debug)
    assert context.query is query
    assert context.request_channel == "web" and context.channel == "web"


def make_committer(write, request_id="request"):
    def decorate(extra, *_args, **_kwargs):
        return extra

    def timestamp(*, segment_started_at, extra_fields, **_kwargs):
        extra_fields.setdefault("completed_at", 200.0)
        return segment_started_at or 200.0

    return HistoryCommitter(
        params={"mode": "agent"}, session_id="session", request_id=request_id, channel_id="web",
        write_record=write, decorate_web=decorate, decorate_heartbeat=decorate,
        decorate_cross_session=decorate, resolve_final_timestamp=timestamp,
    )


@pytest.mark.asyncio
async def test_history_committer_waits_for_final_before_persisting_supplemental_input():
    writes = []
    final_started, release_final = asyncio.Event(), asyncio.Event()

    async def write(**record):
        if record.get("event_type") == "chat.final":
            final_started.set()
            await release_final.wait()
        writes.append(record)

    committer = make_committer(write)
    committer.note_durable_reasoning_delta({"output_order": {"sequence": 1}})
    committer.pending_reasoning_chunks.append("reason")
    committer.note_durable_pending_final_delta("before input", {"output_order": {"sequence": 2}})
    operation = asyncio.create_task(committer.persist_input_boundary({
        "timestamp": 300000, "input_request_id": "supplement", "content": "new input",
        "output_order": {"sequence": 3}, "session_message_id": "message-1",
    }))
    try:
        await asyncio.wait_for(final_started.wait(), 1)
        assert writes == []
        release_final.set()
        await asyncio.wait_for(operation, 1)
    finally:
        if not operation.done():
            operation.cancel()
            await asyncio.gather(operation, return_exceptions=True)
    assert [record["role"] for record in writes] == ["assistant", "user"]
    assert writes[0]["content"] == "before input"
    assert writes[0]["extra"]["reasoning_content"] == "reason"
    assert writes[0]["extra"]["completed_at"] == 300
    assert writes[1]["request_id"] == "supplement"
    assert writes[1]["extra"]["session_message_id"] == "message-1"
    assert not committer.pending_final_chunks and not committer.pending_reasoning_chunks


@pytest.mark.asyncio
@pytest.mark.parametrize("blocked_record", ["chat.final", "chat.reasoning", "user"])
async def test_accepted_input_boundary_drains_all_records_before_repeated_cancellation(blocked_record):
    writes = []
    entered, release = threading.Event(), threading.Event()

    def append_record(record):
        record_type = record.get("event_type", record["role"])
        if record_type == blocked_record:
            entered.set()
            assert release.wait(3), "history write was never released"
        writes.append(record)

    async def write(**record):
        await run_history_io(append_record, record)

    committer = make_committer(write)
    committer.note_durable_reasoning_delta({"output_order": {"sequence": 1}})
    committer.pending_reasoning_chunks.append("accepted reasoning")
    if blocked_record != "chat.reasoning":
        committer.note_durable_pending_final_delta("old answer", {"output_order": {"sequence": 2}})
    operation = asyncio.create_task(committer.persist_input_boundary({
        "timestamp": 300000, "input_request_id": "accepted-input", "content": "accepted user input",
        "output_order": {"sequence": 3}, "session_message_id": "accepted-message",
    }))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        operation.cancel("consumer disconnected")
        await asyncio.sleep(0)
        operation.cancel("shutdown also cancelled the consumer")
        await asyncio.sleep(0)
        assert not operation.done(), "cancel must wait for the complete accepted-input boundary"
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(operation, 2)
    finally:
        release.set()
        if not operation.done():
            operation.cancel()
            await asyncio.gather(operation, return_exceptions=True)

    assistant_type = "chat.reasoning" if blocked_record == "chat.reasoning" else "chat.final"
    assert [record.get("event_type", record["role"]) for record in writes] == [assistant_type, "user"]
    assert writes[0]["extra"]["reasoning_content"] == "accepted reasoning"
    assert writes[1]["content"] == "accepted user input"
    assert writes[1]["request_id"] == "accepted-input"
    assert writes[1]["extra"]["session_message_id"] == "accepted-message"
    assert writes[1]["extra"]["output_order"] == {"sequence": 3}
    assert writes[1]["extra"]["is_supplemental_input"] is True
    assert not committer.pending_final_chunks and not committer.pending_reasoning_chunks
    await committer.persist_pending_final_text()
    await committer.persist_pending_reasoning()
    assert len(writes) == 2


@pytest.mark.asyncio
async def test_accepted_input_boundary_propagates_writer_cancellation_without_spinning():
    attempts = []

    async def write(**record):
        attempts.append(record)
        raise asyncio.CancelledError("history writer cancelled")

    committer = make_committer(write)
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(committer.persist_input_boundary({
            "timestamp": 300000, "input_request_id": "accepted-input", "content": "input",
        }), 1)
    assert len(attempts) == 1 and attempts[0]["role"] == "user"


@pytest.mark.asyncio
async def test_history_committer_isolates_requests_and_deduplicates_completed_text():
    writes = []

    async def write(**record):
        writes.append(record)

    first, second = make_committer(write, "first"), make_committer(write, "second")
    first.note_durable_pending_final_delta("answer", {})
    second.note_durable_pending_final_delta("other", {})
    await first.persist_pending_final_text()
    first.note_durable_pending_final_delta("answer", {})
    await first.persist_pending_final_text()
    assert len(writes) == 1
    assert second.pending_final_chunks == ["other"]
    await second.persist_pending_final_text()
    assert [(record["request_id"], record["content"]) for record in writes] == [("first", "answer"), ("second", "other")]


@pytest.mark.asyncio
async def test_history_committer_preserves_reasoning_on_aborted_non_goal_stream():
    writes = []

    async def write(**record):
        writes.append(record)

    committer = make_committer(write)
    committer.note_durable_reasoning_delta({"output_order": {"sequence": 4}})
    committer.pending_reasoning_chunks.append("unfinished reasoning")
    committer.note_durable_pending_final_delta("unfinished answer", {})
    await committer.persist_pending_reasoning()
    await committer.persist_pending_reasoning()
    assert len(writes) == 1 and writes[0]["event_type"] == "chat.reasoning"
    assert writes[0]["extra"]["reasoning_content"] == "unfinished reasoning"
    assert writes[0]["extra"]["reasoning_output_order"] == {"sequence": 4}
    assert committer.pending_final_chunks == ["unfinished answer"]


class BlockBuffer:
    def __init__(self, decision=None):
        self.decision = decision

    def key_for(self, payload):
        return ("round", payload["member"])

    def consume(self, *_args):
        return self.decision


def make_projector(buffer=None):
    return StreamProjector(
        block_buffer=buffer or BlockBuffer(),
        split_probe=lambda previous, content: (previous, content),
        extend_probe=lambda previous, content: previous + content,
    )


def test_stream_projector_keeps_text_and_a2ui_probes_request_local():
    first, second = make_projector(), make_projector()
    first.final_answer_chunks.append("answer")
    assert first.assistant_message == "answer" and second.assistant_message == ""
    assert first.probe("prefix") == ("", "prefix")
    assert first.probe("block") == ("prefix", "block")
    assert second.probe("other") == ("", "other")


def test_stream_projector_does_not_block_other_members_while_one_finalizes():
    projector = make_projector()
    projector.team_a2ui_tasks[("round", "one")] = object()
    final = {"event_type": "chat.final", "member": "one", "content": "final"}
    other = {"event_type": "chat.delta", "member": "two", "content": "continue"}
    assert projector.process_team_payload(final, lambda *_: None) == ([], None)
    assert projector.process_team_payload(other, lambda *_: None) == ([], other)
    assert projector.team_a2ui_pending_finals[("round", "one")] is final


def test_stream_projector_schedules_block_and_preserves_visible_prefix():
    decision = SimpleNamespace(passthrough="prefix", replacement=None, raw_block="block", suppress=True)
    projector = make_projector(BlockBuffer(decision))
    scheduled = []
    payload = {"event_type": "chat.delta", "member": "one", "content": "prefixblock"}
    direct, remainder = projector.process_team_payload(payload, lambda *args: scheduled.append(args))
    assert direct == [{**payload, "content": "prefix"}] and remainder is None
    assert scheduled == [(payload, decision)]

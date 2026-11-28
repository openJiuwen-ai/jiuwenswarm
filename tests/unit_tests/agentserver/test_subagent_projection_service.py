# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Pure SDK decoding and accepted subagent-event consumer boundaries."""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

import pytest

from jiuwenswarm.server.runtime.agent_adapter.subagent_projection import (
    SubagentProjectionService,
    SubagentStreamEvent,
    parse_subagent_event,
)
from jiuwenswarm.server.runtime.session.history_io import run_stream_parser
from jiuwenswarm.server.utils.stream_utils import decode_stream_chunk as parse_stream_chunk


def _chunk(kind: str, **projection):
    return SimpleNamespace(type=kind, payload={kind: {"subagent_id": "child", **projection}})


@pytest.mark.parametrize("kind", ["subagent_updated", "subagent_message", "subagent_activity"])
@pytest.mark.parametrize("as_dict", [False, True])
def test_sdk_parsers_return_neutral_events_without_projection_or_writes(kind, as_dict, monkeypatch):
    from jiuwenswarm.server.runtime.agent_adapter import interface_deep

    def forbidden(*_args, **_kwargs):
        raise AssertionError("A pure parser reached projection or persistence")

    monkeypatch.setattr(interface_deep.SUBAGENT_PROJECTION, "consume", forbidden)
    monkeypatch.setattr(interface_deep.SUBAGENT_PROJECTION, "project_roster", forbidden)
    monkeypatch.setattr(interface_deep.SUBAGENT_PROJECTION, "persist_transcript", forbidden)
    monkeypatch.setattr(interface_deep.SUBAGENT_PROJECTION, "persist_activity", forbidden)
    chunk = _chunk(kind, parent_session_id="parent", content="result")
    if as_dict:
        chunk = vars(chunk)
    for parser in (parse_stream_chunk, interface_deep.JiuWenSwarmDeepAdapter.decode_stream_chunk):
        parsed = parser(chunk)
        assert isinstance(parsed, SubagentStreamEvent)
        assert parsed.kind == kind
        assert parsed.projection["content"] == "result"


@pytest.mark.asyncio
async def test_consumer_owns_roster_order_and_commits_before_visible_output():
    records = []
    owner = SubagentProjectionService(write_history=lambda **record: records.append(record), clock=lambda: 42.0)
    async def consume(child, status, revision):
        payload = await run_stream_parser(
            parse_stream_chunk,
            _chunk("subagent_updated", subagent_id=child, parent_session_id="parent",
                   status=status, revision=revision, display_name=child),
            projection_owner=owner,
        )
        assert records[-1]["request_id"] == f"subagent-roster-{child}:{revision}"
        assert records[-1]["extra"] == payload
        return payload

    first = await consume("a", "running", 1)
    second = await consume("b", "running", 1)
    completed = await consume("a", "idle", 2)
    assert (first["index"], second["index"], second["total"]) == (0, 1, 2)
    assert second["is_parallel"] is True
    assert completed["status"] == "idle"
    assert completed["legacy_status"] == "completed"
    assert completed["total"] == 2
    assert all(record["mode"] == "subagent" for record in records)
    assert all(record["session_id"] == "parent" for record in records)
    assert all(record["timestamp"] == 42 for record in records)
    owner.clear_parent("parent")
    assert (await consume("c", "running", 1))["index"] == 0


@pytest.mark.asyncio
async def test_silent_transcript_is_single_commit_off_loop_with_parent_child_scope():
    records = []
    loop_thread = threading.get_ident()
    def write(**record):
        assert threading.get_ident() != loop_thread
        records.append(record)

    owner = SubagentProjectionService(write_history=write, clock=lambda: 9)
    chunk = _chunk("subagent_message", seq=7, role="assistant", content="final",
                   event_type="chat.final", reasoning_content="  reason  ", phase_id=2,
                   at_ms=12345, extra={"source": "sdk"})
    result = await run_stream_parser(
        parse_stream_chunk, chunk, projection_owner=owner,
        projection_parent_session_id="parent",
    )
    assert result is None
    assert len(records) == 1
    assert records[0] == {
        "session_id": "parent", "subagent_id": "child", "request_id": "child:7",
        "channel_id": "subagent", "role": "assistant", "content": "final",
        "timestamp": 12.345, "event_type": "chat.final", "mode": "subagent",
        "extra": {"reasoning_content": "reason", "phase_id": 2, "source": "sdk", "parent_session_id": "parent"},
    }
    assert "parent_session_id" not in chunk.payload["subagent_message"]


@pytest.mark.asyncio
async def test_activity_keeps_wire_payload_and_persists_parent_fallback_without_mutation():
    records = []
    owner = SubagentProjectionService(write_history=lambda **record: records.append(record))
    chunk = _chunk("subagent_activity", seq=0, task_id="t", at_ms=1200,
                   summary="search", phase_id=3, tool_call_id="call")
    result = await run_stream_parser(
        parse_stream_chunk, chunk, projection_owner=owner,
        projection_parent_session_id="fallback",
    )
    assert result == {"event_type": "chat.subagent_activity", **chunk.payload["subagent_activity"]}
    assert records[0]["request_id"] == "child:activity:t:0"
    assert records[0]["timestamp"] == 1.2
    assert records[0]["extra"]["subagent_activity"]["parent_session_id"] == "fallback"
    assert "parent_session_id" not in chunk.payload["subagent_activity"]


@pytest.mark.asyncio
async def test_explicit_sdk_parent_is_preserved_and_user_transcript_cannot_change_parent_route():
    records = []
    owner = SubagentProjectionService(write_history=lambda **record: records.append(record))
    await run_stream_parser(
        parse_stream_chunk,
        _chunk("subagent_message", parent_session_id="sdk-parent", role="user", event_type="chat.final"),
        projection_owner=owner, projection_parent_session_id="fallback",
    )
    assert records[0]["session_id"] == "sdk-parent"
    assert records[0]["subagent_id"] == "child"
    assert records[0]["event_type"] is None
    assert records[0]["channel_id"] == "subagent"


@pytest.mark.asyncio
async def test_cancelled_consumer_drains_accepted_transcript_before_cleanup():
    entered, release = threading.Event(), threading.Event()
    records = []
    def write(**record):
        entered.set()
        assert release.wait(5)
        records.append(record["content"])

    owner = SubagentProjectionService(write_history=write)
    task = asyncio.create_task(run_stream_parser(
        parse_stream_chunk,
        _chunk("subagent_message", parent_session_id="p", content="accepted"),
        projection_owner=owner,
    ))
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    records.append("cleanup")
    assert records == ["accepted", "cleanup"]


@pytest.mark.asyncio
async def test_history_failure_is_propagated_instead_of_becoming_silent_success():
    def fail(**_record):
        raise OSError("history disk unavailable")

    owner = SubagentProjectionService(write_history=fail)
    with pytest.raises(OSError, match="history disk unavailable"):
        await run_stream_parser(
            parse_stream_chunk, _chunk("subagent_message", parent_session_id="p"),
            projection_owner=owner,
        )


def test_invalid_envelopes_never_create_a_neutral_event():
    assert parse_subagent_event({"type": "subagent_message", "payload": {"subagent_message": "bad"}}) is None
    assert parse_subagent_event({"type": "llm_output", "payload": "token"}) is None


@pytest.mark.asyncio
async def test_auto_harness_actual_queue_consumer_commits_silent_events_and_emits_only_dicts(monkeypatch):
    from openjiuwen.core.session.stream.base import OutputSchema
    from jiuwenswarm.agents.harness.common.auto_harness.service import AutoHarnessService
    from jiuwenswarm.server.runtime.session import session_history

    records = []
    monkeypatch.setattr(session_history, "append_history_record", lambda **record: records.append(record))
    queue = asyncio.Queue()
    for kind in ("subagent_updated", "subagent_message", "subagent_activity"):
        envelope = _chunk(kind, content="child reply", seq=1)
        queue.put_nowait(OutputSchema(type=envelope.type, payload=envelope.payload, index=0))
    queue.put_nowait(None)
    completed = asyncio.get_running_loop().create_future()
    completed.set_result(None)
    active = SimpleNamespace(
        stream_queue=queue, task=completed, cancelled=False, session_id="harness-parent",
        current_stage_name="repair", pipeline_preference="test",
    )
    service = object.__new__(AutoHarnessService)
    output = [chunk async for chunk, _suspend in service._consume_stream(active, "request", "web")]
    assert len(records) == 3
    assert all(record["session_id"] == "harness-parent" for record in records)
    assert [chunk.payload["event_type"] for chunk in output] == ["chat.subtask_update", "chat.subagent_activity"]
    assert all(isinstance(chunk.payload, dict) for chunk in output)


@pytest.mark.asyncio
@pytest.mark.parametrize("consumer", ["broadcast", "push"])
async def test_repair_progress_consumers_commit_transcripts_without_wire_leak(monkeypatch, consumer):
    from unittest.mock import AsyncMock
    from jiuwenswarm.server.runtime.agent_adapter.evolution_helpers import (
        EvolutionPushContext, broadcast_evolution_progress, push_evolution_progress,
    )
    from jiuwenswarm.server.runtime.session import session_history

    records, output = [], []
    monkeypatch.setattr(session_history, "append_history_record", lambda **record: records.append(record))
    events = [_chunk(kind, seq=1) for kind in ("subagent_updated", "subagent_message", "subagent_activity")]
    if consumer == "broadcast":
        await broadcast_evolution_progress(
            "web", "repair-parent", events, parse_stream_chunk=parse_stream_chunk,
            broadcast_event=lambda channel, session, payload: output.append(payload),
        )
    else:
        transport = SimpleNamespace(send_push=AsyncMock(side_effect=lambda payload: output.append(payload["payload"])))
        await push_evolution_progress(
            EvolutionPushContext(transport, "web", "repair-parent"), "request", events,
            parse_stream_chunk=parse_stream_chunk, build_push_message=lambda **kwargs: kwargs,
        )
    assert len(records) == 3
    assert all(record["session_id"] == "repair-parent" for record in records)
    assert [payload["event_type"] for payload in output] == ["chat.subtask_update", "chat.subagent_activity"]
    assert all(isinstance(payload, dict) for payload in output)


@pytest.mark.asyncio
async def test_expert_relay_keeps_silent_subagent_history_before_its_presentation_filter(monkeypatch):
    from unittest.mock import AsyncMock
    from jiuwenswarm.agents.harness.team.expert_org.launcher import JiuwenExpertTeamLauncher
    from jiuwenswarm.server.runtime.agent_adapter import team_helpers
    from jiuwenswarm.server.runtime.session import session_history

    records = []
    monkeypatch.setattr(session_history, "append_history_record", lambda **record: records.append(record))
    monkeypatch.setattr(team_helpers, "_is_leader_output", lambda _chunk: False)
    monkeypatch.setattr(team_helpers, "_is_teammate_output", lambda _chunk: True)
    launcher = JiuwenExpertTeamLauncher(runtime_manager=SimpleNamespace())
    launcher.push_expert_payload = AsyncMock()
    result = await launcher._relay_expert_chunk(
        _chunk("subagent_message", seq=1, content="silent"), team_id="team",
        session_id="expert-parent", channel_id="web", round_id="round", frame_sequence=0,
    )
    assert result is None
    assert len(records) == 1
    assert records[0]["session_id"] == "expert-parent"
    launcher.push_expert_payload.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_roster_commit_rolls_back_parallel_batch_before_same_revision_retry():
    records = []
    fail_completion = True
    def write(**record):
        nonlocal fail_completion
        if record["extra"]["status"] == "idle" and fail_completion:
            fail_completion = False
            raise OSError("record write failed")
        records.append(record)

    owner = SubagentProjectionService(write_history=write)
    for child in ("a", "b"):
        await run_stream_parser(
            parse_stream_chunk, _chunk("subagent_updated", subagent_id=child, status="running"),
            projection_owner=owner, projection_parent_session_id="parent",
        )
    completion = _chunk("subagent_updated", subagent_id="a", status="idle", revision=2)
    with pytest.raises(OSError, match="record write failed"):
        await run_stream_parser(
            parse_stream_chunk, completion, projection_owner=owner,
            projection_parent_session_id="parent",
        )
    assert owner._progress_batches["parent"] == ["a", "b"]
    retry = await run_stream_parser(
        parse_stream_chunk, completion, projection_owner=owner,
        projection_parent_session_id="parent",
    )
    assert (retry["index"], retry["total"], retry["is_parallel"]) == (0, 2, True)
    assert owner._progress_batches["parent"] == ["b"]
    assert len(records) == 3


@pytest.mark.asyncio
async def test_parent_cleanup_waits_off_loop_and_other_sessions_bypass_slow_history():
    entered, release = threading.Event(), threading.Event()
    def write(**record):
        if record["session_id"] == "slow-parent":
            entered.set()
            assert release.wait(5)

    owner = SubagentProjectionService(write_history=write)
    slow = asyncio.create_task(run_stream_parser(
        parse_stream_chunk, _chunk("subagent_updated", status="running"),
        projection_owner=owner, projection_parent_session_id="slow-parent",
    ))
    cleanup = None
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        cleanup = asyncio.create_task(owner.clear_parent_async("slow-parent"))
        fast = await asyncio.wait_for(run_stream_parser(
            parse_stream_chunk, _chunk("subagent_updated", status="running"),
            projection_owner=owner, projection_parent_session_id="fast-parent",
        ), timeout=1)
        assert fast["parent_session_id"] == "fast-parent"
        assert not cleanup.done()
        assert not slow.done()
        await asyncio.wait_for(asyncio.sleep(0), timeout=0.5)
    finally:
        release.set()
        await slow
        if cleanup is not None:
            await cleanup
    assert "slow-parent" not in owner._progress_batches
    assert "slow-parent" not in owner._parents
    assert owner._progress_batches["fast-parent"] == ["child"]


@pytest.mark.asyncio
@pytest.mark.parametrize("api", ["common", "deep"])
@pytest.mark.parametrize("kind", ["subagent_updated", "subagent_message", "subagent_activity"])
async def test_public_parser_compatibility_keeps_payload_only_json_contract(monkeypatch, api, kind):
    import json
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter
    from jiuwenswarm.server.runtime.session import session_history
    from jiuwenswarm.server.utils.stream_utils import parse_stream_chunk as compatibility_parser

    records = []
    monkeypatch.setattr(session_history, "append_history_record", lambda **record: records.append(record))
    parser = compatibility_parser if api == "common" else JiuWenSwarmDeepAdapter.parse_stream_chunk
    result = await asyncio.to_thread(parser, _chunk(kind, parent_session_id="compat", content="result"))
    assert result is None if kind == "subagent_message" else isinstance(result, dict)
    json.dumps(result)
    assert len(records) == 1
    assert records[0]["wait_for_write"] is True


@pytest.fixture
async def drained_history_writer():
    from jiuwenswarm.server.runtime.session import session_history

    assert await asyncio.to_thread(session_history.flush_pending_writes)
    try:
        yield
    finally:
        assert await asyncio.to_thread(session_history.flush_pending_writes)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["disk", "target_rejected"])
async def test_real_writer_receipt_failure_rolls_back_then_retry_persists_once(
    tmp_path, monkeypatch, drained_history_writer, failure,
):
    import json
    from jiuwenswarm.server.runtime.agent_adapter.subagent_projection import _append_history
    from jiuwenswarm.server.runtime.session import session_history, session_metadata

    monkeypatch.setattr(session_history, "get_agent_sessions_dir", lambda: tmp_path)
    monkeypatch.setattr(session_metadata, "update_session_metadata", lambda **_kwargs: None)
    monkeypatch.setattr(session_metadata, "set_session_delivery_context", lambda **_kwargs: None)
    monkeypatch.setenv("JIUWENSWARM_USE_LEGACY_HISTORY_JSON", "0")
    owner = SubagentProjectionService(write_history=_append_history)
    for child in ("a", "b"):
        await run_stream_parser(
            parse_stream_chunk, _chunk("subagent_updated", subagent_id=child, status="running"),
            projection_owner=owner, projection_parent_session_id="receipt-parent",
        )
    append = session_history._append_record_jsonl
    resolve = session_history.resolve_subagent_history_path
    failed = False
    def failing_append(path, record):
        nonlocal failed
        if failure == "disk" and not failed and record.get("status") == "idle":
            failed = True
            raise OSError("injected record disk error")
        append(path, record)

    def rejecting_resolver(parent, child, **kwargs):
        nonlocal failed
        if failure == "target_rejected" and not failed and child == "a":
            failed = True
            return None, "injected unsafe subagent target"
        return resolve(parent, child, **kwargs)

    monkeypatch.setattr(session_history, "_append_record_jsonl", failing_append)
    monkeypatch.setattr(session_history, "resolve_subagent_history_path", rejecting_resolver)
    completion = _chunk("subagent_updated", subagent_id="a", status="idle", revision=2)
    expected = OSError if failure == "disk" else ValueError
    with pytest.raises(expected, match="injected"):
        await run_stream_parser(
            parse_stream_chunk, completion, projection_owner=owner,
            projection_parent_session_id="receipt-parent",
        )
    assert owner._progress_batches["receipt-parent"] == ["a", "b"]
    retry = await run_stream_parser(
        parse_stream_chunk, completion, projection_owner=owner,
        projection_parent_session_id="receipt-parent",
    )
    assert (retry["index"], retry["total"], retry["is_parallel"]) == (0, 2, True)
    path, error = resolve("receipt-parent", "a")
    assert error is None
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert [row["status"] for row in rows] == ["running", "idle"]
    assert owner._progress_batches["receipt-parent"] == ["b"]

# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Real writer receipts, with disk faults below the queue admission boundary."""
from __future__ import annotations

import asyncio
import json
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponseChunk
from jiuwenswarm.server.runtime.agent_adapter import interface
from jiuwenswarm.server.runtime.agent_adapter.history_committer import HistoryCommitter
from jiuwenswarm.server.runtime.session import session_history, session_metadata
from jiuwenswarm.server.runtime.session.history_io import run_history_io


@pytest.fixture
def history_target(tmp_path, monkeypatch):
    assert session_history.flush_pending_writes(5)
    monkeypatch.setattr(session_history, "get_agent_sessions_dir", lambda: tmp_path)
    monkeypatch.setattr(session_history, "use_legacy_history_json", lambda: False)
    metadata, routes = [], []
    monkeypatch.setattr(session_metadata, "update_session_metadata", lambda **record: metadata.append(record))
    monkeypatch.setattr(session_metadata, "set_session_delivery_context", lambda **record: routes.append(record))
    yield tmp_path, metadata, routes
    assert session_history.flush_pending_writes(5)


def record(**extra):
    return dict(session_id="receipt-session", request_id="receipt-request", channel_id="web",
                role="assistant", event_type="chat.final", content="answer", timestamp=1.0, **extra)


def rows(target):
    path = target / "receipt-session" / "history.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


@pytest.mark.asyncio
async def test_record_ack_propagates_real_disk_failure_before_metadata_and_retries_once(history_target, monkeypatch):
    target, metadata, routes = history_target
    original = session_history._append_record_jsonl

    def fail(path, item):
        raise OSError("real writer disk unavailable")

    monkeypatch.setattr(session_history, "_append_record_jsonl", fail)
    with pytest.raises(OSError, match="real writer disk unavailable"):
        await run_history_io(session_history.append_history_record, wait_for_write=True, **record())
    assert rows(target) == [] and metadata == [] and routes == []
    monkeypatch.setattr(session_history, "_append_record_jsonl", original)
    await run_history_io(session_history.append_history_record, wait_for_write=True, **record())
    assert [item["content"] for item in rows(target)] == ["answer"]
    assert len(metadata) == 1 and routes == []


@pytest.mark.asyncio
async def test_accepted_record_repeated_cancel_waits_for_actual_writer_and_metadata(history_target, monkeypatch):
    target, metadata, _routes = history_target
    entered, release = threading.Event(), threading.Event()
    original = session_history._append_record_jsonl

    def gate(path, item):
        entered.set()
        assert release.wait(5)
        original(path, item)
    monkeypatch.setattr(session_history, "_append_record_jsonl", gate)
    task = asyncio.create_task(run_history_io(
        session_history.append_history_record, wait_for_write=True, **record(),
    ))
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        assert rows(target) == [] and metadata == []
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 3)
    finally:
        release.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert len(rows(target)) == 1 and len(metadata) == 1


@pytest.mark.asyncio
async def test_legacy_default_keeps_asynchronous_admission(history_target, monkeypatch):
    target, metadata, _routes = history_target
    entered, release = threading.Event(), threading.Event()
    original = session_history._append_record_jsonl

    def gate(path, item):
        entered.set()
        assert release.wait(5)
        original(path, item)
    monkeypatch.setattr(session_history, "_append_record_jsonl", gate)
    try:
        session_history.append_history_record(**record())
        assert await asyncio.to_thread(entered.wait, 3)
        assert rows(target) == [] and len(metadata) == 1
    finally:
        release.set()
        assert await asyncio.to_thread(session_history.flush_pending_writes, 3)
    assert len(rows(target)) == 1


@pytest.mark.asyncio
async def test_invalid_confirmed_child_target_raises_but_legacy_skip_remains(history_target, monkeypatch):
    _target, metadata, _routes = history_target
    monkeypatch.setattr(session_history, "resolve_subagent_history_path", lambda *args, **kwargs: (None, "rejected child"))
    with pytest.raises(ValueError, match="rejected child"):
        await run_history_io(session_history.append_history_record, wait_for_write=True, **record(subagent_id="child"))
    assert metadata == []
    session_history.append_history_record(**record(subagent_id="child"))
    assert await asyncio.to_thread(session_history.flush_pending_writes, 3)
    assert len(metadata) == 1


@pytest.mark.asyncio
async def test_committer_retains_exact_failed_record_until_real_writer_retry(history_target, monkeypatch):
    target, metadata, _routes = history_target
    original = session_history._append_record_jsonl

    def fail(path, item):
        raise OSError("actual disk error")
    monkeypatch.setattr(session_history, "_append_record_jsonl", fail)

    async def write(**record):
        await run_history_io(session_history.append_history_record, wait_for_write=True, **record)

    def decorate(extra, *args, **kwargs):
        return extra
    committer = HistoryCommitter(
        params={"mode": "agent"}, session_id="receipt-session", request_id="receipt-request", channel_id="web",
        write_record=write, decorate_web=decorate, decorate_heartbeat=decorate, decorate_cross_session=decorate,
        resolve_final_timestamp=lambda **kwargs: 42.0,
    )
    committer.note_durable_pending_final_delta("answer", {})
    with pytest.raises(OSError, match="actual disk error"):
        await committer.persist_pending_final_text()
    assert committer.pending_final_chunks == ["answer"] and metadata == []
    monkeypatch.setattr(session_history, "_append_record_jsonl", original)
    await committer.retry_pending_writes()
    await committer.persist_pending_final_text()
    assert [(item["content"], item["timestamp"]) for item in rows(target)] == [("answer", 42.0)]
    assert committer.pending_final_chunks == [] and len(metadata) == 1


@pytest.mark.asyncio
async def test_actual_foreground_final_waits_for_disk_and_surfaces_fault(history_target, monkeypatch):
    target, metadata, _routes = history_target
    original = session_history._append_record_jsonl
    emitted = []

    def fail_final(path, item):
        if item.get("event_type") == "chat.final":
            raise OSError("foreground disk error")
        original(path, item)
    monkeypatch.setattr(session_history, "_append_record_jsonl", fail_final)
    monkeypatch.setattr(interface, "restore_chat_send_equipment_params", lambda *args: None)
    monkeypatch.setattr(interface, "is_auto_memory_enabled", lambda *args: False)

    class Adapter:
        async def process_message_stream_impl(self, request, inputs):
            yield AgentResponseChunk(request_id=request.request_id, channel_id="web",
                                     payload={"event_type": "chat.final", "content": "answer"})
    facade = interface.JiuWenSwarm.__new__(interface.JiuWenSwarm)
    facade._sdk_name = "test"
    facade._session_manager = SimpleNamespace(get_session_id=lambda sid: sid)
    facade._ensure_adapter = lambda **kwargs: Adapter()
    facade._build_inputs = lambda request: ({"query": "hello"}, "local", SimpleNamespace(text="hello"))
    facade.reconcile_session_mcp = AsyncMock()
    facade._make_retry_without_a2ui_call = lambda **kwargs: None
    request = AgentRequest(request_id="receipt-request", session_id="receipt-session", channel_id="web",
                           params={"mode": "agent", "query": "hello"}, is_stream=True)
    with pytest.raises(OSError, match="foreground disk error"):
        async for chunk in facade.process_message_stream(request):
            emitted.append(chunk)
    assert not any(chunk.payload and chunk.payload.get("event_type") == "chat.final" for chunk in emitted)
    assert not any(item.get("event_type") == "chat.final" for item in rows(target))
    assert len(metadata) == 1  # Only the accepted user record, never the failed final.


@pytest.mark.asyncio
async def test_actual_control_final_disk_error_is_not_acknowledged_and_cleanup_retries(history_target, monkeypatch):
    target, metadata, _routes = history_target
    original = session_history._append_record_jsonl
    fail = True
    closed, emitted = [], []

    def fail_final(path, item):
        if fail and item.get("event_type") == "chat.final":
            raise OSError("control disk error")
        original(path, item)
    monkeypatch.setattr(session_history, "_append_record_jsonl", fail_final)
    monkeypatch.setattr(interface, "restore_chat_send_equipment_params", lambda *args: None)
    monkeypatch.setattr(interface, "compute_chat_send_mcp_needed", lambda *args: [])

    class Adapter:
        cleanup = AsyncMock()

        async def process_message_stream_impl(self, request, inputs):
            try:
                yield AgentResponseChunk(request_id=request.request_id, channel_id="web",
                                         payload={"event_type": "chat.delta", "content": "answer"})
                yield AgentResponseChunk(request_id=request.request_id, channel_id="web",
                                         payload={"event_type": "chat.final", "content": "answer"})
            finally:
                closed.append("closed")
    adapter = Adapter()
    facade = interface.JiuWenSwarm.__new__(interface.JiuWenSwarm)
    facade._adapter = adapter
    facade._session_manager = SimpleNamespace(get_session_id=lambda sid: sid, close_all_sessions=AsyncMock())
    facade._ensure_adapter = lambda **kwargs: adapter
    facade._adapter_mode_for_request = lambda request: "agent"
    facade._build_inputs = lambda request: ({"query": "yes"}, "local", SimpleNamespace(text="yes"))
    facade.reconcile_session_mcp = AsyncMock()
    request = AgentRequest(request_id="receipt-request", session_id="receipt-session", channel_id="web",
                           params={"mode": "agent", "source": "ask_user_interrupt", "request_id": "question",
                                   "answers": [{"selected_options": ["yes"]}]}, is_stream=True)
    with pytest.raises(OSError, match="control disk error"):
        async for chunk in facade.deliver_control_input(request):
            emitted.append(chunk.payload["event_type"])
    assert emitted == ["chat.delta"] and closed == ["closed"]
    assert [item["event_type"] for item in rows(target)] == ["chat.ask_user_answer"]
    assert len(metadata) == 1 and len(facade._control_input_executions) == 1
    adapter.cleanup.assert_not_awaited()
    fail = False
    await facade.cleanup()
    assert [item["event_type"] for item in rows(target)] == [
        "chat.ask_user_answer", "chat.final", "chat.request_completed",
    ]
    assert rows(target)[-1]["status"] == "error"
    assert len(metadata) == 2 and not facade._control_input_executions
    adapter.cleanup.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("filtered", ["empty", "heartbeat"])
async def test_filtered_confirmed_records_intentionally_have_no_writer_receipt(history_target, monkeypatch, filtered):
    target, metadata, routes = history_target

    def forbid(*args, **kwargs):
        raise AssertionError("filtered history unexpectedly enqueued a write")
    monkeypatch.setattr(session_history, "_enqueue_history_item", forbid)
    value = record()
    if filtered == "empty":
        value["content"] = "   "
    else:
        value["session_id"] = "heartbeat_receipt"
    await run_history_io(session_history.append_history_record, wait_for_write=True, **value)
    assert rows(target) == [] and metadata == [] and routes == []

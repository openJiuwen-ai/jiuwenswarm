# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Request-local pre-reads retain the existing history and lifecycle contract."""
from __future__ import annotations

import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.agents.harness.common import session_ops_service as service
from jiuwenswarm.server.runtime.session import lifecycle as lc
from jiuwenswarm.server.runtime.session import session_history as history


def save_state(sid, **changes):
    with lc.resource_lock("session", sid):
        value = lc.state("session", sid)
        value.update(changes)
        lc.save_locked("session", sid, value)


@pytest.fixture
def stored_history(tmp_path, monkeypatch):
    sessions = tmp_path / "sessions"
    monkeypatch.setattr(lc, "get_agent_sessions_dir", lambda: sessions)
    monkeypatch.setattr(lc, "get_agent_root_dir", lambda: tmp_path)
    monkeypatch.setattr(history, "get_agent_sessions_dir", lambda: sessions)
    monkeypatch.setattr(service, "_fork_source_for_session", lambda *_args: "")
    sid = "session-prefetch"
    lc.claim_runtime(sid)
    folder = sessions / sid
    folder.mkdir(parents=True, exist_ok=True)
    records = [
        {"role": "user", "request_id": "old", "content": "original question"},
        {"role": "assistant", "request_id": "old", "event_type": "chat.final", "content": "original answer"},
        {"role": "user", "request_id": "current", "content": "current input"},
        {"role": "user", "request_id": "later", "content": "queued input"},
    ]

    def write(values=records, *, legacy=False):
        path = folder / ("history.json" if legacy else "history.jsonl")
        payload = json.dumps(values) if legacy else "\n".join(json.dumps(record) for record in values) + "\n"
        with lc.resource_lock("session", sid):
            path.write_text(payload, encoding="utf-8")
        return path

    write()
    engine = SimpleNamespace(get_context=lambda **_kwargs: None, create_context=AsyncMock())
    deep_agent = SimpleNamespace(react_agent=SimpleNamespace(context_engine=engine,
        _config=SimpleNamespace(context_processors=[])))
    monkeypatch.setattr(service, "resolve_live_agent_session", lambda *_args: object())
    return SimpleNamespace(sid=sid, folder=folder, records=records, write=write,
        deep_agent=deep_agent, engine=engine)


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", [False, True])
async def test_current_snapshot_reads_once_and_keeps_request_cutoff(stored_history, monkeypatch, legacy):
    stored = stored_history
    if legacy:
        (stored.folder / "history.jsonl").unlink()
        stored.write(legacy=True)
    reads = []
    original = service.load_history_records

    def load(sid):
        reads.append(sid)
        return original(sid)

    monkeypatch.setattr(service, "load_history_records", load)
    snapshot = service.prefetch_session_history(stored.sid, history_before_request_id="current")
    assert snapshot is not None and len(snapshot.records) == 4
    restored = await service.warmup_session_context(deep_agent=stored.deep_agent,
        session_id=stored.sid, history_before_request_id="current", history_snapshot=snapshot)
    assert restored and reads == [stored.sid]
    messages = stored.engine.create_context.await_args.kwargs["history_messages"]
    assert [message.content for message in messages] == ["original question", "original answer"]


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["generation", "owner", "release", "file", "path"])
async def test_stale_snapshot_falls_back_to_actual_history(stored_history, monkeypatch, change):
    stored = stored_history
    if change == "path":
        (stored.folder / "history.jsonl").unlink()
        stored.write(legacy=True)
    snapshot = service.prefetch_session_history(stored.sid, history_before_request_id="current")
    assert snapshot is not None
    if change == "generation":
        save_state(stored.sid, generation=snapshot.generation + 1)
    elif change == "owner":
        save_state(stored.sid, runtime_owner={"pid": os.getpid(), "started_at": 0})
    elif change == "release":
        lc.release_runtime(stored.sid)
    else:
        changed = [*stored.records]
        changed[0] = {**changed[0], "content": "changed durable question"}
        stored.write(changed)
    reads = []
    original = service.load_history_records
    monkeypatch.setattr(service, "load_history_records", lambda sid: (reads.append(sid), original(sid))[1])
    assert await service.warmup_session_context(deep_agent=stored.deep_agent,
        session_id=stored.sid, history_before_request_id="current", history_snapshot=snapshot)
    assert reads == [stored.sid]
    messages = stored.engine.create_context.await_args.kwargs["history_messages"]
    expected = "changed durable question" if change in {"file", "path"} else "original question"
    assert messages[0].content == expected


@pytest.mark.parametrize("missing", ["owner", "receipt", "cutoff", "file"])
def test_missing_preparation_prerequisite_preserves_late_read(stored_history, missing):
    stored = stored_history
    boundary = "current"
    if missing == "owner":
        lc.release_runtime(stored.sid)
    elif missing == "receipt":
        stored.write(stored.records[:2])
    elif missing == "cutoff":
        boundary = None
    else:
        (stored.folder / "history.jsonl").unlink()
    assert service.prefetch_session_history(stored.sid, history_before_request_id=boundary) is None


@pytest.mark.asyncio
async def test_request_identity_mismatch_never_uses_other_prefix(stored_history):
    stored = stored_history
    snapshot = service.prefetch_session_history(stored.sid, history_before_request_id="current")
    assert await service.warmup_session_context(deep_agent=stored.deep_agent,
        session_id=stored.sid, history_before_request_id="later", history_snapshot=snapshot)
    messages = stored.engine.create_context.await_args.kwargs["history_messages"]
    assert [message.content for message in messages] == ["original question", "original answer", "current input"]


@pytest.mark.asyncio
async def test_same_session_queued_receipt_invalidates_snapshot_without_replaying_input(stored_history):
    stored = stored_history
    snapshot = service.prefetch_session_history(stored.sid, history_before_request_id="current")
    stored.write([*stored.records,
        {"role": "user", "request_id": "next-queued", "content": "another queued input"}])
    assert await service.warmup_session_context(deep_agent=stored.deep_agent,
        session_id=stored.sid, history_before_request_id="current", history_snapshot=snapshot)
    messages = stored.engine.create_context.await_args.kwargs["history_messages"]
    assert [message.content for message in messages] == ["original question", "original answer"]


@pytest.mark.asyncio
async def test_missing_current_receipt_keeps_history_that_arrives_during_assembly(stored_history):
    stored = stored_history
    stored.write(stored.records[:1])
    snapshot = service.prefetch_session_history(stored.sid, history_before_request_id="current")
    assert snapshot is None
    stored.write(stored.records)
    assert await service.warmup_session_context(deep_agent=stored.deep_agent,
        session_id=stored.sid, history_before_request_id="current", history_snapshot=snapshot)
    messages = stored.engine.create_context.await_args.kwargs["history_messages"]
    assert [message.content for message in messages] == ["original question", "original answer"]


@pytest.mark.asyncio
async def test_snapshot_keeps_completed_tool_call_and_result_in_original_order(stored_history):
    stored = stored_history
    tool_records = [
        stored.records[0],
        {"role": "assistant", "request_id": "old", "event_type": "chat.tool_call",
         "tool_call": {"name": "original_tool", "tool_call_id": "original-tool-id", "arguments": {"value": 1}}},
        {"role": "assistant", "request_id": "old", "event_type": "chat.tool_result",
         "tool_call_id": "original-tool-id", "rendered_result": "persisted tool result",
         "result": "unrendered compatibility result"},
        *stored.records[1:],
    ]
    stored.write(tool_records)
    snapshot = service.prefetch_session_history(stored.sid, history_before_request_id="current")
    assert snapshot is not None and len(snapshot.records) == 6
    assert await service.warmup_session_context(deep_agent=stored.deep_agent,
        session_id=stored.sid, history_before_request_id="current", history_snapshot=snapshot)
    messages = stored.engine.create_context.await_args.kwargs["history_messages"]
    assert [message.content for message in messages] == [
        "original question", "", "persisted tool result", "original answer",
    ]
    assert messages[1].tool_calls[0].id == messages[2].tool_call_id == "original-tool-id"


def test_lifecycle_fence_refuses_snapshot_without_changing_files(stored_history):
    stored = stored_history
    before = (stored.folder / "history.jsonl").read_bytes()
    save_state(stored.sid, blocked=True,
        operation={"status": "pending", "operation_id": "prefetch-blocked"})
    assert service.prefetch_session_history(stored.sid, history_before_request_id="current") is None
    assert (stored.folder / "history.jsonl").read_bytes() == before

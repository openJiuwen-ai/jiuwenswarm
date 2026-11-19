# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Request-local pre-reads retain the existing history and lifecycle contract."""
from __future__ import annotations

import asyncio
import json
import os
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.agents.harness.common import session_ops_service as service
from jiuwenswarm.server.runtime.session import lifecycle as lc
from jiuwenswarm.server.runtime.session import history_replay
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
    original = history_replay._read_prefix

    def load(path, *args, **kwargs):
        reads.append(path)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(history_replay, "_read_prefix", load)
    snapshot = service.prefetch_session_history(stored.sid, history_before_request_id="current")
    assert snapshot is not None and len(snapshot.records) == 4
    restored = await service.warmup_session_context(deep_agent=stored.deep_agent,
        session_id=stored.sid, history_before_request_id="current", history_snapshot=snapshot)
    assert restored and reads == [snapshot.path]
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
    original = service.read_history_replay
    monkeypatch.setattr(service, "read_history_replay", lambda sid, **kwargs: (reads.append(sid), original(sid, **kwargs))[1])
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


@pytest.mark.asyncio
async def test_queued_recovery_reads_large_history_only_after_admission(stored_history, monkeypatch):
    from jiuwenswarm.server.runtime.session import recovery_budget as recovery

    stored = stored_history
    budget = recovery._RecoveryBudget(capacity=1, max_waiters=1, wait_seconds=2)
    monkeypatch.setattr(recovery, "_budget", budget)
    stored.write([{**stored.records[0], "content": "x" * (256 * 1024)}, *stored.records[1:]])
    assert service.prefetch_session_history(stored.sid, history_before_request_id="current") is None
    reads = []
    read = service.read_history_replay

    def read_history(*args, **kwargs):
        reads.append(args[0])
        return read(*args, **kwargs)

    monkeypatch.setattr(service, "read_history_replay", read_history)
    async with recovery.admit_context_recovery("other"):
        task = asyncio.create_task(service.warmup_session_context(
            deep_agent=stored.deep_agent, session_id=stored.sid, history_before_request_id="current",
        ))
        async with asyncio.timeout(2):
            while not budget._waiters:
                await asyncio.sleep(0.001)
        assert not reads
        stored.write([{**stored.records[0], "content": "rewritten"}, *stored.records[1:]])
    assert await task
    assert reads == [stored.sid]
    assert stored.engine.create_context.await_args.kwargs["history_messages"][0].content == "rewritten"


@pytest.mark.asyncio
@pytest.mark.parametrize("error, code", [
    (MemoryError("checkpoint"), "HISTORY_RESTORE_RESOURCE"),
    (OSError("checkpoint"), "HISTORY_RESTORE_FAILED"),
])
async def test_first_checkpoint_probe_preserves_recovery_error(stored_history, monkeypatch, error, code):
    stored = stored_history

    def fail_read(_key):
        raise error

    session = SimpleNamespace(get_state=fail_read, get_session_id=lambda: stored.sid)
    monkeypatch.setattr(service, "resolve_live_agent_session", lambda *_args: session)
    with pytest.raises(service.HistoryReplayError) as failed:
        await service.warmup_session_context(deep_agent=stored.deep_agent, session_id=stored.sid)
    assert failed.value.code == code
    assert failed.value.__cause__ is error
    stored.engine.create_context.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("rows", ["empty", "current", "long_current", "warm"])
async def test_fresh_and_warm_sessions_bypass_saturated_recovery(stored_history, monkeypatch, rows):
    from jiuwenswarm.server.runtime.session import recovery_budget as recovery
    from jiuwenswarm.server.runtime.session import context_snapshot

    stored = stored_history
    budget = recovery._RecoveryBudget(capacity=1, max_waiters=0, wait_seconds=1)
    monkeypatch.setattr(recovery, "_budget", budget)
    session = SimpleNamespace(get_state=lambda _key: None, get_session_id=lambda: stored.sid)
    monkeypatch.setattr(service, "resolve_live_agent_session", lambda *_args: session)
    if rows == "warm":
        stored.engine.get_context = lambda **_kwargs: object()
    else:
        stored.write([] if rows == "empty" else [{
            "role": "user", "request_id": "now", "content": "x" * (256 * 1024 if rows == "long_current" else 1),
        }])
        def reject_read(*_args, **_kwargs):
            pytest.fail("first-turn preparation must not start a full replay")

        monkeypatch.setattr(service, "read_history_replay", reject_read)
        assert service.prefetch_session_history(stored.sid, history_before_request_id="now") is None
    async with recovery.admit_context_recovery("other"):
        assert not await asyncio.create_task(context_snapshot.restore_checkpoint_context(
            context_engine=stored.engine, session=session, processors=[], before_request_id="now",
        ))
        assert await asyncio.create_task(service.warmup_session_context(
            deep_agent=stored.deep_agent, session_id=stored.sid, history_before_request_id="now",
        )) is (rows == "warm")


_CURRENT_ROW = b'{"role":"user","request_id":"now","content":"input"}'


@pytest.mark.asyncio
@pytest.mark.parametrize("suffix, payload, outcome", [
    (".jsonl", b" \t\r\n\n", "bypass"),
    (".json", b"\xef\xbb\xbf [ \t ]\n", "bypass"),
    (".jsonl", b"\xef\xbb\xbf" + _CURRENT_ROW + b"\n", "bypass"),
    (".json", b"[\n" + _CURRENT_ROW + b"\n]\n", "bypass"),
    (".jsonl", b"\xef\xbb\xbf", "failed"),
    (".jsonl", b"\xef\xbb\xbf \r\n" + _CURRENT_ROW + b"\n", "failed"),
    (".jsonl", _CURRENT_ROW.replace(b',"content"', b',\n"content"'), "failed"),
    (".jsonl", _CURRENT_ROW + b"\n{broken", "failed"),
    (".jsonl", b'{"role":[],"request_id":"now"}\n', "failed"),
    (".json", b"[" + _CURRENT_ROW + b"]trailing garbage", "failed"),
    (".json", b"[]trailing garbage", "failed"),
    (".jsonl", _CURRENT_ROW + b'\n{"role":"user","request_id":"later"}\n', "read"),
])
async def test_empty_prefix_must_agree_with_authoritative_reader(stored_history, monkeypatch, suffix, payload, outcome):
    stored = stored_history
    (stored.folder / "history.jsonl").unlink()
    (stored.folder / ("history" + suffix)).write_bytes(payload)
    read = service.read_history_replay
    calls = []

    def counted_read(*args, **kwargs):
        calls.append(args[0])
        return read(*args, **kwargs)

    monkeypatch.setattr(service, "read_history_replay", counted_read)
    if outcome == "failed":
        with pytest.raises(service.HistoryReplayError) as reference:
            read(stored.sid, before_request_id="now")
        with pytest.raises(service.HistoryReplayError) as restored:
            await service.warmup_session_context(deep_agent=stored.deep_agent, session_id=stored.sid,
                                                 history_before_request_id="now")
        assert reference.value.code == restored.value.code == "HISTORY_RESTORE_FAILED"
    else:
        read(stored.sid, before_request_id="now")
        assert not await service.warmup_session_context(deep_agent=stored.deep_agent, session_id=stored.sid,
                                                       history_before_request_id="now")
    assert calls == ([] if outcome == "bypass" else [stored.sid])


def test_oversized_current_first_file_goes_to_replay_without_a_speculative_scan(stored_history, monkeypatch):
    from jiuwenswarm.server.runtime.session import history_stream
    from jiuwenswarm.server.runtime.session.history_replay import HistoryReplayLimits

    stored = stored_history
    path = stored.folder / "history.jsonl"
    with path.open("wb") as source:
        source.write(_CURRENT_ROW + b"\n")
        source.truncate(HistoryReplayLimits().max_record_bytes + 1)
    monkeypatch.setattr(history_stream, "iter_history_rows", lambda *_args, **_kwargs: pytest.fail("unbounded probe"))
    assert service._has_recovery_prefix(stored.sid, "now")


@pytest.mark.asyncio
async def test_full_read_pool_skips_prefetch_and_queues_the_real_adapter_restore(stored_history, monkeypatch):
    from jiuwenswarm.server.runtime.session import recovery_budget as recovery
    from tests.unit_tests.agentserver.test_session_history_parallel_prepare import child_adapter, router

    stored = stored_history
    child = child_adapter()
    child._instance = stored.deep_agent
    parent = router(monkeypatch, child)
    budget = recovery._RecoveryBudget(capacity=1, max_waiters=1, wait_seconds=10)
    monkeypatch.setattr(recovery, "_budget", budget)
    async with recovery.admit_context_recovery("holder"):
        with ExitStack() as slots:
            for _ in range(2):
                assert history_replay._READ_SLOTS.acquire(blocking=False)
                slots.callback(history_replay._READ_SLOTS.release)
            task = asyncio.create_task(parent._get_or_create_session_adapter(
                stored.sid, history_before_request_id="current",
            ))
            try:
                async with asyncio.timeout(3):
                    while not budget._waiters:
                        await asyncio.sleep(0.01)
            except BaseException:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                raise
    assert await task is child
    assert [m.content for m in stored.engine.create_context.await_args.kwargs["history_messages"]] == [
        "original question", "original answer",
    ]
    child.cleanup.assert_not_awaited()


@pytest.mark.asyncio
async def test_busy_lifecycle_fence_does_not_hold_a_speculative_reader_slot(stored_history):
    stored = stored_history
    with lc.resource_lock("session", stored.sid):
        assert await asyncio.wait_for(asyncio.to_thread(
            service.prefetch_session_history, stored.sid, history_before_request_id="current",
        ), 2) is None
        with ExitStack() as slots:
            for _ in range(2):
                assert history_replay._READ_SLOTS.acquire(blocking=False)
                slots.callback(history_replay._READ_SLOTS.release)
    assert service.prefetch_session_history(stored.sid, history_before_request_id="current") is not None


@pytest.mark.asyncio
async def test_bad_speculative_prefix_is_rechecked_by_the_admitted_restore(stored_history):
    stored = stored_history
    (stored.folder / "history.jsonl").write_bytes(b'{"role":"user","request_id":"old"}\n{incomplete')
    assert service.prefetch_session_history(stored.sid, history_before_request_id="current") is None
    with pytest.raises(history_replay.HistoryReplayError) as failed:
        await service.warmup_session_context(deep_agent=stored.deep_agent, session_id=stored.sid,
                                             history_before_request_id="current")
    assert failed.value.code == "HISTORY_RESTORE_FAILED"

# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Recovery stays bounded and wakes promptly at cancellation boundaries."""

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from types import SimpleNamespace

import pytest
from openjiuwen.core.context_engine import ContextEngine

from jiuwenswarm.agents.harness.common import session_ops_service as ops
from jiuwenswarm.server.runtime.session import lifecycle as lc
from jiuwenswarm.server.runtime.session.history_io import run_history_io
from jiuwenswarm.server.runtime.session.recovery_budget import LegacyLimitSource
from tests.unit_tests.server.test_archive_lifecycle import archive
from tests.unit_tests.test_context_migration import _completion
from tests.unit_tests.test_context_snapshots import native_session, stored


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["generation", "missing_cursor", "invalid_cursor", "state_error"])
async def test_invalid_cached_limit_rechecks_history_without_losing_context(stored, monkeypatch, invalid):
    await native_session(stored, monkeypatch)
    stored.write([
        {"role": "user", "request_id": "old", "content": "PERSISTED_QUESTION"},
        {"role": "assistant", "request_id": "old", "event_type": "chat.final", "content": "PERSISTED_ANSWER"},
        {"role": "user", "request_id": "current", "content": "CURRENT_INPUT"},
    ])
    original = stored.path.read_bytes()
    cursor = ops.read_history_replay(stored.sid, before_request_id="current").prefix_cursor
    source = LegacyLimitSource(stored.sid, "current", cursor)
    assert ops._legacy_limit_source_is_current(source)
    if invalid == "missing_cursor":
        source = SimpleNamespace(session_id=stored.sid, before_request_id="current")
    elif invalid == "invalid_cursor":
        source = LegacyLimitSource(stored.sid, "current", None)
    else:
        read_state = lc.state
        invalid_reads = 2 if invalid == "generation" else 1

        def state(kind, resource_id):
            nonlocal invalid_reads
            if invalid_reads and (kind, resource_id) == ("session", stored.sid):
                # The guard and the cached-limit check each read lifecycle state.
                invalid_reads -= 1
                if invalid == "state_error":
                    raise RuntimeError("speculative lifecycle read failed")
                return {**read_state(kind, resource_id), "generation": None}
            return read_state(kind, resource_id)

        monkeypatch.setattr(lc, "state", state)
    engine = ContextEngine()
    agent = SimpleNamespace(react_agent=SimpleNamespace(context_engine=engine), _interaction_session=stored.session)
    assert await ops.warmup_session_context(
        deep_agent=agent, session_id=stored.sid, history_before_request_id="current", prior_limit=source,
    )
    contents = [message.content for message in engine.get_context(session_id=stored.sid).get_messages()]
    assert contents == ["PERSISTED_QUESTION", "PERSISTED_ANSWER"]
    assert stored.path.read_bytes() == original


def test_cached_limit_validation_propagates_cancellation(tmp_path, monkeypatch):
    monkeypatch.setattr(lc, "get_agent_root_dir", lambda: tmp_path)

    def cancelled(*_args):
        raise asyncio.CancelledError

    monkeypatch.setattr(lc, "state", cancelled)
    source = LegacyLimitSource("cancelled", "current", SimpleNamespace(generation=0))
    with pytest.raises(asyncio.CancelledError):
        ops._legacy_limit_source_is_current(source)


@pytest.mark.asyncio
@pytest.mark.parametrize("precheck", ["prefix", "cached_limit"])
async def test_locked_recovery_prechecks_leave_shared_io_threads_available(tmp_path, monkeypatch, precheck):
    monkeypatch.setattr(lc, "get_agent_root_dir", lambda: tmp_path)
    monkeypatch.setattr(lc, "get_agent_sessions_dir", lambda: tmp_path / "sessions")
    session_ids = [f"locked-{index}" for index in range(20)]
    for sid in session_ids:
        path = tmp_path / "sessions" / sid / "history.jsonl"
        path.parent.mkdir(parents=True)
        path.write_text('{"role":"user","content":"old"}\n', encoding="utf-8")
    locked, release = threading.Event(), threading.Event()

    def hold_locks():
        with ExitStack() as stack:
            for sid in session_ids:
                stack.enter_context(lc.resource_lock("session", sid))
            locked.set()
            assert release.wait(5)

    owner = threading.Thread(target=hold_locks)
    owner.start()
    async with asyncio.timeout(2):
        while not locked.is_set():
            await asyncio.sleep(0.005)
    pool = ThreadPoolExecutor(max_workers=20)
    asyncio.get_running_loop().set_default_executor(pool)
    if precheck == "prefix":
        reads = [asyncio.create_task(run_history_io(ops._has_recovery_prefix, sid, "current"))
                 for sid in session_ids]
    else:
        reads = [asyncio.create_task(run_history_io(ops._legacy_limit_source_is_current,
                 SimpleNamespace(session_id=sid, cursor=SimpleNamespace(
                     generation=0, path=tmp_path / "sessions" / sid / "history.jsonl", identity=(),
                 )))) for sid in session_ids]
    try:
        # Every speculative check has a real file lock held by another thread.
        # An unrelated read must run before those locks are released.
        await asyncio.sleep(0.1)
        control = asyncio.create_task(run_history_io(lc.state, "session", "unrelated"))
        done, _ = await asyncio.wait({control}, timeout=0.5)
        assert done, "speculative recovery exhausted the shared I/O executor"
        assert await control == {}
        assert await asyncio.gather(*reads) == [precheck == "prefix"] * 20
    finally:
        release.set()
        owner.join(2)
        await asyncio.gather(*reads, return_exceptions=True)
        if "control" in locals():
            await control
        pool.shutdown(wait=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_at", ["claim", "update"])
async def test_cancelled_unarchive_registration_marks_failure_and_wakes_recovery(archive, monkeypatch, cancel_at):
    service, create, root, _ = archive
    create()
    original_history = (root / "sessions/sess_a/history.json").read_bytes()
    await service.session("sess_a", "archive", "web")
    service.start_recovery()
    # Let the recovery scan finish and enter its ordinary idle wait.
    await asyncio.sleep(0.15)
    entered, release = threading.Event(), threading.Event()
    original = lc.claim_operation if cancel_at == "claim" else lc.update
    stalled_once = False

    def stalled(*args, **kwargs):
        nonlocal stalled_once
        if not stalled_once and args[:2] == ("session", "sess_a"):
            stalled_once = True
            entered.set()
            assert release.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(lc, "claim_operation" if cancel_at == "claim" else "update", stalled)
    task = asyncio.create_task(service.session("sess_a", "unarchive", "web"))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        operation_id = lc.state("session", "sess_a")["operation"]["operation_id"]
        for _ in range(2):
            task.cancel()
            await asyncio.sleep(0)
        assert service._locks[("session", "sess_a")].locked()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        operation = lc.state("session", "sess_a")["operation"]
        assert operation["operation_id"] == operation_id
        assert operation["status"] == "failed"
        async with asyncio.timeout(3):
            while (lc.state("session", "sess_a")["operation"]["status"] != "completed"
                   or service._locks[("session", "sess_a")].locked()):
                await asyncio.sleep(0.02)
        await asyncio.gather(*list(service._finalizer_tasks))
        lc.guard("sess_a")
        assert (root / "sessions/sess_a/history.json").read_bytes() == original_history
        assert not service._locks[("session", "sess_a")].locked()
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("later_audit", ["receipt", "automatic_summary"])
async def test_repeated_terminal_audit_does_not_merge_independent_model_groups(stored, monkeypatch, later_audit):
    await native_session(stored, monkeypatch)
    records = [{"role": "assistant", "event_type": "context.compact_summary",
                "compact_metadata": {"trigger": "automatic"}, "content": "AUDIT_ONLY"},
               {"role": "user", "request_id": "cancelled", "content": "CANCELLED_INPUT"},
               {"role": "assistant", "request_id": "cancelled", "event_type": "chat.tool_call",
                "tool_call": {"name": "read", "tool_call_id": "orphan", "arguments": "{}"}},
               _completion("cancelled")]
    for index in range(1100):
        rid = f"turn-{index}"
        records.extend([
            {"role": "user", "request_id": rid, "content": f"USER_{index}"},
            {"role": "assistant", "request_id": rid, "event_type": "chat.final", "content": f"ANSWER_{index}"},
            _completion(rid, "success"),
        ])
    if later_audit == "receipt":
        records.append(_completion("cancelled"))
    else:
        records.append({"role": "assistant", "request_id": "cancelled", "event_type": "context.compact_summary",
                        "compact_metadata": {"trigger": "automatic"}, "content": "AUDIT_ONLY"})
    records.append({"role": "user", "request_id": "current", "content": "CURRENT_INPUT_MUST_BE_EXCLUDED"})
    stored.write(records)
    original = stored.path.read_bytes()
    engine = ContextEngine()
    create = engine.create_context

    async def create_context(**kwargs):
        context = await create(**kwargs)

        async def preserve_small_context(**_kwargs):
            return "not_compressed"

        monkeypatch.setattr(context, "compress_context", preserve_small_context)
        return context

    monkeypatch.setattr(engine, "create_context", create_context)
    agent = SimpleNamespace(react_agent=SimpleNamespace(context_engine=engine), _interaction_session=stored.session)
    try:
        assert await ops.warmup_session_context(
            deep_agent=agent, session_id=stored.sid, history_before_request_id="current",
        )
        contents = [message.content for message in engine.get_context(session_id=stored.sid).get_messages()]
        assert "CANCELLED_INPUT" in contents
        assert "USER_0" in contents and "USER_1099" in contents
        assert "ANSWER_0" in contents and "ANSWER_1099" in contents
        assert "CURRENT_INPUT_MUST_BE_EXCLUDED" not in contents
    finally:
        assert stored.path.read_bytes() == original

# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Cross-session queue notifications reach the target in state order."""

import asyncio

import pytest

from jiuwenswarm.server.runtime.session.session_message_service import (
    SessionMessageExecutionResult,
    SessionMessageService,
    SessionMessageSource,
)
from jiuwenswarm.server.runtime.session.session_message_store import SessionMessageStore


class _Admission:
    async def begin_session_message(self, session_id, run_id):
        pass

    async def end_session_message(self, session_id, run_id):
        pass


@pytest.mark.asyncio
async def test_queued_notification_precedes_running(tmp_path, monkeypatch):
    statuses = []
    finished = asyncio.Event()

    async def notify(record):
        statuses.append(record.status)
        if record.status == "succeeded":
            finished.set()

    async def execute(record):
        return SessionMessageExecutionResult(status="succeeded")

    service = SessionMessageService(
        store=SessionMessageStore(tmp_path / "messages.sqlite3"),
        admission=_Admission(),
        execute=execute,
        status_callback=notify,
        available=True,
    )
    monkeypatch.setattr(
        service,
        "_session_metadata",
        lambda session_id: {
            "session_id": session_id,
            "title": session_id,
            "user_id": "user-1",
            "channel_id": "web",
            "mode": "agent.code.normal",
        },
    )
    await service.send_message(
        SessionMessageSource(
            session_id="source-1",
            request_id="request-1",
            tool_call_id="call-1",
            idempotency_key="call-1",
            user_id="user-1",
        ),
        target_session_id="target-1",
        message="check",
    )
    await asyncio.wait_for(finished.wait(), timeout=1)

    assert statuses == ["queued", "running", "succeeded"]
    await service.stop()


@pytest.mark.asyncio
async def test_stalled_status_push_does_not_block_execution(tmp_path, monkeypatch):
    import jiuwenswarm.server.runtime.session.session_message_service as module

    monkeypatch.setattr(module, "_STATUS_PUSH_TIMEOUT_SECONDS", 0.02)
    executed = asyncio.Event()

    async def notify(_record):
        await asyncio.Event().wait()

    async def execute(_record):
        executed.set()
        return SessionMessageExecutionResult(status="succeeded")

    service = SessionMessageService(
        store=SessionMessageStore(tmp_path / "messages.sqlite3"),
        admission=_Admission(),
        execute=execute,
        status_callback=notify,
        available=True,
    )
    monkeypatch.setattr(
        service,
        "_session_metadata",
        lambda session_id: {
            "session_id": session_id,
            "title": session_id,
            "user_id": "user-1",
            "channel_id": "web",
            "mode": "agent.code.normal",
        },
    )
    await service.send_message(
        SessionMessageSource(
            session_id="source-1",
            request_id="request-1",
            tool_call_id="call-1",
            idempotency_key="call-1",
            user_id="user-1",
        ),
        target_session_id="target-1",
        message="check",
    )
    await asyncio.wait_for(executed.wait(), timeout=1)
    await service.stop()


@pytest.mark.asyncio
async def test_cancelled_sender_does_not_strand_accepted_message(tmp_path, monkeypatch):
    pushing_queued = asyncio.Event()
    executed = asyncio.Event()

    async def notify(record):
        if record.status == "queued":
            pushing_queued.set()
            await asyncio.Event().wait()

    async def execute(_record):
        executed.set()
        return SessionMessageExecutionResult(status="succeeded")

    service = SessionMessageService(
        store=SessionMessageStore(tmp_path / "messages.sqlite3"),
        admission=_Admission(),
        execute=execute,
        status_callback=notify,
        available=True,
    )
    monkeypatch.setattr(
        service,
        "_session_metadata",
        lambda session_id: {
            "session_id": session_id,
            "title": session_id,
            "user_id": "user-1",
            "channel_id": "web",
            "mode": "agent.code.normal",
        },
    )
    sender = asyncio.create_task(service.send_message(
        SessionMessageSource(
            session_id="source-1",
            request_id="request-1",
            tool_call_id="call-1",
            idempotency_key="call-1",
            user_id="user-1",
        ),
        target_session_id="target-1",
        message="check",
    ))
    await asyncio.wait_for(pushing_queued.wait(), timeout=1)
    sender.cancel()
    with pytest.raises(asyncio.CancelledError):
        await sender
    await asyncio.wait_for(executed.wait(), timeout=1)
    await service.stop()


@pytest.mark.asyncio
async def test_queued_snapshot_is_target_scoped_and_ordered(tmp_path, monkeypatch):
    async def execute(_record):
        raise AssertionError("queue must stay unavailable during snapshot")

    service = SessionMessageService(
        store=SessionMessageStore(tmp_path / "messages.sqlite3"),
        admission=_Admission(),
        execute=execute,
        available=False,
    )
    monkeypatch.setattr(
        service,
        "_session_metadata",
        lambda session_id: {
            "session_id": session_id,
            "title": session_id,
            "user_id": "user-1",
            "channel_id": "web",
            "mode": "agent.code.normal",
        },
    )
    for index in (1, 2):
        await service.send_message(
            SessionMessageSource(
                session_id="source-1",
                request_id=f"request-{index}",
                tool_call_id=f"call-{index}",
                idempotency_key=f"call-{index}",
                user_id="user-1",
            ),
            target_session_id="target-1",
            message=f"message-{index}",
        )
    records = await service.queued_for_target("target-1", "user-1")
    assert [record["content"] for record in records] == ["message-1", "message-2"]
    assert await service.queued_for_target("target-1", "other-user") == []
    assert await service.queued_for_target("source-1", "user-1") == []
    await service.stop()

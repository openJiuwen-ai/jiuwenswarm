# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for TaskTool synchronous-delegation event forwarding.

Issue #4861: the browser_agent runs as a synchronous task_tool subagent whose
stream chunks stay inside TaskTool, so Web clients saw no event frames for the
whole delegation. These tests pin the roster frames, the ``subagent_activity``
mirroring, the running heartbeat, and the failure isolation of the emitter.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, AsyncIterator

import pytest
from openjiuwen.harness.subagent_runtime import (
    SUBAGENT_ACTIVITY_EVENT_TYPE,
    SUBAGENT_UPDATED_EVENT_TYPE,
)
from openjiuwen.harness.tools.subagent.task_tool import TaskTool

from jiuwenswarm.server.runtime.agent_adapter import task_tool_events
from jiuwenswarm.server.runtime.agent_adapter.task_tool_events import (
    _ActivityForwardingSubagent,
    _SyncActivityEmitter,
    _invoke_with_event_forwarding,
    apply_task_tool_event_patch,
)
from jiuwenswarm.server.runtime.agent_adapter.managed_subagent_control import (
    active_cua_execution,
    bind_managed_task_request,
    reset_managed_task_request,
    bind_managed_task_session,
    cancel_cua_execution,
    has_active_cua,
    unbind_managed_task_session,
)


@pytest.fixture(autouse=True)
def _reset_event_counters() -> None:
    """Keep the module-level revision/seq registries isolated per test."""
    task_tool_events._REVISIONS.clear()
    task_tool_events._ACTIVITY_SEQS.clear()
    yield
    task_tool_events._REVISIONS.clear()
    task_tool_events._ACTIVITY_SEQS.clear()


class _RecordingSession:
    """Fake parent session that records every write_stream frame."""

    def __init__(self, *, always_fail: bool = False) -> None:
        self.frames: list[Any] = []
        self.write_attempts = 0
        self._always_fail = always_fail

    async def write_stream(self, data: Any) -> None:
        self.write_attempts += 1
        if self._always_fail:
            raise RuntimeError("stream closed")
        self.frames.append(data)


class _FakeSubagent:
    """Fake subagent exposing the stream/invoke/card surface TaskTool uses."""

    def __init__(self, chunks: list[Any] | None = None) -> None:
        self.card = SimpleNamespace(name="browser_agent", id="openjiuwen.browser_agent")
        self.marker = "inner-marker"
        self._chunks = chunks or []

    async def stream(self, inputs: Any, **kwargs: Any) -> AsyncIterator[Any]:
        for chunk in self._chunks:
            yield chunk

    async def invoke(self, inputs: Any, **kwargs: Any) -> Any:
        return {"output": "invoke-fallback"}


def _tool_call_chunk() -> dict[str, Any]:
    return {
        "type": "tool_call",
        "payload": {
            "tool_call": {
                "tool_name": "browser_navigate",
                "arguments": {"url": "https://example.com"},
            }
        },
    }


def _roster_payloads(session: _RecordingSession) -> list[dict[str, Any]]:
    return [
        frame.payload["subagent_updated"]
        for frame in session.frames
        if frame.type == SUBAGENT_UPDATED_EVENT_TYPE
    ]


def _activity_payloads(session: _RecordingSession) -> list[dict[str, Any]]:
    return [
        frame.payload["subagent_activity"]
        for frame in session.frames
        if frame.type == SUBAGENT_ACTIVITY_EVENT_TYPE
    ]


def _kwargs(session: _RecordingSession, **overrides: Any) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "parent_session": session,
        "sub_session_id": "sess_sub_browser_agent_ab12",
        "normalized_type": "browser_agent",
        "parent_session_id": "parent-sess-1",
        "task_description": "search the web",
    }
    kwargs.update(overrides)
    return kwargs


async def _consuming_original(task_tool: Any, subagent: Any, **kwargs: Any) -> Any:
    task_tool.received_subagent = subagent
    seen: list[Any] = []
    async for chunk in subagent.stream(None):
        seen.append(chunk)
    task_tool.seen_chunks = seen
    return {"output": "done"}


async def _failing_original(task_tool: Any, subagent: Any, **kwargs: Any) -> Any:
    raise RuntimeError("boom")


async def _slow_original(task_tool: Any, subagent: Any, **kwargs: Any) -> Any:
    await asyncio.sleep(0.08)
    return {"output": "done"}


async def _blocking_original(task_tool: Any, subagent: Any, **kwargs: Any) -> Any:
    task_tool.started.set()
    await asyncio.Future()


async def test_forwarding_emits_roster_and_activity_frames() -> None:
    session = _RecordingSession()
    chunks = [
        {"type": "llm_output", "payload": {"content": "partial"}},
        _tool_call_chunk(),
        {"type": "answer", "payload": {"content": "done"}},
    ]
    subagent = _FakeSubagent(chunks)
    task_tool: Any = SimpleNamespace()

    result = await _invoke_with_event_forwarding(
        task_tool, _consuming_original, subagent, _kwargs(session)
    )

    assert result == {"output": "done"}
    roster = _roster_payloads(session)
    activities = _activity_payloads(session)
    assert roster[0]["status"] == "running"
    assert roster[0]["turn_outcome"] is None
    assert roster[-1]["status"] == "idle"
    assert roster[-1]["turn_outcome"] == "completed"
    assert all(frame["subagent_id"] == "sess_sub_browser_agent_ab12" for frame in roster)
    assert all(frame["parent_session_id"] == "parent-sess-1" for frame in roster)
    assert len(activities) == 1
    activity = activities[0]
    assert activity["kind"] == "tool_call"
    assert activity["tool_name"] == "browser_navigate"
    assert activity["subagent_id"] == "sess_sub_browser_agent_ab12"
    assert activity["task_id"] == "sess_sub_browser_agent_ab12"
    assert activity["parent_session_id"] == "parent-sess-1"
    assert activity["seq"] == 1


async def test_forwarding_passes_chunks_through_unchanged() -> None:
    session = _RecordingSession()
    chunks = [_tool_call_chunk(), {"type": "answer", "payload": {}}]
    subagent = _FakeSubagent(chunks)
    task_tool: Any = SimpleNamespace()

    await _invoke_with_event_forwarding(
        task_tool, _consuming_original, subagent, _kwargs(session)
    )

    assert task_tool.seen_chunks == chunks
    assert task_tool.received_subagent is not subagent
    assert task_tool.received_subagent.card is subagent.card
    assert task_tool.received_subagent.marker == "inner-marker"


async def test_forwarding_flushes_thinking_tail() -> None:
    session = _RecordingSession()
    chunks = [
        {"type": "llm_reasoning", "payload": {"content": "thinking hard"}},
        {"type": "answer", "payload": {}},
    ]
    subagent = _FakeSubagent(chunks)
    task_tool: Any = SimpleNamespace()

    await _invoke_with_event_forwarding(
        task_tool, _consuming_original, subagent, _kwargs(session)
    )

    activities = _activity_payloads(session)
    thinking = [frame for frame in activities if frame["kind"] == "thinking"]
    assert len(thinking) == 1
    assert "thinking hard" in thinking[0]["summary"]


async def test_forwarding_emits_errored_roster_on_failure() -> None:
    session = _RecordingSession()
    subagent = _FakeSubagent([_tool_call_chunk()])
    task_tool: Any = SimpleNamespace()

    with pytest.raises(RuntimeError, match="boom"):
        await _invoke_with_event_forwarding(
            task_tool, _failing_original, subagent, _kwargs(session)
        )

    roster = _roster_payloads(session)
    assert roster[0]["status"] == "running"
    assert roster[-1]["turn_outcome"] == "failed"
    assert roster[-1]["error"]["message"] == "boom"


async def test_resume_delegation_continues_activity_seq() -> None:
    """A resumed delegation reuses the sub_session_id; seq must not restart.

    The Web client dedupes by ``subagent_id:task_id:seq``, so renumbering
    from 1 on resume would silently drop the resumed activities.
    """
    session = _RecordingSession()
    task_tool: Any = SimpleNamespace()
    kwargs = _kwargs(session)

    await _invoke_with_event_forwarding(
        task_tool, _consuming_original, _FakeSubagent([_tool_call_chunk()]), kwargs
    )
    await _invoke_with_event_forwarding(
        task_tool, _consuming_original, _FakeSubagent([_tool_call_chunk()]), kwargs
    )

    seqs = [frame["seq"] for frame in _activity_payloads(session)]
    assert seqs == [1, 2]


async def test_new_delegation_starts_activity_seq_from_one() -> None:
    """A fresh sub_session_id (new browser task) must start at seq 1 again."""
    session = _RecordingSession()
    task_tool: Any = SimpleNamespace()

    await _invoke_with_event_forwarding(
        task_tool,
        _consuming_original,
        _FakeSubagent([_tool_call_chunk()]),
        _kwargs(session),
    )
    await _invoke_with_event_forwarding(
        task_tool,
        _consuming_original,
        _FakeSubagent([_tool_call_chunk()]),
        _kwargs(session, sub_session_id="sess_sub_browser_agent_cd34"),
    )

    seqs = [frame["seq"] for frame in _activity_payloads(session)]
    assert seqs == [1, 1]


async def test_heartbeat_touches_roster_while_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(task_tool_events, "_HEARTBEAT_INTERVAL_S", 0.02)
    session = _RecordingSession()
    subagent = _FakeSubagent()
    task_tool: Any = SimpleNamespace()

    await _invoke_with_event_forwarding(
        task_tool, _slow_original, subagent, _kwargs(session)
    )
    settled = len(session.frames)
    await asyncio.sleep(0.06)

    roster = _roster_payloads(session)
    running_touches = roster[1:-1]
    assert len(running_touches) >= 1
    assert all(frame["status"] == "running" for frame in running_touches)
    revisions = [frame["revision"] for frame in roster]
    assert revisions == sorted(revisions)
    assert roster[-1]["turn_outcome"] == "completed"
    assert len(session.frames) == settled


async def test_missing_parent_session_keeps_original_subagent() -> None:
    session = _RecordingSession()
    subagent = _FakeSubagent()
    task_tool: Any = SimpleNamespace()

    result = await _invoke_with_event_forwarding(
        task_tool, _consuming_original, subagent, _kwargs(session, parent_session=None)
    )

    assert result == {"output": "done"}
    assert task_tool.received_subagent is subagent
    assert session.frames == []


async def test_emitter_disables_after_repeated_failures() -> None:
    session = _RecordingSession(always_fail=True)
    emitter = _SyncActivityEmitter(
        parent_session=session,
        parent_session_id="parent-sess-1",
        subagent_id="sess_sub_browser_agent_ab12",
    )

    for _ in range(5):
        await emitter.forward_chunk(_tool_call_chunk())
    await emitter.flush()

    assert emitter._disabled is True
    assert session.write_attempts == 3


async def test_proxy_delegates_unknown_attributes() -> None:
    subagent = _FakeSubagent()
    emitter = _SyncActivityEmitter(
        parent_session=_RecordingSession(),
        parent_session_id="parent-sess-1",
        subagent_id="sub-1",
    )

    proxy = _ActivityForwardingSubagent(subagent, emitter)

    assert proxy.card is subagent.card
    assert proxy.marker == "inner-marker"


def test_patch_applies_once_and_wraps_seam() -> None:
    apply_task_tool_event_patch()
    apply_task_tool_event_patch()

    assert TaskTool.roster_event_patch_applied is True
    assert TaskTool.roster_event_patch_mode == "dispatch_only"
    assert TaskTool._invoke_created_subagent.__name__ == "_invoke_created_subagent_with_events"


async def test_managed_cua_delegation_can_be_cancelled_by_request_id() -> None:
    session = _RecordingSession()
    task_tool: Any = SimpleNamespace(started=asyncio.Event())
    binding = bind_managed_task_session("parent-sess-1", "managed-request-1")
    try:
        invocation = asyncio.create_task(
            _invoke_with_event_forwarding(
                task_tool,
                _blocking_original,
                _FakeSubagent(),
                _kwargs(session, normalized_type="cua_agent"),
            )
        )
        await task_tool.started.wait()
        assert has_active_cua("managed-request-1")
        assert cancel_cua_execution("managed-request-1") is True
        with pytest.raises(asyncio.CancelledError):
            await invocation
    finally:
        unbind_managed_task_session(binding)
    assert not has_active_cua("managed-request-1")


async def test_non_cua_delegation_is_not_registered_for_special_cancel() -> None:
    session = _RecordingSession()
    task_tool: Any = SimpleNamespace()
    binding = bind_managed_task_session("parent-sess-1", "managed-request-2")
    try:
        await _invoke_with_event_forwarding(
            task_tool,
            _consuming_original,
            _FakeSubagent(),
            _kwargs(session, normalized_type="browser_agent"),
        )
    finally:
        unbind_managed_task_session(binding)

    assert not has_active_cua("managed-request-2")
    assert cancel_cua_execution("managed-request-2") is False


async def test_managed_session_registers_cua_when_scheduler_drops_context() -> None:
    session = _RecordingSession()
    task_tool: Any = SimpleNamespace(started=asyncio.Event())
    binding = bind_managed_task_session("parent-sess-1", "managed-request-3")
    invocation = asyncio.create_task(
        _invoke_with_event_forwarding(
            task_tool,
            _blocking_original,
            _FakeSubagent(),
            _kwargs(session, normalized_type="cua_agent"),
        ),
        context=None,
    )
    try:
        await task_tool.started.wait()
        assert has_active_cua("managed-request-3")
        invocation.cancel()
        with pytest.raises(asyncio.CancelledError):
            await invocation
    finally:
        unbind_managed_task_session(binding)

    assert not has_active_cua("managed-request-3")


async def test_two_cua_delegations_of_one_request_are_both_tracked_and_cancelled() -> None:
    from jiuwenswarm.server.runtime.agent_adapter.managed_subagent_control import (
        register_cua_execution,
        unregister_cua_execution,
    )

    started = [asyncio.Event(), asyncio.Event()]
    registrations = []

    async def run(index):
        registrations.append(register_cua_execution(
            parent_session_id="parent", sub_session_id=f"sub-{index}",
        ))
        started[index].set()
        await asyncio.sleep(60)

    binding = bind_managed_task_session("parent", "managed-request-many")

    first = asyncio.create_task(run(0))
    second = asyncio.create_task(run(1))
    await started[0].wait()
    await started[1].wait()
    unregister_cua_execution(registrations[1])
    still_tracked = has_active_cua("managed-request-many")
    unregister_cua_execution(registrations[0])
    assert still_tracked and not has_active_cua("managed-request-many")

    registrations.clear()
    first.cancel()
    second.cancel()
    await asyncio.gather(first, second, return_exceptions=True)

    started = [asyncio.Event(), asyncio.Event()]
    first = asyncio.create_task(run(0))
    second = asyncio.create_task(run(1))
    await started[0].wait()
    await started[1].wait()
    assert cancel_cua_execution("managed-request-many") is True
    await asyncio.wait({first, second}, timeout=1)
    both = first.cancelled() and second.cancelled()
    first.cancel()
    second.cancel()
    for registration in registrations:
        unregister_cua_execution(registration)
    unbind_managed_task_session(binding)
    assert both


async def test_repeated_cancel_preserves_all_cua_cleanup_tasks() -> None:
    from jiuwenswarm.server.runtime.agent_adapter.managed_subagent_control import (
        register_cua_execution,
        unregister_cua_execution,
    )

    started = [asyncio.Event(), asyncio.Event()]
    cleaning = [asyncio.Event(), asyncio.Event()]
    release = asyncio.Event()

    async def run(index):
        registration = register_cua_execution(parent_session_id="cleanup-parent", sub_session_id=f"child-{index}")
        started[index].set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning[index].set()
            try:
                await release.wait()
            finally:
                unregister_cua_execution(registration)

    binding = bind_managed_task_session("cleanup-parent", "cleanup-request")
    tasks = [asyncio.create_task(run(index)) for index in range(2)]
    try:
        await asyncio.gather(*(event.wait() for event in started))
        assert cancel_cua_execution("cleanup-request") is True
        await asyncio.gather(*(event.wait() for event in cleaning))
        assert has_active_cua("cleanup-request")
        assert cancel_cua_execution("cleanup-request") is True
        assert all(task.cancelling() == 1 and not task.done() for task in tasks)
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        assert all(task.cancelled() for task in tasks)
        assert not has_active_cua("cleanup-request")
    finally:
        release.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        unbind_managed_task_session(binding)

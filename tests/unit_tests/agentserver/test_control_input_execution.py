# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Continuation durability and lifetime contracts through the public facade."""
from __future__ import annotations

import asyncio
from contextlib import aclosing
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponseChunk
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.runtime.agent_adapter import interface


def resume(request_id="resume-1", question_id="question-1"):
    return AgentRequest(
        request_id=request_id, channel_id="web", session_id="same-session",
        req_method=ReqMethod.CHAT_SEND,
        params={"source": "ask_user_interrupt", "request_id": question_id,
                "answers": [{"selected_options": ["yes"]}], "mode": "agent"},
    )


@pytest.fixture
def make_facade(monkeypatch):
    def make(stream, append=None):
        writes, closed = [], []
        facade = interface.JiuWenSwarm.__new__(interface.JiuWenSwarm)
        facade._session_manager = SimpleNamespace(
            get_session_id=lambda sid: sid, close_all_sessions=AsyncMock(),
            close_session=AsyncMock(return_value=False), has_session_runtime=lambda sid: False,
        )
        facade._adapter_mode_for_request = lambda request: "agent"
        facade._build_inputs = lambda request: ({"query": request.params["answers"]}, "local", None)
        facade.reconcile_session_mcp = AsyncMock()

        async def adapter_stream(request, inputs):
            try:
                async for payload in stream(request):
                    yield AgentResponseChunk(
                        request_id=request.request_id, channel_id=request.channel_id,
                        payload=payload, is_complete=payload.get("event_type") == "chat.final",
                    )
            finally:
                closed.append(request.request_id)

        adapter = SimpleNamespace(process_message_stream_impl=adapter_stream, cleanup=AsyncMock())
        facade._adapter = adapter
        facade._ensure_adapter = lambda **kwargs: adapter
        monkeypatch.setattr(interface, "restore_chat_send_equipment_params", lambda *_: None)
        monkeypatch.setattr(interface, "compute_chat_send_mcp_needed", lambda *_: [])
        monkeypatch.setattr(interface, "append_history_record", append or (lambda **record: writes.append(record)))
        return facade, writes, closed, adapter
    return make


@pytest.mark.asyncio
async def test_control_boundaries_share_committer_and_keep_segment_order(make_facade):
    async def stream(request):
        yield {"event_type": "chat.delta", "content": "before", "output_order": {"sequence": 1}}
        yield {"event_type": "chat.tool_call", "tool_call": {"id": "tool-1"}}
        yield {"event_type": "chat.reasoning", "content": "thought"}
        yield {"event_type": "chat.delta", "content": "after"}
        yield {"event_type": "chat.ask_user_question", "request_id": "next-question"}
        yield {"event_type": "chat.delta", "content": "complete"}
        yield {"event_type": "chat.final", "content": "complete"}
        yield {"event_type": "chat.final", "content": "complete"}
        yield {"event_type": "chat.final", "content": ""}
    facade, writes, closed, adapter = make_facade(stream)
    chunks = [chunk async for chunk in facade.deliver_control_input(resume())]
    assert len(chunks) == 9
    assert [record["event_type"] for record in writes] == [
        "chat.ask_user_answer", "chat.final", "chat.tool_call", "chat.final",
        "chat.ask_user_question", "chat.final",
    ]
    assert [record["content"] for record in writes if record["event_type"] == "chat.final"] == [
        "before", "after", "complete",
    ]
    assert writes[0]["request_id"] == "question-1"
    assert all(record["request_id"] == "resume-1" for record in writes[1:])
    assert writes[1]["extra"]["output_order"] == {"sequence": 1}
    assert writes[3]["extra"]["reasoning_content"] == "thought"
    assert all((record["extra"] or {}).get("source") != "ask_user_interrupt" for record in writes[1:])
    assert closed == ["resume-1"] and not facade._control_input_executions
    adapter.cleanup.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["close", "error", "exhaust"])
async def test_partial_output_uses_normal_end_fallback_without_finalizing_aborts(make_facade, ending):
    async def stream(request):
        yield {"event_type": "chat.delta", "content": "accepted partial"}
        if ending == "error":
            raise ValueError("sdk failed")
    facade, writes, closed, _adapter = make_facade(stream)
    async with aclosing(facade.deliver_control_input(resume())) as output:
        assert (await anext(output)).payload["content"] == "accepted partial"
        if ending == "error":
            with pytest.raises(ValueError, match="sdk failed"):
                await anext(output)
        elif ending == "exhaust":
            assert [chunk async for chunk in output] == []
    expected = [("chat.ask_user_answer", "")]
    if ending == "exhaust":
        expected.append(("chat.final", "accepted partial"))
    assert [(record["event_type"], record["content"]) for record in writes] == expected
    assert closed == ["resume-1"] and not facade._control_input_executions


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["chat.tool_call", "chat.final", "chat.ask_user_question"])
async def test_failed_output_boundary_is_not_acknowledged_and_owner_is_retryable(make_facade, boundary):
    writes, fail = [], True

    def append(**record):
        if fail and record["event_type"] == boundary:
            raise OSError("disk full")
        writes.append(record)

    async def stream(request):
        yield {"event_type": "chat.delta", "content": "before tool"}
        yield {"event_type": boundary, "content": "before tool" if boundary == "chat.final" else "",
               "tool_call": {"id": "once"}}
        yield {"event_type": "chat.final", "content": "never observed"}
    facade, _unused, closed, adapter = make_facade(stream, append)
    output = facade.deliver_control_input(resume())
    assert (await anext(output)).payload["event_type"] == "chat.delta"
    with pytest.raises(OSError, match="disk full"):
        await anext(output)
    assert closed == ["resume-1"]
    assert len(facade._control_input_executions) == 1
    assert facade.has_session_runtime("same-session")
    with pytest.raises(OSError, match="disk full"):
        await facade.cleanup()
    assert facade._adapter is adapter
    adapter.cleanup.assert_not_awaited()
    fail = False
    await facade.cleanup()
    assert facade._adapter is None and not facade._control_input_executions
    adapter.cleanup.assert_awaited_once()
    expected = ["chat.ask_user_answer", "chat.final"]
    if boundary != "chat.final":
        expected.append(boundary)
    assert [record["event_type"] for record in writes] == expected
    assert writes[1]["content"] == "before tool"


@pytest.mark.asyncio
async def test_final_write_repeated_cancellation_waits_before_cleanup_and_keeps_one_record(make_facade):
    writes, seen = [], []
    entered, release = threading.Event(), threading.Event()

    def append(**record):
        if record["event_type"] == "chat.final":
            entered.set()
            assert release.wait(5)
        writes.append(record)

    async def stream(request):
        yield {"event_type": "chat.delta", "content": "answer"}
        yield {"event_type": "chat.final", "content": "answer"}
    facade, _unused, closed, _adapter = make_facade(stream, append)

    async def collect():
        async for chunk in facade.deliver_control_input(resume()):
            seen.append(chunk.payload["event_type"])
    task = asyncio.create_task(collect())
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done() and closed == []
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 3)
    finally:
        release.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert seen == ["chat.delta"]
    assert [(record["event_type"], record["content"]) for record in writes] == [
        ("chat.ask_user_answer", ""), ("chat.final", "answer"),
    ]
    assert closed == ["resume-1"] and not facade._control_input_executions


@pytest.mark.asyncio
async def test_consecutive_answers_keep_one_sdk_session_and_distinct_history_owners(make_facade):
    async def stream(request):
        yield {"event_type": "chat.delta", "content": request.request_id}
        yield {"event_type": "chat.final", "content": ""}
    facade, writes, closed, adapter = make_facade(stream)
    for index in (1, 2):
        awaitables = facade.deliver_control_input(resume(f"resume-{index}", f"question-{index}"))
        assert len([chunk async for chunk in awaitables]) == 2
        assert facade._adapter is adapter
    assert [(record["request_id"], record["content"]) for record in writes] == [
        ("question-1", ""), ("resume-1", "resume-1"),
        ("question-2", ""), ("resume-2", "resume-2"),
    ]
    assert closed == ["resume-1", "resume-2"]
    assert not facade._control_input_executions


@pytest.mark.asyncio
async def test_failed_inner_close_retains_owner_and_partial_history_until_retry(make_facade):
    async def unused_stream(request):
        if False:
            yield {}
    facade, writes, _closed, adapter = make_facade(unused_stream)

    class Stream:
        sent = False
        attempts = 0

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self.sent:
                raise StopAsyncIteration
            self.sent = True
            return AgentResponseChunk(
                request_id="resume-1", channel_id="web",
                payload={"event_type": "chat.delta", "content": "partial"},
            )

        async def aclose(self):
            self.attempts += 1
            if self.attempts == 1:
                raise OSError("sdk close failed")
    inner = Stream()
    adapter.process_message_stream_impl = lambda request, inputs: inner
    with pytest.raises(OSError, match="sdk close failed"):
        _chunks = [chunk async for chunk in facade.deliver_control_input(resume())]
    assert len(facade._control_input_executions) == 1
    assert [record["event_type"] for record in writes] == ["chat.ask_user_answer"]
    adapter.cleanup.assert_not_awaited()
    await facade.cleanup()
    assert inner.attempts == 2 and not facade._control_input_executions
    assert [(record["event_type"], record["content"]) for record in writes] == [
        ("chat.ask_user_answer", ""), ("chat.final", "partial"),
    ]
    adapter.cleanup.assert_awaited_once()


@pytest.mark.asyncio
async def test_cleanup_cancels_running_control_owner_before_closing_adapter(make_facade):
    entered = asyncio.Event()

    async def stream(request):
        yield {"event_type": "chat.delta", "content": "partial"}
        entered.set()
        await asyncio.Event().wait()
    facade, writes, closed, adapter = make_facade(stream)

    async def collect():
        return [chunk async for chunk in facade.deliver_control_input(resume())]
    task = asyncio.create_task(collect())
    try:
        await asyncio.wait_for(entered.wait(), 2)
        await asyncio.wait_for(facade.cleanup(), 2)
        assert task.cancelled()
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert not facade._control_input_executions and closed == ["resume-1"]
    assert [record["content"] for record in writes] == [""]
    adapter.cleanup.assert_awaited_once()


@pytest.mark.asyncio
async def test_retry_from_another_task_does_not_cancel_the_finished_control_caller(make_facade):
    fail, writes = True, []

    def append(**record):
        if fail and record["event_type"] == "chat.final":
            raise OSError("disk full")
        writes.append(record)

    async def stream(request):
        yield {"event_type": "chat.delta", "content": "partial"}
    facade, _unused, _closed, _adapter = make_facade(stream, append)
    with pytest.raises(OSError, match="disk full"):
        _chunks = [chunk async for chunk in facade.deliver_control_input(resume())]
    fail = False
    await asyncio.wait_for(asyncio.create_task(facade.cleanup()), 2)
    assert not facade._control_input_executions
    assert writes[-1]["content"] == "partial"


@pytest.mark.asyncio
async def test_goal_intermediate_partial_is_preserved_when_control_consumer_closes(make_facade):
    async def stream(request):
        yield {"event_type": "chat.delta", "content": "goal progress", "goal_intermediate": True}
        await asyncio.Event().wait()
    facade, writes, closed, _adapter = make_facade(stream)
    async with aclosing(facade.deliver_control_input(resume())) as output:
        assert (await anext(output)).payload["goal_intermediate"] is True
    assert [(record["event_type"], record["content"]) for record in writes] == [
        ("chat.ask_user_answer", ""), ("chat.final", "goal progress"),
    ]
    assert closed == ["resume-1"] and not facade._control_input_executions

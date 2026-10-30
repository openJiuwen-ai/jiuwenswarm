"""Continuation handoff retains one owner for every still-pending control."""

import asyncio

import pytest

from jiuwenswarm.runtime.session import RuntimeSessionCoordinator, SessionWorkKind
from jiuwenswarm.runtime.session.model import SessionExecutionState
from tests.unit_tests.runtime.test_runtime_session_coordinator import _register, _park_on_cards
from tests.unit_tests.runtime.test_stream_interaction_answer import (
    SESSION_ID, _answer, _chunk, _collect, _request, harness, isolated_agent_dirs,
)


async def _respond(coordinator, card, *reissued, streaming=True):
    if streaming:
        async def output():
            for item in reissued:
                yield item
        return [item async for item in coordinator.deliver_control_stream(
            "session-a", card, output, suspension_key=lambda item: item,
        )]
    return await coordinator.deliver_control(
        "session-a", card, lambda: asyncio.sleep(0, result=list(reissued)),
        suspension_key=lambda item: item,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("first_answer", ["question_1", "question_2"])
async def test_completed_reissued_question_cannot_reenter_agent(harness, first_answer):
    h = harness
    published, finish = asyncio.Event(), asyncio.Event()
    delivered_ids = []
    remaining = "question_2" if first_answer == "question_1" else "question_1"

    async def original(request):
        for question in ("question_1", "question_2"):
            yield _chunk(request, "chat.ask_user_question", request_id=question,
                         source="permission_interrupt")
        published.set()
        await finish.wait()

    async def delivered(request):
        target = request.params["request_id"]
        delivered_ids.append(target)
        if target == first_answer:
            yield _chunk(request, "chat.ask_user_question", request_id=remaining,
                         source="permission_interrupt")
        else:
            yield _chunk(request, "chat.final", content="completed")

    h.agent.process_message_stream = original
    h.agent.deliver_control_input = delivered
    consumer = asyncio.create_task(_collect(h.runtime.stream(_request(), trigger_hook=False)))
    try:
        await asyncio.wait_for(published.wait(), 1)
        await _collect(h.client.stream_interaction_answer(_answer(first_answer, source="permission_interrupt")))
        finish.set()
        await asyncio.wait_for(consumer, 1)
        await _collect(h.client.stream_interaction_answer(_answer(remaining, source="permission_interrupt")))
        assert all(item.state.terminal for item in h.coordinator.snapshot_session(SESSION_ID).executions)
        with pytest.raises(RuntimeError, match="no active execution"):
            await _collect(h.client.stream_interaction_answer(_answer(remaining, source="permission_interrupt")))
        assert delivered_ids == [first_answer, remaining]
    finally:
        finish.set()
        if not consumer.done():
            consumer.cancel()
            await asyncio.gather(consumer, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_handoff_preserves_an_unreissued_pending_control(streaming):
    coordinator = RuntimeSessionCoordinator()
    await _register(coordinator)
    try:
        await _park_on_cards(coordinator, "card-a", "card-b", "card-c")
        await _respond(coordinator, "card-a", "card-b", streaming=streaming)
        assert coordinator.has_control_target("session-a", "card-c")
        await _respond(coordinator, "card-b", streaming=streaming)
        assert not coordinator.has_control_target("session-a", "card-b")
        await _respond(coordinator, "card-c", streaming=streaming)
        assert all(item.state.terminal for item in coordinator.snapshot_session("session-a").executions)
    finally:
        await coordinator.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("second_streaming", [False, True])
async def test_answer_before_previous_control_eof_releases_original_owner(second_streaming):
    coordinator = RuntimeSessionCoordinator()
    await _register(coordinator)
    published, finish_original, finish_first = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def original():
        yield "card-a"
        yield "card-b"
        published.set()
        await finish_original.wait()

    async def consume_original():
        return [item async for item in coordinator.run_stream(
            "session-a", "original", SessionWorkKind.CHAT_STREAM, original,
            suspension_key=lambda item: item,
        )]

    async def first():
        yield "card-b"
        await finish_first.wait()

    consumer = asyncio.create_task(consume_original())
    continuation = None
    try:
        await asyncio.wait_for(published.wait(), 1)
        continuation = coordinator.deliver_control_stream(
            "session-a", "card-a", first, suspension_key=lambda item: item,
        )
        assert await anext(continuation) == "card-b"
        await _respond(coordinator, "card-b", streaming=second_streaming)
        finish_first.set()
        assert [item async for item in continuation] == []
        assert not coordinator.has_control_target("session-a", "card-b")
        finish_original.set()
        assert await asyncio.wait_for(consumer, 1) == ["card-a", "card-b"]
        assert all(item.state.terminal for item in coordinator.snapshot_session("session-a").executions)
    finally:
        finish_original.set()
        finish_first.set()
        if continuation is not None:
            await continuation.aclose()
        await coordinator.close()
        await asyncio.gather(consumer, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("retry", ["card-a", "card-b"])
async def test_child_creation_failure_releases_claims_and_keeps_parent_waiting(monkeypatch, streaming, retry):
    coordinator = RuntimeSessionCoordinator()
    await _register(coordinator)
    try:
        await _park_on_cards(coordinator, "card-a", "card-b")
        create = coordinator._new_execution

        def fail(*_args, **_kwargs):
            raise ValueError("registry admission failed")

        monkeypatch.setattr(coordinator, "_new_execution", fail)
        with pytest.raises(ValueError, match="registry admission failed"):
            await _respond(coordinator, "card-a", streaming=streaming)
        parent = coordinator.snapshot_session("session-a").executions[0]
        assert parent.state is SessionExecutionState.WAITING_FOR_CONTROL
        assert coordinator.has_control_target("session-a", "card-a")
        assert coordinator.has_control_target("session-a", "card-b")
        monkeypatch.setattr(coordinator, "_new_execution", create)
        other = "card-b" if retry == "card-a" else "card-a"
        await _respond(coordinator, retry, other, streaming=streaming)
        await _respond(coordinator, other, streaming=streaming)
        assert all(item.state.terminal for item in coordinator.snapshot_session("session-a").executions)
    finally:
        await coordinator.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancelled", [False, True])
async def test_unsuccessful_stream_handoff_preserves_parent_for_retry(cancelled):
    coordinator = RuntimeSessionCoordinator()
    await _register(coordinator)

    async def fail():
        yield "card-b"
        if cancelled:
            raise asyncio.CancelledError()
        raise ValueError("delivery failed")

    try:
        await _park_on_cards(coordinator, "card-a", "card-b")
        error = asyncio.CancelledError if cancelled else ValueError
        with pytest.raises(error):
            _ = [item async for item in coordinator.deliver_control_stream(
                "session-a", "card-a", fail, suspension_key=lambda item: item,
            )]
        assert coordinator.has_control_target("session-a", "card-a")
        assert coordinator.has_control_target("session-a", "card-b")
        await _respond(coordinator, "card-a", "card-b")
        await _respond(coordinator, "card-b")
        assert all(item.state.terminal for item in coordinator.snapshot_session("session-a").executions)
    finally:
        await coordinator.close()

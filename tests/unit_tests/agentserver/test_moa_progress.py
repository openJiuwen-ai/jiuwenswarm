# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Optional MoA notices cannot control model results or cancellation."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from openjiuwen.core.session import with_session
from openjiuwen.harness.moa import MoAConfig
from openjiuwen.harness.moa.consultation import consult

from jiuwenswarm.server.runtime.moa_progress import (
    MoaProgress, ProgressJevSystemOneClient, ProgressLlmVerifierScorer, ReferenceProgressModel,
)


def _progress_with_delivery(monkeypatch, delivery):
    progress = MoaProgress()
    mailbox = progress.open()
    if delivery == "full":
        for index in range(64):
            mailbox.record(str(index), "reference", "started")
            mailbox.record(str(index), "reference", "completed")
        assert len(mailbox.pending) == 128
    elif delivery == "failed":
        def fail(*_args):
            raise ValueError("notification unavailable")
        monkeypatch.setattr(mailbox, "record", fail)
    elif delivery == "closed":
        progress.close()
    return progress, mailbox


@pytest.mark.parametrize("error,terminal", [(ValueError("private failure"), "failed"), (asyncio.CancelledError(), "cancelled")])
@pytest.mark.asyncio
async def test_reference_preserves_original_failure_or_cancellation(error, terminal):
    progress = MoaProgress()
    mailbox = progress.open()
    model = ReferenceProgressModel(SimpleNamespace(invoke=AsyncMock(side_effect=error)), progress)
    with pytest.raises(type(error)) as caught:
        await model.invoke(messages=[])
    assert caught.value is error
    assert [event["status"] for event in mailbox.pending] == ["started", terminal]


@pytest.mark.parametrize("delivery", ["unconsumed", "full", "failed", "closed"])
@pytest.mark.asyncio
async def test_reference_result_survives_unavailable_notices(monkeypatch, delivery):
    progress, _mailbox = _progress_with_delivery(monkeypatch, delivery)
    original = SimpleNamespace(invoke=AsyncMock(return_value=SimpleNamespace(content="valid advice")))
    model = ReferenceProgressModel(original, progress)
    session = SimpleNamespace(write_stream=AsyncMock(side_effect=AssertionError("model must not write notices")))

    @with_session()
    async def invoke(session):
        return await consult(MoAConfig(reference_models=(model,), reference_timeout=0.2), [])

    sources, successes = await invoke(session=session)
    assert successes == 1
    assert sources[0].text == "valid advice"
    original.invoke.assert_awaited_once()
    session.write_stream.assert_not_called()


@pytest.mark.asyncio
async def test_cancellation_does_not_wait_for_notice_delivery():
    progress = MoaProgress()
    mailbox = progress.open()
    entered = asyncio.Event()
    exited = asyncio.Event()

    async def respond(messages, tools):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            exited.set()

    task = asyncio.create_task(ReferenceProgressModel(SimpleNamespace(invoke=respond), progress).invoke(messages=[]))
    await asyncio.wait_for(entered.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 1)
    assert exited.is_set()
    assert [event["status"] for event in mailbox.pending] == ["started", "cancelled"]


@pytest.mark.asyncio
async def test_late_completion_cannot_enter_next_execution():
    progress = MoaProgress()
    old = progress.open()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def respond(messages, tools):
        entered.set()
        await release.wait()
        return "answer"

    task = asyncio.create_task(ReferenceProgressModel(SimpleNamespace(invoke=respond), progress).invoke(messages=[]))
    await asyncio.wait_for(entered.wait(), 1)
    progress.close()
    current = progress.open()
    release.set()
    assert await task == "answer"
    assert old.closed and not old.pending
    assert not current.pending


@pytest.mark.asyncio
async def test_parallel_operations_reserve_terminal_capacity():
    progress = MoaProgress()
    mailbox = progress.open()
    entered = 0
    ready = asyncio.Event()
    release = asyncio.Event()

    async def respond(messages, tools):
        nonlocal entered
        entered += 1
        if entered == 100:
            ready.set()
        await release.wait()
        return "answer"

    model = ReferenceProgressModel(SimpleNamespace(invoke=respond), progress)
    tasks = [asyncio.create_task(model.invoke(messages=[])) for _ in range(100)]
    try:
        await asyncio.wait_for(ready.wait(), 1)
        assert len(mailbox.pending) == 64
    finally:
        release.set()
        results = await asyncio.gather(*tasks)
    assert results == ["answer"] * 100
    assert len(mailbox.pending) == 128
    starts = [event["operation_id"] for event in mailbox.pending if event["status"] == "started"]
    ends = [event["operation_id"] for event in mailbox.pending if event["status"] == "completed"]
    assert len(set(starts)) == 64
    assert set(starts) == set(ends)


@pytest.mark.parametrize("delivery", ["unconsumed", "full", "failed", "closed"])
@pytest.mark.asyncio
async def test_jev_valid_decision_survives_unavailable_notifications(monkeypatch, delivery):
    from openjiuwen.core.foundation.llm.system_one import JevSystemOneClient, SystemOneResponse
    from openjiuwen.harness.moa import JevConfig
    from openjiuwen.harness.moa.state import MoAState
    from openjiuwen.harness.moa.verifier.jev_verifier.verifier import decide_reference

    response = SystemOneResponse.model_validate({
        "model": "mock-jev", "usage": {"input_tokens": 0, "output_tokens": 0},
        "answers": {"consult_reference": {
            "type": "choice", "choice": "CONSULT", "confidence": 0.9,
            "probabilities": {"CONSULT": 0.9, "CONTINUE": 0.1},
        }},
    })
    call = AsyncMock(return_value=response)
    monkeypatch.setattr(JevSystemOneClient, "system_one", call)
    client = object.__new__(ProgressJevSystemOneClient)
    client.progress, mailbox = _progress_with_delivery(monkeypatch, delivery)
    state = MoAState()
    assert await decide_reference(JevConfig(client=client, timeout=0.2), state, [], None, None) is True
    assert state.last_jev_reason == "consult"
    assert state.jev_calls == 1
    call.assert_awaited_once()
    if delivery == "unconsumed":
        assert [event["status"] for event in mailbox.pending] == ["started", "completed"]


@pytest.mark.parametrize("delivery", ["unconsumed", "full", "failed", "closed"])
@pytest.mark.asyncio
async def test_llm_score_and_checkpoint_inputs_are_unchanged(monkeypatch, delivery):
    from openjiuwen.harness.moa.state import MoAState
    from openjiuwen.harness.moa.verifier.llm_verifier.scorer import score_progress

    tracker = SimpleNamespace(update=AsyncMock(return_value=0.6), result=lambda: None)
    repeats = []
    def factory(problem, n_evaluations):
        repeats.append(n_evaluations)
        return tracker
    scorer = ProgressLlmVerifierScorer(tracker_factory=factory)
    scorer.progress, _mailbox = _progress_with_delivery(monkeypatch, delivery)
    config = MoAConfig(reference_models=(object(),), mode="verifier_routed", scorer=scorer)
    state = MoAState(problem="task", steps=["first"])
    assert await score_progress(config, state) == 0.6
    state.steps.append("second")
    assert await score_progress(config, state) == 0.6
    assert repeats == [3]
    assert [call.args for call in tracker.update.await_args_list] == [("first",), ("second",)]


@pytest.mark.asyncio
async def test_stream_delivers_hints_while_source_waits_and_releases_reader():
    progress = MoaProgress()
    mailbox = progress.open()
    entered = asyncio.Event()
    released = asyncio.Event()
    stopped = asyncio.Event()

    async def source():
        entered.set()
        try:
            await released.wait()
            yield "ordinary output"
            await asyncio.Event().wait()
        finally:
            stopped.set()

    stream = progress.stream(source())
    first = asyncio.create_task(anext(stream))
    await asyncio.wait_for(entered.wait(), 1)
    model = ReferenceProgressModel(SimpleNamespace(invoke=AsyncMock(return_value="answer")), progress)
    assert await model.invoke(messages=[]) == "answer"
    assert (await asyncio.wait_for(first, 1)).payload["status"] == "started"
    assert (await anext(stream)).payload["status"] == "completed"
    released.set()
    assert await anext(stream) == "ordinary output"
    await stream.aclose()
    assert stopped.is_set()
    assert mailbox.closed and progress.mailbox is None


@pytest.mark.asyncio
async def test_source_error_is_preserved_and_mailbox_is_closed():
    progress = MoaProgress()
    error = ValueError("source failed")
    async def source():
        yield "before error"
        raise error
    stream = progress.stream(source())
    assert await anext(stream) == "before error"
    with pytest.raises(ValueError) as caught:
        await anext(stream)
    assert caught.value is error
    assert progress.mailbox is None

# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Hidden drafts remain diagnostic events, not visible answers or completion."""

import io
from types import SimpleNamespace

import pytest

from jiuwenswarm.channels.process_cli.duplex_control import DuplexController
from jiuwenswarm.channels.process_cli.machine_io import OneShotWriter
from jiuwenswarm.channels.process_cli.machine import _MachineRun
from jiuwenswarm.channels.process_cli.machine_result import RunSummary
from jiuwenswarm.channels.process_cli.protocol import OneShotRunInput
from jiuwenswarm.channels.process_cli.render import EventRenderer
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.runtime.events import RuntimeEvent


def _event(event_type, content="", **payload):
    return RuntimeEvent("run", "process_cli", "session", {"event_type": event_type, "content": content, **payload})


def _drafts_and_final():
    return [
        _event("chat.delta", "HIDDEN_9801", output_suppressed=True),
        _event("chat.final", "IMAGE_DRAFT", output_suppressed=True),
        _event("chat.reasoning", "HIDDEN_REASONING", output_suppressed=True),
        _event("chat.final", "9801 + PROOF-X", delivery_reconciled=True),
    ]


@pytest.mark.parametrize("output_format", ["human", "json", "jsonl"])
def test_hidden_drafts_do_not_hide_reconciled_human_answer_or_remove_diagnostics(output_format):
    stdout, stderr = io.StringIO(), io.StringIO()
    renderer = EventRenderer(output_format, stdout=stdout, stderr=stderr, show_reasoning=True)
    for event in _drafts_and_final():
        renderer.render(event)
    renderer.finish(session_id="session", request_id="run")
    assert len(renderer.events) == 4
    assert renderer.events[0]["payload"]["output_suppressed"] is True
    if output_format == "human":
        assert stdout.getvalue().strip() == "9801 + PROOF-X"
        assert "HIDDEN_REASONING" not in stderr.getvalue()
    else:
        assert "HIDDEN_9801" in stdout.getvalue() and "IMAGE_DRAFT" in stdout.getvalue()


def test_final_aggregation_uses_only_reconciled_visible_answer():
    summary = RunSummary()
    for event in _drafts_and_final():
        summary.observe(event)
    assert summary.output == summary.final_answer == "9801 + PROOF-X"
    cancelled = RunSummary()
    cancelled.observe(_event("chat.delta", "9801", output_suppressed=True))
    cancelled.observe(_event("chat.final", "15", delivery_reconciled=True))
    assert cancelled.output == cancelled.final_answer == "15"


def test_error_is_still_rendered_and_aggregated_with_a_suppression_flag():
    event = _event("chat.error", "Delivery failed", code="DELIVERY_FAILED", output_suppressed=True)
    summary = RunSummary()
    summary.observe(event)
    assert summary.error.code == "DELIVERY_FAILED"
    stdout, stderr = io.StringIO(), io.StringIO()
    renderer = EventRenderer("human", stdout=stdout, stderr=stderr)
    renderer.render(event)
    assert renderer.failed and "Delivery failed" in stderr.getvalue()


@pytest.mark.asyncio
@pytest.mark.parametrize("visible_final", [False, True])
async def test_suppressed_final_cannot_complete_duplex_execution(visible_final):
    hidden = _event("chat.final", "IMAGE_DRAFT", output_suppressed=True)
    hidden.is_complete = True
    hidden.runtime_completion = "completed"
    observed = []

    async def stream():
        yield hidden
        if visible_final:
            yield _event("chat.final", "9801 + PROOF-X", delivery_reconciled=True)

    control = DuplexController(None, OneShotWriter(io.StringIO(), request_id="run"))
    try:
        completed = await control.consume(
            stream(), client=SimpleNamespace(),
            request=AgentRequest(request_id="run", session_id="session", channel_id="process_cli"),
            observe=observed.append,
        )
        assert completed is visible_final
        assert observed[0] is hidden
    finally:
        await control.stop_input()
        await control.close_streams()


@pytest.mark.asyncio
@pytest.mark.parametrize("visible_final", [False, True])
async def test_suppressed_final_cannot_complete_noninteractive_execution(visible_final):
    hidden = _event("chat.final", "IMAGE_DRAFT", output_suppressed=True)
    hidden.is_complete = True

    async def stream():
        yield hidden
        if visible_final:
            yield _event("chat.final", "9801 + PROOF-X", delivery_reconciled=True)

    run = _MachineRun(OneShotRunInput(input="question"), OneShotWriter(io.StringIO(), request_id="run"))
    run.stream = stream()
    assert await run.consume_noninteractive() is visible_final
    assert run.summary.output == ("9801 + PROOF-X" if visible_final else None)

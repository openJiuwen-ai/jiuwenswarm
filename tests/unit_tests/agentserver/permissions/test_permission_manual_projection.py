"""Public permission questions must round-trip through real SDK continuation."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from openjiuwen.core.session.interaction.interactive_input import InteractiveInput
from openjiuwen.core.single_agent.interrupt.response import ToolCallInterruptRequest

from jiuwenswarm.agents.harness.common.rails.interrupt.interrupt_helpers import (
    convert_interactions_to_ask_user_question,
)
from jiuwenswarm.agents.harness.common.rails.permissions.root_permission_queue import RootPermissionQueue
from jiuwenswarm.agents.harness.common.rails.permissions.root_permission_queue_rail import (
    bind_root_permission_request, reset_root_permission_request,
)
from jiuwenswarm.agents.harness.common.rails import stream_event_rail as stream_producers
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter
from jiuwenswarm.server.utils.stream_utils import parse_stream_chunk
from tests.unit_tests.agentserver.permissions import test_permission_answer_cutover as continuation
from tests.unit_tests.agentserver.permissions.test_permission_answer_cutover import answer_host  # noqa: F401
from tests.unit_tests.agentserver.permissions.test_permission_cold_build import cold  # noqa: F401


async def _stream_request(h, **params):
    h.request_count += 1
    request = AgentRequest(
        request_id=f"projection-{h.request_count}",
        session_id=h.adapter._parent_session_id, channel_id="web",
        req_method=ReqMethod.CHAT_SEND,
        params={"mode": "agent", "project_dir": str(h.root), **params},
    )
    inputs, _, _ = h.facade.build_inputs(request)

    async def collect():
        return [chunk async for chunk in h.adapter.process_message_stream_impl(request, inputs)]

    chunks = await asyncio.wait_for(collect(), 10)
    assert chunks
    payloads = [chunk.payload for chunk in chunks if isinstance(chunk.payload, dict)]
    errors = [payload for payload in payloads if payload.get("event_type") in (
        "chat.error", "error", "execution.error",
    )]
    assert not errors, (errors, len(h.script.calls), h.projection_diagnostics)
    h.projected = [
        deepcopy(payload) for payload in payloads
        if payload.get("event_type") == "chat.ask_user_question"
    ]
    return SimpleNamespace(ok=True, payload=payloads)


def _answer(question, action, *, field="value"):
    item = question["questions"][0]
    option = next(option for option in item["options"] if option["value"] == action)
    answer = {"selected_options": [option[field]]}
    if "card_id" in item:
        answer["card_id"] = item["card_id"]
    return {"source": question["source"], "request_id": question["request_id"], "answers": [answer]}


def _questions_once(h, count):
    assert len(h.projected) == count, h.projected
    assert len({question["request_id"] for question in h.projected}) == count
    return h.projected


async def _start(h, monkeypatch, *, mode="manual", kind="permission", count=1):
    from jiuwenswarm.agents.harness import agent_observability
    monkeypatch.setattr(agent_observability, "sync_agent_observability", lambda **_kwargs: None)
    h.verified_projection = Mock(wraps=stream_producers.build_verified_permission_ask_user_question)
    h.ask_projection = Mock(wraps=stream_producers._ask_user_question_payload_from_interrupt)
    monkeypatch.setattr(stream_producers, "build_verified_permission_ask_user_question", h.verified_projection)
    monkeypatch.setattr(stream_producers, "_ask_user_question_payload_from_interrupt", h.ask_projection)
    h.projection_diagnostics = []
    original_parser = h.adapter._parse_stream_chunk

    def parse(chunk, **kwargs):
        parsed = original_parser(chunk, **kwargs)
        if getattr(chunk, "type", None) in ("__interaction__", "controller_output", "answer"):
            h.projection_diagnostics.append((chunk.type, repr(chunk.payload), parsed))
        return parsed

    monkeypatch.setattr(h.adapter, "_parse_stream_chunk", parse)
    monkeypatch.setattr(continuation, "_request", _stream_request)
    # The existing helper also inspects SDK state for its own assertions. Its
    # state-derived answer is deliberately discarded: only public output below
    # may supply the locator and selected option for this test's continuation.
    await continuation._interrupt(h, monkeypatch, mode=mode, kind=kind, count=count)
    return _questions_once(h, count)[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["manual", "auto"])
@pytest.mark.parametrize("action", ["allow_once", "reject"])
async def test_public_permission_question_resumes_exactly_once(answer_host, monkeypatch, mode, action):
    h = answer_host
    question = await _start(h, monkeypatch, mode=mode)
    assert question["source"] == "permission_interrupt"
    assert ("card_id" in question["questions"][0]) is (mode == "auto")
    assert bool(h.verified_projection.call_count) is (mode == "auto")
    if mode == "auto":
        verified_card = h.verified_projection.call_args.args[1]
        assert question["questions"][0]["card_id"] == verified_card.key.invocation_id
        assert question["request_id"] == verified_card.key.tool_call_id
    await _stream_request(h, **_answer(question, action))
    _questions_once(h, 0)
    assert h.executions == ([{"value": "once"}] if action == "allow_once" else [])
    assert len(h.script.calls) == 2 and len(h.dispatches) == 2
    assert isinstance(h.dispatches[-1].inputs["query"], InteractiveInput)


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["value", "label"])
async def test_manual_batch_projects_remaining_question_after_each_answer(answer_host, monkeypatch, field):
    h = answer_host
    first = await _start(h, monkeypatch, count=2)
    assert "card_id" not in first["questions"][0]
    await _stream_request(h, **_answer(first, "allow_once", field=field))
    assert len(h.script.calls) == 1 and len(h.dispatches) == 2
    assert h.executions == [{"value": "once-0"}]
    second = _questions_once(h, 1)[0]
    assert second["request_id"] != first["request_id"]
    assert "card_id" not in second["questions"][0]
    await _stream_request(h, **_answer(second, "allow_once", field=field))
    _questions_once(h, 0)
    assert h.executions == [{"value": "once-0"}, {"value": "once-1"}]
    assert len(h.script.calls) == 2 and len(h.dispatches) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["manual", "auto"])
async def test_public_ask_question_keeps_independent_continuation(answer_host, monkeypatch, mode):
    h = answer_host
    question = await _start(h, monkeypatch, mode=mode, kind="ask")
    assert question["source"] == "ask_user_interrupt"
    assert h.ask_projection.call_count >= 1
    item = question["questions"][0]
    assert "card_id" not in item
    await _stream_request(h, source=question["source"], request_id=question["request_id"],
                          answers=[{"question": item["question"], "custom_input": "blue"}])
    _questions_once(h, 0)
    assert len(h.script.calls) == 2 and len(h.dispatches) == 2
    assert "blue" in repr(h.script.calls[-1]) and h.executions == []


def _manual_interaction(call_id="manual-call"):
    return {"id": call_id, "value": ToolCallInterruptRequest(
        tool_call_id=call_id, tool_name="run_command", message="Approve this tool?",
    )}


def _project(producer, interactions, queue=None):
    if producer == "converter":
        return convert_interactions_to_ask_user_question(interactions, root_permission_queue=queue)
    token = bind_root_permission_request(
        root_session_id="projection-session", request_id="projection-request",
        enabled=queue is not None, queue=queue,
    )
    try:
        chunk = SimpleNamespace(type="__interaction__", payload=interactions)
        parser = JiuWenSwarmDeepAdapter.parse_stream_chunk if producer == "deep" else parse_stream_chunk
        return parser(chunk)
    finally:
        reset_root_permission_request(token)


@pytest.mark.parametrize("producer", ["converter", "deep", "common"])
@pytest.mark.parametrize("count", [1, 2])
def test_manual_producers_preserve_first_pending_projection(producer, count):
    interactions = [_manual_interaction(f"manual-{index}") for index in range(count)]
    question = _project(producer, interactions)
    assert question["request_id"] == "manual-0"
    assert question["source"] == "permission_interrupt"
    assert [option["value"] for option in question["questions"][0]["options"]] == [
        "allow_once", "session_allow", "always_allow", "reject",
    ]
    assert "card_id" not in question["questions"][0]


def _smart_interaction(*, pending=True):
    queue = RootPermissionQueue(id_factory=lambda: "projection-card")
    card = queue.begin(
        root_session_id="projection-session", request_id="projection-request",
        execution_session_id="projection-session", tool_call_id="smart-call", tool_name="run_command",
    )
    request = ToolCallInterruptRequest(
        tool_call_id="smart-call", tool_name="run_command", message="Approve?",
        metadata={"tool_invocation_key": card.key.to_wire()},
    )
    if pending:
        card = queue.mark_pending(card.key, request=request, auto_manual=True, root_context=None)
    return queue, card, {"id": "smart-call", "value": request}


@pytest.mark.parametrize("producer", ["converter", "common"])
def test_live_smart_locator_is_not_downgraded_to_manual(producer):
    queue, card, interaction = _smart_interaction()
    question = _project(producer, [interaction], queue)
    assert question["request_id"] == card.key.tool_call_id
    assert question["questions"][0]["card_id"] == card.key.invocation_id
    assert question["source"] == "permission_interrupt"
    assert queue.get(card.key) == card


@pytest.mark.parametrize("producer", ["converter", "deep", "common"])
@pytest.mark.parametrize("invalid", ["no_queue", "malformed", "foreign", "expired", "active", "missing", "batch"])
def test_invalid_smart_projection_cannot_fall_back_to_manual(producer, invalid):
    queue, card, interaction = _smart_interaction(pending=invalid != "active")
    bound_queue = queue
    if invalid == "no_queue":
        bound_queue = None
    elif invalid == "malformed":
        interaction["value"].metadata["tool_invocation_key"] = "not-a-key"
    elif invalid == "foreign":
        interaction["value"].metadata["tool_invocation_key"]["root_session_id"] = "foreign-session"
    elif invalid == "expired":
        assert queue.finish(card.key)
    elif invalid == "missing":
        interaction = _manual_interaction()
    interactions = [interaction, deepcopy(interaction)] if invalid == "batch" else [interaction]
    assert _project(producer, interactions, bound_queue) is None
    assert queue.get(card.key) == (None if invalid == "expired" else card)


@pytest.mark.parametrize("questions,request_id,expected", [
    ([{"card_id": "card-b"}, {"card_id": " card-a "}], "request", ("permission", ("card-a", "card-b"))),
    ([{"card_id": "card-a"}], "", ("permission", ("card-a",))),
    ([{"card_id": "card-a"}, {}], "request", None),
    ([{}, {"card_id": "card-a"}], "request", None),
    ([{"card_id": "card-a"}, {"card_id": " "}], "request", None),
    ([{}, {}], " request ", ("interaction", "permission_interrupt", "request")),
    ([{"card_id": None}, {"card_id": " "}], "request", ("interaction", "permission_interrupt", "request")),
    ([{}], "", None),
    ([{}], "   ", None),
    ([{}], None, None),
    (["not-a-question"], "request", None),
    ([{"card_id": "card-a"}, None], "request", None),
    ([], "", None),
])
def test_permission_presentation_identity_separates_smart_manual_and_invalid(questions, request_id, expected):
    payload = {
        "event_type": "chat.ask_user_question", "source": "permission_interrupt",
        "request_id": request_id, "questions": questions,
    }
    original = deepcopy(payload)
    assert JiuWenSwarmDeepAdapter._ask_user_event_identity(payload) == expected
    assert payload == original

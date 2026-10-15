# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""One turn delegated to a named subagent through the SDK subagent tools.

``params["agent_subagent_required"]`` names a mounted subagent and a task. The
turn must then answer from that child alone. The SDK writes model text to the
session while the model call is still running and fires ``AFTER_MODEL_CALL``
only once the call returns, so the rail that replaces the tool choice cannot
unsend the parent text it replaces; the adapter drops it instead.

The suppressed chunk types are the ones the stream parser renders as model
text. ``MODEL_TEXT_SAMPLES`` pins that list against the parser itself, so a new
text-rendering type cannot be added to one without the other.
"""

import json
from types import SimpleNamespace

import pytest
from openjiuwen.core.foundation.llm import AssistantMessage
from openjiuwen.core.foundation.tool.schema import ToolOutput
from openjiuwen.harness.schema.interaction import RoundWorkItem
from openjiuwen.harness.subagent_runtime import SUBAGENT_ACTIVITY_EVENT_TYPE

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.server.runtime.agent_adapter import interface_deep
from jiuwenswarm.server.runtime.agent_adapter.required_subagent import (
    RUN_CONTEXT_KEY,
    RequiredSubagentRail,
    mounted_subagent_names,
    required_subagent_from_params,
    required_subagent_state,
    validate_required_subagent,
    with_required_subagent_run_context,
)
from tests.unit_tests.agentserver.test_deep_adapter_empty_run_guard import (
    _adapter_ready,
    _install_stream,
    _payload_events,
)

CHILD_ANSWER = "The child inspected one file and reports two callers."
PARENT_TEXT = "I will summarise this myself instead."
CHUNKED_TEXT = "Streamed by the parent as a content chunk."
#: Events the stream parser builds out of model-authored text.
TEXT_EVENTS = frozenset({"chat.delta", "chat.reasoning", "chat.final"})
#: One chunk per suppressed type, to read the parser's verdict on each.
MODEL_TEXT_SAMPLES = {
    "llm_output": {"content": PARENT_TEXT},
    "llm_reasoning": {"content": PARENT_TEXT},
    "content_chunk": {"content": CHUNKED_TEXT},
    "answer": {"output": PARENT_TEXT, "result_type": "answer"},
}
#: The events that keep a working child visible; none of them is model text.
CHILD_EVENT_SAMPLES = {
    "tool_call": {"tool_call": {"name": "subagent_spawn"}},
    "tool_update": {"tool_update": {"content": "waiting"}},
    "tool_result": {"tool_result": {"tool_name": "subagent_wait", "result": "done"}},
    SUBAGENT_ACTIVITY_EVENT_TYPE: {
        "subagent_activity": {"subagent_id": "child-1", "status": "running"}
    },
    "chat.ask_user_question": {"question": "Which file?", "interaction_id": "ask-1"},
}


# --- rail-level behaviour ---------------------------------------------------


class Context:
    """Stand-in for the SDK rail callback context."""

    def __init__(self, state):
        self.extra = {"run_context": SimpleNamespace(extra={RUN_CONTEXT_KEY: state})}
        self.inputs = SimpleNamespace(response=None)
        self.finish = None

    def request_force_finish(self, result):
        self.finish = result


def _state():
    return required_subagent_from_params({
        "agent_subagent_required": {"name": "code_agent", "task": "Inspect one file"}
    })


def _tool_result(ctx, name, data, *, success=True):
    tool_ctx = Context(None)
    tool_ctx.extra = dict(ctx.extra)
    tool_ctx.inputs = SimpleNamespace(
        tool_name=name,
        tool_result=ToolOutput(success=success, data=data),
    )
    return tool_ctx


async def test_named_child_spawns_once_and_only_a_completed_result_answers():
    state = _state()
    rail = RequiredSubagentRail()
    ctx = Context(state)
    await rail.before_model_call(ctx)
    ctx.inputs.response = AssistantMessage(content=PARENT_TEXT)
    await rail.after_model_call(ctx)
    response = ctx.inputs.response
    assert response.content == ""
    assert len(response.tool_calls) == 1
    spawn = response.tool_calls[0]
    assert spawn.name == "subagent_spawn"
    assert json.loads(spawn.arguments)["subagent_type"] == "code_agent"
    assert json.loads(spawn.arguments)["task_description"] == "Inspect one file"

    await rail.after_tool_call(_tool_result(ctx, "subagent_spawn", {"subagent_id": "child-1"}))
    await rail.after_react_iteration(ctx)
    assert ctx.finish is None

    ctx.inputs.response = AssistantMessage(content=PARENT_TEXT)
    await rail.before_model_call(ctx)
    await rail.after_model_call(ctx)
    wait = ctx.inputs.response.tool_calls[0]
    assert wait.name == "subagent_wait"
    assert json.loads(wait.arguments)["subagent_ids"] == ["child-1"]

    wait_ctx = _tool_result(ctx, "subagent_wait", {
        "statuses": {"child-1": "completed"},
        "results": {"child-1": CHILD_ANSWER},
        "timed_out": False,
    })
    await rail.after_tool_call(wait_ctx)
    assert wait_ctx.finish == {"output": CHILD_ANSWER, "result_type": "answer"}
    await rail.after_react_iteration(ctx)
    assert ctx.finish == {"output": CHILD_ANSWER, "result_type": "answer"}
    assert state.completed


@pytest.mark.parametrize("status", ["interrupted", "errored", "not_found"])
async def test_a_failing_child_is_closed_before_the_error_is_reported(status):
    state = _state()
    state.subagent_id = "child-1"
    rail = RequiredSubagentRail()
    ctx = Context(state)
    await rail.before_model_call(ctx)
    wait_ctx = _tool_result(ctx, "subagent_wait", {
        "statuses": {"child-1": status},
        "results": {},
        "timed_out": status == "interrupted",
    })
    await rail.after_tool_call(wait_ctx)
    assert wait_ctx.finish is None
    await rail.after_react_iteration(ctx)
    assert ctx.finish is None

    ctx.inputs.response = AssistantMessage(content=PARENT_TEXT)
    await rail.before_model_call(ctx)
    await rail.after_model_call(ctx)
    close = ctx.inputs.response.tool_calls[0]
    assert close.name == "subagent_close"
    assert json.loads(close.arguments) == {"subagent_id": "child-1"}

    close_ctx = _tool_result(ctx, "subagent_close", {"subagent_id": "child-1"})
    await rail.after_tool_call(close_ctx)
    assert close_ctx.finish["result_type"] == "error"
    assert state.closed
    await rail.after_react_iteration(ctx)
    assert ctx.finish["result_type"] == "error"
    assert status in ctx.finish["output"]


async def test_a_timed_out_child_is_named_when_the_close_refuses():
    state = _state()
    state.subagent_id = "child-2"
    state.elapsed_ms = state.timeout_ms - 60_000
    rail = RequiredSubagentRail()
    ctx = Context(state)
    await rail.before_model_call(ctx)
    wait_ctx = _tool_result(ctx, "subagent_wait", {
        "statuses": {"child-2": "running"}, "results": {}, "timed_out": True,
    })
    await rail.after_tool_call(wait_ctx)
    assert state.cleanup_pending
    assert wait_ctx.finish is None

    ctx.inputs.response = AssistantMessage(content=PARENT_TEXT)
    await rail.before_model_call(ctx)
    await rail.after_model_call(ctx)
    assert ctx.inputs.response.tool_calls[0].name == "subagent_close"
    close_ctx = _tool_result(ctx, "subagent_close", None, success=False)
    await rail.after_tool_call(close_ctx)
    assert close_ctx.finish["result_type"] == "error"
    assert "child-2 may still be active" in close_ctx.finish["output"]
    assert not state.closed


async def test_any_other_tool_fails_the_delegation():
    state = _state()
    rail = RequiredSubagentRail()
    ctx = Context(state)
    await rail.before_model_call(ctx)
    await rail.after_tool_call(_tool_result(ctx, "bash", {"content": "ran"}))
    await rail.after_react_iteration(ctx)
    assert ctx.finish["result_type"] == "error"


def test_the_run_context_keeps_existing_settings_and_leaves_the_request_alone():
    state = _state()
    original = {"run": {"context": {"extra": {"other": "value"}}}}
    updated = with_required_subagent_run_context(original, state)
    assert required_subagent_state(updated) is state
    assert updated["run"]["context"]["extra"]["other"] == "value"
    assert required_subagent_state(original) is None


def test_the_sdk_work_item_copy_shares_the_delegation_state():
    state = _state()
    inputs = with_required_subagent_run_context({"query": "inspect"}, state)
    work = RoundWorkItem.user(request_id="test", inputs=inputs)

    assert work.inputs is not inputs
    assert required_subagent_state(work.inputs) is state
    required_subagent_state(work.inputs).completed = True
    assert state.completed


# --- validation -------------------------------------------------------------


def _mounted_agent(*, names=("code_agent", "research_agent"), max_iterations=42):
    cards = [SimpleNamespace(name=tool) for tool in
             ("subagent_spawn", "subagent_wait", "subagent_close")]
    return SimpleNamespace(
        ability_manager=SimpleNamespace(list=lambda: cards),
        deep_config=SimpleNamespace(
            subagents=[SimpleNamespace(agent_card=SimpleNamespace(name=name))
                       for name in names],
        ),
        react_agent=SimpleNamespace(config=SimpleNamespace(max_iterations=max_iterations)),
    )


def test_a_mounted_name_passes_validation():
    validate_required_subagent(_mounted_agent(), _state())


def test_an_unmounted_name_is_refused_and_the_roster_is_in_the_message():
    agent = _mounted_agent(names=("research_agent", "general-purpose"))
    with pytest.raises(ValueError) as refusal:
        validate_required_subagent(agent, _state())
    message = str(refusal.value)
    assert "'code_agent' is not mounted" in message
    assert "research_agent, general-purpose" in message


def test_an_empty_roster_is_refused_and_says_so():
    with pytest.raises(ValueError, match="Mounted subagents: none"):
        validate_required_subagent(_mounted_agent(names=()), _state())


def test_delegation_is_refused_when_the_subagent_tools_are_not_mounted():
    agent = _mounted_agent()
    agent.ability_manager = SimpleNamespace(list=lambda: [])
    with pytest.raises(ValueError, match="Subagent delegation is not enabled"):
        validate_required_subagent(agent, _state())


def test_an_iteration_budget_too_small_to_close_is_refused_before_the_spawn():
    with pytest.raises(ValueError, match="at least 42 ReAct iterations"):
        validate_required_subagent(_mounted_agent(max_iterations=41), _state())


def test_the_roster_reader_keeps_the_first_spec_of_each_name():
    config = SimpleNamespace(subagents=[
        SimpleNamespace(agent_card=SimpleNamespace(name="code_agent")),
        SimpleNamespace(agent_card=SimpleNamespace(name="code_agent")),
        SimpleNamespace(card=SimpleNamespace(name="general-purpose")),
        SimpleNamespace(agent_card=SimpleNamespace(name="  ")),
    ])
    assert mounted_subagent_names(config) == ["code_agent", "general-purpose"]


# --- the turn the adapter emits --------------------------------------------


def _delegating_adapter(monkeypatch, chunks, *, completed=True):
    """An adapter whose next turn is a required delegation to ``code_agent``."""
    adapter = _adapter_ready(monkeypatch)
    adapter._enable_auto_permission = False
    adapter._instance.active_round = None
    adapter._instance.register_rail = _async_noop
    adapter._instance.deep_config = SimpleNamespace(
        subagents=[SimpleNamespace(agent_card=SimpleNamespace(name="code_agent"))]
    )
    adapter._instance.ability_manager = SimpleNamespace(list=lambda: [
        SimpleNamespace(name=tool)
        for tool in ("subagent_spawn", "subagent_wait", "subagent_close")
    ])
    adapter._instance.react_agent = SimpleNamespace(
        config=SimpleNamespace(max_iterations=42)
    )
    _install_stream(adapter, chunks)

    # The rail runs inside the SDK, which this test does not start, so the
    # delegation state is settled here as a finished run would have left it.
    settle = adapter._prepare_root_input_dispatch

    async def prepare(request, inputs):
        inputs = await settle(request, inputs)
        state = required_subagent_state(inputs)
        if state is not None:
            state.subagent_id = "child-1"
            if completed:
                state.completed, state.result = True, CHILD_ANSWER
            else:
                state.error = "Required subagent delegation failed: child errored."
        return inputs

    adapter._prepare_root_input_dispatch = prepare
    return adapter


async def _async_noop(*_args, **_kwargs):
    return None


def _request(*, delegate: bool, stream: bool) -> AgentRequest:
    params = {"query": "Who calls this function?", "mode": "agent"}
    if delegate:
        params["agent_subagent_required"] = {
            "name": "code_agent",
            "task": "Find the callers of this function",
        }
    return AgentRequest(
        request_id="req-required-subagent",
        channel_id="channel-one",
        session_id="sess-required-subagent",
        params=params,
        is_stream=stream,
    )


def _model_text_chunks():
    return [
        SimpleNamespace(type="llm_reasoning", payload={"content": "Thinking aloud."}),
        SimpleNamespace(type="content_chunk", payload={"content": CHUNKED_TEXT}),
        SimpleNamespace(type="llm_output", payload={"content": PARENT_TEXT}),
        SimpleNamespace(type="answer", payload={"output": PARENT_TEXT, "result_type": "answer"}),
    ]


def _child_event_chunks():
    return [
        SimpleNamespace(type=chunk_type, payload=payload)
        for chunk_type, payload in CHILD_EVENT_SAMPLES.items()
    ]


def _parsed_event_type(chunk_type, payload):
    parsed = interface_deep.JiuWenSwarmDeepAdapter.parse_stream_chunk(
        SimpleNamespace(type=chunk_type, payload=payload)
    )
    return (parsed or {}).get("event_type")


async def _stream_events(adapter, request):
    return _payload_events([
        chunk async for chunk in adapter.process_message_stream_impl(
            request, dict(request.params)
        )
    ])


def test_the_suppressed_types_are_the_ones_the_parser_renders_as_model_text():
    """The set is the parser's text-rendering types, and nothing else."""
    assert set(MODEL_TEXT_SAMPLES) == interface_deep._MODEL_TEXT_CHUNK_TYPES
    for chunk_type, payload in MODEL_TEXT_SAMPLES.items():
        assert _parsed_event_type(chunk_type, payload) in TEXT_EVENTS


def test_the_events_that_keep_the_child_visible_are_not_model_text():
    for chunk_type, payload in CHILD_EVENT_SAMPLES.items():
        assert _parsed_event_type(chunk_type, payload) not in TEXT_EVENTS
        assert chunk_type not in interface_deep._MODEL_TEXT_CHUNK_TYPES


async def test_the_childs_result_becomes_the_turns_final(monkeypatch):
    adapter = _delegating_adapter(monkeypatch, _model_text_chunks())
    events = await _stream_events(adapter, _request(delegate=True, stream=True))

    finals = [event for event in events if event.get("event_type") == "chat.final"]
    assert [event["content"] for event in finals] == [CHILD_ANSWER]
    assert not any(event.get("event_type") == "chat.error" for event in events)


async def test_no_parent_model_text_reaches_the_stream(monkeypatch):
    adapter = _delegating_adapter(monkeypatch, _model_text_chunks())
    events = await _stream_events(adapter, _request(delegate=True, stream=True))

    blob = json.dumps(events, ensure_ascii=False)
    assert PARENT_TEXT not in blob
    assert CHUNKED_TEXT not in blob
    assert not any(
        event.get("event_type") in ("chat.delta", "chat.reasoning") for event in events
    )


async def test_without_the_suppression_the_same_turn_leaks_parent_text(monkeypatch):
    """The guard, demonstrated by removing it: the text is on the stream."""
    monkeypatch.setattr(interface_deep, "_MODEL_TEXT_CHUNK_TYPES", frozenset())
    adapter = _delegating_adapter(monkeypatch, _model_text_chunks())
    events = await _stream_events(adapter, _request(delegate=True, stream=True))

    blob = json.dumps(events, ensure_ascii=False)
    assert PARENT_TEXT in blob
    assert CHUNKED_TEXT in blob


async def test_each_suppressed_type_leaks_on_its_own_without_the_guard(monkeypatch):
    """Every name in the set earns its place: drop it and that chunk leaks."""
    for chunk_type, payload in MODEL_TEXT_SAMPLES.items():
        narrowed = interface_deep._MODEL_TEXT_CHUNK_TYPES - {chunk_type}
        monkeypatch.setattr(interface_deep, "_MODEL_TEXT_CHUNK_TYPES", narrowed)
        adapter = _delegating_adapter(
            monkeypatch, [SimpleNamespace(type=chunk_type, payload=payload)]
        )
        events = await _stream_events(adapter, _request(delegate=True, stream=True))
        assert [
            event["content"]
            for event in events
            if event.get("event_type") in TEXT_EVENTS
            and event.get("content") != CHILD_ANSWER
        ], chunk_type


async def test_the_child_stays_visible_while_it_works(monkeypatch):
    """Tool, subagent and ask-user events reach the stream under a delegation."""
    adapter = _delegating_adapter(monkeypatch, _child_event_chunks())
    events = await _stream_events(adapter, _request(delegate=True, stream=True))

    seen = [event.get("event_type") for event in events]
    assert seen == [
        "chat.tool_call",
        "chat.tool_update",
        "chat.tool_result",
        "chat.subagent_activity",
        "chat.ask_user_question",
        "chat.final",
    ]


async def test_a_failing_child_yields_chat_error_and_no_final(monkeypatch):
    adapter = _delegating_adapter(monkeypatch, _model_text_chunks(), completed=False)
    events = await _stream_events(adapter, _request(delegate=True, stream=True))

    errors = [event for event in events if event.get("event_type") == "chat.error"]
    assert len(errors) == 1
    assert "child errored" in errors[0]["error"]
    assert not any(event.get("event_type") == "chat.final" for event in events)
    assert PARENT_TEXT not in json.dumps(events, ensure_ascii=False)


async def test_an_answer_error_from_the_run_is_reported_not_swallowed(monkeypatch):
    """The suppression drops the chunk; the failure it reported still arrives."""
    adapter = _delegating_adapter(monkeypatch, [
        SimpleNamespace(type="content_chunk", payload={"content": CHUNKED_TEXT}),
        SimpleNamespace(
            type="answer", payload={"output": "model call failed", "result_type": "error"}
        ),
    ])
    events = await _stream_events(adapter, _request(delegate=True, stream=True))

    assert [event for event in events if event.get("event_type") == "chat.error"] == [
        {"event_type": "chat.error", "error": "model call failed"}
    ]
    assert not any(event.get("event_type") == "chat.final" for event in events)
    assert CHUNKED_TEXT not in json.dumps(events, ensure_ascii=False)


async def test_a_turn_without_the_parameter_is_unchanged(monkeypatch):
    adapter = _delegating_adapter(monkeypatch, _model_text_chunks())
    events = await _stream_events(adapter, _request(delegate=False, stream=True))

    assert [
        (event.get("event_type"), event.get("content")) for event in events
    ] == [
        ("chat.reasoning", "Thinking aloud."),
        ("chat.delta", CHUNKED_TEXT),
        ("chat.delta", PARENT_TEXT),
        ("chat.final", PARENT_TEXT),
    ]
    assert not any(event.get("event_type") == "chat.error" for event in events)


async def test_an_unmounted_name_is_refused_before_the_turn_starts(monkeypatch):
    adapter = _delegating_adapter(monkeypatch, [])
    request = _request(delegate=True, stream=True)
    request.params["agent_subagent_required"]["name"] = "no_such_agent"

    with pytest.raises(ValueError, match="'no_such_agent' is not mounted"):
        await adapter._prepare_root_input_dispatch(request, dict(request.params))


async def test_the_unary_reply_carries_the_verified_child_result(monkeypatch):
    adapter = _delegating_adapter(monkeypatch, _model_text_chunks())
    request = _request(delegate=True, stream=False)

    response = await adapter.process_message_impl(request, dict(request.params))

    assert response.ok
    assert response.payload == {"content": CHILD_ANSWER}


async def test_the_unary_reply_reports_a_failing_child_as_an_error(monkeypatch):
    adapter = _delegating_adapter(monkeypatch, _model_text_chunks(), completed=False)
    request = _request(delegate=True, stream=False)

    response = await adapter.process_message_impl(request, dict(request.params))

    assert not response.ok
    assert "child errored" in response.payload["error"]
    assert "content" not in response.payload

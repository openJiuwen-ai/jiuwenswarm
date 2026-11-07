from __future__ import annotations

import json

import pytest
from openjiuwen.core.events import OPS_BUILD_EVENT_TYPE, Event, ExtensionEventBus, HandlerLevel

from jiuwenswarm.common.session_message import SESSION_MESSAGE_INTERNAL_KEY
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.runtime.agent_adapter import prompt_context as prompt_context_module
from jiuwenswarm.extensions_v2.event_types.prompt_context import (
    PROMPT_CONTEXT_EVENT,
    InstructionFile,
    PromptContextBlock,
    PromptContextResult,
    register_prompt_context_events,
)
from jiuwenswarm.server.runtime.agent_adapter import interface as interface_module


def _envelope(rendered: str) -> dict:
    return json.loads(rendered[rendered.index("{"):])


@pytest.mark.asyncio
async def test_async_build_inputs_prepares_prompt_context_once_and_projects_result(monkeypatch) -> None:
    monkeypatch.setattr(interface_module, "get_config", lambda: {"preferred_language": "zh"})
    monkeypatch.setattr(interface_module, "get_memory_mode", lambda _config: "disabled")

    bus = ExtensionEventBus()
    register_prompt_context_events(bus)
    await bus.trigger(Event(OPS_BUILD_EVENT_TYPE, None))
    observed = []

    async def rewrite(event, ops, next_handler):
        del ops
        observed.append(event.event_input)
        return PromptContextResult(
            blocks=[
                PromptContextBlock(name="currentDate", text="changed-date"),
                PromptContextBlock(name="interactionContext", text="changed-interaction"),
                PromptContextBlock(name="custom", text="custom-text"),
            ],
            instructionFiles=[
                InstructionFile(
                    path="/workspace/AGENTS.md",
                    kind="project",
                    content="project rules",
                    isUnverified=True,
                )
            ],
        )

    bus.register_event_handler(PROMPT_CONTEXT_EVENT, rewrite, {}, HandlerLevel.USER)
    monkeypatch.setattr(prompt_context_module, "_get_extension_bus", lambda: bus)

    request = AgentRequest(
        request_id="req-prompt-context",
        channel_id="web",
        session_id="session-prompt-context",
        req_method=ReqMethod.CHAT_SEND,
        is_stream=True,
        params={
            "query": "总结这份文档",
            "mode": "agent",
        },
        metadata={"interaction_context": "original-interaction"},
    )

    facade = interface_module.JiuWenSwarm()
    inputs, _, turn = await facade.build_inputs_async(request)
    envelope = _envelope(inputs["query"])

    assert len(observed) == 1
    assert [block.name for block in observed[0].blocks] == ["currentDate", "interactionContext"]
    assert envelope["timestamp"] == "changed-date"
    assert inputs["query"].startswith("\nchanged-interaction\n\n")
    assert envelope["prompt_context"] == {
        "blocks": [{"name": "custom", "text": "custom-text"}],
        "instructionFiles": [
            {
                "path": "/workspace/AGENTS.md",
                "kind": "project",
                "content": "project rules",
                "parent": None,
                "isUnverified": True,
            }
        ],
    }

    turn.with_text("team rewrite").render()
    assert len(observed) == 1


@pytest.mark.asyncio
async def test_async_build_inputs_projects_deleted_known_contexts(monkeypatch) -> None:
    monkeypatch.setattr(interface_module, "get_config", lambda: {"preferred_language": "zh"})
    monkeypatch.setattr(interface_module, "get_memory_mode", lambda _config: "disabled")

    bus = ExtensionEventBus()
    register_prompt_context_events(bus)
    await bus.trigger(Event(OPS_BUILD_EVENT_TYPE, None))

    async def delete_known_contexts(event, ops, next_handler):
        del event, ops, next_handler
        return PromptContextResult(
            blocks=[PromptContextBlock(name="custom", text="kept")],
        )

    bus.register_event_handler(
        PROMPT_CONTEXT_EVENT,
        delete_known_contexts,
        {},
        HandlerLevel.USER,
    )
    monkeypatch.setattr(prompt_context_module, "_get_extension_bus", lambda: bus)

    inputs, _, _ = await interface_module.JiuWenSwarm().build_inputs_async(
        AgentRequest(
            request_id="req-prompt-context-delete",
            channel_id="web",
            session_id="session-prompt-context",
            req_method=ReqMethod.CHAT_SEND,
            metadata={"interaction_context": "must be removed"},
            params={"query": "hello", "mode": "agent"},
        )
    )
    envelope = _envelope(inputs["query"])

    assert "timestamp" not in envelope
    assert "must be removed" not in inputs["query"]
    assert envelope["prompt_context"] == {
        "blocks": [{"name": "custom", "text": "kept"}],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "params",
    [
        {"query": "@alice hello", "mode": "agent", "team": True},
        {"query": "hello", "mode": "team"},
        {"query": "hello", "mode": "agent", "type": "group_chat"},
        {"query": {"type": "group_chat", "content": "hello"}, "mode": "agent"},
    ],
)
async def test_async_build_inputs_applies_prompt_context_to_team_and_group_chat(
    monkeypatch,
    params: dict,
) -> None:
    monkeypatch.setattr(interface_module, "get_config", lambda: {"preferred_language": "zh"})
    monkeypatch.setattr(interface_module, "get_memory_mode", lambda _config: "disabled")

    bus = ExtensionEventBus()
    register_prompt_context_events(bus)
    await bus.trigger(Event(OPS_BUILD_EVENT_TYPE, None))
    observed = []

    async def observe(event, ops, next_handler):
        del ops
        observed.append(event.event_input)
        return await next_handler(event)

    bus.register_event_handler(PROMPT_CONTEXT_EVENT, observe, {}, HandlerLevel.USER)
    monkeypatch.setattr(prompt_context_module, "_get_extension_bus", lambda: bus)

    inputs, _, turn = await interface_module.JiuWenSwarm().build_inputs_async(
        AgentRequest(
            request_id="req-prompt-context-team-member",
            channel_id="web",
            session_id="session-prompt-context",
            req_method=ReqMethod.CHAT_SEND,
            params=params,
        )
    )

    assert len(observed) == 1
    assert turn.prepared_contexts is not None
    assert isinstance(inputs["query"], str)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("req_method", "params"),
    [
        (ReqMethod.CHAT_SEND, {"query": "hello", "mode": "auto_harness"}),
        (
            ReqMethod.CHAT_SEND,
            {"query": "hello", "mode": "agent", "is_supplement": True},
        ),
        (ReqMethod.CHAT_RESUME, {"query": "hello", "mode": "agent"}),
        (ReqMethod.CHAT_ANSWER, {"query": "hello", "mode": "agent"}),
        (ReqMethod.CHAT_CANCEL, {"query": "hello", "mode": "agent"}),
        (ReqMethod.SESSION_INPUT_INTENT, {"query": "hello", "mode": "agent"}),
    ],
)
async def test_async_build_inputs_skips_non_applicable_session_requests(
    monkeypatch,
    req_method: ReqMethod,
    params: dict,
) -> None:
    monkeypatch.setattr(interface_module, "get_config", lambda: {"preferred_language": "zh"})
    monkeypatch.setattr(interface_module, "get_memory_mode", lambda _config: "disabled")
    observed = []

    class RecordingBus:
        async def trigger(self, event, session):
            observed.append((event, session))
            return PromptContextResult(blocks=list(event.event_input.blocks))

    monkeypatch.setattr(prompt_context_module, "_get_extension_bus", lambda: RecordingBus())

    inputs, _, turn = await interface_module.JiuWenSwarm().build_inputs_async(
        AgentRequest(
            request_id="req-prompt-context-special",
            channel_id="web",
            session_id="session-prompt-context",
            req_method=req_method,
            params=params,
        )
    )

    assert observed == []
    assert turn.prepared_contexts is None
    assert isinstance(inputs["query"], str)


@pytest.mark.asyncio
async def test_async_build_inputs_skips_requests_without_a_session_id(monkeypatch) -> None:
    monkeypatch.setattr(interface_module, "get_config", lambda: {"preferred_language": "zh"})
    monkeypatch.setattr(interface_module, "get_memory_mode", lambda _config: "disabled")
    observed = []

    class RecordingBus:
        async def trigger(self, event, session):
            observed.append((event, session))
            return PromptContextResult(blocks=list(event.event_input.blocks))

    monkeypatch.setattr(prompt_context_module, "_get_extension_bus", lambda: RecordingBus())

    inputs, _, turn = await interface_module.JiuWenSwarm().build_inputs_async(
        AgentRequest(
            request_id="req-prompt-context-no-session",
            channel_id="web",
            req_method=ReqMethod.CHAT_SEND,
            params={"query": "hello", "mode": "agent"},
        )
    )

    assert observed == []
    assert turn.prepared_contexts is None
    assert isinstance(inputs["query"], str)


@pytest.mark.asyncio
async def test_async_build_inputs_skips_a2ui_payload_and_retry(monkeypatch) -> None:
    monkeypatch.setenv("JIUWENSWARM_A2UI_ENABLED", "true")
    monkeypatch.setattr(interface_module, "get_config", lambda: {"preferred_language": "zh"})
    monkeypatch.setattr(interface_module, "get_memory_mode", lambda _config: "disabled")
    observed = []

    class RecordingBus:
        async def trigger(self, event, session):
            observed.append((event, session))
            return PromptContextResult(blocks=list(event.event_input.blocks))

    monkeypatch.setattr(prompt_context_module, "_get_extension_bus", lambda: RecordingBus())

    for params, metadata in (
        (
            {
                "query": {
                    "type": "a2ui.client_event",
                    "userAction": {"context": {"value": "ok"}},
                },
                "mode": "agent",
            },
            {},
        ),
        ({"query": "hello", "mode": "agent"}, {"skip_a2ui": True}),
    ):
        _, _, turn = await interface_module.JiuWenSwarm().build_inputs_async(
            AgentRequest(
                request_id="req-prompt-context-a2ui",
                channel_id="web",
                session_id="session-prompt-context",
                req_method=ReqMethod.CHAT_SEND,
                params=params,
                metadata=metadata,
            )
        )
        assert turn.prepared_contexts is None

    assert observed == []


@pytest.mark.asyncio
async def test_async_build_inputs_skips_a2ui_payload_when_a2ui_is_disabled(monkeypatch) -> None:
    monkeypatch.delenv("JIUWENSWARM_A2UI_ENABLED", raising=False)
    monkeypatch.setattr(interface_module, "get_config", lambda: {"preferred_language": "zh"})
    monkeypatch.setattr(interface_module, "get_memory_mode", lambda _config: "disabled")
    observed = []

    class RecordingBus:
        async def trigger(self, event, session):
            observed.append((event, session))
            return PromptContextResult(blocks=list(event.event_input.blocks))

    monkeypatch.setattr(prompt_context_module, "_get_extension_bus", lambda: RecordingBus())

    _, _, turn = await interface_module.JiuWenSwarm().build_inputs_async(
        AgentRequest(
            request_id="req-prompt-context-a2ui-disabled",
            channel_id="web",
            session_id="session-prompt-context",
            req_method=ReqMethod.CHAT_SEND,
            params={
                "query": {
                    "type": "a2ui.client_event",
                    "userAction": {"context": {"value": "ok"}},
                },
                "mode": "agent",
            },
        )
    )

    assert observed == []
    assert turn.prepared_contexts is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("params", "metadata"),
    [
        ({"query": "hello", "mode": "agent", "source": "heartbeat"}, {}),
        (
            {"query": "hello", "mode": "agent"},
            {SESSION_MESSAGE_INTERNAL_KEY: {"source_session_id": "source-session"}},
        ),
    ],
)
async def test_async_build_inputs_skips_internal_session_requests(
    monkeypatch,
    params: dict,
    metadata: dict,
) -> None:
    monkeypatch.setattr(interface_module, "get_config", lambda: {"preferred_language": "zh"})
    monkeypatch.setattr(interface_module, "get_memory_mode", lambda _config: "disabled")
    observed = []

    class RecordingBus:
        async def trigger(self, event, session):
            observed.append((event, session))
            return PromptContextResult(blocks=list(event.event_input.blocks))

    monkeypatch.setattr(prompt_context_module, "_get_extension_bus", lambda: RecordingBus())

    _, _, turn = await interface_module.JiuWenSwarm().build_inputs_async(
        AgentRequest(
            request_id="req-prompt-context-internal",
            channel_id="web",
            session_id="session-prompt-context",
            req_method=ReqMethod.CHAT_SEND,
            params=params,
            metadata=metadata,
        )
    )

    assert observed == []
    assert turn.prepared_contexts is None


@pytest.mark.asyncio
async def test_async_build_inputs_fails_open_to_event_bus_errors(monkeypatch, caplog) -> None:
    monkeypatch.setattr(interface_module, "get_config", lambda: {"preferred_language": "zh"})
    monkeypatch.setattr(interface_module, "get_memory_mode", lambda _config: "disabled")

    bus = ExtensionEventBus()
    register_prompt_context_events(bus)
    await bus.trigger(Event(OPS_BUILD_EVENT_TYPE, None))

    async def fail(*_args, **_kwargs):
        raise RuntimeError("secret prompt context")

    bus.register_event_handler(PROMPT_CONTEXT_EVENT, fail, {}, HandlerLevel.USER)
    monkeypatch.setattr(prompt_context_module, "_get_extension_bus", lambda: bus)

    facade = interface_module.JiuWenSwarm()
    inputs, _, _ = await facade.build_inputs_async(
        AgentRequest(
            request_id="req-prompt-context-fail-open",
            channel_id="web",
            session_id="session-prompt-context",
            req_method=ReqMethod.CHAT_SEND,
            params={"query": "hello", "mode": "agent"},
        )
    )

    assert _envelope(inputs["query"])["content"] == "hello"
    assert "timestamp" in _envelope(inputs["query"])
    host_logs = [
        record.getMessage()
        for record in caplog.records
        if record.name == prompt_context_module.__name__
    ]
    assert all("secret prompt context" not in message for message in host_logs)


@pytest.mark.asyncio
async def test_async_build_inputs_preserves_context_when_handler_mutates_then_fails(
    monkeypatch,
) -> None:
    monkeypatch.setattr(interface_module, "get_config", lambda: {"preferred_language": "zh"})
    monkeypatch.setattr(interface_module, "get_memory_mode", lambda _config: "disabled")

    bus = ExtensionEventBus()
    register_prompt_context_events(bus)
    await bus.trigger(Event(OPS_BUILD_EVENT_TYPE, None))

    async def mutate_then_fail(event, _ops, _next_handler):
        event.event_input.blocks.clear()

    bus.register_event_handler(PROMPT_CONTEXT_EVENT, mutate_then_fail, {}, HandlerLevel.USER)
    monkeypatch.setattr(prompt_context_module, "_get_extension_bus", lambda: bus)

    inputs, _, _ = await interface_module.JiuWenSwarm().build_inputs_async(
        AgentRequest(
            request_id="req-prompt-context-mutation-fail",
            channel_id="web",
            session_id="session-prompt-context",
            req_method=ReqMethod.CHAT_SEND,
            metadata={"interaction_context": "preserve this context"},
            params={"query": "hello", "mode": "agent"},
        )
    )
    envelope = _envelope(inputs["query"])

    assert envelope["content"] == "hello"
    assert "timestamp" in envelope
    assert "preserve this context" in inputs["query"]
@pytest.mark.asyncio
async def test_async_build_inputs_fails_open_to_invalid_event_output(monkeypatch) -> None:
    monkeypatch.setattr(interface_module, "get_config", lambda: {"preferred_language": "zh"})
    monkeypatch.setattr(interface_module, "get_memory_mode", lambda _config: "disabled")

    class InvalidBus:
        async def trigger(self, *_args, **_kwargs):
            return object()

    monkeypatch.setattr(prompt_context_module, "_get_extension_bus", lambda: InvalidBus())

    inputs, _, _ = await interface_module.JiuWenSwarm().build_inputs_async(
        AgentRequest(
            request_id="req-prompt-context-invalid-output",
            channel_id="web",
            session_id="session-prompt-context",
            req_method=ReqMethod.CHAT_SEND,
            metadata={"interaction_context": "preserve this context"},
            params={"query": "hello", "mode": "agent"},
        )
    )
    envelope = _envelope(inputs["query"])

    assert envelope["content"] == "hello"
    assert "timestamp" in envelope
    assert "preserve this context" in inputs["query"]

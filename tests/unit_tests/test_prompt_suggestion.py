# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""A finished turn forks its own last model call into one or two suggestions of the user's next input."""

from __future__ import annotations

import asyncio
import contextlib
import copy
import functools
import json
import sys
import types
from types import SimpleNamespace
from typing import Any, AsyncIterator
from uuid import uuid4

import pytest

from openjiuwen.core.common.constants.constant import INTERACTION
from openjiuwen.core.foundation.llm import (
    AssistantMessage, ModelClientConfig, ModelRequestConfig, ToolCall, ToolMessage, UserMessage,
)
from openjiuwen.core.foundation.llm.schema.message import UsageMetadata
from openjiuwen.core.foundation.llm.schema.message_chunk import AssistantMessageChunk
from openjiuwen.core.foundation.tool import ToolCard
from openjiuwen.core.runner import Runner
from openjiuwen.core.session import InteractiveInput
from openjiuwen.core.single_agent.agents.react_agent import ReActAgent
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext, AgentRail, ModelCallInputs
from openjiuwen.core.single_agent.schema.agent_card import AgentCard
from openjiuwen.harness import DeepAgent, create_deep_agent
from openjiuwen.harness.schema.interaction import SendInputRequest
from openjiuwen.harness.schema.state import DeepAgentState, PlanModeState

from jiuwenswarm.agents.harness.common.rails.ask_user_rail import StructuredAskUserRail
from jiuwenswarm.agents.harness.common.recommendation import prompt_suggestion, topic_controls
from jiuwenswarm.agents.harness.common.recommendation.prompt_suggestion import POLICY_ID
from jiuwenswarm.agents.harness.common.recommendation.topic_starter import TopicStarterStore
from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponse, AgentResponseChunk
from jiuwenswarm.common.schema.message import ReqMethod
from gateway.channel_manager.base import RobotMessageRouter
from gateway.channel_manager.web.web_connect import WebChannel, WebChannelConfig
from gateway.message_handler.message_handler import MessageHandler
from jiuwenswarm.server.gateway_push.transport import WebSocketGatewayPushTransport
from jiuwenswarm.server.gateway_push.wire import build_server_push_wire
from jiuwenswarm.server.runtime.agent_adapter import interface as interface_module
from jiuwenswarm.server.runtime.agent_adapter.interface import JiuWenSwarm
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter
from jiuwenswarm.server.runtime.session import session_history

SESSION = "web_suggestion_session"


def _usage(prompt: int, cached: int, completion: int) -> UsageMetadata:
    return UsageMetadata(input_tokens=prompt, cache_tokens=cached, output_tokens=completion,
                         total_tokens=prompt + completion)


class _ModelClient:
    """The dialogue model client at the model-call boundary; it keeps a copy of every request as it was sent."""

    def __init__(self, *replies: Any) -> None:
        self.model_client_config = ModelClientConfig(client_provider="OpenAI", api_key="local-test",
                                                     api_base="http://127.0.0.1:1")
        self.model_config = ModelRequestConfig(model_name="dialogue-model")
        self.requests: list[SimpleNamespace] = []
        self._replies = list(replies)

    async def invoke(self, messages: list[Any], *, tools: Any = None, model: str | None = None, **kwargs: Any):
        self.requests.append(SimpleNamespace(messages=copy.deepcopy(list(messages)), tools=copy.deepcopy(tools),
                                             model=model, kwargs=kwargs))
        reply = self._replies.pop(0)
        return await reply() if callable(reply) else reply

    async def stream(self, messages: list[Any], **kwargs: Any) -> AsyncIterator[AssistantMessageChunk]:
        """The streaming call DeepAgent's task loop makes: the reply as one chunk."""
        reply = await self.invoke(messages, **kwargs)
        yield AssistantMessageChunk(content=reply.content, tool_calls=reply.tool_calls,
                                    usage_metadata=reply.usage_metadata, reasoning_content=reply.reasoning_content)


class _Page:
    """The Web channel the gateway pushes to."""

    channel_id = "web"

    def __init__(self) -> None:
        self.events: list[Any] = []

    async def send(self, msg: Any) -> None:
        self.events.append(msg)


class _Host:
    """AgentServer's agent adapter: every turn runs on one real ReActAgent."""

    def __init__(self, agent: ReActAgent) -> None:
        self.agent = agent
        # What the next turn does after the agent answers: a further stream event, or a failure.
        self.ending: dict[str, Any] | BaseException | None = None

    async def handle_heartbeat(self, _request: AgentRequest) -> None:
        return None

    async def process_message_impl(self, request: AgentRequest, _inputs: dict[str, Any]) -> AgentResponse:
        result = await self.agent.invoke({"query": request.params["query"], "conversation_id": request.session_id})
        ending, self.ending = self.ending, None
        if isinstance(ending, BaseException):
            raise ending
        payload = {"content": result["output"], **(ending or {})}
        return AgentResponse(request_id=request.request_id, channel_id=request.channel_id,
                             ok=payload.get("event_type") != "chat.error", payload=payload)

    async def process_message_stream_impl(
        self, request: AgentRequest, _inputs: dict[str, Any],
    ) -> AsyncIterator[AgentResponseChunk]:
        result = await self.agent.invoke({"query": request.params["query"], "conversation_id": request.session_id})
        ending, self.ending = self.ending, None
        if isinstance(ending, BaseException):
            raise ending
        yield AgentResponseChunk(request_id=request.request_id, channel_id=request.channel_id,
                                 payload={"event_type": "chat.final", "content": result["output"]}, is_complete=False)
        if ending is not None:
            yield AgentResponseChunk(request_id=request.request_id, channel_id=request.channel_id,
                                     payload=ending, is_complete=False)


class _DeepHost:
    """AgentServer's deep adapter at its dispatch: each input goes to the session's DeepAgent interaction.

    The agent asking the user (ask_user) reaches the page as the question; the page's answer resumes it.
    """

    def __init__(self, agent: DeepAgent) -> None:
        self.agent = agent
        self.question_call_id: str | None = None

    async def process_message_stream_impl(
        self, request: AgentRequest, _inputs: dict[str, Any],
    ) -> AsyncIterator[AgentResponseChunk]:
        query: Any = request.params["query"]
        if request.params.get("source") == "ask_user_interrupt":
            query = InteractiveInput()
            query.update(self.question_call_id, {"answers": {
                answer["question"]: answer["selected_options"][0] for answer in request.params["answers"]}})
        output = await self.agent.attach_output()
        try:
            await self.agent.send_input(SendInputRequest(request_id=request.request_id, inputs={"query": query}))
            async for chunk in output:
                if chunk.type == INTERACTION:
                    self.question_call_id = chunk.payload.id
                    payload = {"event_type": "chat.ask_user_question", "source": "ask_user_interrupt",
                               "request_id": request.request_id,
                               "questions": json.loads(chunk.payload.value.tool_args)["questions"]}
                elif chunk.type == "answer":
                    payload = {"event_type": "chat.final", "content": chunk.payload["output"]}
                else:
                    continue
                yield AgentResponseChunk(request_id=request.request_id, channel_id=request.channel_id,
                                         payload=payload, is_complete=False)
        finally:
            await output.close()


@contextlib.asynccontextmanager
async def _deep_host(client: _ModelClient, tmp_path, monkeypatch) -> AsyncIterator[_DeepHost]:
    """The session's DeepAgent as AgentServer builds it, bound to SESSION by ``start_interaction``.

    It carries the rails every agent rail set attaches, the ask_user rail, and the general-purpose
    subagent, which shares the root's rails and model.
    """
    adapter = JiuWenSwarmDeepAdapter()
    with monkeypatch.context() as patch:
        # Importing the real observability rail starts an exporter.
        stub = types.ModuleType("openjiuwen.harness.observability")
        stub.AgentObservabilityRail = AgentRail
        patch.setitem(sys.modules, "openjiuwen.harness.observability", stub)
        rails = adapter._instantiate_rails([], {})  # pylint: disable=protected-access
    await Runner.start()
    agent = create_deep_agent(
        model=client, rails=[*rails, StructuredAskUserRail()], add_general_purpose_agent=True,
        enable_task_loop=True, workspace=str(tmp_path / "workspace"),
        enable_model_anomaly_detection_rail=False, enable_read_image_multimodal=False,
    )
    try:
        await agent.ensure_initialized()
        adapter._instance = agent  # pylint: disable=protected-access
        adapter._is_session_scoped_adapter = True  # pylint: disable=protected-access
        adapter._parent_session_id = SESSION  # pylint: disable=protected-access
        await adapter.start_interaction(SESSION)
        yield _DeepHost(agent)
    finally:
        await agent.stop()
        await Runner.stop()


@pytest.fixture
def product(tmp_path, monkeypatch):
    """AgentServer and gateway in one process, joined by the real server-push wire."""
    monkeypatch.setattr(session_history, "get_agent_sessions_dir", lambda: tmp_path / "sessions")
    monkeypatch.setattr(prompt_suggestion, "get_agent_workspace_dir", lambda: tmp_path)
    monkeypatch.setattr(prompt_suggestion, "forks", prompt_suggestion.PromptSuggestionForks())
    controls = {"enabled": True, "history_after": 0.0}
    # The prompt switch lives in config.yaml, so it is pinned to this fixture's controls rather
    # than read from whatever config.yaml the machine running the test happens to have.
    from jiuwenswarm.agents.harness.common.recommendation import switches

    monkeypatch.setattr(switches, "is_prompt_suggestion_enabled",
                        lambda *_args, **_kwargs: bool(controls.get("prompt_enabled", True)))
    monkeypatch.setattr(topic_controls, "load_recommendation_controls", lambda: dict(controls))
    monkeypatch.setattr(interface_module, "get_config", lambda: {"preferred_language": "zh"})
    monkeypatch.setattr(interface_module, "get_memory_mode", lambda _cfg: "off")
    monkeypatch.setattr(interface_module, "build_user_prompt", lambda query, **_kw: query)

    monkeypatch.setattr(MessageHandler, "_instance", None)
    gateway = MessageHandler(SimpleNamespace())
    page = _Page()
    store = TopicStarterStore(tmp_path / "recommendation.db")
    gateway.on_prompt_suggestion = functools.partial(prompt_suggestion.deliver, page, store)

    async def push(_transport: Any, msg: dict[str, Any]) -> bool:
        await getattr(gateway, "_handle_agent_server_push")(build_server_push_wire(msg))
        return True

    monkeypatch.setattr(WebSocketGatewayPushTransport, "send_push", push)
    return SimpleNamespace(page=page, store=store, controls=controls)


async def _agent(client: _ModelClient, monkeypatch) -> ReActAgent:
    agent = ReActAgent(AgentCard(id=uuid4().hex, name="host", description="host"))
    agent.set_llm(client)
    agent.config.model_name = "dialogue-model"
    agent.ability_manager.add(ToolCard(id="lookup", name="lookup", description="Look something up",
                                       input_params={"type": "object", "properties": {}}))

    async def run_tool(*_args: Any, **_kwargs: Any):
        return "found", ToolMessage(content="found", tool_call_id="call-1")

    monkeypatch.setattr(agent.ability_manager, "_execute_single_tool_call", run_tool)
    await agent.register_rail(prompt_suggestion.PromptSuggestionForkRail())
    return agent


def _facade(host: _Host) -> JiuWenSwarm:
    facade = JiuWenSwarm()
    facade._adapter = host  # pylint: disable=protected-access
    facade._sdk_name = "harness"  # pylint: disable=protected-access
    return facade


async def _turn(facade: JiuWenSwarm, turn_id: str, query: str, *, metadata: dict[str, Any] | None = None,
                stream: bool = True, **params: Any) -> None:
    request = AgentRequest(request_id=turn_id, channel_id="web", session_id=SESSION,
                           req_method=ReqMethod.CHAT_SEND, params={"query": query, "mode": "agent", **params},
                           metadata=metadata, is_stream=stream)
    if not stream:
        await facade.process_message(request)
        return
    async for _ in facade.process_message_stream(request):
        pass


async def _settled() -> None:
    """Let the fire-and-forget fork finish."""
    for task in list(prompt_suggestion.forks.in_flight()):
        await task


def _generations(store: TopicStarterStore) -> list[dict[str, Any]]:
    rows = store.query_events(policy_id=POLICY_ID, event_type="prompt_suggestion_generation", limit=100)
    return [row["payload"] for row in rows]


async def test_finished_turn_forks_its_last_model_call_and_reaches_the_page(product, monkeypatch) -> None:
    main_answer = AssistantMessage(content="说明已经译成英文，术语按原文统一了。",
                                   usage_metadata=_usage(3000, 2600, 120))
    client = _ModelClient(
        AssistantMessage(content="", tool_calls=[ToolCall(id="call-1", type="function", name="lookup", arguments="{}")],
                         usage_metadata=_usage(2500, 2400, 20)),
        main_answer,
        AssistantMessage(content="导出成双语对照", usage_metadata=_usage(3150, 3000, 6)),
    )
    facade = _facade(_Host(await _agent(client, monkeypatch)))

    await _turn(facade, "req-1", "把这段说明译成英文并统一术语")
    await _settled()

    main_call, fork = client.requests[1], client.requests[2]
    assert len(client.requests) == 3
    # The fork resends the last main call unchanged: same model, tools and messages, then the
    # answer that call produced and one user message asking for the suggestion.
    assert fork.model == main_call.model == "dialogue-model"
    assert fork.tools == main_call.tools and [tool.name for tool in fork.tools] == ["lookup"]
    assert fork.messages[:len(main_call.messages)] == main_call.messages
    assert fork.messages[len(main_call.messages)] == main_answer
    assert len(fork.messages) == len(main_call.messages) + 2
    assert isinstance(fork.messages[-1], UserMessage)
    # No parameter of the parent's is overridden: no max_tokens, and the parent's own reasoning setting.
    assert fork.kwargs == {}

    [event] = product.page.events
    assert event.session_id == SESSION
    assert {key: event.payload[key] for key in ("event_type", "session_id", "request_id", "text")} == {
        "event_type": "prompt_suggestion.ready", "session_id": SESSION, "request_id": "req-1", "text": "导出成双语对照",
    }
    [generation] = _generations(product.store)
    assert generation["suggestion_id"] == event.payload["suggestion_id"]
    assert (generation["outcome"], generation["request_id"], generation["model"]) == ("candidate", "req-1", "dialogue-model")
    assert generation["prompt_version"] == "prompt-suggestion-fork-v11"
    assert generation["usage"]["prompt_tokens_details"] == {"cached_tokens": 3000}
    assert generation["latency_ms"] >= 0


async def test_tool_denial_continues_without_executing_tools_or_changing_main_history(product, monkeypatch) -> None:
    fork_started, release = asyncio.Event(), asyncio.Event()
    tool_reply = AssistantMessage(
        content="", tool_calls=[ToolCall(id="suggestion-lookup", type="function", name="lookup", arguments="{}")],
        response_id="fork-tool", usage_metadata=_usage(1100, 1000, 20),
    )

    async def first_fork() -> AssistantMessage:
        fork_started.set()
        await release.wait()
        return tool_reply

    client = _ModelClient(
        AssistantMessage(content="译好了。", usage_metadata=_usage(900, 800, 10)),
        first_fork,
        AssistantMessage(content="导出成双语对照", response_id="fork-text", usage_metadata=_usage(1170, 1100, 6)),
    )
    agent = await _agent(client, monkeypatch)
    executed = []

    async def run_tool(*args: Any, **kwargs: Any):
        executed.append((args, kwargs))
        return "found", ToolMessage(content="found", tool_call_id="suggestion-lookup")

    monkeypatch.setattr(agent.ability_manager, "_execute_single_tool_call", run_tool)
    await _turn(_facade(_Host(agent)), "req-1", "把这段说明译成英文")
    await fork_started.wait()
    session_history.flush_history_writes()
    before = session_history.load_history_records(SESSION)
    release.set()
    await _settled()

    assert [event.payload["text"] for event in product.page.events] == ["导出成双语对照"]
    assert len(client.requests) == 3
    first, continued = client.requests[1:]
    assert continued.messages == [*first.messages, tool_reply,
                                  ToolMessage(content="生成建议无需使用工具", tool_call_id="suggestion-lookup")]
    assert continued.tools == first.tools == client.requests[0].tools
    assert continued.model == first.model == "dialogue-model"
    assert first.kwargs == continued.kwargs == {}
    assert len(executed) == 0
    assert session_history.load_history_records(SESSION) == before


def _outcomes(store: TopicStarterStore) -> list[tuple[str, str | None]]:
    return [(generation["outcome"], generation.get("reason")) for generation in _generations(store)]


async def test_tool_denial_keeps_each_provider_receipt_and_first_response_identity(product, monkeypatch) -> None:
    client = _ModelClient(
        AssistantMessage(content="译好了。", usage_metadata=_usage(900, 800, 10)),
        AssistantMessage(content="", response_id="fork-a", usage_metadata=_usage(1100, 1000, 20), tool_calls=[
            ToolCall(id="lookup-a", type="function", name="lookup", arguments="{}"),
            ToolCall(id="lookup-b", type="function", name="lookup", arguments="{}"),
        ]),
        AssistantMessage(content="", response_id="fork-b", usage_metadata=_usage(1170, 1100, 20), tool_calls=[
            ToolCall(id="lookup-c", type="function", name="lookup", arguments="{}"),
        ]),
        AssistantMessage(content="导出成双语对照", response_id="fork-c", usage_metadata=_usage(1240, 1200, 6)),
    )
    await _turn(_facade(_Host(await _agent(client, monkeypatch))), "req-1", "把这段说明译成英文")
    await _settled()

    assert [event.payload["text"] for event in product.page.events] == ["导出成双语对照"]
    assert client.requests[2].messages[-2:] == [
        ToolMessage(content="生成建议无需使用工具", tool_call_id="lookup-a"),
        ToolMessage(content="生成建议无需使用工具", tool_call_id="lookup-b"),
    ]
    assert client.requests[3].messages[-1] == ToolMessage(content="生成建议无需使用工具", tool_call_id="lookup-c")
    [generation] = _generations(product.store)
    assert generation["provider_response_id"] == "fork-a"
    assert generation["usage"]["prompt_tokens"] == 1100
    assert [(receipt["model"], receipt["provider_response_id"], receipt["usage"]["prompt_tokens"],
             receipt["usage"]["prompt_tokens_details"]["cached_tokens"], receipt["usage"]["completion_tokens"])
            for receipt in generation["provider_receipts"]] == [
        ("dialogue-model", "fork-a", 1100, 1000, 20),
        ("dialogue-model", "fork-b", 1170, 1100, 20),
        ("dialogue-model", "fork-c", 1240, 1200, 6),
    ]
    assert all(receipt["latency_ms"] >= 0 for receipt in generation["provider_receipts"])
    assert generation["total_usage"] == {
        "prompt_tokens": 3510, "completion_tokens": 46, "total_tokens": 3556,
        "prompt_tokens_details": {"cached_tokens": 3300}, "completion_tokens_details": {"reasoning_tokens": 0},
    }


@pytest.mark.parametrize(("first_text", "outcome", "reason", "shown"), [
    ("继续", "candidate", None, ["继续"]),
    ("谢谢，译得很好", "filtered", "evaluative", []),
])
async def test_tool_denial_selects_and_filters_the_first_assistant_text(
    product, monkeypatch, first_text, outcome, reason, shown,
) -> None:
    client = _ModelClient(
        AssistantMessage(content="译好了。"),
        AssistantMessage(content=first_text, response_id="first", tool_calls=[
            ToolCall(id="lookup-a", type="function", name="lookup", arguments="{}"),
        ]),
        AssistantMessage(content="导出成双语对照", response_id="last"),
    )
    await _turn(_facade(_Host(await _agent(client, monkeypatch))), "req-1", "把这段说明译成英文")
    await _settled()

    assert len(client.requests) == 3
    assert [event.payload["text"] for event in product.page.events] == shown
    assert _outcomes(product.store) == [(outcome, reason)]
    assert _generations(product.store)[0]["text"] == first_text


@pytest.mark.parametrize("limit", [1, 3])
async def test_tool_denial_inherits_the_main_agents_iteration_limit(product, monkeypatch, limit) -> None:
    client = _ModelClient(AssistantMessage(content="译好了。"), *(AssistantMessage(
        content="", response_id=f"fork-{index}", usage_metadata=_usage(1100, 1000, 20),
        tool_calls=[ToolCall(id=f"lookup-{index}", type="function", name="lookup", arguments="{}")],
    ) for index in range(limit)))
    agent = await _agent(client, monkeypatch)
    agent.config.max_iterations = limit

    await _turn(_facade(_Host(agent)), "req-1", "把这段说明译成英文")
    await _settled()

    assert len(client.requests) == limit + 1
    assert product.page.events == []
    assert _outcomes(product.store) == [("suppressed", "empty")]
    [generation] = _generations(product.store)
    assert len(generation["provider_receipts"]) == limit
    assert generation["missing_provider_receipts"] == 0 and generation["usage_complete"] is True


async def test_tool_denial_error_keeps_the_received_usage_and_never_reaches_the_page(product, monkeypatch) -> None:
    async def failure() -> AssistantMessage:
        raise RuntimeError("provider unavailable")

    client = _ModelClient(
        AssistantMessage(content="译好了。"),
        AssistantMessage(content="", response_id="fork-tool", usage_metadata=_usage(1100, 1000, 20), tool_calls=[
            ToolCall(id="lookup-a", type="function", name="lookup", arguments="{}"),
        ]),
        failure,
    )
    await _turn(_facade(_Host(await _agent(client, monkeypatch))), "req-1", "把这段说明译成英文")
    await _settled()

    assert len(client.requests) == 3
    assert product.page.events == []
    assert _outcomes(product.store) == [("failed", "RuntimeError")]
    [generation] = _generations(product.store)
    assert generation["provider_response_id"] == "fork-tool"
    assert len(generation["provider_receipts"]) == 1
    assert generation["total_usage"]["total_tokens"] == generation["usage"]["total_tokens"] == 1120
    assert generation["missing_provider_receipts"] == 1 and generation["usage_complete"] is False
    session_history.flush_history_writes()
    assert session_history.load_history_records(SESSION)[-1]["event_type"] == "chat.request_completed"


@pytest.mark.parametrize("entrypoint", ["process_message", "execute_message"])
async def test_non_stream_finished_turn_reaches_the_page(product, monkeypatch, entrypoint) -> None:
    main_answer = AssistantMessage(content="说明已经译成英文，术语按原文统一了。",
                                   usage_metadata=_usage(3000, 2600, 120))
    client = _ModelClient(main_answer, AssistantMessage(content="导出成双语对照"))
    facade = _facade(_Host(await _agent(client, monkeypatch)))
    request = AgentRequest(request_id="req-1", channel_id="web", session_id=SESSION,
                           req_method=ReqMethod.CHAT_SEND, params={"query": "把这段说明译成英文", "mode": "agent"})

    result = await getattr(facade, entrypoint)(request)
    await _settled()

    assert result.ok
    assert len(client.requests) == 2
    main_call, fork = client.requests
    assert fork.messages == [*main_call.messages, main_answer, fork.messages[-1]]
    assert fork.tools == main_call.tools
    assert fork.model == main_call.model and fork.kwargs == {}
    assert [event.payload["text"] for event in product.page.events] == ["导出成双语对照"]
    assert _outcomes(product.store) == [("candidate", None)]


@pytest.mark.parametrize(("ending", "reason"), [
    ({"event_type": "chat.error", "error": "上游服务不可用"}, "last_response_error"),
    (RuntimeError("request failed"), "last_response_error"),
    (asyncio.CancelledError(), "aborted"),
    ({"event_type": "chat.ask_user_question", "questions": [{"question": "要压到几页？"}]}, "pending_interaction"),
    ({"event_type": "harness.activate_interaction"}, "pending_interaction"),
    ({"approval_chunks": [{"event_type": "plan.approval_required"}]}, "pending_interaction"),
])
async def test_non_stream_failure_or_question_gets_no_suggestion(product, monkeypatch, ending, reason) -> None:
    client = _ModelClient(AssistantMessage(content="好的", usage_metadata=_usage(900, 800, 10)))
    host = _Host(await _agent(client, monkeypatch))
    host.ending = ending
    request = AgentRequest(request_id="req-1", channel_id="web", session_id=SESSION,
                           req_method=ReqMethod.CHAT_SEND, params={"query": "把这段说明译成英文", "mode": "agent"})

    with contextlib.suppress(RuntimeError, asyncio.CancelledError):
        await _facade(host).process_message(request)
    await _settled()

    assert len(client.requests) == 1
    assert product.page.events == []
    assert _outcomes(product.store) == [("suppressed", reason)]


@pytest.mark.parametrize(("ending", "reason"), [
    ({"event_type": "chat.error", "error": "上游服务不可用"}, "last_response_error"),
    (RuntimeError("stream broke"), "last_response_error"),
    (asyncio.CancelledError(), "aborted"),
    ({"event_type": "chat.ask_user_question", "source": "ask_user_interrupt", "request_id": "req-1",
      "questions": [{"question": "要压到几页？"}]}, "pending_interaction"),
    ({"event_type": "chat.ask_user_question", "source": "permission_interrupt", "request_id": "req-1",
      "questions": [{"question": "允许改写这个文件吗？"}]}, "pending_interaction"),
    ({"event_type": "harness.activate_interaction", "request_id": "req-1"}, "pending_interaction"),
    ({"event_type": "plan.approval_required", "request_id": "req-1"}, "pending_interaction"),
])
async def test_turn_that_did_not_end_with_the_answer_gets_no_suggestion(product, monkeypatch, ending, reason) -> None:
    client = _ModelClient(AssistantMessage(content="好的", usage_metadata=_usage(900, 800, 10)))
    host = _Host(await _agent(client, monkeypatch))
    host.ending = ending

    with contextlib.suppress(RuntimeError, asyncio.CancelledError):
        await _turn(_facade(host), "req-1", "把这段说明译成英文")
    await _settled()

    assert len(client.requests) == 1
    assert product.page.events == []
    assert _outcomes(product.store) == [("suppressed", reason)]


@pytest.mark.parametrize(("params", "metadata"), [
    ({"automation": {"kind": "heartbeat", "job_id": "hb-1", "run_id": "run-1"}}, None),
    ({"source": "proactive_recommendation"}, None),
    ({"team": True}, None),
    ({}, {"_jiuwenswarm_cross_session": {"message_id": "sm-1", "source_session_id": "other-session"}}),
])
@pytest.mark.parametrize("stream", [True, False])
async def test_turn_the_person_did_not_ask_for_gets_no_suggestion(product, monkeypatch, params, metadata, stream) -> None:
    client = _ModelClient(AssistantMessage(content="已处理", usage_metadata=_usage(900, 800, 10)))

    await _turn(_facade(_Host(await _agent(client, monkeypatch))), "req-1", "整理今天的待办", metadata=metadata,
                stream=stream, **params)
    await _settled()

    assert len(client.requests) == 1
    assert product.page.events == []
    # Nothing is recorded for a turn that was never a candidate for a suggestion.
    assert _outcomes(product.store) == []


async def test_answer_to_the_agents_question_is_followed_by_a_suggestion(product, monkeypatch) -> None:
    client = _ModelClient(
        AssistantMessage(content="先确认一下篇幅。", usage_metadata=_usage(900, 800, 10)),
        AssistantMessage(content="译好了，按一页排版。", usage_metadata=_usage(1100, 900, 30)),
        AssistantMessage(content="再出一版术语表", usage_metadata=_usage(1200, 1100, 6)),
    )
    host = _Host(await _agent(client, monkeypatch))
    facade = _facade(host)
    host.ending = {"event_type": "chat.ask_user_question", "source": "ask_user_interrupt", "request_id": "req-1",
                   "questions": [{"question": "要压到几页？"}]}
    await _turn(facade, "req-1", "把这段说明译成英文")

    # A Web page answers the question as control input into the session's execution.
    answer = AgentRequest(request_id="req-2", channel_id="web", session_id=SESSION, req_method=ReqMethod.CHAT_SEND,
                          params={"query": "一页", "mode": "agent", "source": "ask_user_interrupt",
                                  "request_id": "req-1",
                                  "answers": [{"question": "要压到几页？", "selected_options": ["一页"]}]},
                          is_stream=True)
    async for _ in facade.deliver_control_input(answer):
        pass
    await _settled()

    assert [event.payload["text"] for event in product.page.events] == ["再出一版术语表"]
    assert product.page.events[0].payload["request_id"] == "req-2"
    assert _outcomes(product.store) == [("suppressed", "pending_interaction"), ("candidate", None)]


async def test_deep_agent_turn_forks_the_main_agents_last_call(product, monkeypatch, tmp_path) -> None:
    question = {"header": "篇幅", "question": "要压到几页？",
                "options": [{"label": "一页", "description": "压到一页"}, {"label": "两页", "description": "保留两页"}]}
    main_answer = AssistantMessage(content="译好了，按一页排版。", reasoning_content="按一页排，术语沿用附录。",
                                   usage_metadata=_usage(3000, 2600, 120))
    client = _ModelClient(
        # The main agent hands a lookup to the general-purpose subagent, which runs on the same model.
        AssistantMessage(content="", tool_calls=[ToolCall(
            id="call-1", type="function", name="task_tool", arguments=json.dumps(
                {"subagent_type": "general-purpose", "task_description": "找出说明里的术语表"}, ensure_ascii=False),
        )], usage_metadata=_usage(2500, 2400, 20)),
        AssistantMessage(content="术语表在附录。", usage_metadata=_usage(800, 0, 10)),
        AssistantMessage(content="", tool_calls=[ToolCall(
            id="ask-1", type="function", name="ask_user",
            arguments=json.dumps({"questions": [question]}, ensure_ascii=False),
        )], usage_metadata=_usage(2700, 2500, 20)),
        main_answer,
        AssistantMessage(content="再出一版术语表", usage_metadata=_usage(3150, 3000, 6)),
    )

    async with _deep_host(client, tmp_path, monkeypatch) as host:
        facade = _facade(host)
        await _turn(facade, "req-1", "把这段说明译成英文")
        answer = AgentRequest(request_id="req-2", channel_id="web", session_id=SESSION,
                              req_method=ReqMethod.CHAT_SEND, is_stream=True,
                              params={"query": "一页", "mode": "agent", "source": "ask_user_interrupt",
                                      "request_id": "req-1",
                                      "answers": [{"question": "要压到几页？", "selected_options": ["一页"]}]})
        async for _ in facade.deliver_control_input(answer):
            pass
        await _settled()

    assert _outcomes(product.store) == [("suppressed", "pending_interaction"), ("candidate", None)]
    assert [(event.payload["request_id"], event.payload["text"]) for event in product.page.events] == [
        ("req-2", "再出一版术语表")]
    assert len(client.requests) == 5
    subagent_call, main_call, fork = client.requests[1], client.requests[3], client.requests[4]
    # The fork resends the main agent's last call as it was sent, system prompt and tools included,
    # then that call's answer and the suggestion prompt; the subagent's call is not the one forked.
    assert fork.model == main_call.model == "dialogue-model"
    assert fork.tools == main_call.tools and fork.tools
    assert fork.messages[0].role == "system"
    assert fork.messages[:len(main_call.messages)] == main_call.messages
    assert fork.messages[:len(subagent_call.messages)] != subagent_call.messages
    forked_answer = fork.messages[len(main_call.messages)]
    assert (forked_answer.content, forked_answer.reasoning_content) == (main_answer.content,
                                                                       main_answer.reasoning_content)
    assert fork.messages[-1] == UserMessage(content=prompt_suggestion.SUGGESTION_PROMPT)
    assert len(fork.messages) == len(main_call.messages) + 2
    assert fork.kwargs == {}


async def _slow_fork_turn(product, monkeypatch, during_fork, *, deny_tool: bool = False) -> None:
    """A turn whose suggestion is still being generated when ``during_fork`` runs."""
    fork_started, release = asyncio.Event(), asyncio.Event()

    async def slow_fork() -> AssistantMessage:
        fork_started.set()
        await release.wait()
        return AssistantMessage(content="导出成双语对照", usage_metadata=_usage(1100, 1000, 6))

    replies = [AssistantMessage(content="译好了。", usage_metadata=_usage(900, 800, 10))]
    if deny_tool:
        replies.append(AssistantMessage(content="", response_id="fork-tool", usage_metadata=_usage(1100, 1000, 20),
                                        tool_calls=[ToolCall(id="lookup-a", type="function", name="lookup", arguments="{}")]))
    client = _ModelClient(*replies, slow_fork)
    await _turn(_facade(_Host(await _agent(client, monkeypatch))), "req-1", "把这段说明译成英文")
    await fork_started.wait()
    await during_fork()
    release.set()
    await _settled()


@pytest.mark.parametrize("change", [
    {"prompt_enabled": False, "updated_at": 5.0},
    # Deleting the data turns recommendations off; turning them on again starts after the deletion.
    {"enabled": True, "deleted_at": 5.0, "updated_at": 6.0},
])
@pytest.mark.parametrize("deny_tool", [False, True])
async def test_suggestion_in_flight_when_recommendations_stop_writes_and_shows_nothing(
    product, monkeypatch, change, deny_tool,
) -> None:
    async def stop() -> None:
        product.controls.update(change)

    await _slow_fork_turn(product, monkeypatch, stop, deny_tool=deny_tool)

    assert product.page.events == []
    assert _outcomes(product.store) == []


async def test_agent_server_shutdown_cancels_the_suggestion_in_flight(product, monkeypatch) -> None:
    await _slow_fork_turn(product, monkeypatch, prompt_suggestion.forks.shutdown)

    assert prompt_suggestion.forks.in_flight() == []


@pytest.mark.parametrize("deny_tool", [False, True])
async def test_composer_event_cancels_only_its_sessions_in_flight_fork(product, monkeypatch, deny_tool) -> None:
    from jiuwenswarm.common.e2a.gateway_normalize import e2a_response_to_agent_response
    from jiuwenswarm.common.e2a.agent_compat import e2a_to_agent_request
    from jiuwenswarm.common.e2a.models import E2AResponse
    from gateway.channel_manager.web.app_web_handlers import WebHandlersBindParams, _register_web_handlers
    from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer

    server = AgentWebSocketServer()
    calls = []

    class AgentClient:
        async def send_request(self, envelope):
            calls.append(envelope)
            socket = _Browser()
            await server.dispatch_parsed_request(socket, e2a_to_agent_request(envelope), asyncio.Lock())
            return e2a_response_to_agent_response(E2AResponse.from_dict(socket.frames[-1]))

    class Page:
        channel_id = "web"

        def __init__(self):
            self.methods = {}
            self.responses = []

        def register_method(self, name, handler):
            self.methods[name] = handler

        def on_connect(self, _handler):
            pass

        def on_disconnect(self, _handler):
            pass

        async def send_response(self, _ws, _req_id, **response):
            self.responses.append(response)

    page = Page()
    _register_web_handlers(WebHandlersBindParams(channel=page, agent_client=AgentClient(),
                                                 topic_history_store=product.store))

    async def type_into_composer() -> None:
        invalid_socket = _Browser()
        await server.dispatch_parsed_request(invalid_socket, AgentRequest(
            request_id="invalid-cancel", channel_id="web", req_method=ReqMethod.PROMPT_SUGGESTION_CANCEL,
        ), asyncio.Lock())
        invalid_response = e2a_response_to_agent_response(E2AResponse.from_dict(invalid_socket.frames[-1]))
        assert invalid_response.ok is False and invalid_response.payload["code"] == "BAD_REQUEST"
        assert len(prompt_suggestion.forks.in_flight()) == 1
        event = page.methods["prompt_suggestion.event"]
        await event(None, "invalid", {"event_type": "input_changed", "session_id": ""}, "connection-only")
        assert page.responses[-1]["ok"] is False
        assert calls == []
        await event(None, "other", {"event_type": "input_changed", "session_id": "web_other_session"}, "connection-only")
        assert page.responses[-1]["ok"] is True
        assert page.responses[-1]["payload"] == {"cancelled": False}
        assert len(prompt_suggestion.forks.in_flight()) == 1
        await event(None, "typed", {"event_type": "input_changed", "session_id": SESSION}, "connection-only")
        assert page.responses[-1]["payload"] == {"cancelled": True}
        await asyncio.sleep(0)

    await _slow_fork_turn(product, monkeypatch, type_into_composer, deny_tool=deny_tool)

    assert product.page.events == []
    assert _outcomes(product.store) == [("cancelled", None)]
    if deny_tool:
        [generation] = _generations(product.store)
        assert generation["provider_response_id"] == "fork-tool"
        assert len(generation["provider_receipts"]) == 1
        assert generation["total_usage"]["total_tokens"] == 1120
        assert generation["missing_provider_receipts"] == 1 and generation["usage_complete"] is False
    assert prompt_suggestion.forks.answered_call(SESSION, "req-1") is not None
    assert product.page.events == []
    assert _outcomes(product.store) == [("cancelled", None)]



@pytest.mark.parametrize("stream", [True, False])
@pytest.mark.parametrize("deny_tool", [False, True])
async def test_new_request_cancels_the_suggestion_still_being_generated(product, monkeypatch, stream, deny_tool) -> None:
    fork_started = asyncio.Event()

    async def slow_fork() -> AssistantMessage:
        fork_started.set()
        await asyncio.Event().wait()
        raise AssertionError("the next request cancels this generation")

    replies = [AssistantMessage(content="译好了。", usage_metadata=_usage(900, 800, 10))]
    if deny_tool:
        replies.append(AssistantMessage(content="", response_id="fork-tool", usage_metadata=_usage(1100, 1000, 20),
                                        tool_calls=[ToolCall(id="lookup-a", type="function", name="lookup", arguments="{}")]))
    client = _ModelClient(
        *replies,
        slow_fork,
        AssistantMessage(content="术语表也整理好了。", usage_metadata=_usage(1000, 900, 10)),
        AssistantMessage(content="导出成双语对照", usage_metadata=_usage(1100, 1000, 6)),
    )
    facade = _facade(_Host(await _agent(client, monkeypatch)))
    await _turn(facade, "req-1", "把这段说明译成英文")
    await fork_started.wait()

    await _turn(facade, "req-2", "再整理一份术语表", stream=stream)
    await _settled()

    assert [event.payload["request_id"] for event in product.page.events] == ["req-2"]
    assert _outcomes(product.store) == [("cancelled", None), ("candidate", None)]
    if deny_tool:
        cancelled = _generations(product.store)[0]
        assert cancelled["provider_response_id"] == "fork-tool"
        assert len(cancelled["provider_receipts"]) == 1 and cancelled["missing_provider_receipts"] == 1


@pytest.mark.parametrize(("enabled", "parent_usage", "outcomes"), [
    # With recommendations off no suggestion is generated, and none is recorded.
    (False, _usage(900, 800, 10), []),
    # The fork reads the call's prompt from the cache and pays afresh for its 10,500-token answer.
    (True, _usage(14_000, 3_000, 10_500), [("suppressed", "cache_cold")]),
])
async def test_no_suggestion_when_recommendations_are_off_or_the_parent_was_not_cached(
    product, monkeypatch, enabled, parent_usage, outcomes,
) -> None:
    product.controls["prompt_enabled"] = enabled
    client = _ModelClient(AssistantMessage(content="译好了。", usage_metadata=parent_usage))

    await _turn(_facade(_Host(await _agent(client, monkeypatch))), "req-1", "把这段说明译成英文")
    await _settled()

    assert len(client.requests) == 1
    assert product.page.events == []
    assert _outcomes(product.store) == outcomes


async def test_a_first_turn_whose_prompt_was_not_cached_yet_is_followed_by_a_suggestion(product, monkeypatch) -> None:
    # A new conversation's first call reads nothing from the cache; it caches its prompt as it is
    # served, so the fork sent right after it pays afresh only for the short answer.
    client = _ModelClient(
        AssistantMessage(content="可以这样拍。", usage_metadata=_usage(37_000, 4_000, 300)),
        AssistantMessage(content="那逆光呢"),
    )

    await _turn(_facade(_Host(await _agent(client, monkeypatch))), "req-1", "阴天拍建筑怎么出立体感")
    await _settled()

    assert len(client.requests) == 2
    assert [event.payload["request_id"] for event in product.page.events] == ["req-1"]
    assert _outcomes(product.store) == [("candidate", None)]


@pytest.mark.parametrize(("reply", "outcome", "reason"), [
    (AssistantMessage(content="  "), "suppressed", "empty"),
    (AssistantMessage(content="谢谢，译得很好"), "filtered", "evaluative"),
    (AssistantMessage(content="我来帮你整理术语表"), "filtered", "claude_voice"),
    (AssistantMessage(content="（不输出）"), "filtered", "meta_wrapped"),
])
async def test_reply_that_is_not_the_users_next_input_is_not_shown(product, monkeypatch, reply, outcome, reason) -> None:
    client = _ModelClient(AssistantMessage(content="译好了。", usage_metadata=_usage(900, 800, 10)), reply)

    await _turn(_facade(_Host(await _agent(client, monkeypatch))), "req-1", "把这段说明译成英文")
    await _settled()

    assert len(client.requests) == 2
    assert product.page.events == []
    assert _outcomes(product.store) == [(outcome, reason)]


def test_prompt_may_predict_a_new_next_input_and_still_withholds_invented_consent() -> None:
    prompt = prompt_suggestion.SUGGESTION_PROMPT
    assert "回答里没有写过" in prompt
    assert "不要输出回答里没有的选项" not in prompt
    assert "不要输出替用户同意的那一句" in prompt
    assert "其它下一句" in prompt
    assert "（本轮不预测）" in prompt
    assert prompt_suggestion.PROMPT_VERSION == "prompt-suggestion-fork-v11"
    assert prompt_suggestion.MAX_SUGGESTIONS == 2


async def test_two_likely_next_inputs_reach_the_page_and_a_bad_line_is_dropped(product, monkeypatch) -> None:
    reply = AssistantMessage(
        content="买ETF，长期配置\n谢谢\n跌破4100美元时提醒我\n再写一条多余的",
        usage_metadata=_usage(3150, 3000, 12),
    )
    client = _ModelClient(
        AssistantMessage(content="说明已经译成英文。", usage_metadata=_usage(3000, 2600, 120)),
        reply,
    )
    await _turn(_facade(_Host(await _agent(client, monkeypatch))), "req-1", "黄金现在值得买吗")
    await _settled()

    shown = [event.payload["text"] for event in product.page.events]
    assert shown == ["买ETF，长期配置", "跌破4100美元时提醒我"]
    generations = _generations(product.store)
    assert [(row["outcome"], row["text"]) for row in generations] == [
        ("candidate", "买ETF，长期配置"),
        ("candidate", "跌破4100美元时提醒我"),
    ]
    assert len({row["suggestion_id"] for row in generations}) == 2
    assert [event.payload["suggestion_id"] for event in product.page.events] == [row["suggestion_id"] for row in generations]


@pytest.mark.parametrize(("text", "reason"), [
    pytest.param("无输出。", "meta_text", id="saved-phase20-empty-response"),
    ("无输出", "meta_text"), ("无输出！", "meta_text"),
    ("不输出", "meta_text"), ("不输出。", "meta_text"),
    pytest.param("什么都不输出。", "meta_text", id="review-acceptance-20261013-echoed-instruction"),
    ("什么也不输出", "meta_text"),
    ("检查无输出的原因", None), ("排查不输出的原因", None), ("为什么脚本什么都不输出", None),
])
async def test_chinese_empty_results_are_not_sent_but_commands_containing_the_words_are(
    product, monkeypatch, text, reason,
) -> None:
    fork_started, release = asyncio.Event(), asyncio.Event()

    async def reply() -> AssistantMessage:
        fork_started.set()
        await release.wait()
        return AssistantMessage(content=text, usage_metadata=_usage(1100, 1000, 6))

    main_answer = AssistantMessage(content="收到。", usage_metadata=_usage(900, 800, 10))
    client = _ModelClient(main_answer, reply)
    agent = await _agent(client, monkeypatch)
    executed = []

    async def run_tool(*args: Any, **kwargs: Any):
        executed.append((args, kwargs))
        return "found", ToolMessage(content="found", tool_call_id="lookup")

    monkeypatch.setattr(agent.ability_manager, "_execute_single_tool_call", run_tool)
    query = "本轮只确认收到" if reason else "检查这段脚本为什么没有输出"
    await _turn(_facade(_Host(agent)), "req-1", query)
    await fork_started.wait()
    session_history.flush_history_writes()
    before = session_history.load_history_records(SESSION)
    release.set()
    await _settled()

    assert len(client.requests) == 2
    main, fork = client.requests
    assert fork.messages[:-2] == main.messages
    assert fork.messages[-2] == main_answer
    assert fork.tools == main.tools
    assert fork.model == main.model == "dialogue-model"
    assert fork.kwargs == main.kwargs == {}
    assert len(executed) == 0
    assert session_history.load_history_records(SESSION) == before
    assert [event.payload["text"] for event in product.page.events] == ([] if reason else [text])
    assert _outcomes(product.store) == [("filtered" if reason else "candidate", reason)]
    assert _generations(product.store)[0]["text"] == text


@pytest.mark.parametrize(("text", "reason"), [
    ("继续下一步", "nonspecific"), ("继续下一步。", "nonspecific"),
    ("继续下一步!", "nonspecific"), ("继续下一步……", "nonspecific"),
    ("继续", None), ("继续吧", None), ("是的", None), ("试试看", None),
    ("继续比较大潮和小潮", None), ("继续下一步比较口感", None),
    ("解释“继续下一步”的含义", None),
])
async def test_next_step_placeholder_is_not_sent_but_specific_commands_are(product, monkeypatch, text, reason) -> None:
    client = _ModelClient(
        AssistantMessage(content="比较已经答完。", usage_metadata=_usage(900, 800, 10)),
        AssistantMessage(content=text, usage_metadata=_usage(1100, 1000, 6)),
    )

    await _turn(_facade(_Host(await _agent(client, monkeypatch))), "req-1", "解释合并与删除记忆，再比较适用情况")
    await _settled()

    assert [event.payload["text"] for event in product.page.events] == ([] if reason else [text])
    assert _outcomes(product.store) == [("filtered" if reason else "candidate", reason)]


@pytest.mark.parametrize(("text", "reason"), [
    ("是的", None), ("当然", None), ("好", None), ("好的", None),
    ("推送", None), ("提交", None), ("部署", None), ("停止", None),
    ("继续", None), ("检查", None), ("退出", None), ("不", None), ("不要", None),
    ("继续吧", None), pytest.param("试试看", None, id="try-it-out"),
    ("北京", "too_few_words"), ("SQLite", "too_few_words"), ("看起来不错", "evaluative"),
    ("无建议", "meta_text"), ("继续。提交", "multiple_sentences"),
])
async def test_chinese_source_commands_keep_the_original_short_input_exception(product, monkeypatch, text, reason) -> None:
    client = _ModelClient(
        AssistantMessage(content="译好了。", usage_metadata=_usage(900, 800, 10)),
        AssistantMessage(content=text, usage_metadata=_usage(1100, 1000, 6)),
    )

    await _turn(_facade(_Host(await _agent(client, monkeypatch))), "req-1", "把这段说明译成英文")
    await _settled()

    assert [event.payload["text"] for event in product.page.events] == ([text] if reason is None else [])
    assert _outcomes(product.store) == [("candidate" if reason is None else "filtered", reason)]


def _completed(request_id: str) -> None:
    session_history.append_history_record(session_id=SESSION, request_id=request_id, channel_id="web", role="user",
                                          content="把这段说明译成英文", timestamp=1.0)
    session_history.enqueue_history_request_completion(SESSION, request_id).result(timeout=2)


async def _push_suggestion(request_id: str) -> None:
    await WebSocketGatewayPushTransport().send_push({
        "channel_id": "", "session_id": SESSION, "request_id": request_id,
        "payload": {"event_type": "prompt_suggestion.ready", "suggestion_id": uuid4().hex, "text": "导出成双语对照"},
    })


@pytest.mark.parametrize(("change", "deliveries"), [
    ("another request", [("suppressed", "stale_request")]),
    # With recommendations off the gateway neither pushes nor records the suggestion.
    ("consent withdrawn", []),
])
async def test_gateway_drops_a_suggestion_that_is_no_longer_current(product, change, deliveries) -> None:
    _completed("req-1")
    if change == "another request":
        session_history.append_history_record(session_id=SESSION, request_id="req-2", channel_id="web", role="user",
                                              content="再整理一份术语表", timestamp=2.0)
        session_history.flush_history_writes()
    else:
        product.controls["prompt_enabled"] = False

    await _push_suggestion("req-1")

    assert product.page.events == []
    rows = product.store.query_events(policy_id=POLICY_ID, event_type="prompt_suggestion_delivery", limit=10)
    assert [(row["payload"]["status"], row["payload"]["reason"]) for row in rows] == deliveries


@pytest.mark.parametrize("missing", ["session", "model client"])
async def test_a_model_call_without_a_session_or_model_client_is_left_alone(product, missing) -> None:
    agent = ReActAgent(AgentCard(id=uuid4().hex, name="host", description="host"))
    agent.config.model_name = "dialogue-model"
    if missing != "model client":
        agent.set_llm(_ModelClient())
    ctx = AgentCallbackContext(
        agent=agent,
        inputs=ModelCallInputs(messages=[UserMessage(content="把这段说明译成英文")],
                               response=AssistantMessage(content="译好了。", usage_metadata=_usage(900, 800, 10))),
        session=None if missing == "session" else SimpleNamespace(get_session_id=lambda: SESSION),
    )
    forks = prompt_suggestion.forks
    forks.request_started(SESSION, person_turn=True)

    await prompt_suggestion.PromptSuggestionForkRail().after_model_call(ctx)

    forks.turn_ended(SESSION, "req-1", status="success", waiting_for_user=False, completion=None)
    assert forks.answered_call(SESSION, "req-1") is None
    assert _outcomes(product.store) == [("suppressed", "no_model_call")]


@pytest.mark.parametrize("mode", ["plan", "normal"])
async def test_a_turn_in_plan_mode_gets_no_suggestion(product, mode) -> None:
    agent = ReActAgent(AgentCard(id=uuid4().hex, name="host", description="host"))
    agent.config.model_name = "dialogue-model"
    agent.set_llm(_ModelClient())
    rail = prompt_suggestion.PromptSuggestionForkRail()
    # The conversation's DeepAgent, as far as the rail reads it: the session's state.
    rail.init(SimpleNamespace(load_state=lambda _session: DeepAgentState(plan_mode=PlanModeState(mode=mode))))
    ctx = AgentCallbackContext(
        agent=agent,
        inputs=ModelCallInputs(messages=[UserMessage(content="先出个改版计划，等我确认再动手")],
                               response=AssistantMessage(content="计划分三步……", usage_metadata=_usage(900, 800, 10))),
        session=SimpleNamespace(get_session_id=lambda: SESSION),
    )
    forks = prompt_suggestion.forks
    forks.request_started(SESSION, person_turn=True)

    await rail.after_model_call(ctx)
    forks.turn_ended(SESSION, "req-1", status="success", waiting_for_user=False, completion=None)
    await forks.shutdown()

    assert (("suppressed", "plan_mode") in _outcomes(product.store)) is (mode == "plan")
    assert bool(forks.in_flight()) is False


def test_every_agent_rail_set_keeps_the_last_model_call(monkeypatch) -> None:
    # Importing the real observability rail starts an exporter; this test is about the standing rails only.
    stub = types.ModuleType("openjiuwen.harness.observability")
    stub.AgentObservabilityRail = lambda: object()
    monkeypatch.setitem(sys.modules, "openjiuwen.harness.observability", stub)

    rails = object.__new__(JiuWenSwarmDeepAdapter)._instantiate_rails([], {})  # pylint: disable=protected-access

    assert sum(isinstance(rail, prompt_suggestion.PromptSuggestionForkRail) for rail in rails) == 1


@pytest.mark.parametrize("text", [
    "导出成双语对照", "好的，继续", "检查错别字", "术语要统一成哪套译法？", "帮我来一份术语表", "提醒我不错过截止日期",
    "run the tests", "yes", "/review", "push it to the release branch",
])
def test_filter_keeps_what_the_user_would_type(text) -> None:
    assert prompt_suggestion.filter_suggestion(text) is None


@pytest.mark.parametrize(("text", "reason"), [
    ("done", "done"),
    ("不出", "meta_text"),
    ("（不出）", "meta_text"),
    ("Stay silent, the next step is unclear", "meta_text"),
    ("[no suggestion]", "meta_wrapped"),
    ("（不输出）", "meta_wrapped"),
    ("本轮不预测", "meta_text"),
    ("（本轮不预测）", "meta_wrapped"),
    ("API Error: 500 upstream", "error_message"),
    ("Suggestion: run the tests", "prefixed_label"),
    ("建议：检查错别字", "prefixed_label"),
    ("北京", "too_few_words"),
    ("refactor", "too_few_words"),
    ("把整份说明逐段译成英文并在每段后面附上原文和页码以便逐句核对术语", "too_long"),
    ("please run every single unit test in the repository and then report back to me", "too_many_words"),
    ("检查错别字。然后导出", "multiple_sentences"),
    ("Run the tests. Then push it", "multiple_sentences"),
    ("**导出**成双语对照", "has_formatting"),
    ("- 导出成双语对照", "has_formatting"),
    ("looks good, ship it", "evaluative"),
    ("谢谢，就这样吧", "evaluative"),
    ("Let me run the tests", "claude_voice"),
    ("我帮你导出成双语对照", "claude_voice"),
    ("以下是术语表的整理方案", "claude_voice"),
])
def test_filter_drops_what_is_not_the_users_next_input(text, reason) -> None:
    assert prompt_suggestion.filter_suggestion(text) == reason


class _Browser:
    """A page's websocket."""

    remote_address = ("127.0.0.1", 12345)

    def __init__(self) -> None:
        self.frames: list[dict[str, Any]] = []

    async def send(self, data: str) -> None:
        self.frames.append(json.loads(data))


async def test_suggestion_reaches_the_pages_open_on_its_session_only(product) -> None:
    channel = WebChannel(WebChannelConfig(enabled=True), RobotMessageRouter())
    channel.on_message(lambda _msg: None)
    on_session, elsewhere = _Browser(), _Browser()
    for browser, session_id in ((on_session, SESSION), (elsewhere, "web_other_session")):
        await getattr(channel, "_handle_raw_message")(browser, json.dumps({
            "type": "req", "id": f"open-{session_id}", "method": "chat.send",
            "params": {"session_id": session_id, "content": "你好"},
        }), {})
    _completed("req-1")

    await prompt_suggestion.deliver(channel, product.store, SESSION, "req-1",
                                         {"suggestion_id": "s-1", "text": "导出成双语对照"})
    for _ in range(20):
        await asyncio.sleep(0)

    shown = [frame["payload"] for frame in on_session.frames if frame.get("event") == "prompt_suggestion.ready"]
    assert [{key: payload[key] for key in ("session_id", "request_id", "suggestion_id", "text")} for payload in shown] == [
        {"session_id": SESSION, "request_id": "req-1", "suggestion_id": "s-1", "text": "导出成双语对照"},
    ]
    assert not [frame for frame in elsewhere.frames if frame.get("event") == "prompt_suggestion.ready"]


async def test_turn_whose_last_call_still_asked_for_a_tool_gets_no_suggestion(product, monkeypatch) -> None:
    # The turn ran out of iterations right after the model asked for a tool: that call has no answer
    # to follow, and a request ending on an unanswered tool call is malformed.
    client = _ModelClient(AssistantMessage(
        content="", tool_calls=[ToolCall(id="call-1", type="function", name="lookup", arguments="{}")],
        usage_metadata=_usage(900, 800, 10),
    ))
    agent = await _agent(client, monkeypatch)
    agent.config.max_iterations = 1

    await _turn(_facade(_Host(agent)), "req-1", "把这段说明译成英文")
    await _settled()

    assert len(client.requests) == 1
    assert product.page.events == []
    assert _outcomes(product.store) == [("suppressed", "unanswered_tool_call")]

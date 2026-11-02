# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Client tools: the request events a pause streams and the answers that resume it."""

from __future__ import annotations

import asyncio
import copy
import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from openjiuwen.core.foundation.llm import (
    AssistantMessage, ModelClientConfig, ModelRequestConfig, ToolCall, UsageMetadata,
)
from openjiuwen.core.foundation.llm.schema.message_chunk import AssistantMessageChunk
from openjiuwen.core.foundation.tool import ClientTool, ClientToolResult, ClientToolStatus, ToolCard
from openjiuwen.core.runner import Runner
from openjiuwen.core.session.agent import create_agent_session
from openjiuwen.core.single_agent.interrupt import ClientToolRequest
from openjiuwen.harness import create_deep_agent
from openjiuwen.harness.schema.interaction import SendInputRequest

from jiuwenswarm.agents.harness.common.rails.interrupt.interrupt_helpers import (
    build_permission_rail,
    convert_interactions_to_ask_user_question,
    is_interrupt_resume_payload,
)
from gateway.common.interrupt_resume import CLIENT_TOOL_INTERRUPT_SOURCE
from jiuwenswarm.common.schema.message import EventType, ReqMethod
from jiuwenswarm.runtime.client_tools import (
    ClientToolAnswer,
    client_tool_answers,
    client_tool_request_event,
)
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.observability.turn import SessionTurnTracker
from jiuwenswarm.runtime import AgentRuntime
from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer
from jiuwenswarm.server.runtime.agent_adapter.interface import JiuWenSwarm
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter
from tests.unit_tests.runtime.test_runtime_service import FakeAgentManager, FakePlanController

_REQUEST = {"interrupt_id": "intr_1", "interrupt_kind": "client_tool", "tool_name": "read_page",
            "arguments": {"page": 2}, "client_request": {"input": {"page": 1}}, "tool_call_id": "call-1"}


def test_a_paused_client_tool_call_becomes_one_request_event() -> None:
    event = {
        "event_type": EventType.CHAT_CLIENT_TOOL_REQUEST.value, "request_id": "call-1", "interrupt_id": "intr_1",
        "source": CLIENT_TOOL_INTERRUPT_SOURCE, "tool_name": "read_page", "arguments": {"page": 2},
        "client_tool": {"input": {"page": 1}},
    }
    assert client_tool_request_event({"id": "call-1", "value": _REQUEST}) == event
    assert client_tool_request_event(SimpleNamespace(id="call-1", value=ClientToolRequest(**_REQUEST))) == event
    # A tool without build_request sends no request of its own: the client runs the call's final arguments.
    card_only = ClientToolRequest(**{**_REQUEST, "client_request": {}})
    assert client_tool_request_event({"id": "call-1", "value": card_only}) == {**event, "client_tool": {}}
    # Questions and approvals keep their own projection, and a client tool call is not a question.
    question = {"interrupt_kind": "ask_user", "tool_name": "ask_user", "questions": []}
    assert client_tool_request_event({"id": "call-1", "value": question}) is None
    assert client_tool_request_event({"id": "", "value": _REQUEST}) is None
    assert convert_interactions_to_ask_user_question([{"id": "call-1", "value": _REQUEST}]) is None


def test_each_result_answers_the_interrupt_it_names() -> None:
    answers = [
        {"interrupt_id": "intr_a", "request_id": "a",
         "result": {"content": [{"type": "text", "text": "one"}], "is_error": False, "data": {"slide": 3}}},
        {"interrupt_id": "intr_b", "request_id": "b", "result": {"status": "client_gone"}},
        {"request_id": "c", "result": {"content": "no interrupt id"}},
        {"interrupt_id": "intr_d", "request_id": "d", "result": "plain text"},
        {"interrupt_id": "intr_e", "request_id": "e", "result": {"slide": 3, "title": "Plan"}},
        {"interrupt_id": "intr_f", "result": {"content": "no call id"}}, "noise",
        {"interrupt_id": "intr_g", "request_id": "g"},
    ]
    assert client_tool_answers(answers) == [
        ClientToolAnswer(request_id="a", interrupt_id="intr_a",
                         result=ClientToolResult(content=[{"type": "text", "text": "one"}], data={"slide": 3})),
        ClientToolAnswer(request_id="b", interrupt_id="intr_b",
                         result=ClientToolResult(status=ClientToolStatus.CLIENT_GONE)),
        ClientToolAnswer(request_id="d", interrupt_id="intr_d", result=ClientToolResult(content="plain text")),
        ClientToolAnswer(request_id="e", interrupt_id="intr_e",
                         result=ClientToolResult.coerce({"slide": 3, "title": "Plan"})),
    ]
    built = JiuWenSwarm._build_interactive_input_from_answers("a", answers, CLIENT_TOOL_INTERRUPT_SOURCE)
    assert set(built.interrupt_answers) == {"intr_a", "intr_b", "intr_d", "intr_e"}
    assert built.user_inputs == {}
    assert is_interrupt_resume_payload({"source": CLIENT_TOOL_INTERRUPT_SOURCE, "request_id": "a",
                                        "answers": answers[:1]})


class _Socket:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, data) -> None:
        self.sent.append(json.loads(data))


def test_a_client_resume_nobody_waits_on_is_refused_with_a_code_the_client_drops_it_by() -> None:
    server = AgentWebSocketServer.__new__(AgentWebSocketServer)
    server._session_message_service = None
    server._session_stream_tasks = {}
    runtime = AgentRuntime(agent_manager=FakeAgentManager(), initializer=AsyncMock(),
                           plan_controller=FakePlanController())
    server._execution_runtime = lambda: runtime
    server._trigger_before_chat_request_hook = AsyncMock()
    socket = _Socket()
    resume = AgentRequest(request_id="resume-1", channel_id="web", session_id="session-1", user_id="user-1",
                          req_method=ReqMethod.CHAT_SEND, is_stream=False, metadata={"ws_id": "ws-1"},
                          params={"source": CLIENT_TOOL_INTERRUPT_SOURCE, "answers": [
                              {"interrupt_id": "gone", "request_id": "call-1", "result": {"content": "late"}}]})

    asyncio.run(server.dispatch_parsed_request(socket, resume, asyncio.Lock()))

    refusal = socket.sent[-1]
    assert (refusal["status"], refusal["body"]["details"]["code"]) == ("failed", "CLIENT_TOOL_ANSWER_MISMATCH")


def test_a_client_tool_resume_continues_the_turn_that_asked() -> None:
    adapter = JiuWenSwarmDeepAdapter.__new__(JiuWenSwarmDeepAdapter)
    adapter._instance = SimpleNamespace()
    adapter._turn_tracker = SessionTurnTracker()
    asked = adapter._resolve_trajectory_turn({"query": "add a slide", "mode": "agent"})
    resume = {"source": CLIENT_TOOL_INTERRUPT_SOURCE, "answers": [
        {"interrupt_id": "intr_1", "request_id": "call-1", "result": {"status": "client_gone"}}]}

    assert adapter._resolve_trajectory_turn(resume) == asked


# --- the real SDK loop ------------------------------------------------------------


class _ParallelModel:
    """First turn: two client tool calls at once; then answer with what came back."""

    def __init__(self) -> None:
        self.model_client_config = ModelClientConfig(
            client_provider="OpenAI", api_key="local-test", api_base="http://127.0.0.1:1",
        )
        self.model_config = ModelRequestConfig(model_name="client-tool-test")
        self.messages: list = []

    async def invoke(self, messages, **kwargs):
        self.messages.append(copy.deepcopy(messages))
        if not any(getattr(m, "tool_call_id", None) for m in messages):
            return AssistantMessage(content="", tool_calls=[
                ToolCall(id=f"read-{page}", type="function", name="read_page",
                         arguments=json.dumps({"page": page}))
                for page in (1, 2)
            ], usage_metadata=UsageMetadata(model_name="client-tool-test", finish_reason="tool_calls"))
        return AssistantMessage(content="done", usage_metadata=UsageMetadata(
            model_name="client-tool-test", finish_reason="stop"))

    async def stream(self, messages, **kwargs):
        response = await self.invoke(messages, **kwargs)
        yield AssistantMessageChunk(
            content=response.content, tool_calls=response.tool_calls, usage_metadata=response.usage_metadata,
        )

    def tool_results(self) -> dict[str, str]:
        return {m.tool_call_id: m.content for m in self.messages[-1] if getattr(m, "tool_call_id", None)}


class _ReadPage(ClientTool):
    def __init__(self) -> None:
        super().__init__(ToolCard(name="read_page", description="Read one page in the client", input_params={
            "type": "object", "properties": {"page": {"type": "integer"}},
        }))

    def build_request(self, arguments, context):
        return {"input": arguments}


class _Loop:
    """One DeepAgent turn at a time, keeping what the stream sent the client."""

    def __init__(self, tmp_path, rails=()) -> None:
        self.model = _ParallelModel()
        self.agent = create_deep_agent(
            model=self.model, tools=[_ReadPage()], rails=list(rails),
            workspace=str(tmp_path), enable_task_loop=True, max_iterations=4,
            enable_model_anomaly_detection_rail=False, enable_read_image_multimodal=False,
        )

    async def __aenter__(self) -> "_Loop":
        await Runner.start()
        session = create_agent_session(session_id=f"client-tool-{uuid.uuid4().hex}", card=self.agent.card)
        await session.pre_run(inputs={})
        await self.agent.ensure_initialized()
        await self.agent.start(session=session)
        return self

    async def __aexit__(self, *_exc) -> None:
        await self.agent.stop()
        await Runner.stop()

    async def turn(self, request_id: str, query) -> list:
        stream = await self.agent.attach_output()
        reader = asyncio.create_task(_collect(stream))
        await self.agent.send_input(SendInputRequest(request_id=request_id, inputs={"query": query}))
        chunks = await asyncio.wait_for(reader, 15)
        await stream.close()
        return [chunk.payload for chunk in chunks if getattr(chunk, "type", None) == "__interaction__"]


async def _collect(stream):
    return [chunk async for chunk in stream]


def _requests(interactions: list) -> list[dict]:
    events = (client_tool_request_event(item) for item in interactions)
    return [event for event in events if event]


def _answer(*results: tuple[dict, object]):
    """The resume a client sends: one text result, or a status, per answered request."""
    return JiuWenSwarm._build_interactive_input_from_answers(
        results[0][0]["request_id"],
        [{"interrupt_id": request["interrupt_id"], "request_id": request["request_id"],
          "result": result if isinstance(result, dict) else {"content": result}}
         for request, result in results],
        CLIENT_TOOL_INTERRUPT_SOURCE,
    )


@pytest.mark.asyncio
async def test_parallel_calls_pause_once_and_one_resume_answers_both(tmp_path) -> None:
    async with _Loop(tmp_path) as loop:
        requests = _requests(await loop.turn("turn", "read both pages"))
        assert [(r["request_id"], r["client_tool"]) for r in requests] == [
            ("read-1", {"input": {"page": 1}}), ("read-2", {"input": {"page": 2}}),
        ]
        assert all(r["interrupt_id"] for r in requests)

        assert _requests(await loop.turn("resume", _answer((requests[0], "one"), (requests[1], "two")))) == []
        assert loop.model.tool_results() == {"read-1": "one", "read-2": "two"}


@pytest.mark.asyncio
async def test_a_call_a_resume_leaves_unanswered_stays_paused_with_its_interrupt(tmp_path) -> None:
    async with _Loop(tmp_path) as loop:
        first, second = _requests(await loop.turn("turn", "read both pages"))

        still = _requests(await loop.turn("partial", _answer((first, "one"))))
        assert [(r["request_id"], r["interrupt_id"]) for r in still] == [("read-2", second["interrupt_id"])]

        assert _requests(await loop.turn("rest", _answer((second, "two")))) == []
        assert loop.model.tool_results() == {"read-1": "one", "read-2": "two"}


@pytest.mark.asyncio
async def test_a_call_whose_client_went_away_ends_with_a_retryable_error(tmp_path) -> None:
    async with _Loop(tmp_path) as loop:
        first, second = _requests(await loop.turn("turn", "read both pages"))
        gone = {"status": "client_gone"}
        assert _requests(await loop.turn("gone", _answer((first, gone), (second, gone)))) == []
        results = loop.model.tool_results()
        assert set(results) == {"read-1", "read-2"}
        assert all("disconnected" in text and "call it again" in text for text in results.values())


@pytest.mark.asyncio
async def test_a_denied_call_never_reaches_the_client(tmp_path) -> None:
    rail = build_permission_rail({"permissions": {"enabled": True, "tools": {"read_page": "deny"}}})
    async with _Loop(tmp_path, rails=[rail]) as loop:
        assert _requests(await loop.turn("turn", "read both pages")) == []
        results = loop.model.tool_results()
        assert set(results) == {"read-1", "read-2"}
        assert all("PERMISSION_DENIED" in text for text in results.values())


@pytest.mark.asyncio
async def test_an_approved_call_goes_to_the_client_once(tmp_path) -> None:
    """Asked like any tool: the client gets the call only after the user allows it."""
    rail = build_permission_rail({"permissions": {"enabled": True, "tools": {"read_page": "ask"}}})
    async with _Loop(tmp_path, rails=[rail]) as loop:
        asked = await loop.turn("turn", "read both pages")
        assert _requests(asked) == []
        approvals = [convert_interactions_to_ask_user_question([item]) for item in asked]
        assert [a["request_id"] for a in approvals] == ["read-1", "read-2"]

        for approval in approvals:
            allow = JiuWenSwarm._build_interactive_input_from_answers(
                approval["request_id"], [{"selected_options": ["allow_once"]}], "permission_interrupt",
                interrupt_id=approval["interrupt_id"],
            )
            # Every pause lists each call still waiting for its client.
            requests = _requests(await loop.turn(f"allow-{approval['request_id']}", allow))
        assert sorted(r["request_id"] for r in requests) == ["read-1", "read-2"]

        assert _requests(await loop.turn("resume", _answer(*[(r, f"text {r['request_id']}") for r in requests]))) == []
        assert loop.model.tool_results() == {"read-1": "text read-1", "read-2": "text read-2"}

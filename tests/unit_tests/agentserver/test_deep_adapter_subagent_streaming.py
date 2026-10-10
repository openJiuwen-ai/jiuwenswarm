# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Subagents use streaming transport without changing TaskTool's final result."""

import asyncio
import json

import httpx
import pytest
import pytest_asyncio
from openai import AsyncOpenAI
from openjiuwen.core.foundation.llm import ModelClientConfig, ModelRequestConfig
from openjiuwen.core.foundation.llm.model_clients.openai_model_client import OpenAIModelClient
from openjiuwen.core.foundation.tool import ToolCard
from openjiuwen.core.foundation.tool.function.function import LocalFunction
from openjiuwen.core.session.agent import create_agent_session
from openjiuwen.core.single_agent import AgentCard
from openjiuwen.core.single_agent.rail.base import AgentRail
from openjiuwen.harness import create_deep_agent
from openjiuwen.harness.schema.config import SubAgentConfig
from openjiuwen.harness.tools.subagent.task_tool import TaskTool

from jiuwenswarm.common.invocation_context.model_trace import TraceAwareModel
from jiuwenswarm.llm_sse_patch import apply_openai_sse_stream_patch
# Import the desktop/Swarm adapter's runtime patches.
from jiuwenswarm.server.runtime.agent_adapter import interface_deep  # noqa: F401


def _sse(*deltas, finish_reason="stop", gateway_format=False):
    frames = [
        {
            "id": "completion-test", "created": 1, "model": "test",
            "object": "chat.completion.chunk",
            "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
        }
        for delta in deltas
    ]
    frames.append({
        "id": "completion-test", "created": 1, "model": "test",
        "object": "chat.completion.chunk",
        "choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
    })
    if gateway_format:
        for frame in frames:
            choice = frame["choices"][0]
            delta = choice.pop("delta")
            choice["message"] = {
                "token_text": delta.get("content", ""),
                "reasoning_token_text": delta.get("reasoning_content", ""),
            }
    return "".join("data: " + json.dumps(frame) + "\n\n" for frame in frames) + "data: [DONE]\n\n"


@pytest.fixture
def parent(tmp_path, monkeypatch):
    # The independent vision-capability probe is not part of task execution.
    monkeypatch.setattr("openjiuwen.harness.deep_agent.schedule_image_support_probe",
                        lambda model: None)
    model = TraceAwareModel(
        model_client_config=ModelClientConfig(
            client_provider="OpenAI", api_key="test-key", api_base="http://test.invalid/v1",
        ),
        model_config=ModelRequestConfig(model="test"),
    )
    return create_deep_agent(
        model=model, workspace=str(tmp_path), auto_create_workspace=False,
        add_general_purpose_agent=True, max_iterations=3,
        subagents=[SubAgentConfig(
            agent_card=AgentCard(name="specialist", description="test specialist"),
            system_prompt="Test specialist.",
            factory_kwargs={"auto_create_workspace": False},
        )],
    )


async def _delegate(parent, subagent_type):
    session = create_agent_session(session_id=parent.card.id, card=parent.card)
    await session.pre_run()
    tool = TaskTool(ToolCard(id="test-task", name="task_tool", description="test"), parent)
    try:
        return await tool.invoke(
            {"subagent_type": subagent_type, "task_description": "Return the test result."},
            session=session, tool_call_id="test-call",
        )
    finally:
        await session.post_run()


@pytest_asyncio.fixture
async def gateway(parent, monkeypatch):
    # Production startup installs this compatibility patch for the desktop gateway.
    monkeypatch.setattr(OpenAIModelClient, "_parse_stream_chunk", OpenAIModelClient._parse_stream_chunk)
    monkeypatch.setattr(OpenAIModelClient, "_sse_stream_patch_applied",
                        getattr(OpenAIModelClient, "_sse_stream_patch_applied", False), raising=False)
    apply_openai_sse_stream_patch()
    requests = []
    responses = []

    def respond(request):
        body = json.loads(request.content)
        requests.append(body)
        if not body.get("stream"):
            # Reproduce the gateway's HTTP 200 / empty non-streaming reply.
            return httpx.Response(200, json={
                "id": "empty", "created": 1, "model": "test", "object": "chat.completion",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": ""},
                             "finish_reason": "stop"}],
            })
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, text=responses.pop(0),
        )

    async with AsyncOpenAI(
        api_key="test-key", base_url="http://test.invalid/v1", max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)),
    ) as sdk:
        monkeypatch.setattr(parent._deep_config.model._client, "_create_async_openai_client",
                            lambda timeout=None: sdk)
        yield requests, responses


@pytest.mark.asyncio
@pytest.mark.parametrize("subagent_type", ["general-purpose", "specialist"])
@pytest.mark.parametrize("gateway_format", [False, True], ids=["openai", "desktop-gateway"])
async def test_task_tool_streams_and_returns_aggregated_text(parent, gateway, subagent_type, gateway_format):
    requests, responses = gateway
    messages = []

    class CaptureResponseRail(AgentRail):
        async def after_model_call(self, ctx):
            messages.append(ctx.inputs.response)

    spec = parent._find_subagent_spec(subagent_type)
    spec.rails = [*(spec.rails or []), CaptureResponseRail()]
    responses.append(_sse({"reasoning_content": "Think first."},
                          {"content": "Hello "}, {"content": "world"}, gateway_format=gateway_format))
    result = await _delegate(parent, subagent_type)
    assert requests and all(request["stream"] is True for request in requests)
    assert result.success is True
    assert result.data["output"] == "Hello world"
    assert result.data["agent_id"]
    assert messages[0].reasoning_content == "Think first."
    assert messages[0].usage_metadata.total_tokens == 12


@pytest.mark.asyncio
async def test_task_tool_executes_fragmented_tool_call_then_streams_answer(parent, gateway):
    requests, responses = gateway
    calls = []

    def echo(text):
        calls.append(text)
        return "tool result: " + text

    parent._find_subagent_spec("specialist").tools = [LocalFunction(
        ToolCard(id="test-echo", name="echo", description="Echo text.", input_params={
            "type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"],
        }), echo,
    )]
    responses.extend([
        _sse(
            {"tool_calls": [{"index": 0, "id": "call-echo", "type": "function",
                             "function": {"name": "echo", "arguments": '{"text":"'}}]},
            {"tool_calls": [{"index": 0, "function": {"arguments": 'hello"}'}}]},
            finish_reason="tool_calls",
        ),
        _sse({"content": "Done: "}, {"content": "hello"}),
    ])
    result = await _delegate(parent, "specialist")
    assert len(requests) == 2
    assert all(request["stream"] is True for request in requests)
    assert calls == ["hello"]
    assert any(message["role"] == "tool" and "tool result: hello" in message["content"]
               for message in requests[1]["messages"])
    assert result.success is True
    assert result.data["output"] == "Done: hello"


@pytest.mark.asyncio
async def test_stream_error_is_not_reported_as_successful_empty_output(parent, gateway):
    requests, responses = gateway
    # Native LLMRetryRail retries twice; all attempts must preserve the upstream error.
    responses.extend(['data: {"error":{"message":"gateway unavailable","type":"server_error"}}\n\n'] * 3)
    with pytest.raises(Exception, match="gateway unavailable"):
        await _delegate(parent, "specialist")
    assert requests and all(request["stream"] is True for request in requests)


@pytest.mark.asyncio
async def test_task_cancellation_propagates_to_stream_request(parent, monkeypatch):
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def respond(request):
        assert json.loads(request.content)["stream"] is True
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    async with AsyncOpenAI(
        api_key="test-key", base_url="http://test.invalid/v1", max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)),
    ) as sdk:
        monkeypatch.setattr(parent._deep_config.model._client, "_create_async_openai_client",
                            lambda timeout=None: sdk)
        task = asyncio.create_task(_delegate(parent, "specialist"))
        try:
            await asyncio.wait_for(started.wait(), timeout=5)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert cancelled.is_set()


@pytest.mark.asyncio
async def test_subagent_streaming_does_not_change_parent_invoke(parent, gateway):
    requests, responses = gateway
    responses.append(_sse({"content": "child result"}))
    model = parent._deep_config.model
    await _delegate(parent, "specialist")
    await parent.invoke({"query": "Main agent.", "conversation_id": parent.card.id})
    assert [request["stream"] for request in requests] == [True, False]
    assert parent._deep_config.model is model
    assert parent._react_agent._get_llm() is model


def test_reused_subagent_and_runtime_patch_are_idempotent(parent):
    child = parent.create_subagent("specialist", "first")
    parent._deep_config.subagents = [child]
    interface_deep._jws_install_subagent_runtime_patch()
    assert parent.create_subagent("specialist", "second") is child
    assert parent.create_subagent("specialist", "third") is child
    assert len(child.find_rails_by_type((interface_deep._SubagentStreamingRail,))) == 1
